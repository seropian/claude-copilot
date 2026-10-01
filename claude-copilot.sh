#!/usr/bin/env bash
# claude-copilot: run Claude Code on GitHub Copilot models. See README.md.
# check https://github.com/seropian/claude-copilot for updates.
# Requires node/npx, python3, nc, lsof, claude.

# args: shim port (empty = we don't own one yet), gateway port. Kills our shim, drops our run marker, and if
# no other run is alive, kills the gateway we started (pidfile exists only
# when some run started it).
_cc_cleanup() {
  local t="${TMPDIR:-/tmp}" f p other=0 gpid
  if [ -n "$1" ]; then
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | xargs kill 2>/dev/null
    rm -f "$t/claude-copilot.$1.run"
  fi
  while IFS= read -r f; do
    p="${f##*claude-copilot.}"; p="${p%.run}"
    if nc -z -w1 localhost "$p" 2>/dev/null; then other=1; else rm -f "$f"; fi
  done < <(find "$t" -maxdepth 1 -name 'claude-copilot.*.run' 2>/dev/null)
  [ "$other" = 1 ] && return 0
  if [ -f "$t/claude-copilot.$2.pid" ]; then
    gpid=$(cat "$t/claude-copilot.$2.pid")
    pkill -P "$gpid" 2>/dev/null; kill "$gpid" 2>/dev/null
    rm -f "$t/claude-copilot.$2.pid"
    lsof -ti "tcp:$2" -sTCP:LISTEN 2>/dev/null | xargs kill 2>/dev/null
  fi
}

