#!/usr/bin/env bash
# claude-copilot: run Claude Code on GitHub Copilot models. See README.md.

# Prerequisites: python3 (3.6+), curl, claude. Prints what is missing and how to get it.
_cc_check() {
  local c missing=""
  for c in python3 curl claude; do
    command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
  done
  if [ -n "$missing" ]; then
    echo "claude-copilot: missing required tools:$missing" >&2
    case "$missing" in *python3*) echo "  python3: https://www.python.org/downloads/ (macOS: brew install python)" >&2 ;; esac
    case "$missing" in *curl*) echo "  curl: use your package manager (brew install curl, apt install curl)" >&2 ;; esac
    case "$missing" in *claude*) echo "  claude (Claude Code): https://code.claude.com/docs/en/setup" >&2 ;; esac
    return 1
  fi
  if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' 2>/dev/null; then
    echo "claude-copilot: python3 3.6 or newer is required (found $(python3 --version 2>&1))" >&2
    return 1
  fi
}

# args: shim pid, port file. Kills our shim and removes the files.
# No pid yet (we were interrupted before the shim wrote its port file)? Use the early pid file.
_cc_cleanup() {
  local p="$1"
  [ -z "$p" ] && [ -n "$2" ] && [ -f "$2.pid" ] && p=$(cat "$2.pid" 2>/dev/null)
  case "$p" in ''|*[!0-9]*) ;; *) kill "$p" 2>/dev/null ;; esac
  [ -n "$2" ] && rm -f "$2" "$2.pid" "$2.tmp"
  return 0
}

_cc_fail() { trap - HUP TERM INT; return 1; }

