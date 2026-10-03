import hmac, http.server, json, os, sys, threading, time, urllib.request, urllib.error, uuid
GH, GHAPI = "https://github.com", "https://api.github.com"
CLIENT_ID = "Iv1.b507a08c87ecfe98"
VSC, PLUG = "1.104.3", "0.26.7"
BASE_H = {"editor-version": "vscode/" + VSC, "editor-plugin-version": "copilot-chat/" + PLUG,
          "user-agent": "GitHubCopilotChat/" + PLUG, "x-github-api-version": "2025-04-01"}
TOKEN_FILE = os.path.expanduser(os.environ.get("COPILOT_TOKEN_FILE") or "~/.local/share/claude-copilot/github_token")
KEY = os.environ.get("COPILOT_SHIM_KEY", "")
MAX_BODY = 64 * 1024 * 1024