claude-copilot() {
  local gport="${COPILOT_API_PORT:-4141}"
  local sport="${COPILOT_SHIM_PORT:-4142}"
  local model="${COPILOT_CLAUDE_MODEL:-claude-sonnet-5.5}"
  local cmd="${COPILOT_API_CMD:-npx copilot-api@0.7.0 start --port $gport}"
  local log="${TMPDIR:-/tmp}/copilot-api.log"
  local mine="" i tailpid rc gpid pidf="${TMPDIR:-/tmp}/claude-copilot.$gport.pid"

  # Shim source (python). argv: <gateway port> <listen port>.
  # Folds role:"system" messages into user turns (see README, "How it works"). Models that
  # Copilot serves only on /responses get their /v1/messages calls translated. Rest passes through.
  local shim='
import http.server, json, sys, time, urllib.request, urllib.error, uuid
UP = "http://localhost:" + sys.argv[1]
CP = "https://api.githubcopilot.com"
def blocks(c): return [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
def txt(c): return "".join(b.get("text", "") for b in blocks(c) if b.get("type") == "text")
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
def cp_headers(stream):
    tok = json.load(urllib.request.urlopen(UP + "/token", timeout=10))["token"]
    return {"authorization": "Bearer " + tok, "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
        "copilot-integration-id": "vscode-chat", "editor-version": "vscode/1.104.3",
        "editor-plugin-version": "copilot-chat/0.26.7", "user-agent": "GitHubCopilotChat/0.26.7",
        "openai-intent": "conversation-panel", "x-github-api-version": "2025-04-01",
        "x-request-id": str(uuid.uuid4())}
RM = {"at": 0, "ids": set()}
def resp_models():
    if time.time() - RM["at"] > 300:
        RM["at"] = time.time()
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(CP + "/models", headers=cp_headers(False)), timeout=15))["data"]
            RM["ids"] = {m["id"] for m in d if "/responses" in (m.get("supported_endpoints") or []) and "/v1/messages" not in (m.get("supported_endpoints") or [])}
        except Exception as e:
            RM["at"] = 0
            sys.stderr.write("shim: model list failed (%r)\n" % e)
    return RM["ids"]
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
                parts.append({"type": "input_image", "image_url": "data:%s;base64,%s" % (b["source"]["media_type"], b["source"]["data"])})
            elif t == "tool_use":
                add_msg(items, role, parts)
                items.append({"type": "function_call", "call_id": b["id"], "name": b["name"], "arguments": json.dumps(b.get("input") or {})})
            elif t == "tool_result":
                add_msg(items, role, parts)
                items.append({"type": "function_call_output", "call_id": b["tool_use_id"], "output": ("Error: " if b.get("is_error") else "") + txt(b.get("content"))})
        add_msg(items, role, parts)
    body = {"model": j["model"], "input": items, "stream": bool(j.get("stream")), "store": False}
    if txt(j.get("system")): body["instructions"] = txt(j["system"])
    if j.get("max_tokens"): body["max_output_tokens"] = j["max_tokens"]
    tools = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
              "parameters": t.get("input_schema") or {"type": "object", "properties": {}}} for t in j.get("tools") or []]
    if tools:
        body["tools"] = tools
        tc = j.get("tool_choice") or {}
        if tc.get("type"):
            body["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}.get(tc["type"]) or {"type": "function", "name": tc.get("name")}
    return body
def usage(r):
    u = r.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
    return {"input_tokens": max(u.get("input_tokens", 0) - cached, 0), "output_tokens": u.get("output_tokens", 0), "cache_read_input_tokens": cached}
def stop_reason(r, tool):
    if tool: return "tool_use"
    return "max_tokens" if (r.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "end_turn"
def err(msg): return {"type": "error", "error": {"type": "api_error", "message": msg}}
def from_responses(r, model):
    content = []
    for o in r.get("output", []):
        if o["type"] == "message":
            content += [{"type": "text", "text": p["text"]} for p in o.get("content", []) if p.get("type") == "output_text"]
        elif o["type"] == "function_call":
            content.append({"type": "tool_use", "id": o["call_id"], "name": o["name"], "input": json.loads(o.get("arguments") or "{}")})
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason(r, any(b["type"] == "tool_use" for b in content)), "stop_sequence": None, "usage": usage(r)}
class H(http.server.BaseHTTPRequestHandler):
    def sse(self, ev, **d):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (ev, json.dumps({"type": ev, **d}))).encode()); self.wfile.flush()
    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def stream_out(self, r, model):
        self.send_response(200); self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache"); self.send_header("connection", "close"); self.end_headers()
        self.sse("message_start", message={"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model,
                 "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}})
        idx, open_, sent, tool, done = -1, {}, set(), False, False
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line[5:].strip() == "[DONE]": continue
            e = json.loads(line[5:]); t = e.get("type"); oi = e.get("output_index")
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
                sys.stderr.write("shim: responses stream error: %s\n" % m)
                self.sse("error", error=err(m)["error"]); done = True; break
        if not done: self.sse("error", error=err("upstream stream ended early")["error"])
    def via_responses(self, j):
        model, stream = j["model"], bool(j.get("stream"))
        try:
            r = urllib.request.urlopen(urllib.request.Request(CP + "/responses", data=json.dumps(to_responses(j)).encode(),
                                       headers=cp_headers(stream), method="POST"), timeout=600)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace"); sys.stderr.write("shim: /responses %s: %s\n" % (e.code, msg[:300]))
            return self.reply(e.code, err(msg))
        except Exception as e:
            sys.stderr.write("shim: /responses failed (%r)\n" % e)
            return self.reply(502, err(repr(e)))
        try:
            if stream: self.stream_out(r, model)
            else: self.reply(200, from_responses(json.load(r), model))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            r.close()
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0))); j = {}
        try:
            j = json.loads(body)
            if "messages" in j: j = fix(j)
            body = json.dumps(j).encode()
        except Exception as e:
            sys.stderr.write("shim: body passthrough (%r)\n" % e)
        if self.path.split("?")[0] == "/v1/messages" and j.get("model") in resp_models():
            return self.via_responses(j)
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        try:
            r = urllib.request.urlopen(urllib.request.Request(UP + self.path, data=body, headers=hdrs, method="POST"), timeout=600)
        except urllib.error.HTTPError as e:
            r = e
        code = r.status if hasattr(r, "status") else r.code
        if code >= 400: sys.stderr.write("shim: upstream %s on %s\n" % (code, self.path))
        self.send_response(code)
        for k, v in r.headers.items():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"): self.send_header(k, v)
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
    def do_GET(self):
        try:
            r = urllib.request.urlopen(UP + self.path, timeout=30); code = r.status
        except urllib.error.HTTPError as e:
            r = e; code = e.code
        data = r.read()
        self.send_response(code); self.send_header("content-type", r.headers.get("content-type", "application/json"))
        self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
