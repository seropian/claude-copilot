CT, LOCK = {"tok": "", "exp": 0, "api": "https://api.githubcopilot.com"}, threading.Lock()


def log(msg): sys.stderr.write(msg + "\n"); sys.stderr.flush()


class GithubAuth:
    def read_token(self, path=None):
        try:
            with open(path or TOKEN_FILE) as f: return f.read().strip()
        except OSError: return ""

    def save_token(self, token):
        os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(TOKEN_FILE, 0o600)
        with os.fdopen(fd, "w") as f: f.write(token)

    def post_json(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "accept": "application/json", "user-agent": BASE_H["user-agent"]})
        try: return json.load(urllib.request.urlopen(req, timeout=20))
        except urllib.error.HTTPError as e: return json.loads(e.read() or b"{}")

    def login(self):
        if read_token(): return 0
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

    def copilot_token(self):
        with LOCK:
            if CT["tok"] and CT["exp"] - time.time() > 120: return CT["tok"]
            h = {**BASE_H, "authorization": "token " + read_token(), "accept": "application/json"}
            try: r = json.load(urllib.request.urlopen(urllib.request.Request(GHAPI + "/copilot_internal/v2/token", headers=h), timeout=15))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise RuntimeError("GitHub refused the Copilot token request (%s). Delete %s and rerun to log in again." % (e.code, TOKEN_FILE))
                raise RuntimeError("GitHub token request failed (%s), try again in a moment." % e.code)
            except Exception as e: raise RuntimeError("GitHub token request failed (%r), try again in a moment." % e)
            try: CT.update(tok=r["token"], exp=r["expires_at"], api=(r.get("endpoints") or {}).get("api") or CT["api"])
            except (KeyError, TypeError): raise RuntimeError("GitHub returned an unexpected Copilot token response.")
            return CT["tok"]


class CopilotClient:
    def __init__(self, auth=None):
        self.auth = auth or GithubAuth()

    def headers(self, stream, vision=False, agent=False):
        h = {**BASE_H, "authorization": "Bearer " + copilot_token(), "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "copilot-integration-id": "vscode-chat", "openai-intent": "conversation-panel",
            "x-initiator": "agent" if agent else "user", "x-request-id": str(uuid.uuid4())}
        if vision: h["copilot-vision-request"] = "true"
        return h

    def open(self, path, body, stream, vision=False, agent=False, extra=None):
        h = self.headers(stream, vision, agent); h.update(extra or {})
        return urllib.request.urlopen(urllib.request.Request(CT["api"] + path, data=json.dumps(body).encode(), headers=h, method="POST"), timeout=600)


AUTH = GithubAuth()
CLIENT = CopilotClient(AUTH)


def read_token(path=TOKEN_FILE): return AUTH.read_token(path)
def save_token(token): return AUTH.save_token(token)
def post_json(url, body): return AUTH.post_json(url, body)
def login(): return AUTH.login()
def copilot_token(): return AUTH.copilot_token()
def cp_headers(stream, vision=False, agent=False): return CLIENT.headers(stream, vision, agent)
def cp_open(path, body, stream, vision=False, agent=False, extra=None): return CLIENT.open(path, body, stream, vision, agent, extra)
