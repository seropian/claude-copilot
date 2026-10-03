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

class MessageTranslator:
    def blocks(self, content): return blocks(content)
    def text(self, content): return txt(content)
    def to_responses(self, request): return to_responses(request)
    def from_responses(self, response, model): return from_responses(response, model)
    def to_chat(self, request): return to_chat(request)
    def from_chat(self, response, model): return from_chat(response, model)


TRANSLATOR = MessageTranslator()

def picker_settings():
    o = [{"model": m["id"], "label": m["display_name"]} for m in picker_models()]
    return {"modelPicker": {"options": o}} if o else {}


def translator(): return TRANSLATOR