'

  # a hangup/kill/ctrl-c anywhere below tears down what we started
  [ -n "${ZSH_VERSION:-}" ] && setopt local_options no_monitor no_notify 2>/dev/null
  trap '[ -n "$tailpid" ] && kill "$tailpid" 2>/dev/null; _cc_cleanup "$mine" "$gport"; trap - HUP TERM INT; return 130' HUP TERM INT

  # 1) gateway: start if :$gport is closed, show its log while waiting
  if ! nc -z -w2 localhost "$gport" 2>/dev/null; then
    echo "claude-copilot: starting gateway (log: $log)" >&2
    : > "$log"
    rm -f "$pidf"
    ( nohup sh -c "echo \$\$ > '$pidf'; exec $cmd" >"$log" 2>&1 & )
    tail -f "$log" >&2 2>/dev/null &
    tailpid=$!
    # first run needs a GitHub device-code login, so allow up to 3 min
    for i in $(seq 1 180); do
      nc -z -w1 localhost "$gport" 2>/dev/null && break
      gpid=$(cat "$pidf" 2>/dev/null)
      if [ -n "$gpid" ] && ! kill -0 "$gpid" 2>/dev/null; then break; fi
      sleep 1
    done
    kill "$tailpid" 2>/dev/null; wait "$tailpid" 2>/dev/null
    if ! nc -z -w1 localhost "$gport" 2>/dev/null; then
      echo "claude-copilot: gateway didn't come up, see $log" >&2
      _cc_cleanup "" "$gport"
      trap - HUP TERM INT
      return 1
    fi
  fi

  # 2) shim: refuse if its port is taken, otherwise start and wait for it
  if nc -z -w1 localhost "$sport" 2>/dev/null; then
    echo "claude-copilot: port $sport busy, set COPILOT_SHIM_PORT" >&2
    _cc_cleanup "" "$gport"
    trap - HUP TERM INT
    return 1
  fi
  mine="$sport"
  : > "${TMPDIR:-/tmp}/claude-copilot.$sport.run"
  ( nohup python3 -c "$shim" "$gport" "$sport" >>"$log" 2>&1 & )
  for i in 1 2 3 4 5 6 7 8 9 10; do
    nc -z -w1 localhost "$sport" 2>/dev/null && break
    sleep 0.3
  done

  # 3) claude, with env scoped to this one command (shell env stays clean).
  # Feed every model Copilot lists into the /model picker (modelPicker setting, user scope).
  local ms
  ms=$(curl -s -m 5 "http://127.0.0.1:$sport/v1/models" | python3 -c '
import sys, json
o = [{"model": m["id"], "label": m.get("display_name") or m["id"]} for m in json.load(sys.stdin)["data"]
     if not any(s in m["id"] for s in ("embedding", "compaction"))]
print(json.dumps({"modelPicker": {"options": o}}) if o else "")' 2>/dev/null)
  # INT is a no-op here (claude resets it to default) so claude's exit code survives ctrl-c.
  trap : INT
  ANTHROPIC_BASE_URL="http://127.0.0.1:$sport" \
  ANTHROPIC_AUTH_TOKEN="placeholder" \
  ANTHROPIC_MODEL="$model" \
  ANTHROPIC_DEFAULT_SONNET_MODEL="${COPILOT_SONNET_MODEL:-claude-sonnet-5.5}" \
  ANTHROPIC_DEFAULT_OPUS_MODEL="${COPILOT_OPUS_MODEL:-claude-opus-5.5}" \
  ANTHROPIC_DEFAULT_FABLE_MODEL="${COPILOT_FABLE_MODEL:-claude-opus-5.5}" \
  ANTHROPIC_DEFAULT_HAIKU_MODEL="${COPILOT_HAIKU_MODEL:-claude-haiku-4.5}" \
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1 \
  command claude --model "$model" ${ms:+--settings "$ms"} "$@"
  rc=$?

  # 4) cleanup: shim always, gateway only if we started it and nobody else uses it
  _cc_cleanup "$sport" "$gport"
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
  exit $?
fi
unset _cc_sourced
