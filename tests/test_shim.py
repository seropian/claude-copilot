"""Tests for the python shim embedded in claude-copilot.sh. Stdlib only."""
import http.server
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_shim():
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from claude_copilot_shim import auth, config, helpers, models, server, stream, transforms
    modules = (config, auth, models, helpers, transforms, stream, server)

    class ShimFacade:
        def __getattr__(self, name):
            for source in modules:
                if hasattr(source, name):
                    return getattr(source, name)
            raise AttributeError(name)

        def __setattr__(self, name, value):
            found = False
            for source in modules:
                if hasattr(source, name):
                    setattr(source, name, value)
                    found = True
            if not found:
                object.__setattr__(self, name, value)

    mod = ShimFacade()
    mod.H = server.CopilotRequestHandler
    return mod


def load_artifact_shim():
    src = open(os.path.join(ROOT, "dist", "claude-copilot.sh")).read()
    m = re.search(r"local shim='\n(.*?)\n'\n", src, re.S)
    assert m, "shim source not found in claude-copilot.sh"
    return m.group(1)


shim = load_shim()


class BuildArtifact(unittest.TestCase):
    def test_artifact_embeds_source_exactly(self):
        import build
        self.assertEqual(load_artifact_shim(), build.read_shim())

    def test_package_imports_without_filename_order(self):
        import importlib
        sys.path.insert(0, os.path.join(ROOT, "src"))
        names = ["config", "auth", "models", "helpers", "transforms", "stream", "server", "cli"]
        for name in reversed(names):
            importlib.import_module("claude_copilot_shim." + name)
        self.assertEqual(shim.H.__name__, "CopilotRequestHandler")

    def test_artifact_is_valid_shell(self):
        self.assertTrue(os.access(os.path.join(ROOT, "dist", "claude-copilot.sh"), os.X_OK))



class Upstream:
    """Fake Copilot API. routes: path -> (status, content-type, bytes)."""

    def __init__(self):
        self.routes, self.seen = {}, []
        up = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                up.seen.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body) if body else None})
                route = up.routes.get(self.path)
                if callable(route):
                    route = route(up.seen[-1])
                status, ct, data = route or (404, "application/json", b"{}")
                self.send_response(status)
                self.send_header("content-type", ct)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def json(self, path, obj, status=200):
        self.routes[path] = (status, "application/json", json.dumps(obj).encode())

    def sse(self, path, events):
        data = "".join("data: %s\n\n" % (e if isinstance(e, str) else json.dumps(e)) for e in events).encode()
        self.routes[path] = (200, "text/event-stream", data)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def model_entry(mid, endpoints, picker=True, typ="chat", name=None):
    return {"id": mid, "name": name or mid, "model_picker_enabled": picker,
            "capabilities": {"type": typ}, "supported_endpoints": endpoints}


