class ModelCatalog:
    def __init__(self, client=None):
        self.client = client or CLIENT
        self.cache = {"at": 0, "data": []}

    def models(self):
        if time.time() - self.cache["at"] > 300:
            self.cache["at"] = time.time()
            try:
                req = urllib.request.Request(CT["api"] + "/models", headers=cp_headers(False))
                self.cache["data"] = json.load(urllib.request.urlopen(req, timeout=15))["data"]
            except RuntimeError:
                self.cache["at"] = 0; raise
            except Exception as e:
                self.cache["at"] = time.time() - 270
                log("shim: model list failed (%r)" % e)
        return self.cache["data"]

    def endpoints(self, model):
        for m in self.models():
            if m["id"] == model: return m.get("supported_endpoints") or []
        return []

    def picker_models(self):
        return [{"type": "model", "id": m["id"], "display_name": m.get("name") or m["id"]} for m in self.models()
                if m.get("model_picker_enabled") and (m.get("capabilities") or {}).get("type") == "chat"]


CATALOG = ModelCatalog()
MODELS = CATALOG.cache

def models(): return CATALOG.models()
def endpoints(model): return CATALOG.endpoints(model)
def picker_models(): return CATALOG.picker_models()
