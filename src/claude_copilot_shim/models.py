import json, threading, time, urllib.request
from .config import Config
from .auth import CLIENT


class CatalogUnavailable(Exception):
    pass


class ModelCatalog:
    def __init__(self, client, clock=None, logger=None):
        self.client = client
        self.clock = clock or client.auth.config.clock
        self.logger = logger or client.auth.config.logger
        self.cache = {"at": 0, "data": []}
        self.lock = threading.Lock()

    def models(self):
        with self.lock:
            return self._models()

    def _models(self):
        if self.clock() - self.cache["at"] > 300:
            self.cache["at"] = self.clock()
            try:
                req = urllib.request.Request(self.client.auth.token["api"] + "/models", headers=self.client.headers(False))
                self.cache["data"] = json.load(self.client.opener(req, timeout=15))["data"]
            except RuntimeError:
                self.cache["at"] = 0; raise
            except Exception as e:
                self.cache["at"] = self.clock() - 270
                self.logger("shim: model list failed (%r)" % e)
        return self.cache["data"]

    def endpoints(self, model):
        data = self.models()
        if not data: raise CatalogUnavailable("Copilot model list unavailable, try again in a moment.")
        for m in data:
            if m["id"] == model: return m.get("supported_endpoints") or []
        return []

    def picker_models(self):
        result = []
        seen = set()
        for m in self.models():
            if not isinstance(m, dict):
                continue
            capabilities = m.get("capabilities")
            if (not isinstance(capabilities, dict) or capabilities.get("type") != "chat"
                    or m.get("model_picker_enabled") is not True):
                continue
            model_id = m.get("id")
            if not isinstance(model_id, str) or not model_id or model_id in seen:
                continue
            seen.add(model_id)
            name = m.get("name")
            display_name = name if isinstance(name, str) and name else model_id
            if m.get("preview") is True:
                display_name += " (Preview)"
            result.append({"type": "model", "id": model_id, "display_name": display_name})
        return result


CATALOG = ModelCatalog(CLIENT)
MODELS = CATALOG.cache

def models(): return CATALOG.models()
def endpoints(model): return CATALOG.endpoints(model)
def picker_models(): return CATALOG.picker_models()