class Base(unittest.TestCase):
    def setUp(self):
        self.up = Upstream()
        self.addCleanup(self.up.close)
        shim.CT.update(tok="tok", exp=time.time() + 99999, api=self.up.url)
        shim.MODELS.update(at=0, data=[])
        shim.THINK.clear()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), shim.H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def models(self, *entries):
        self.up.json("/models", {"data": list(entries)})

    def req(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            resp = urllib.request.urlopen(r, timeout=10)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status if hasattr(resp, "status") else resp.code, resp.read(), resp

    def post(self, body, path="/v1/messages"):
        s, data, resp = self.req("POST", path, body)
        return s, data, resp

    @staticmethod
    def events(data):
        out = []
        for chunk in data.decode().split("\n\n"):
            lines = dict(l.split(": ", 1) for l in chunk.split("\n") if ": " in l)
            if "data" in lines:
                out.append(json.loads(lines["data"]))
        return out


# ---------- pure helpers ----------

class Helpers(unittest.TestCase):
    def test_blocks_and_txt(self):
        self.assertEqual(shim.blocks("hi"), [{"type": "text", "text": "hi"}])
        self.assertEqual(shim.blocks(None), [])
        self.assertEqual(shim.txt([{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]), "ab")
        self.assertEqual(shim.txt("plain"), "plain")

    def test_data_url(self):
        self.assertEqual(shim.data_url({"media_type": "image/png", "data": "AAA"}), "data:image/png;base64,AAA")

    def test_tool_defs_default_schema(self):
        d = shim.tool_defs({"tools": [{"name": "t"}, {"name": "u", "description": "d", "input_schema": {"type": "object", "properties": {"x": {}}}}]})
        self.assertEqual(d[0], ("t", "", {"type": "object", "properties": {}}))
        self.assertEqual(d[1][1], "d")
        self.assertEqual(shim.tool_defs({}), [])

    def test_usage_subtracts_cached(self):
        self.assertEqual(shim.mk_usage(100, 5, 40), {"input_tokens": 60, "output_tokens": 5, "cache_read_input_tokens": 40})
        self.assertEqual(shim.mk_usage(10, 1, 50)["input_tokens"], 0)

    def test_has_image_and_is_agent(self):
        img = {"messages": [{"role": "user", "content": [{"type": "image"}]}]}
        self.assertTrue(shim.has_image(img))
        self.assertFalse(shim.has_image({"messages": [{"role": "user", "content": "x"}]}))
        self.assertTrue(shim.is_agent({"messages": [{"role": "assistant", "content": "x"}]}))
        self.assertFalse(shim.is_agent({"messages": [{"role": "user", "content": "x"}]}))

    def test_fix_merges_consecutive_user_and_orders_tool_results(self):
        j = {"messages": [
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r"}]},
        ]}
        out = shim.fix(j)["messages"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[0]["content"][0]["type"], "tool_result")
        self.assertEqual([b["type"] for b in out[0]["content"]], ["tool_result", "text", "text"])

    def test_fix_keeps_alternating(self):
        j = {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]}
        self.assertEqual(len(shim.fix(j)["messages"]), 3)

    def test_result_text_marks_errors(self):
        self.assertEqual(shim.result_text({"content": "ok"}), "ok")
        self.assertEqual(shim.result_text({"content": "bad", "is_error": True}), "Error: bad")

    def test_tool_choice(self):
        self.assertEqual(shim.tool_choice({"tool_choice": {"type": "auto"}}, True), "auto")
        self.assertEqual(shim.tool_choice({"tool_choice": {"type": "none"}}, True), "none")
        self.assertEqual(shim.tool_choice({"tool_choice": {"type": "any"}}, True), "required")
        self.assertEqual(shim.tool_choice({"tool_choice": {"type": "tool", "name": "f"}}, True), {"type": "function", "name": "f"})
        self.assertEqual(shim.tool_choice({"tool_choice": {"type": "tool", "name": "f"}}, False), {"type": "function", "function": {"name": "f"}})
        self.assertIsNone(shim.tool_choice({}, True))

    def test_err_shape(self):
        self.assertEqual(shim.err("x"), {"type": "error", "error": {"type": "api_error", "message": "x"}})

    def test_estimate(self):
        self.assertEqual(shim.estimate(400), {"input_tokens": 100})
        self.assertEqual(shim.estimate(0), {"input_tokens": 1})

    def test_parse_args(self):
        self.assertEqual(shim.parse_args('{"a": 1}'), {"a": 1})
        self.assertEqual(shim.parse_args(""), {})
        self.assertEqual(shim.parse_args("{broken"), {})

    def test_sse_events(self):
        lines = [b"event: x\n", b'data: {"a": 1}\n', b"\n", b"data: [DONE]\n"]
        self.assertEqual(list(shim.sse_events(lines)), [{"a": 1}, {"type": "[DONE]"}])


# ---------- format conversion ----------

class Responses(unittest.TestCase):
    def test_to_responses(self):
        j = {"model": "m", "stream": True, "max_tokens": 50, "system": [{"type": "text", "text": "be nice"}],
             "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}}],
             "tool_choice": {"type": "any"},
             "messages": [
                 {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}]},
                 {"role": "assistant", "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "c1", "name": "f", "input": {"x": 1}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "done"}]},
             ]}
        b = shim.to_responses(j)
        self.assertEqual(b["instructions"], "be nice")
        self.assertEqual(b["max_output_tokens"], 50)
        self.assertTrue(b["stream"])
        self.assertFalse(b["store"])
        self.assertEqual(b["tool_choice"], "required")
        self.assertEqual(b["tools"][0], {"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}})
        items = b["input"]
        self.assertEqual(items[0]["content"][0], {"type": "input_text", "text": "hi"})
        self.assertEqual(items[0]["content"][1], {"type": "input_image", "image_url": "data:image/png;base64,QQ=="})
        self.assertEqual(items[1]["content"], [{"type": "output_text", "text": "ok"}])
        self.assertEqual(items[2], {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"x": 1}'})
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "c1", "output": "done"})

    def test_to_responses_minimal(self):
        b = shim.to_responses({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertNotIn("instructions", b)
        self.assertNotIn("tools", b)
        self.assertNotIn("max_output_tokens", b)
        self.assertFalse(b["stream"])

    def test_from_responses(self):
        r = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "hey"}]},
                        {"type": "function_call", "call_id": "c", "name": "f", "arguments": '{"a": 2}'}],
             "usage": {"input_tokens": 10, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 4}}}
        o = shim.from_responses(r, "m")
        self.assertEqual(o["content"][0], {"type": "text", "text": "hey"})
        self.assertEqual(o["content"][1], {"type": "tool_use", "id": "c", "name": "f", "input": {"a": 2}})
        self.assertEqual(o["stop_reason"], "tool_use")
        self.assertEqual(o["usage"], {"input_tokens": 6, "output_tokens": 3, "cache_read_input_tokens": 4})
        self.assertEqual(o["model"], "m")
        self.assertTrue(o["id"].startswith("msg_"))

    def test_stop_reason(self):
        self.assertEqual(shim.stop_reason({}, False), "end_turn")
        self.assertEqual(shim.stop_reason({"incomplete_details": {"reason": "max_output_tokens"}}, False), "max_tokens")
        self.assertEqual(shim.stop_reason({"incomplete_details": {"reason": "max_output_tokens"}}, True), "tool_use")


class Chat(unittest.TestCase):
    def test_to_chat(self):
        j = {"model": "m", "stream": True, "max_tokens": 9, "temperature": 0.5, "top_p": 0.9, "stop_sequences": ["X"],
             "system": "sys", "tool_choice": {"type": "tool", "name": "f"},
             "tools": [{"name": "f", "input_schema": {"type": "object"}}],
             "messages": [
                 {"role": "user", "content": "hi"},
                 {"role": "assistant", "content": [{"type": "text", "text": "a"}, {"type": "tool_use", "id": "c", "name": "f", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": "r", "is_error": True},
                                              {"type": "text", "text": "more"}]},
             ]}
        b = shim.to_chat(j)
        self.assertEqual(b["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(b["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(b["messages"][2]["tool_calls"][0]["function"], {"name": "f", "arguments": "{}"})
        self.assertEqual(b["messages"][3], {"role": "tool", "tool_call_id": "c", "content": "Error: r"})
        self.assertEqual(b["messages"][4], {"role": "user", "content": "more"})
        self.assertEqual(b["stream_options"], {"include_usage": True})
        self.assertEqual((b["max_tokens"], b["temperature"], b["top_p"], b["stop"]), (9, 0.5, 0.9, ["X"]))
        self.assertEqual(b["tool_choice"], {"type": "function", "function": {"name": "f"}})
        self.assertEqual(b["tools"][0]["function"]["name"], "f")

    def test_to_chat_image_becomes_parts(self):
        j = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "Zg=="}}]}]}
        parts = shim.to_chat(j)["messages"][0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "look"})
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/jpeg;base64,Zg==")

    def test_to_chat_assistant_tool_only_has_null_content(self):
        j = {"model": "m", "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "c", "name": "f", "input": {}}]}]}
        self.assertIsNone(shim.to_chat(j)["messages"][0]["content"])

    def test_to_chat_no_stream_options_when_not_streaming(self):
        b = shim.to_chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertNotIn("stream_options", b)
        self.assertFalse(b["stream"])

    def test_from_chat_text_and_tools(self):
        r = {"choices": [{"message": {"content": "yo", "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": '{"k": 1}'}}]}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 5}}}
        o = shim.from_chat(r, "m")
        self.assertEqual(o["content"][0]["text"], "yo")
        self.assertEqual(o["content"][1]["input"], {"k": 1})
        self.assertEqual(o["stop_reason"], "tool_use")
        self.assertEqual(o["usage"]["input_tokens"], 15)

    def test_from_chat_finish_reasons(self):
        for fr, want in (("stop", "end_turn"), ("length", "max_tokens"), ("content_filter", "end_turn"), (None, "end_turn")):
            o = shim.from_chat({"choices": [{"message": {"content": "x"}, "finish_reason": fr}]}, "m")
            self.assertEqual(o["stop_reason"], want)

    def test_from_chat_empty(self):
        o = shim.from_chat({}, "m")
        self.assertEqual(o["content"], [])


