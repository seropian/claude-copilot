import json
from .config import log
from .helpers import has_image
from .models import endpoints
from .auth import cp_open

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
            try: n = json.load(r).get("input_tokens")
            finally: r.close()
            if isinstance(n, int): return {"input_tokens": n}
    except Exception as e:
        log("shim: count_tokens fell back to estimate (%r)" % e)
    return estimate(raw_len)

