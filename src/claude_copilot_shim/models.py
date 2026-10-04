import json, time, urllib.request
from .config import Config
from .auth import CLIENT


class ModelCatalog:
    def __init__(self, client, clock=None, logger=None):
        self.client = client
        self.clock = clock or client.auth.config.clock
        self.logger = logger or client.auth.config.logger
        self.cache = {"at": 0, "data": []}

    def models(self):
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
        for m in self.models():
            if m["id"] == model: return m.get("supported_endpoints") or []
        return []

    def picker_models(self):
        return [{"type": "model", "id": m["id"], "display_name": m.get("name") or m["id"]} for m in self.models()
                if m.get("model_picker_enabled") and (m.get("capabilities") or {}).get("type") == "chat"]


CATALOG = ModelCatalog(CLIENT)
MODELS = CATALOG.cache

def models(): return CATALOG.models()
def endpoints(model): return CATALOG.endpoints(model)
def picker_models(): return CATALOG.picker_models()