# ---------- model picker ----------

class Picker(Base):
    def test_picker_models_filters(self):
        self.models(model_entry("a", [], name="A"), model_entry("b", [], picker=False), model_entry("emb", [], typ="embeddings"))
        self.assertEqual(shim.picker_models(), [{"type": "model", "id": "a", "display_name": "A"}])

    def test_picker_settings(self):
        self.models(model_entry("a", [], name="A"))
        self.assertEqual(shim.picker_settings(), {"modelPicker": {"options": [{"model": "a", "label": "A"}]}})

    def test_picker_settings_empty(self):
        self.models()
        self.assertEqual(shim.picker_settings(), {})

    def test_endpoints(self):
        self.models(model_entry("a", ["/responses"]))
        self.assertEqual(shim.endpoints("a"), ["/responses"])
        self.assertEqual(shim.endpoints("nope"), [])

    def test_models_cached_for_five_minutes(self):
        self.models(model_entry("a", []))
        shim.models()
        shim.models()
        self.assertEqual(len([s for s in self.up.seen if s["path"] == "/models"]), 1)

    def test_models_failure_returns_stale_and_retries_soon(self):
        shim.MODELS.update(at=0, data=[{"id": "old"}])  # upstream has no /models route -> 404
        self.assertEqual(shim.models(), [{"id": "old"}])
        self.assertLess(time.time() - shim.MODELS["at"], 300)
        self.assertGreater(time.time() - shim.MODELS["at"], 200)  # retry in ~30s


# ---------- HTTP surface ----------

