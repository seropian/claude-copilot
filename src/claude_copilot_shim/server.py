import hmac, http.server, json, urllib.error, uuid
from .config import KEY, MAX_BODY, log
from .auth import TransientAuthError, cp_open
from .helpers import adapt_thinking, drop_path, err, fix, has_image, is_agent, THINK, thinking_fix, upstream_err
from .models import CatalogUnavailable, endpoints, picker_models
from .stream import sse_events, count_tokens
from .transforms import CHAT_STOP, chat_usage, from_chat, from_responses, picker_settings, stop_reason, to_chat, to_responses, usage

class CopilotRequestHandler(http.server.BaseHTTPRequestHandler):
    def sse(self, ev, **d):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (ev, json.dumps({"type": ev, **d}))).encode()); self.wfile.flush()
    def reply(self, code, obj, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def start_stream(self, model):
        self.send_response(200); self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache"); self.send_header("connection", "close"); self.end_headers()
        self.sse("message_start", message={"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model,
                 "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}})
    def stream_responses(self, r, model):
        self.start_stream(model)
        idx, open_, sent, tool, done = -1, {}, set(), False, False
        for e in sse_events(r):
            t = e.get("type"); oi = e.get("output_index")
            if t == "response.output_item.added" and e["item"]["type"] == "function_call":
                idx += 1; open_[oi] = idx; tool = True
                self.sse("content_block_start", index=idx, content_block={"type": "tool_use", "id": e["item"]["call_id"], "name": e["item"]["name"], "input": {}})
            elif t == "response.output_text.delta":
                if oi not in open_:
                    idx += 1; open_[oi] = idx
                    self.sse("content_block_start", index=idx, content_block={"type": "text", "text": ""})
                self.sse("content_block_delta", index=open_[oi], delta={"type": "text_delta", "text": e["delta"]})
            elif t == "response.function_call_arguments.delta":
                if oi not in open_: continue
                sent.add(oi); self.sse("content_block_delta", index=open_[oi], delta={"type": "input_json_delta", "partial_json": e["delta"]})
            elif t == "response.output_item.done" and oi in open_:
                if e["item"]["type"] == "function_call" and oi not in sent and e["item"].get("arguments"):
                    self.sse("content_block_delta", index=open_[oi], delta={"type": "input_json_delta", "partial_json": e["item"]["arguments"]})
                self.sse("content_block_stop", index=open_.pop(oi))
            elif t in ("response.completed", "response.incomplete"):
                for i in open_.values(): self.sse("content_block_stop", index=i)
                self.sse("message_delta", delta={"stop_reason": stop_reason(e["response"], tool), "stop_sequence": None}, usage=usage(e["response"]))
                self.sse("message_stop"); done = True; break
            elif t in ("response.failed", "error"):
                m = (e.get("response", {}).get("error") or e.get("error") or e).get("message", "upstream error")
                log("shim: responses stream error: %s" % m)
                self.sse("error", **err(m)); done = True; break
        if not done: self.sse("error", **err("upstream stream ended early"))
    def stream_chat(self, r, model):
        self.start_stream(model)
        idx, cur, tools, ids, pend, stop, use, done = -1, None, {}, {}, {}, None, {}, False
        live = set()
        def close(only=None):
            nonlocal cur
            for i in sorted(live if only is None else live & only):
                self.sse("content_block_stop", index=i); live.discard(i)
                if i == cur: cur = None
        def close_all(): close()
        def args(n, a):
            if tools[n] in live: self.sse("content_block_delta", index=tools[n], delta={"type": "input_json_delta", "partial_json": a})
        for e in sse_events(r):
            if e.get("type") == "[DONE]": done = True; break
            if e.get("usage"): use = e["usage"]
            ch = (e.get("choices") or [None])[0]
            if not ch: continue
            d = ch.get("delta") or {}
            if d.get("content"):
                if cur is None:
                    close_all(); idx += 1; cur = idx; live.add(idx)
                    self.sse("content_block_start", index=idx, content_block={"type": "text", "text": ""})
                self.sse("content_block_delta", index=cur, delta={"type": "text_delta", "text": d["content"]})
            for tc in d.get("tool_calls") or []:
                n = tc.get("index", 0); f = tc.get("function") or {}
                if f.get("name") and (n not in tools or (tc.get("id") and ids.get(n) and tc["id"] != ids[n])):
                    close({cur, tools.get(n)}); idx += 1; tools[n] = idx; ids[n] = tc.get("id"); live.add(idx)
                    self.sse("content_block_start", index=idx, content_block={"type": "tool_use", "id": tc.get("id") or "call_" + uuid.uuid4().hex[:24], "name": f["name"], "input": {}})
                    if pend.get(n): args(n, pend.pop(n))
                if f.get("arguments"):
                    if n in tools: args(n, f["arguments"])
                    else: pend[n] = pend.get(n, "") + f["arguments"]
            if ch.get("finish_reason"): stop = ch["finish_reason"]
        close_all()
        if stop is None and not done:
            self.sse("error", **err("upstream stream ended early")); return
        self.sse("message_delta", delta={"stop_reason": "max_tokens" if stop == "length" else "tool_use" if tools else CHAT_STOP.get(stop, "end_turn"), "stop_sequence": None}, usage=chat_usage(use))
        self.sse("message_stop")
    def via(self, j, path, body, convert, stream_fn):
        model, stream = j["model"], bool(j.get("stream"))
        try:
            r = cp_open(path, body, stream, has_image(j), is_agent(j))
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace"); e.close(); log("shim: %s %s: %s" % (path, e.code, msg[:300]))
            ra = e.headers.get("retry-after") if e.headers else None
            return self.reply(e.code, upstream_err(e.code, msg), {"retry-after": ra} if ra else None)
        except RuntimeError:
            raise
        except Exception as e:
            log("shim: %s failed (%r)" % (path, e)); return self.reply(502, err(str(e)))
        try:
            if stream: stream_fn(r, model)
            else: self.reply(200, convert(json.load(r), model))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("shim: %s translation failed (%r)" % (path, e))
            try:
                if stream: self.sse("error", **err("shim translation failed: %r" % e))
                else: self.reply(502, err("shim translation failed: %r" % e))
            except OSError: pass
        finally:
            r.close()
    def native(self, j):
        extra = {k: self.headers[k] for k in ("anthropic-version", "anthropic-beta") if self.headers.get(k)}
        adapt_thinking(j)
        for _ in range(5):
            try:
                r = cp_open("/v1/messages", j, bool(j.get("stream")), has_image(j), is_agent(j), extra)
                break
            except urllib.error.HTTPError as e:
                data = e.read(); e.close()
                try: obj = json.loads(data); m = obj["error"]["message"]
                except Exception: obj, m = err(data.decode(errors="replace")), ""
                key = m.split(":")[0]
                if e.code == 400 and m.endswith("Extra inputs are not permitted") and drop_path(j, key):
                    log("shim: dropping field %s, Copilot rejects it" % key); continue
                new = thinking_fix(m) if e.code == 400 and isinstance(j.get("thinking"), dict) else False
                if new is not False:
                    THINK[(j["model"], j["thinking"].get("type"))] = new
                    log("shim: %s rejects thinking %s, using %s" % (j["model"], j["thinking"].get("type"), new)); adapt_thinking(j); continue
                log("shim: native %s: %s" % (e.code, data.decode(errors="replace")[:400]))
                ra = e.headers.get("retry-after") if e.headers else None
                return self.reply(e.code, obj, {"retry-after": ra} if ra else None)
            except RuntimeError:
                raise
            except Exception as e:
                log("shim: native failed (%r)" % e); return self.reply(502, err(str(e)))
        else:
            return self.reply(502, err("Copilot kept rejecting request fields"))
        self.send_response(r.status)
        for k, v in r.headers.items():
            if k.lower() in ("content-type", "retry-after", "cache-control"): self.send_header(k, v)
        self.send_header("connection", "close"); self.end_headers()
        try:
            while True:
                chunk = r.read1(8192)
                if not chunk: break
                self.wfile.write(chunk); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            r.close()
    def allowed(self):
        host = (self.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            self.reply(403, err("bad host header")); return False
        if KEY:
            auth = self.headers.get("authorization") or ""
            given = self.headers.get("x-api-key") or (auth[7:] if auth.lower().startswith("bearer ") else "")
            if not hmac.compare_digest(given.encode(), KEY.encode()):
                self.reply(401, err("invalid api key")); return False
        return True
    def do_POST(self):
        if not self.allowed(): return
        try: n = int(self.headers.get("content-length") or 0)
        except ValueError: return self.reply(400, err("bad content-length"))
        if n < 0 or n > MAX_BODY: return self.reply(413, err("request too large"))
        raw = self.rfile.read(n)
        try:
            j = fix(json.loads(raw))
        except Exception as e:
            return self.reply(400, err("bad request body: %r" % e))
        p = self.path.split("?")[0]
        try:
            if p == "/v1/messages/count_tokens":
                extra = {k: self.headers[k] for k in ("anthropic-version", "anthropic-beta") if self.headers.get(k)}
                return self.reply(200, count_tokens(j, len(raw), extra))
            if p != "/v1/messages": return self.reply(404, err("not found: " + p))
            eps = endpoints(j.get("model"))
            if "/v1/messages" in eps: return self.native(j)
            if "/responses" in eps: return self.via(j, "/responses", to_responses(j), from_responses, self.stream_responses)
            return self.via(j, "/chat/completions", to_chat(j), from_chat, self.stream_chat)
        except TransientAuthError as e:
            log("shim: %s" % e); self.reply(503, err(str(e)), {"retry-after": "5"})
        except RuntimeError as e:
            log("shim: %s" % e); self.reply(401, err(str(e)))
        except CatalogUnavailable as e:
            log("shim: %s" % e); self.reply(503, err(str(e)))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("shim: POST %s failed (%r)" % (p, e)); self.reply(500, err("shim error: %r" % e))
    def do_GET(self):
        if not self.allowed(): return
        try:
            path = self.path.split("?")[0]
            if path == "/claude-settings": return self.reply(200, picker_settings())
            if path == "/v1/models":
                data = picker_models()
                return self.reply(200, {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None})
        except (TransientAuthError, CatalogUnavailable) as e:
            return self.reply(503, err(str(e)), {"retry-after": "5"})
        except RuntimeError as e:
            return self.reply(401, err(str(e)))
        except Exception as e:
            log("shim: GET %s failed (%r)" % (self.path, e)); return self.reply(500, err("shim error: %r" % e))
        self.reply(404, err("not found"))
    def log_message(self, *a): pass
