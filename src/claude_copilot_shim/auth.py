import json, os, sys, threading, time, urllib.request, urllib.error, uuid
from .config import BASE_H, CLIENT_ID, GH, GHAPI, TOKEN_FILE, Config, log

# Compatibility aliases for callers migrating from the old facade.

def _sync_legacy_config():
    if AUTH.config.token_file != TOKEN_FILE:
        AUTH.config.token_file = TOKEN_FILE
    if AUTH.config.github_api != GHAPI:
        AUTH.config.github_api = GHAPI
    if AUTH.config.logger is not log:
        AUTH.config.logger = log


class TransientAuthError(RuntimeError):
    pass


class GithubAuth:
    def __init__(self, config=None, opener=None):
        self.config = config or Config()
        self.opener = opener or urllib.request.urlopen
        self.token = {"tok": "", "exp": 0, "api": "https://api.githubcopilot.com"}
        self.lock = threading.Lock()

    def read_token(self, path=None):
        try:
            with open(path or self.config.token_file) as f: return f.read().strip()
        except OSError: return ""

    def save_token(self, token):
        path = self.config.token_file
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f: f.write(token)

    def post_json(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "accept": "application/json", "user-agent": self.config.base_headers["user-agent"]})
        try: return json.load(self.opener(req, timeout=20))
        except urllib.error.HTTPError as e: return json.loads(e.read() or b"{}")

    def login(self):
        if self.read_token(): return 0
        try:
            interactive = sys.stdin.isatty()
        except (AttributeError, OSError):
            interactive = False
        if not interactive:
            self.config.logger("claude-copilot: no GitHub token stored and stdin is not interactive; run claude-copilot once in a terminal to log in")
            return 1
        d = self.post_json(self.config.github + "/login/device/code", {"client_id": CLIENT_ID, "scope": "read:user"})
        if "device_code" not in d: self.config.logger("claude-copilot: device login failed: %r" % d); return 1
        self.config.logger("claude-copilot: open %s and enter the code %s" % (d["verification_uri"], d["user_code"]))
        interval, end = d.get("interval", 5), self.config.clock() + d.get("expires_in", 900)
        while self.config.clock() < end:
            self.config.sleep(interval + 1)
            r = self.post_json(self.config.github + "/login/oauth/access_token", {"client_id": CLIENT_ID, "device_code": d["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            if r.get("access_token"):
                self.save_token(r["access_token"]); self.config.logger("claude-copilot: logged in"); return 0
            e = r.get("error")
            if e == "slow_down": interval = r.get("interval", interval + 5)
            elif e and e != "authorization_pending": self.config.logger("claude-copilot: login failed: %s" % e); return 1
        self.config.logger("claude-copilot: login timed out"); return 1

    def copilot_token(self):
        with self.lock:
            if self.token["tok"] and self.token["exp"] - self.config.clock() > 120: return self.token["tok"]
            h = dict(self.config.base_headers, authorization="token " + self.read_token(), accept="application/json")
            try: r = json.load(self.opener(urllib.request.Request(self.config.github_api + "/copilot_internal/v2/token", headers=h), timeout=15))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise RuntimeError("GitHub refused the Copilot token request (%s). Delete %s and rerun to log in again." % (e.code, self.config.token_file))
                raise TransientAuthError("GitHub token request failed (%s), try again in a moment." % e.code)
            except Exception as e: raise TransientAuthError("GitHub token request failed (%r), try again in a moment." % e)
            try: self.token.update(tok=r["token"], exp=r["expires_at"], api=(r.get("endpoints") or {}).get("api") or self.token["api"])
            except (KeyError, TypeError): raise TransientAuthError("GitHub returned an unexpected Copilot token response.")
            return self.token["tok"]


class CopilotClient:
    def __init__(self, auth):
        self.auth = auth
        self.opener = auth.opener

    def headers(self, stream, vision=False, agent=False):
        h = dict(self.auth.config.base_headers, authorization="Bearer " + self.auth.copilot_token(),
            **{"content-type": "application/json", "accept": "text/event-stream" if stream else "application/json",
               "copilot-integration-id": "vscode-chat", "openai-intent": "conversation-panel",
               "x-initiator": "agent" if agent else "user", "x-request-id": str(uuid.uuid4())})
        if vision: h["copilot-vision-request"] = "true"
        return h

    def open(self, path, body, stream, vision=False, agent=False, extra=None):
        h = self.headers(stream, vision, agent); h.update(extra or {})
        return self.opener(urllib.request.Request(self.auth.token["api"] + path, data=json.dumps(body).encode(), headers=h, method="POST"), timeout=600)


DEFAULT_CONFIG = Config.from_env()
AUTH = GithubAuth(DEFAULT_CONFIG)
CLIENT = CopilotClient(AUTH)
CT = AUTH.token
LOCK = AUTH.lock
_DEFAULT_POST_JSON = AUTH.post_json


def read_token(path=None):
    _sync_legacy_config()
    return AUTH.read_token(path)
def save_token(token):
    _sync_legacy_config()
    return AUTH.save_token(token)
def post_json(url, body): return _DEFAULT_POST_JSON(url, body)
def login():
    _sync_legacy_config()
    original_post_json = AUTH.post_json
    original_sleep = AUTH.config.sleep
    AUTH.post_json = post_json
    AUTH.config.sleep = time.sleep
    try:
        return AUTH.login()
    finally:
        AUTH.post_json = original_post_json
        AUTH.config.sleep = original_sleep
def copilot_token():
    _sync_legacy_config()
    return AUTH.copilot_token()
def cp_headers(stream, vision=False, agent=False): return CLIENT.headers(stream, vision, agent)
def cp_open(path, body, stream, vision=False, agent=False, extra=None): return CLIENT.open(path, body, stream, vision, agent, extra)