class Server(Base):
    def test_count_tokens(self):
        s, data, _ = self.post({"messages": [{"role": "user", "content": "x" * 400}]}, "/v1/messages/count_tokens")
        self.assertEqual(s, 200)
        self.assertGreater(json.loads(data)["input_tokens"], 90)

    def test_count_tokens_native_uses_copilot(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        seen_bodies = []

        def route(seen):
            seen_bodies.append(seen["body"])
            return 200, "application/json", b'{"input_tokens": 16}'

        self.up.routes["/v1/messages/count_tokens"] = route
        s, data, _ = self.post({"model": "claude-x", "messages": [{"role": "user", "content": "x" * 400}]}, "/v1/messages/count_tokens")
        self.assertEqual((s, json.loads(data)), (200, {"input_tokens": 16}))
        self.assertEqual(seen_bodies[0]["model"], "claude-x")

    def test_count_tokens_native_failure_falls_back_to_estimate(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        self.up.routes["/v1/messages/count_tokens"] = lambda seen: (400, "application/json", b'{"error": {"message": "nope"}}')
        s, data, _ = self.post({"model": "claude-x", "messages": [{"role": "user", "content": "x" * 400}]}, "/v1/messages/count_tokens")
        self.assertEqual(s, 200)
        self.assertGreater(json.loads(data)["input_tokens"], 90)

    def test_count_tokens_non_native_model_is_estimated_locally(self):
        self.models(model_entry("gpt-x", ["/responses"]))
        s, data, _ = self.post({"model": "gpt-x", "messages": [{"role": "user", "content": "x" * 400}]}, "/v1/messages/count_tokens")
        self.assertEqual(s, 200)
        self.assertGreater(json.loads(data)["input_tokens"], 90)

    def test_unknown_post_path(self):
        s, data, _ = self.post({"messages": []}, "/nope")
        self.assertEqual(s, 404)
        self.assertEqual(json.loads(data)["type"], "error")

    def test_bad_body(self):
        s, data, _ = self.req("POST", "/v1/messages", raw=b"not json")
        self.assertEqual(s, 400)
        self.assertIn("bad request body", json.loads(data)["error"]["message"])

    def test_get_unknown(self):
        self.models()
        s, _, _ = self.req("GET", "/whatever")
        self.assertEqual(s, 404)

    def test_get_v1_models(self):
        self.models(model_entry("a", [], name="A"), model_entry("b", [], name="B"))
        s, data, _ = self.req("GET", "/v1/models")
        d = json.loads(data)
        self.assertEqual(s, 200)
        self.assertEqual([m["id"] for m in d["data"]], ["a", "b"])
        self.assertEqual((d["first_id"], d["last_id"], d["has_more"]), ("a", "b", False))

    def test_get_v1_models_empty(self):
        self.models()
        d = json.loads(self.req("GET", "/v1/models")[1])
        self.assertIsNone(d["first_id"])

    def test_get_claude_settings(self):
        self.models(model_entry("a", [], name="A"))
        d = json.loads(self.req("GET", "/claude-settings")[1])
        self.assertEqual(d["modelPicker"]["options"][0]["model"], "a")

    def test_get_auth_error_is_401(self):
        orig = shim.copilot_token
        shim.copilot_token = lambda: (_ for _ in ()).throw(RuntimeError("no token"))
        self.addCleanup(setattr, shim, "copilot_token", orig)
        s, data, _ = self.req("GET", "/v1/models")
        self.assertEqual(s, 401)
        self.assertIn("no token", json.loads(data)["error"]["message"])

    def test_post_auth_error_is_401(self):
        orig = shim.endpoints
        shim.endpoints = lambda m: (_ for _ in ()).throw(RuntimeError("no token"))
        self.addCleanup(setattr, shim, "endpoints", orig)
        s, _, _ = self.post({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(s, 401)

    def test_token_failure_after_models_cached_is_401_on_every_route(self):
        self.models(model_entry("claude-x", ["/v1/messages"]), model_entry("gpt", ["/responses"]), model_entry("gem", ["/chat/completions"]))
        shim.models()  # cache the list while the token still works
        orig = shim.copilot_token
        shim.copilot_token = lambda: (_ for _ in ()).throw(RuntimeError("login rejected"))
        self.addCleanup(setattr, shim, "copilot_token", orig)
        for model in ("claude-x", "gpt", "gem"):
            s, data, _ = self.post({"model": model, "messages": [{"role": "user", "content": "x"}]})
            self.assertEqual(s, 401, model)
            self.assertIn("login rejected", json.loads(data)["error"]["message"])

    def test_models_auth_failure_is_not_cached(self):
        orig = shim.copilot_token
        shim.copilot_token = lambda: (_ for _ in ()).throw(RuntimeError("no token"))
        with self.assertRaises(RuntimeError):
            shim.models()
        shim.copilot_token = orig
        self.models(model_entry("a", []))
        self.assertEqual([m["id"] for m in shim.models()], ["a"])  # retried immediately after the token works again

    # --- routing ---

    def test_native_route_passthrough_and_headers(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        self.up.json("/v1/messages", {"id": "m1", "type": "message", "content": []})
        body = {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]}
        r = urllib.request.Request(self.base + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                   headers={"anthropic-version": "2023-06-01", "anthropic-beta": "b1"})
        resp = urllib.request.urlopen(r, timeout=10)
        self.assertEqual(json.loads(resp.read())["id"], "m1")
        seen = [s for s in self.up.seen if s["path"] == "/v1/messages"][0]
        h = {k.lower(): v for k, v in seen["headers"].items()}
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertEqual(h["anthropic-beta"], "b1")
        self.assertEqual(h["authorization"], "Bearer tok")
        self.assertEqual(h["x-initiator"], "user")
        self.assertEqual(h["copilot-integration-id"], "vscode-chat")

    def test_native_drops_rejected_field(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        calls = []

        def route(seen):
            calls.append(dict(seen["body"]))
            if "context_management" in seen["body"]:
                return 400, "application/json", json.dumps({"error": {"message": "context_management: Extra inputs are not permitted"}}).encode()
            return 200, "application/json", b'{"ok": true}'

        self.up.routes["/v1/messages"] = route
        s, data, _ = self.post({"model": "claude-x", "context_management": {}, "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(s, 200)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("context_management", calls[1])

    def test_native_system_role_rewritten_before_upstream(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        seen_bodies = []

        def route(seen):
            seen_bodies.append(seen["body"])
            return 200, "application/json", b'{"ok": true}'

        self.up.routes["/v1/messages"] = route
        s, _, _ = self.post({"model": "claude-x", "messages": [
            {"role": "user", "content": "hi"}, {"role": "system", "content": "reminder"}]})
        self.assertEqual(s, 200)
        msgs = seen_bodies[0]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user"])
        self.assertEqual(len(msgs[0]["content"]), 2)

    def test_native_field_rejection_loop_is_bounded(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        calls = []

        def route(seen):
            calls.append(1)
            return 400, "application/json", json.dumps({"error": {"message": "f%d: Extra inputs are not permitted" % len(calls)}}).encode()

        self.up.routes["/v1/messages"] = route
        body = {"model": "claude-x", "messages": [{"role": "user", "content": "x"}]}
        body.update({"f%d" % i: 1 for i in range(1, 10)})
        s, data, _ = self.post(body)
        self.assertEqual(s, 502)
        self.assertEqual(len(calls), 5)

    def _reject_paths(self, body, paths):
        """Upstream that rejects each path in `paths` while its field is still present; returns recorded bodies."""
        self.models(model_entry("claude-x", ["/v1/messages"]))
        calls = []

        def route(seen):
            calls.append(json.loads(json.dumps(seen["body"])))
            for path, present in paths:
                if present(seen["body"]):
                    return 400, "application/json", json.dumps({"error": {"message": path + ": Extra inputs are not permitted"}}).encode()
            return 200, "application/json", b'{"ok": true}'

        self.up.routes["/v1/messages"] = route
        return calls, self.post({"model": "claude-x", "max_tokens": 5, **body})[0]

    def test_native_drops_nested_rejected_fields(self):
        calls, s = self._reject_paths({
            "metadata": {"user_id": "u", "bogus": 1},
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "bogus": 1}]}],
            "tools": [{"name": "t", "input_schema": {"type": "object"}, "bogus": 1}],
            "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "scope": "global"}}],
        }, [
            ("metadata.bogus", lambda b: "bogus" in b["metadata"]),
            ("messages.0.content.0.text.bogus", lambda b: "bogus" in b["messages"][0]["content"][0]),
            ("tools.0.custom.bogus", lambda b: "bogus" in b["tools"][0]),
            ("system.0.cache_control.ephemeral.scope", lambda b: "scope" in b["system"][0]["cache_control"]),
        ])
        self.assertEqual(s, 200)
        last = calls[-1]
        self.assertEqual(last["metadata"], {"user_id": "u"})
        self.assertEqual(last["messages"][0]["content"][0], {"type": "text", "text": "hi"})
        self.assertEqual(last["tools"][0], {"name": "t", "input_schema": {"type": "object"}})
        self.assertEqual(last["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_native_unresolvable_rejected_path_passes_through(self):
        calls, s = self._reject_paths({"metadata": {"a": 1}}, [("metadata.nope.zzz", lambda b: True)])
        self.assertEqual(s, 400)
        self.assertEqual(len(calls), 1)

    def _thinking_route(self, reject):
        """Upstream /v1/messages that 400s with `reject(thinking)` (a message or None) and records bodies."""
        self.models(model_entry("claude-x", ["/v1/messages"]))
        calls = []

        def route(seen):
            calls.append(seen["body"].get("thinking", "absent"))
            msg = reject(seen["body"].get("thinking"))
            if msg:
                return 400, "application/json", json.dumps({"type": "error", "error": {"message": msg}}).encode()
            return 200, "application/json", b'{"ok": true}'

        self.up.routes["/v1/messages"] = route
        return calls

    def _send(self, thinking):
        body = {"model": "claude-x", "messages": [{"role": "user", "content": "x"}]}
        if thinking is not None:
            body["thinking"] = thinking
        return self.post(body)[0]

    def test_thinking_disabled_becomes_between_tools(self):
        msg = 'To turn thinking off on this model, send "thinking": {"type": "between_tools"} instead of {"type": "disabled"}'
        calls = self._thinking_route(lambda t: msg if t == {"type": "disabled"} else None)
        self.assertEqual(self._send({"type": "disabled"}), 200)
        self.assertEqual(calls, [{"type": "disabled"}, {"type": "between_tools"}])

    def test_thinking_fix_is_learned_per_model(self):
        msg = 'To turn thinking off on this model, send "thinking": {"type": "between_tools"} instead of {"type": "disabled"}'
        calls = self._thinking_route(lambda t: msg if t == {"type": "disabled"} else None)
        self._send({"type": "disabled"})
        del calls[:]
        self.assertEqual(self._send({"type": "disabled"}), 200)
        self.assertEqual(calls, [{"type": "between_tools"}])  # no failed round trip the second time

    def test_thinking_unsupported_is_dropped(self):
        msg = '"thinking.type.disabled" is not supported for this model. Use "thinking.type.adaptive" and "output_config.effort"'
        calls = self._thinking_route(lambda t: msg if t == {"type": "disabled"} else None)
        self.assertEqual(self._send({"type": "disabled"}), 200)
        self.assertEqual(calls, [{"type": "disabled"}, "absent"])

    def test_adaptive_rejection_is_left_to_claude_code(self):
        # Dropping adaptive thinking breaks requests that also carry a clear_thinking context edit, and Claude Code
        # already falls back by itself, so the 400 must reach it unchanged.
        calls = self._thinking_route(lambda t: "adaptive thinking is not supported on this model" if t == {"type": "adaptive"} else None)
        self.assertEqual(self._send({"type": "adaptive"}), 400)
        self.assertEqual(calls, [{"type": "adaptive"}])

    def test_thinking_enabled_becomes_adaptive(self):
        msg = '"thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" and "output_config.effort"'
        calls = self._thinking_route(lambda t: msg if t and t["type"] == "enabled" else None)
        self.assertEqual(self._send({"type": "enabled", "budget_tokens": 1024}), 200)
        self.assertEqual(calls[-1], {"type": "adaptive"})

    def test_requests_without_thinking_are_never_touched(self):
        shim.THINK[("claude-x", None)] = {"type": "adaptive"}
        calls = self._thinking_route(lambda t: None)
        self.assertEqual(self._send(None), 200)
        self.assertEqual(calls, ["absent"])

    def test_unrelated_400_with_thinking_passes_through(self):
        calls = self._thinking_route(lambda t: "something else broke")
        self.assertEqual(self._send({"type": "adaptive"}), 400)
        self.assertEqual(len(calls), 1)

    def test_native_other_errors_pass_through(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        self.up.json("/v1/messages", {"type": "error", "error": {"message": "slow down"}}, status=429)
        s, data, _ = self.post({"model": "claude-x", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(s, 429)
        self.assertEqual(json.loads(data)["error"]["message"], "slow down")

    def test_native_agent_and_vision_headers(self):
        self.models(model_entry("claude-x", ["/v1/messages"]))
        self.up.json("/v1/messages", {})
        self.post({"model": "claude-x", "messages": [
            {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}]},
            {"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}]})
        h = {k.lower(): v for k, v in self.up.seen[-1]["headers"].items()}
        self.assertEqual(h["x-initiator"], "agent")
        self.assertEqual(h["copilot-vision-request"], "true")

    def test_responses_route_nonstream(self):
        self.models(model_entry("gpt", ["/responses"]))
        self.up.json("/responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
                                    "usage": {"input_tokens": 3, "output_tokens": 1}})
        s, data, _ = self.post({"model": "gpt", "messages": [{"role": "user", "content": "hi"}]})
        o = json.loads(data)
        self.assertEqual(s, 200)
        self.assertEqual(o["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(o["stop_reason"], "end_turn")
        self.assertEqual(self.up.seen[-1]["body"]["input"][0]["content"][0]["text"], "hi")

    def test_chat_route_nonstream(self):
        self.models(model_entry("gem", ["/chat/completions"]))
        self.up.json("/chat/completions", {"choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}], "usage": {}})
        s, data, _ = self.post({"model": "gem", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(json.loads(data)["content"][0]["text"], "yo")

    def test_unknown_model_falls_back_to_chat(self):
        self.models()
        self.up.json("/chat/completions", {"choices": [{"message": {"content": "k"}, "finish_reason": "stop"}]})
        s, data, _ = self.post({"model": "mystery", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(s, 200)
        self.assertEqual(self.up.seen[-1]["path"], "/chat/completions")

    def test_upstream_http_error_passthrough(self):
        self.models(model_entry("gpt", ["/responses"]))
        self.up.json("/responses", {"error": "quota"}, status=403)
        s, data, _ = self.post({"model": "gpt", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(s, 403)
        self.assertIn("quota", json.loads(data)["error"]["message"])

    def test_upstream_unreachable_is_502(self):
        self.models(model_entry("gpt", ["/responses"]))
        shim.CT["api"] = "http://127.0.0.1:1"
        s, data, _ = self.post({"model": "gpt", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(s, 502)

    # --- streaming ---

    def test_stream_responses_text_and_tool(self):
        self.models(model_entry("gpt", ["/responses"]))
        self.up.sse("/responses", [
            {"type": "response.output_text.delta", "output_index": 0, "delta": "Hel"},
            {"type": "response.output_text.delta", "output_index": 0, "delta": "lo"},
            {"type": "response.output_item.done", "output_index": 0, "item": {"type": "message"}},
            {"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "call_id": "c", "name": "f"}},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"a":'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "1}"},
            {"type": "response.output_item.done", "output_index": 1, "item": {"type": "function_call", "arguments": '{"a":1}'}},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 2}}},
        ])
        s, data, resp = self.post({"model": "gpt", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(resp.headers["content-type"], "text/event-stream")
        ev = self.events(data)
        types_ = [e["type"] for e in ev]
        self.assertEqual(types_[0], "message_start")
        self.assertEqual(types_[-1], "message_stop")
        text = "".join(e["delta"]["text"] for e in ev if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
        self.assertEqual(text, "Hello")
        args = "".join(e["delta"]["partial_json"] for e in ev if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta")
        self.assertEqual(args, '{"a":1}')  # not duplicated by the item.done fallback
        starts = [e for e in ev if e["type"] == "content_block_start"]
        self.assertEqual([b["content_block"]["type"] for b in starts], ["text", "tool_use"])
        self.assertEqual([e["index"] for e in starts], [0, 1])
        self.assertEqual(types_.count("content_block_stop"), 2)
        md = [e for e in ev if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "tool_use")
        self.assertEqual(md["usage"]["output_tokens"], 2)

    def test_stream_responses_args_only_on_done(self):
        self.models(model_entry("gpt", ["/responses"]))
        self.up.sse("/responses", [
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "call_id": "c", "name": "f"}},
            {"type": "response.output_item.done", "output_index": 0, "item": {"type": "function_call", "arguments": '{"z":1}'}},
            {"type": "response.completed", "response": {}},
        ])
        _, data, _ = self.post({"model": "gpt", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        args = "".join(e["delta"]["partial_json"] for e in self.events(data) if e["type"] == "content_block_delta")
        self.assertEqual(args, '{"z":1}')

    def test_stream_responses_incomplete_is_max_tokens(self):
        self.models(model_entry("gpt", ["/responses"]))
        self.up.sse("/responses", [
            {"type": "response.output_text.delta", "output_index": 0, "delta": "x"},
            {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
        ])
        _, data, _ = self.post({"model": "gpt", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        md = [e for e in self.events(data) if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "max_tokens")

    def test_stream_responses_failure_emits_error(self):
        self.models(model_entry("gpt", ["/responses"]))
        self.up.sse("/responses", [{"type": "response.failed", "response": {"error": {"message": "boom"}}}])
        _, data, _ = self.post({"model": "gpt", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        e = self.events(data)[-1]
        self.assertEqual(e["type"], "error")
        self.assertEqual(e["error"]["message"], "boom")

    def test_stream_responses_early_end_emits_error(self):
        self.models(model_entry("gpt", ["/responses"]))
        self.up.sse("/responses", [{"type": "response.output_text.delta", "output_index": 0, "delta": "x"}])
        _, data, _ = self.post({"model": "gpt", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        e = self.events(data)[-1]
        self.assertEqual(e["type"], "error")
        self.assertIn("ended early", e["error"]["message"])

    def test_stream_chat_text_then_tool(self):
        self.models(model_entry("gem", ["/chat/completions"]))
        self.up.sse("/chat/completions", [
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "f", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
            "[DONE]",
        ])
        _, data, _ = self.post({"model": "gem", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        ev = self.events(data)
        starts = [e["content_block"]["type"] for e in ev if e["type"] == "content_block_start"]
        self.assertEqual(starts, ["text", "tool_use"])
        args = "".join(e["delta"]["partial_json"] for e in ev if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta")
        self.assertEqual(args, '{"q":1}')
        md = [e for e in ev if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "tool_use")
        self.assertEqual(md["usage"], {"input_tokens": 9, "output_tokens": 4, "cache_read_input_tokens": 0})
        self.assertEqual(ev[-1]["type"], "message_stop")
        # stream_options asked for usage
        self.assertEqual(self.up.seen[-1]["body"]["stream_options"], {"include_usage": True})

    def test_stream_chat_plain_stop(self):
        self.models(model_entry("gem", ["/chat/completions"]))
        self.up.sse("/chat/completions", [
            {"choices": [{"delta": {"content": "a"}}]},
            {"choices": [{"delta": {"content": "b"}, "finish_reason": "length"}]},
            "[DONE]",
        ])
        _, data, _ = self.post({"model": "gem", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        ev = self.events(data)
        self.assertEqual([e["type"] for e in ev if e["type"] == "content_block_start"], ["content_block_start"])
        self.assertEqual([e for e in ev if e["type"] == "message_delta"][0]["delta"]["stop_reason"], "max_tokens")

    def test_stream_chat_early_end_emits_error(self):
        self.models(model_entry("gem", ["/chat/completions"]))
        self.up.sse("/chat/completions", [{"choices": [{"delta": {"content": "a"}}]}])
        _, data, _ = self.post({"model": "gem", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(self.events(data)[-1]["type"], "error")


# ---------- auth / login ----------

class Auth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.tf = os.path.join(self.tmp, "sub", "github_token")
        old = shim.TOKEN_FILE
        shim.TOKEN_FILE = self.tf
        self.addCleanup(setattr, shim, "TOKEN_FILE", old)
        # read_token's default arg was bound at definition time; rebind via wrapper
        orig = shim.read_token
        shim.read_token = lambda path=None: orig(self.tf if path is None else path)
        self.addCleanup(setattr, shim, "read_token", orig)
        self.logs = []
        old_stdin = sys.stdin
        sys.stdin = type("TerminalStdin", (), {"isatty": lambda self: True})()
        self.addCleanup(setattr, sys, "stdin", old_stdin)
        olog = shim.log
        shim.log = self.logs.append
        self.addCleanup(setattr, shim, "log", olog)

    def test_read_token_missing(self):
        self.assertEqual(shim.read_token(), "")

    def test_save_and_read_token_private(self):
        shim.save_token("abc")
        self.assertEqual(shim.read_token(), "abc")
        self.assertEqual(stat.S_IMODE(os.stat(self.tf).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.tf)).st_mode), 0o700)

    def test_login_noop_with_token(self):
        shim.save_token("abc")
        shim.post_json = lambda *a: self.fail("should not hit network")
        self.assertEqual(shim.login(), 0)

    def test_login_without_token_fails_noninteractive(self):
        old_stdin = sys.stdin
        sys.stdin = type("ClosedStdin", (), {"isatty": lambda self: False})()
        self.addCleanup(setattr, sys, "stdin", old_stdin)
        self.assertEqual(shim.login(), 1)
        self.assertTrue(any("not interactive" in line for line in self.logs))

    def test_login_device_flow(self):
        old_stdin = sys.stdin
        sys.stdin = type("TerminalStdin", (), {"isatty": lambda self: True})()
        self.addCleanup(setattr, sys, "stdin", old_stdin)
        replies = iter([
            {"device_code": "dc", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device", "interval": 0, "expires_in": 60},
            {"error": "authorization_pending"},
            {"error": "slow_down", "interval": 0},
            {"access_token": "gho_new"},
        ])
        calls = []
        orig_post, orig_sleep = shim.post_json, shim.time.sleep
        shim.post_json = lambda url, body: (calls.append((url, body)), next(replies))[1]
        shim.time.sleep = lambda s: None
        self.addCleanup(setattr, shim, "post_json", orig_post)
        self.addCleanup(setattr, shim.time, "sleep", orig_sleep)
        self.assertEqual(shim.login(), 0)
        self.assertEqual(shim.read_token(), "gho_new")
        self.assertTrue(any("ABCD-1234" in l for l in self.logs))
        self.assertTrue(calls[0][0].endswith("/login/device/code"))
        self.assertEqual(calls[1][1]["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")

    def test_login_device_start_failure(self):
        orig = shim.post_json
        shim.post_json = lambda url, body: {"error": "nope"}
        self.addCleanup(setattr, shim, "post_json", orig)
        self.assertEqual(shim.login(), 1)
        self.assertEqual(shim.read_token(), "")

    def test_login_denied(self):
        replies = iter([{"device_code": "d", "user_code": "U", "verification_uri": "v", "interval": 0}, {"error": "access_denied"}])
        orig, osl = shim.post_json, shim.time.sleep
        shim.post_json = lambda url, body: next(replies)
        shim.time.sleep = lambda s: None
        self.addCleanup(setattr, shim, "post_json", orig)
        self.addCleanup(setattr, shim.time, "sleep", osl)
        self.assertEqual(shim.login(), 1)
        self.assertTrue(any("access_denied" in l for l in self.logs))

    def test_copilot_token_fetch_cache_and_endpoint(self):
        up = Upstream()
        self.addCleanup(up.close)
        up.json("/copilot_internal/v2/token", {"token": "ct1", "expires_at": time.time() + 3600, "endpoints": {"api": "https://example.test"}})
        shim.save_token("gh")
        old_api, shim.GHAPI = shim.GHAPI, up.url
        self.addCleanup(setattr, shim, "GHAPI", old_api)
        shim.CT.update(tok="", exp=0, api="https://api.githubcopilot.com")
        self.assertEqual(shim.copilot_token(), "ct1")
        self.assertEqual(shim.copilot_token(), "ct1")
        self.assertEqual(len(up.seen), 1)  # cached
        self.assertEqual(shim.CT["api"], "https://example.test")
        self.assertEqual({k.lower(): v for k, v in up.seen[0]["headers"].items()}["authorization"], "token gh")

    def test_copilot_token_refreshes_near_expiry(self):
        up = Upstream()
        self.addCleanup(up.close)
        up.json("/copilot_internal/v2/token", {"token": "fresh", "expires_at": time.time() + 3600})
        shim.save_token("gh")
        old_api, shim.GHAPI = shim.GHAPI, up.url
        self.addCleanup(setattr, shim, "GHAPI", old_api)
        shim.CT.update(tok="stale", exp=time.time() + 60, api="https://api.githubcopilot.com")
        self.assertEqual(shim.copilot_token(), "fresh")

    def test_copilot_token_rejected(self):
        up = Upstream()
        self.addCleanup(up.close)
        up.json("/copilot_internal/v2/token", {}, status=401)
        shim.save_token("gh")
        old_api, shim.GHAPI = shim.GHAPI, up.url
        self.addCleanup(setattr, shim, "GHAPI", old_api)
        shim.CT.update(tok="", exp=0)
        with self.assertRaises(RuntimeError) as cm:
            shim.copilot_token()
        self.assertIn("401", str(cm.exception))
        self.assertIn(self.tf, str(cm.exception))

    def test_copilot_token_5xx_is_transient(self):
        up = Upstream()
        self.addCleanup(up.close)
        up.json("/copilot_internal/v2/token", {}, status=503)
        shim.save_token("gh")
        old_api, shim.GHAPI = shim.GHAPI, up.url
        self.addCleanup(setattr, shim, "GHAPI", old_api)
        shim.CT.update(tok="", exp=0)
        with self.assertRaises(RuntimeError) as cm:
            shim.copilot_token()
        self.assertIn("503", str(cm.exception))
        self.assertNotIn("Delete", str(cm.exception))

    def test_copilot_token_garbage_response_is_runtime_error(self):
        up = Upstream()
        self.addCleanup(up.close)
        up.json("/copilot_internal/v2/token", {"unexpected": 1})
        shim.save_token("gh")
        old_api, shim.GHAPI = shim.GHAPI, up.url
        self.addCleanup(setattr, shim, "GHAPI", old_api)
        shim.CT.update(tok="", exp=0)
        with self.assertRaises(RuntimeError):
            shim.copilot_token()

    def test_copilot_token_network_error_is_runtime_error(self):
        shim.save_token("gh")
        old_api, shim.GHAPI = shim.GHAPI, "http://127.0.0.1:9"
        self.addCleanup(setattr, shim, "GHAPI", old_api)
        shim.CT.update(tok="", exp=0)
        with self.assertRaises(RuntimeError):
            shim.copilot_token()

    def test_save_token_tightens_existing_permissions(self):
        os.makedirs(os.path.dirname(self.tf))
        with open(self.tf, "w") as f: f.write("old")
        os.chmod(self.tf, 0o644)
        shim.save_token("new")
        self.assertEqual(stat.S_IMODE(os.stat(self.tf).st_mode), 0o600)


class Hardening(Base):
    def setUp(self):
        super().setUp()
        self.models(model_entry("gem", ["/chat/completions"]))
        self.up.json("/chat/completions", {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        self.body = {"model": "gem", "messages": [{"role": "user", "content": "hi"}]}

    def set_attr(self, name, val):
        old = getattr(shim, name)
        setattr(shim, name, val)
        self.addCleanup(setattr, shim, name, old)

    def test_key_required_when_set(self):
        self.set_attr("KEY", "sekret")
        self.assertEqual(self.post(self.body)[0], 401)
        self.assertEqual(self.req("GET", "/v1/models")[0], 401)
        self.assertEqual(self.req("GET", "/claude-settings")[0], 401)
        self.assertEqual(self.req("POST", "/v1/messages", self.body, headers={"x-api-key": "wrong"})[0], 401)
        self.assertEqual(self.req("POST", "/v1/messages", self.body, headers={"authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.up.seen and [x for x in self.up.seen if x["path"] == "/chat/completions"], [])

    def test_key_accepted_as_x_api_key_or_bearer(self):
        self.set_attr("KEY", "sekret")
        self.assertEqual(self.req("POST", "/v1/messages", self.body, headers={"x-api-key": "sekret"})[0], 200)
        self.assertEqual(self.req("POST", "/v1/messages", self.body, headers={"authorization": "Bearer sekret"})[0], 200)
        self.assertEqual(self.req("GET", "/claude-settings", headers={"x-api-key": "sekret"})[0], 200)

    def test_no_key_configured_means_open(self):
        self.set_attr("KEY", "")
        self.assertEqual(self.post(self.body)[0], 200)

    def test_foreign_host_header_rejected(self):
        s, _, _ = self.req("POST", "/v1/messages", self.body, headers={"Host": "evil.example"})
        self.assertEqual(s, 403)
        s, _, _ = self.req("GET", "/v1/models", headers={"Host": "evil.example:80"})
        self.assertEqual(s, 403)
        s, _, _ = self.req("POST", "/v1/messages", self.body, headers={"Host": "localhost:1234"})
        self.assertEqual(s, 200)

    def test_oversized_body_rejected_without_reading(self):
        self.set_attr("MAX_BODY", 10)
        s, data, _ = self.req("POST", "/v1/messages", raw=b'{"model": "gem", "messages": []}')
        self.assertEqual(s, 413)

    def test_bad_content_length_is_400(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.putrequest("POST", "/v1/messages")
        c.putheader("content-length", "abc")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_translation_bug_is_500_not_a_hang(self):
        def boom(j): raise ValueError("kaboom")
        self.set_attr("to_chat", boom)
        s, data, _ = self.post(self.body)
        self.assertEqual(s, 500)
        self.assertIn("kaboom", data.decode())

    def test_malformed_blocks_do_not_kill_the_handler(self):
        s, data, _ = self.post({"model": "gem", "messages": [{"role": "assistant", "content": [{"type": "tool_use"}]}]})
        self.assertEqual(s, 500)
        self.assertEqual(self.post(self.body)[0], 200)  # server still alive

    def test_response_conversion_failure_is_502(self):
        def boom(r, m): raise KeyError("x")
        self.set_attr("from_chat", boom)
        s, data, _ = self.post(self.body)
        self.assertEqual(s, 502)
        self.assertIn("translation failed", data.decode())

    def test_tool_result_image_leaves_placeholder(self):
        b = {"type": "tool_result", "tool_use_id": "c", "content": [
            {"type": "text", "text": "see "}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "Zg=="}}]}
        self.assertEqual(shim.result_text(b), "see [image omitted]")

    def test_from_responses_tolerates_odd_output(self):
        r = {"output": [{"type": "reasoning"}, {"foo": 1}, {"type": "message", "content": [{"type": "output_text"}]},
                        {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{not json"}]}
        o = shim.from_responses(r, "m")
        self.assertEqual(o["content"][-1], {"type": "tool_use", "id": "c", "name": "f", "input": {}})

    def test_stream_chat_tool_call_without_id_in_first_chunk(self):
        self.up.sse("/chat/completions", [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "f", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a":1}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, "[DONE]"])
        s, data, _ = self.post({**self.body, "stream": True})
        ev = self.events(data)
        start = [e for e in ev if e["type"] == "content_block_start"][0]["content_block"]
        self.assertEqual((start["type"], start["name"]), ("tool_use", "f"))
        self.assertTrue(start["id"].startswith("call_"))
        args = "".join(e["delta"]["partial_json"] for e in ev if e["type"] == "content_block_delta")
        self.assertEqual(args, '{"a":1}')

    def test_stream_chat_parallel_tool_calls(self):
        self.up.sse("/chat/completions", [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f", "arguments": '{"x":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "b", "function": {"name": "g", "arguments": '{"y":2}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, "[DONE]"])
        s, data, _ = self.post({**self.body, "stream": True})
        ev = self.events(data)
        starts = [e["content_block"]["name"] for e in ev if e["type"] == "content_block_start"]
        self.assertEqual(starts, ["f", "g"])
        first = "".join(e["delta"]["partial_json"] for e in ev if e["type"] == "content_block_delta" and e["index"] == 0)
        self.assertEqual(first, '{"x":1}')

    def test_stream_chat_length_during_tool_call_is_max_tokens(self):
        self.up.sse("/chat/completions", [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f", "arguments": '{"x":'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]}, "[DONE]"])
        s, data, _ = self.post({**self.body, "stream": True})
        md = [e for e in self.events(data) if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "max_tokens")

    def test_stream_translation_failure_emits_error_event(self):
        self.up.sse("/chat/completions", ["{not json"])
        s, data, _ = self.post({**self.body, "stream": True})
        ev = self.events(data)
        self.assertEqual(ev[-1]["type"], "error")


if __name__ == "__main__":
    unittest.main()
