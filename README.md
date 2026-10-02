# claude-copilot

![Screenshot](docs/images/screenshot1.png)

Run Claude Code on GitHub Copilot models. No Anthropic account needed, just a Copilot plan.

One self-contained file: `claude-copilot.sh`. Works in bash 3.2+ (incl. macOS `/bin/bash`) and zsh. Not POSIX sh.

## Requirements

`python3` (3.6+), `curl`, `claude` (Claude Code). Run it from an interactive terminal (the first-run login needs one). No node, no npm packages. The script checks these on startup and tells you what's missing.

First run asks for a GitHub device-code login (open the URL, type the code). The login is kept in `~/.local/share/claude-copilot/github_token` (mode 600). If you already logged in with copilot-api (`~/.local/share/copilot-api`), that token is reused.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/seropian/claude-copilot/main/install.sh | bash
```

Installs to `~/.local/bin/claude-copilot` (override with `INSTALL_DIR`). Rerun to update. Set `CLAUDE_COPILOT_REF` to pin a branch, tag or commit. Want to read it first? Download `install.sh` and run it yourself.

## Uninstall

```sh
curl -fsSL https://raw.githubusercontent.com/seropian/claude-copilot/main/uninstall.sh | bash
```

Removes `~/.local/bin/claude-copilot` (or `INSTALL_DIR`), the saved login in `~/.local/share/claude-copilot`, and the log. Set `KEEP_LOGIN=1` to keep the token. Or by hand: `rm ~/.local/bin/claude-copilot; rm -rf ~/.local/share/claude-copilot`. Also remove the `PATH` line from your shell profile if you added it.

## Usage

```sh
claude-copilot                 # interactive
claude-copilot -p "prompt"     # one-shot
claude-copilot -c              # resume last session
COPILOT_CLAUDE_MODEL=claude-opus-5.5 claude-copilot
```

All args go straight to `claude`. Exit code is passed through.

## How it works

```
claude -> shim (python, 127.0.0.1:<free port>) -> GitHub Copilot API
```

One small python server (embedded in the script) sits between Claude Code and Copilot. There is no other gateway process.

- Claude Code is pointed at the shim via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (a random per-run key; the shim rejects requests without it, and any `Host` header that isn't localhost, so other local processes and web pages can't spend your Copilot quota). Env is scoped to the one `claude` command, your shell stays clean. The shim only listens on `127.0.0.1`.
- **Auth:** GitHub device-code login with the VS Code Copilot client id, then the shim trades the GitHub token for a short-lived Copilot token (`/copilot_internal/v2/token`) and refreshes it before it expires. The API host comes from that token response, so business/enterprise accounts work too.
- **Routing:** per model, the shim reads `supported_endpoints` from Copilot's `/models` (cached 5 min) and picks:
    - `/v1/messages` (Claude models): forwarded as is, Anthropic format.
    - `/responses` (GPT-5.x/6.x, Grok, MAI): translated from `/v1/messages` (text, tools, images, streaming).
    - `/chat/completions` (Gemini, Kimi, others): translated the same way.
- **Request rewrites** (on the native `/v1/messages` route, so Copilot doesn't 400 on what Claude Code sends):
    - System-role messages in `messages` are turned into user messages and adjacent user turns are merged (`tool_result` blocks first). Copilot's Claude 5.x models treat a trailing system message as assistant prefill and reject it.
    - Fields Copilot rejects ("Extra inputs are not permitted", e.g. `safeguards`) are dropped on the 400, top-level or nested (like `cache_control.scope`), then the request is retried (up to 5 times) and logged. A path it can't find is passed through as the 400.
    - `thinking` is rewritten (or dropped) on the 400 and retried, and the fix is remembered per model for the rest of the run. Models accept different settings, and auto mode's classifier always asks for `disabled`, so it'd fail without this. Known quirks:
        - claude-sonnet-5.5 rejects `disabled` (wants `between_tools`).
        - claude-opus-5.5 rejects `disabled`.
        - Most reject `enabled` with a budget (want `adaptive`).
        - A rejected `adaptive` (claude-haiku-4.5) is passed through untouched, Claude Code retries without thinking by itself.
    - `/v1/messages/count_tokens` is forwarded to Copilot for models on its native endpoint, which returns a real count. Other models, or any failure, get a rough estimate (request size in bytes / 4).
- Model aliases (sonnet/opus/haiku/fable) are mapped to Copilot model IDs, since Anthropic's IDs don't exist on Copilot.
- At startup the script asks the shim for the model list (chat models Copilot marks `model_picker_enabled`, so no embeddings, internal models, or the old gpt-3*/gpt-4* families) and passes it to `claude --settings` as a `modelPicker` list, so `/model` shows all of them. Claude Code's own gateway discovery isn't used: it drops any id without `claude` in it.

Lifecycle:

1. Log in if there's no stored token.
2. Start the shim. Bails if its port is taken.
3. Run `claude`.
4. Cleanup: kill the shim. HUP/TERM/INT clean up too.

Run as many instances in parallel as you like. Each run starts its own shim on a free port picked by the OS, so there's nothing to configure.

## Config

All env vars, all optional.

| Var | Default | What |
|---|---|---|
| `COPILOT_CLAUDE_MODEL` | `claude-sonnet-5.5` | main model |
| `COPILOT_SONNET_MODEL` | `claude-sonnet-5.5` | `sonnet` alias target |
| `COPILOT_OPUS_MODEL` | `claude-opus-5.5` | `opus` alias target |
| `COPILOT_FABLE_MODEL` | `claude-opus-5.5` | `fable` alias target. Copilot has no Fable model, so picking it really gives you whatever this points to |
| `COPILOT_HAIKU_MODEL` | `claude-haiku-4.5` | `haiku` alias target |
| `COPILOT_SHIM_PORT` | unset (free port) | pin the shim to a fixed port. Only one run at a time can use it |
| `COPILOT_TOKEN_FILE` | `~/.local/share/claude-copilot/github_token` | where the GitHub token is stored |

Installer vars (only read by `install.sh` / `uninstall.sh`):

| Var | Default | What |
|---|---|---|
| `INSTALL_DIR` | `~/.local/bin` | where the script is installed or removed from |
| `CLAUDE_COPILOT_REF` | `main` | branch, tag or commit to install |
| `KEEP_LOGIN` | unset | set to `1` to keep the saved token on uninstall |

Files:

- `$TMPDIR/claude-copilot.log`: shim log (errors, dropped fields, upstream status codes)
- `~/.local/share/claude-copilot/github_token`: your GitHub token. Delete it to log in again.

## Tested models

**Last checked 2026-10-02 with Claude Code 2.1.286, results may be stale.** Each model got a plain "reply ok" check and a Bash tool-call check, streaming, through the shim. Images were checked on one model per route (claude-sonnet-5.5, gpt-5.5, gemini-3.7-flash).

- **Work (all 24 in the picker):** claude-opus-4.7, claude-opus-4.8, claude-opus-5.5, claude-opus-5, claude-sonnet-5.5, claude-sonnet-5, claude-haiku-4.5, gemini-3.7-flash, gemini-3.8-flash, gpt-5.3-codex, gpt-5.4-mini, gpt-5.4, gpt-5.5, gpt-5.6-luna/-sol/-terra, gpt-5-mini, gpt-6-luna/-sol, gpt-6.1-sol, grok-4.7, kimi-k2.7-code, kimi-k3, mai-code-1.1-flash
- **Hidden from the picker** (Copilot doesn't mark them for the model picker, you can still pass them with `COPILOT_CLAUDE_MODEL`, they go through `/chat/completions`):
    - gpt-4, gpt-4-0613, gpt-4-0125-preview: work (checked 2026-10-02)
    - gpt-4o*, gpt-4.1*, gpt-3.5-turbo*, gpt-4-o-preview: untested. Every call got 429 "exceeded your rate limit for utility models" (account-level limit after a burst of test requests)
    - gpt-41-copilot: 400 model_not_supported

Not tested: `auto`. The model list depends on your Copilot plan.

## Known issues / limits

- **Unofficial:** this uses GitHub's private Copilot endpoints, with the VS Code Copilot client id and headers. It may break when GitHub changes things, and may be against GitHub's terms. Your call.
- **Billing:** premium requests count against your Copilot plan.
- **Rate limits:** Copilot rate-limits per account and model tier (the small "utility" models like gpt-4o-mini hit it first). On a 429 Claude Code retries until it gives up, so it looks like a hang. Look for `429` in `$TMPDIR/claude-copilot.log`, wait it out.
- **Non-Claude models:** GPT/Gemini/Kimi go through a translation layer, tool calling can be less reliable than Claude. Thinking blocks and prompt-cache controls are not translated.
- **`auto` model:** Copilot's auto-routing is done client-side by Copilot's own apps, the API doesn't expose it. Use Copilot CLI for that.
- **Catalog warning:** Claude Code warns the model isn't in its catalog and assumes a 200k context. The script sets `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` to relax that.
- **Auto mode:** works, but the safety checks run as Claude Code's own requests through Copilot (they count against your plan), not on Anthropic's server. The script sets `CLAUDE_CODE_AUTO_MODE_SERVER=0` because Copilot rejects the `safeguards` field the server-side checks need, and this also stops the "session isn't eligible" notice. The classifier's `thinking: disabled` is handled by the shim, see "Request rewrites".
- **`claude --bare`** also avoids the system-message problem, but drops hooks and CLAUDE.md. The shim is the better fix.

## Troubleshooting

- **Port busy:** you set `COPILOT_SHIM_PORT` and something else holds it. Unset it to get a free port.
- **Leftover shim after `kill -9`:** list them with `pgrep -fl "serve 0 "`, then `kill <pid>` the orphaned one. Don't use `pkill -f` here, it would also kill the shims of any other running instances.
- **"GitHub refused the Copilot token request":** the stored login is bad or the account has no Copilot access. Delete `~/.local/share/claude-copilot/github_token` and rerun.
- **Shim didn't start:** check `$TMPDIR/claude-copilot.log`.
- **400 `model_not_supported`:** the model ID doesn't exist on Copilot. `/model` only lists the ones that do.
- **400 "prefill" on a Claude 5.x model:** the request shape changed. Look at the log.
- **400 "Extra inputs are not permitted" showing up for the client:** the shim couldn't find the field Copilot named, or hit the 5-retry cap. Look at the log for the field name.

## How the JetBrains Copilot plugin does the same

Observed locally, plugin is closed source. Short version: the IDE's `copilot-language-server` spawns `claude` and runs its own Anthropic-compatible local endpoint (`127.0.0.1:<random port>`) that forwards to Copilot. The bearer token is per session and unreadable from outside, so a standalone `claude` can't reuse it. That's why this script runs its own shim.

## Credits

Made by [Dikran Seropian](https://github.com/seropian). The device login flow and the Anthropic to chat-completions translation follow [copilot-api](https://github.com/ericc-ch/copilot-api) by ericc-ch (MIT), which earlier versions of this script ran as a gateway.

## License

[Apache 2.0](LICENSE)
