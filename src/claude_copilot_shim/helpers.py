import json, re

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
def err(msg, kind="api_error"): return {"type": "error", "error": {"type": kind, "message": msg}}
ERR_KINDS = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error",
             413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error"}
CTX_RE = re.compile(r"context.length|context window|too many tokens|token limit|maximum context|prompt is too long", re.I)
def upstream_err(code, body):
    """Anthropic-style error for a non-Anthropic upstream failure. Context-length errors get the prefix Claude Code looks for."""
    text = body
    try:
        e = json.loads(body).get("error")
        text = (e.get("message") if isinstance(e, dict) else e) or body
    except (ValueError, AttributeError): pass
    if not isinstance(text, str): text = body
    if code == 400 and CTX_RE.search(text) and not text.lower().startswith("prompt is too long"): text = "prompt is too long: " + text
    return err(text, ERR_KINDS.get(code, "api_error"))
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
def result_images(b):
    return [x["source"] for x in blocks(b.get("content")) if x.get("type") == "image" and (x.get("source") or {}).get("type") == "base64"]
def result_text(b):
    def part(x):
        if x.get("type") == "text": return x.get("text", "")
        return "[image attached in the next message]" if (x.get("source") or {}).get("type") == "base64" else "[image omitted]"
    parts = [part(x) for x in blocks(b.get("content")) if x.get("type") in ("text", "image")]
    return ("Error: " if b.get("is_error") else "") + "".join(parts)
def tool_choice(j, flat):
    tc = j.get("tool_choice") or {}
    t = tc.get("type")
    if t in ("auto", "none"): return t
    if t == "any": return "required"
    if t == "tool": return {"type": "function", "name": tc.get("name")} if flat else {"type": "function", "function": {"name": tc.get("name")}}
    return None

