import os, sys, time

GH, GHAPI = "https://github.com", "https://api.github.com"
CLIENT_ID = "Iv1.b507a08c87ecfe98"
VSC, PLUG = "1.104.3", "0.26.7"
BASE_H = {"editor-version": "vscode/" + VSC, "editor-plugin-version": "copilot-chat/" + PLUG,
          "user-agent": "GitHubCopilotChat/" + PLUG, "x-github-api-version": "2025-04-01"}
TOKEN_FILE = os.path.expanduser(os.environ.get("COPILOT_TOKEN_FILE") or "~/.local/share/claude-copilot/github_token")
KEY = os.environ.get("COPILOT_SHIM_KEY", "")
MAX_BODY = 64 * 1024 * 1024

class Config:
    def __init__(self, token_file=TOKEN_FILE, key=KEY, github=GH, github_api=GHAPI, max_body=MAX_BODY,
                 base_headers=None, sleep=None, clock=None, logger=None):
        self.token_file = token_file
        self.key = key
        self.github = github
        self.github_api = github_api
        self.max_body = max_body
        self.base_headers = dict(base_headers or BASE_H)
        self.sleep = sleep or time.sleep
        self.clock = clock or time.time
        self.logger = logger or log

    @classmethod
    def from_env(cls):
        return cls(os.path.expanduser(os.environ.get("COPILOT_TOKEN_FILE") or TOKEN_FILE),
                   os.environ.get("COPILOT_SHIM_KEY", ""))


def log(msg): sys.stderr.write(msg + "\n"); sys.stderr.flush()