claude-copilot() {
  unset _cc_sig
  _cc_check || return 1
  local sport="${COPILOT_SHIM_PORT:-0}"
  local model="${COPILOT_CLAUDE_MODEL:-claude-sonnet-5.5}"
  local log="${TMPDIR:-/tmp}/claude-copilot.log"
  local spid="" pf="" i rc ms key
  local -a sa=()

  # Shim source (python). Usage: login | serve <port, 0 = any free> <file to write "pid port" to>.
  # login: GitHub device-code login (or reuses a copilot-api login), token kept in ~/.local/share/claude-copilot.
  # serve: local Anthropic-style API in front of GitHub Copilot, see README, "How it works".
  local shim='
import hmac, http.server, json, os, sys, threading, time, urllib.request, urllib.error, uuid
GH, GHAPI = "https://github.com", "https://api.github.com"
CLIENT_ID = "Iv1.b507a08c87ecfe98"
VSC, PLUG = "1.104.3", "0.26.7"
BASE_H = {"editor-version": "vscode/" + VSC, "editor-plugin-version": "copilot-chat/" + PLUG,
          "user-agent": "GitHubCopilotChat/" + PLUG, "x-github-api-version": "2025-04-01"}
TOKEN_FILE = os.path.expanduser(os.environ.get("COPILOT_TOKEN_FILE") or "~/.local/share/claude-copilot/github_token")
LEGACY_FILE = os.path.expanduser("~/.local/share/copilot-api/github_token")
KEY = os.environ.get("COPILOT_SHIM_KEY", "")
MAX_BODY = 64 * 1024 * 1024
def log(msg): sys.stderr.write(msg + "\n"); sys.stderr.flush()
def read_token(path=TOKEN_FILE):
    try:
        with open(path) as f: return f.read().strip()
    except OSError: return ""
def save_token(t):
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(TOKEN_FILE, 0o600)
    with os.fdopen(fd, "w") as f: f.write(t)
def post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": BASE_H["user-agent"]})
    try: return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e: return json.loads(e.read() or b"{}")
def login():
    if read_token(): return 0
    t = read_token(LEGACY_FILE)
    if t:
        save_token(t); log("claude-copilot: reusing the GitHub login from copilot-api"); return 0
    d = post_json(GH + "/login/device/code", {"client_id": CLIENT_ID, "scope": "read:user"})
    if "device_code" not in d: log("claude-copilot: device login failed: %r" % d); return 1
    log("claude-copilot: open %s and enter the code %s" % (d["verification_uri"], d["user_code"]))
    interval, end = d.get("interval", 5), time.time() + d.get("expires_in", 900)
    while time.time() < end:
        time.sleep(interval + 1)
        r = post_json(GH + "/login/oauth/access_token", {"client_id": CLIENT_ID, "device_code": d["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if r.get("access_token"):
            save_token(r["access_token"]); log("claude-copilot: logged in"); return 0
        e = r.get("error")
        if e == "slow_down": interval = r.get("interval", interval + 5)
        elif e and e != "authorization_pending": log("claude-copilot: login failed: %s" % e); return 1
    log("claude-copilot: login timed out"); return 1

CT, LOCK = {"tok": "", "exp": 0, "api": "https://api.githubcopilot.com"}, threading.Lock()
def copilot_token():
    with LOCK:
        if CT["tok"] and CT["exp"] - time.time() > 120: return CT["tok"]
        h = {**BASE_H, "authorization": "token " + read_token(), "accept": "application/json"}
        try: r = json.load(urllib.request.urlopen(urllib.request.Request(GHAPI + "/copilot_internal/v2/token", headers=h), timeout=15))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError("GitHub refused the Copilot token request (%s). Delete %s and rerun to log in again." % (e.code, TOKEN_FILE))
            raise RuntimeError("GitHub token request failed (%s), try again in a moment." % e.code)
        except Exception as e:
            raise RuntimeError("GitHub token request failed (%r), try again in a moment." % e)
        try: CT.update(tok=r["token"], exp=r["expires_at"], api=(r.get("endpoints") or {}).get("api") or CT["api"])
        except (KeyError, TypeError): raise RuntimeError("GitHub returned an unexpected Copilot token response.")
        return CT["tok"]
def cp_headers(stream, vision=False, agent=False):
    h = {**BASE_H, "authorization": "Bearer " + copilot_token(), "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
        "copilot-integration-id": "vscode-chat", "openai-intent": "conversation-panel",
        "x-initiator": "agent" if agent else "user", "x-request-id": str(uuid.uuid4())}
    if vision: h["copilot-vision-request"] = "true"
    return h
def cp_open(path, body, stream, vision=False, agent=False, extra=None):
    h = cp_headers(stream, vision, agent); h.update(extra or {})
    return urllib.request.urlopen(urllib.request.Request(CT["api"] + path, data=json.dumps(body).encode(), headers=h, method="POST"), timeout=600)

MODELS = {"at": 0, "data": []}
def models():
    if time.time() - MODELS["at"] > 300:
        MODELS["at"] = time.time()
        try:
            req = urllib.request.Request(CT["api"] + "/models", headers=cp_headers(False))
            MODELS["data"] = json.load(urllib.request.urlopen(req, timeout=15))["data"]
        except RuntimeError:
            MODELS["at"] = 0; raise
        except Exception as e:
            MODELS["at"] = time.time() - 270
            log("shim: model list failed (%r)" % e)
    return MODELS["data"]
def endpoints(model):
    for m in models():
        if m["id"] == model: return m.get("supported_endpoints") or []
    return []
def picker_models():
    return [{"type": "model", "id": m["id"], "display_name": m.get("name") or m["id"]} for m in models()
            if m.get("model_picker_enabled") and (m.get("capabilities") or {}).get("type") == "chat"]

def blocks(c): return [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
def txt(c): return "".join(b.get("text", "") for b in blocks(c) if b.get("type") == "text")
def data_url(s): return "data:%s;base64,%s" % (s["media_type"], s["data"])
def tool_defs(j): return [(t["name"], t.get("description", ""), t.get("input_schema") or {"type": "object", "properties": {}}) for t in j.get("tools") or []]
def mk_usage(inp, out, cached): return {"input_tokens": max(inp - cached, 0), "output_tokens": out, "cache_read_input_tokens": cached}
def has_image(j): return any(b.get("type") == "image" for m in j.get("messages", []) for b in blocks(m.get("content")))
def is_agent(j): return any(m.get("role") == "assistant" for m in j.get("messages", []))
def fix(j):
    out = []
    for m in j.get("messages", []):
        m = {**m, "role": "user" if m.get("role") == "system" else m.get("role")}
        if out and out[-1]["role"] == "user" and m["role"] == "user":
            merged = blocks(out[-1]["content"]) + blocks(m["content"])
            merged.sort(key=lambda b: b.get("type") != "tool_result")
            out[-1] = {**out[-1], "content": merged}
        else:
            out.append(m)
    j["messages"] = out
    return j
def err(msg): return {"type": "error", "error": {"type": "api_error", "message": msg}}
def drop_path(j, path):
    """Delete the field a Copilot "a.0.b.c: Extra inputs..." path points at. Copilot inserts type names
    into the path (tools.0.custom.x, cache_control.ephemeral.x), so a segment that is not a container is skipped."""
    segs = path.split("."); cur = j
    for i, s in enumerate(segs):
        last = i == len(segs) - 1
        if isinstance(cur, dict) and s in cur:
            if last: del cur[s]; return True
            if isinstance(cur[s], (dict, list)): cur = cur[s]
        elif isinstance(cur, list) and s.isdigit() and int(s) < len(cur):
            if last: return False
            cur = cur[int(s)]
        elif last: return False
    return False
THINK = {}
def thinking_fix(m):
    """Map a Copilot 400 about the thinking setting to a replacement (None = drop it), or False."""
    if "between_tools" in m and "instead of" in m: return {"type": "between_tools"}
    if "thinking.type.enabled" in m and "not supported" in m: return {"type": "adaptive"}
    if "thinking.type" in m and "not supported" in m: return None
    return False
def adapt_thinking(j):
    if not isinstance(j.get("thinking"), dict): return
    new = THINK.get((j.get("model"), j["thinking"].get("type")), False)
    if new is None: j.pop("thinking", None)
    elif new: j["thinking"] = new
def result_text(b):
    parts = [x.get("text", "") if x.get("type") == "text" else "[image omitted]" for x in blocks(b.get("content")) if x.get("type") in ("text", "image")]
    return ("Error: " if b.get("is_error") else "") + "".join(parts)
def tool_choice(j, flat):
    tc = j.get("tool_choice") or {}
    t = tc.get("type")
    if t in ("auto", "none"): return t
    if t == "any": return "required"
    if t == "tool": return {"type": "function", "name": tc.get("name")} if flat else {"type": "function", "function": {"name": tc.get("name")}}
    return None

def add_msg(items, role, parts):
    if parts: items.append({"type": "message", "role": role, "content": parts[:]}); del parts[:]
def to_responses(j):
    items = []
    for m in j["messages"]:
        role, parts = m["role"], []
        for b in blocks(m["content"]):
            t = b.get("type")
            if t == "text":
                parts.append({"type": "output_text" if role == "assistant" else "input_text", "text": b["text"]})
            elif t == "image" and b["source"].get("type") == "base64":
                parts.append({"type": "input_image", "image_url": data_url(b["source"])})
            elif t == "tool_use":
                add_msg(items, role, parts)
                items.append({"type": "function_call", "call_id": b["id"], "name": b["name"], "arguments": json.dumps(b.get("input") or {})})
            elif t == "tool_result":
                add_msg(items, role, parts)
                items.append({"type": "function_call_output", "call_id": b["tool_use_id"], "output": result_text(b)})
        add_msg(items, role, parts)
    body = {"model": j["model"], "input": items, "stream": bool(j.get("stream")), "store": False}
    if txt(j.get("system")): body["instructions"] = txt(j["system"])
    if j.get("max_tokens"): body["max_output_tokens"] = j["max_tokens"]
    tools = [{"type": "function", "name": n, "description": d, "parameters": p} for n, d, p in tool_defs(j)]
    if tools:
        body["tools"] = tools
        tc = tool_choice(j, True)
        if tc: body["tool_choice"] = tc
    return body
def usage(r):
    u = r.get("usage") or {}
    return mk_usage(u.get("input_tokens", 0), u.get("output_tokens", 0), (u.get("input_tokens_details") or {}).get("cached_tokens", 0))
def stop_reason(r, tool):
    if tool: return "tool_use"
    return "max_tokens" if (r.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "end_turn"
def from_responses(r, model):
    content = []
    for o in r.get("output", []):
        if o.get("type") == "message":
            content += [{"type": "text", "text": p.get("text", "")} for p in o.get("content", []) if p.get("type") == "output_text"]
        elif o.get("type") == "function_call":
            content.append({"type": "tool_use", "id": o["call_id"], "name": o["name"], "input": parse_args(o.get("arguments"))})
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason(r, any(b["type"] == "tool_use" for b in content)), "stop_sequence": None, "usage": usage(r)}

def to_chat(j):
    msgs = []
    if txt(j.get("system")): msgs.append({"role": "system", "content": txt(j["system"])})
    for m in j["messages"]:
        bl = blocks(m["content"])
        if m["role"] == "assistant":
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in bl if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": "\n\n".join(b["text"] for b in bl if b.get("type") == "text") or None}
            if calls: msg["tool_calls"] = calls
            msgs.append(msg)
            continue
        for b in bl:
            if b.get("type") == "tool_result": msgs.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": result_text(b)})
        rest = [b for b in bl if b.get("type") in ("text", "image")]
        if any(b["type"] == "image" for b in rest):
            parts = [{"type": "text", "text": b["text"]} if b["type"] == "text" else
                     {"type": "image_url", "image_url": {"url": data_url(b["source"])}}
                     for b in rest if b["type"] == "text" or b["source"].get("type") == "base64"]
            msgs.append({"role": "user", "content": parts})
        elif rest:
            msgs.append({"role": "user", "content": "\n\n".join(b["text"] for b in rest)})
    body = {"model": j["model"], "messages": msgs, "stream": bool(j.get("stream"))}
    if body["stream"]: body["stream_options"] = {"include_usage": True}
    for k in ("max_tokens", "temperature", "top_p"):
        if j.get(k) is not None: body[k] = j[k]
    if j.get("stop_sequences") is not None: body["stop"] = j["stop_sequences"]
    tools = [{"type": "function", "function": {"name": n, "description": d, "parameters": p}} for n, d, p in tool_defs(j)]
    if tools:
        body["tools"] = tools
        tc = tool_choice(j, False)
        if tc: body["tool_choice"] = tc
    return body
def chat_usage(u):
    u = u or {}
    return mk_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
CHAT_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "content_filter": "end_turn"}
def parse_args(s):
    try: return json.loads(s or "{}")
    except ValueError: return {}
def from_chat(r, model):
    ch = (r.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = [{"type": "text", "text": msg["content"]}] if msg.get("content") else []
    for c in msg.get("tool_calls") or []:
        content.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": parse_args(c["function"].get("arguments"))})
    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else CHAT_STOP.get(ch.get("finish_reason"), "end_turn")
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": chat_usage(r.get("usage"))}

def picker_settings():
    o = [{"model": m["id"], "label": m["display_name"]} for m in picker_models()]
    return {"modelPicker": {"options": o}} if o else {}
def sse_events(r):
    for line in r:
        line = line.decode().strip()
        if not line.startswith("data:"): continue
        body = line[5:].strip()
        yield {"type": "[DONE]"} if body == "[DONE]" else json.loads(body)

def estimate(nbytes):
    return {"input_tokens": max(1, nbytes // 4)}

def count_tokens(j, raw_len, extra):
    """Real count from the Copilot native endpoint; the byte estimate for other routes or on any failure."""
    try:
        if "/v1/messages" in endpoints(j.get("model")):
            r = cp_open("/v1/messages/count_tokens", j, False, has_image(j), False, extra)
            n = json.load(r).get("input_tokens")
            if isinstance(n, int): return {"input_tokens": n}
    except Exception as e:
        log("shim: count_tokens fell back to estimate (%r)" % e)
    return estimate(raw_len)

class H(http.server.BaseHTTPRequestHandler):
    def sse(self, ev, **d):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (ev, json.dumps({"type": ev, **d}))).encode()); self.wfile.flush()
    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
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
        idx, cur, tool_open, tools, stop, use, done = -1, None, False, {}, None, {}, False
        def close():
            nonlocal cur, tool_open
            if cur is not None: self.sse("content_block_stop", index=cur); cur = None; tool_open = False
        for e in sse_events(r):
            if e.get("type") == "[DONE]": done = True; break
            if e.get("usage"): use = e["usage"]
            ch = (e.get("choices") or [None])[0]
            if not ch: continue
            d = ch.get("delta") or {}
            if d.get("content"):
                if cur is None or tool_open:
                    close(); idx += 1; cur = idx
                    self.sse("content_block_start", index=idx, content_block={"type": "text", "text": ""})
                self.sse("content_block_delta", index=cur, delta={"type": "text_delta", "text": d["content"]})
            for tc in d.get("tool_calls") or []:
                n = tc.get("index", 0); f = tc.get("function") or {}
                if f.get("name") and (n not in tools or tc.get("id")):
                    close(); idx += 1; cur = idx; tools[n] = idx; tool_open = True
                    self.sse("content_block_start", index=idx, content_block={"type": "tool_use", "id": tc.get("id") or "call_" + uuid.uuid4().hex[:24], "name": f["name"], "input": {}})
                if f.get("arguments") and n in tools:
                    self.sse("content_block_delta", index=tools[n], delta={"type": "input_json_delta", "partial_json": f["arguments"]})
            if ch.get("finish_reason"): stop = ch["finish_reason"]; close()
        close()
        if stop is None and not done:
            self.sse("error", **err("upstream stream ended early")); return
        self.sse("message_delta", delta={"stop_reason": "max_tokens" if stop == "length" else "tool_use" if tools else CHAT_STOP.get(stop, "end_turn"), "stop_sequence": None}, usage=chat_usage(use))
        self.sse("message_stop")
    def via(self, j, path, body, convert, stream_fn):
        model, stream = j["model"], bool(j.get("stream"))
        try:
            r = cp_open(path, body, stream, has_image(j), is_agent(j))
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace"); log("shim: %s %s: %s" % (path, e.code, msg[:300]))
            return self.reply(e.code, err(msg))
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
                data = e.read()
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
                return self.reply(e.code, obj)
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
            given = self.headers.get("x-api-key") or (self.headers.get("authorization") or "")[7:]
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
        except RuntimeError as e:
            log("shim: %s" % e); self.reply(401, err(str(e)))
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
        except RuntimeError as e:
            return self.reply(401, err(str(e)))
        except Exception as e:
            log("shim: GET %s failed (%r)" % (self.path, e)); return self.reply(500, err("shim error: %r" % e))
        self.reply(404, err("not found"))
    def log_message(self, *a): pass
if sys.argv[1] == "login": sys.exit(login())
if sys.argv[1] == "serve":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H)
    with open(sys.argv[3] + ".tmp", "w") as f: f.write("%d %d" % (os.getpid(), srv.server_address[1]))
    os.replace(sys.argv[3] + ".tmp", sys.argv[3])
    srv.serve_forever()
'

  # a hangup/kill/ctrl-c anywhere below tears down what we started (_cc_sig: exit code for the executed wrapper, bash 3.2 loses `return N` from an INT trap)
  [ -n "${ZSH_VERSION:-}" ] && setopt local_options no_monitor no_notify 2>/dev/null
  trap '_cc_cleanup "$spid" "$pf"; trap - HUP TERM INT; _cc_sig=129; return 129' HUP
  trap '_cc_cleanup "$spid" "$pf"; trap - HUP TERM INT; _cc_sig=143; return 143' TERM
  trap '_cc_cleanup "$spid" "$pf"; trap - HUP TERM INT; _cc_sig=130; return 130' INT

  # 1) login: no-op when a token is already stored
  python3 -c "$shim" login || { _cc_fail; return; }

  # 2) shim: own instance per run on a free port (or COPILOT_SHIM_PORT), it writes "pid port" to a temp file
  case "$sport" in ''|*[!0-9]*) echo "claude-copilot: COPILOT_SHIM_PORT must be a port number, got '$sport'" >&2; _cc_fail; return ;; esac
  if [ "$sport" != 0 ] && python3 -c 'import socket, sys; sys.exit(0 if socket.socket().connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$sport"; then
    echo "claude-copilot: port $sport busy, unset COPILOT_SHIM_PORT to pick a free one" >&2
    _cc_fail; return
  fi
  if [ -L "$log" ]; then
    echo "claude-copilot: refusing to write to $log, it is a symlink" >&2; _cc_fail; return
  fi
  key=$(python3 -c 'import secrets; print(secrets.token_hex(24))') || { _cc_fail; return; }
  pf=$(mktemp "${TMPDIR:-/tmp}/claude-copilot.XXXXXX") || { _cc_fail; return; }
  ( umask 077; COPILOT_SHIM_KEY="$key" nohup python3 -c "$shim" serve "$sport" "$pf" >>"$log" 2>&1 & echo $! >"$pf.pid" )
  sport=""
  for i in $(seq 1 100); do
    read -r spid sport < "$pf" 2>/dev/null
    [ -n "$sport" ] && break
    sleep 0.1
  done
  if [ -z "$sport" ]; then
    echo "claude-copilot: shim didn't start, see $log" >&2
    tail -n 5 "$log" 2>/dev/null | sed 's/^/  /' >&2
    _cc_cleanup "$spid" "$pf"
    _cc_fail; return
  fi

  # 3) claude, with env scoped to this one command (shell env stays clean).
  # Feed every model Copilot offers into the /model picker (modelPicker setting, user scope).
  ms=$(printf 'header = "x-api-key: %s"\n' "$key" | curl -sf -m 20 -K - "http://127.0.0.1:$sport/claude-settings")
  case "$ms" in "{}"|"") ;; "{"*) sa=(--settings "$ms") ;; esac
  # INT is a no-op here (claude resets it to default) so claude's exit code survives ctrl-c.
  trap : INT
  ANTHROPIC_BASE_URL="http://127.0.0.1:$sport" \
  ANTHROPIC_AUTH_TOKEN="$key" \
  ANTHROPIC_MODEL="$model" \
  ANTHROPIC_DEFAULT_SONNET_MODEL="${COPILOT_SONNET_MODEL:-claude-sonnet-5.5}" \
  ANTHROPIC_DEFAULT_OPUS_MODEL="${COPILOT_OPUS_MODEL:-claude-opus-5.5}" \
  ANTHROPIC_DEFAULT_FABLE_MODEL="${COPILOT_FABLE_MODEL:-claude-opus-5.5}" \
  ANTHROPIC_DEFAULT_HAIKU_MODEL="${COPILOT_HAIKU_MODEL:-claude-haiku-4.5}" \
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  CLAUDE_CODE_AUTO_MODE_SERVER=0 \
  CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1 \
  command claude --model "$model" ${sa[@]+"${sa[@]}"} "$@"
  rc=$?

  # 4) cleanup
  _cc_cleanup "$spid" "$pf"
  trap - HUP TERM INT
  return $rc
}

# executed (not sourced): run it. zsh: sourced context ends in ":file".
# bash: sourced when $BASH_SOURCE differs from $0.
_cc_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
  case "${ZSH_EVAL_CONTEXT:-}" in *:file*) _cc_sourced=1 ;; esac
elif [ -n "${BASH_VERSION:-}" ]; then
  [ "${BASH_SOURCE[0]}" != "$0" ] && _cc_sourced=1
fi
if [ "$_cc_sourced" = 0 ]; then
  claude-copilot "$@"
  _cc_rc=$?
  exit "${_cc_sig:-$_cc_rc}"
fi
unset _cc_sourced
