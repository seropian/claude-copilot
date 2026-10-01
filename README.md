# claude-copilot

Run Claude Code on GitHub Copilot models. No Anthropic account needed, just a Copilot plan.

One self-contained file: `claude-copilot.sh`. Works in bash 3.2+ (incl. macOS `/bin/bash`) and zsh. Not POSIX sh.

## Requirements

`node`/`npx`, `python3`, `nc`, `lsof`, `claude`. Run it from a normal terminal.

First run asks for a GitHub device-code login (open the URL, type the code). The login is cached in `~/.local/share/copilot-api`.

## Install

Put it on PATH and run it as a command:

Install it straight from GitHub:

```sh
curl -fsSL https://raw.githubusercontent.com/seropian/claude-copilot/main/install.sh | bash
```

Installs to `~/.local/bin/claude-copilot`. Rerun to update.

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
claude -> shim (python, :4142) -> copilot-api (npx, :4141) -> GitHub Copilot
```

- Claude Code is pointed at a local server via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (the token is ignored locally). Env is scoped to the one `claude` command, your shell stays clean.
- [copilot-api](https://github.com/ericc-ch/copilot-api) turns Anthropic/OpenAI requests into Copilot requests using your GitHub login. Pinned to `0.7.0`, since it sees your GitHub token.
- The shim fixes one incompatibility: Claude Code 2.1.x sends a trailing `role: "system"` message in `messages`, and Copilot's Claude 5.x models reject it with 400 "does not support assistant message prefill". The shim rewrites system-role messages to user and merges adjacent user turns (`tool_result` blocks first). Everything else, incl. streaming, passes through.
- Model aliases (sonnet/opus/haiku/fable) are mapped to Copilot model IDs, since Anthropic's IDs don't exist on Copilot.
- At startup the script reads `/v1/models` (every model your Copilot plan offers, minus embeddings) and passes them to `claude --settings` as a `modelPicker` list, so `/model` shows all of them. Claude Code's own gateway discovery isn't used: it drops any id without `claude` in it. The picker can't tell which models actually work (see "Tested models"), and rate-limited ones (429) will just hang until Claude Code gives up.

Lifecycle:

1. Start the gateway only if `:4141` is closed (waits up to 3 min, for the login). Bails fast if the gateway process dies.
2. Start the shim. Bails if its port is taken.
3. Run `claude`.
4. Cleanup: kill the shim. Kill the gateway only if this script started it and no other run is using it. HUP/TERM/INT clean up too.

Parallel runs work: use a different `COPILOT_SHIM_PORT` for each. They share one gateway, the last run out kills it.

## Config

All env vars, all optional.

| Var | Default | What |
|---|---|---|
| `COPILOT_CLAUDE_MODEL` | `claude-sonnet-5.5` | main model |
| `COPILOT_SONNET_MODEL` | `claude-sonnet-5.5` | `sonnet` alias target |
| `COPILOT_OPUS_MODEL` | `claude-opus-5.5` | `opus` alias target |
| `COPILOT_FABLE_MODEL` | `claude-opus-5.5` | `fable` alias target. Copilot has no Fable model, so picking it really gives you whatever this points to |
| `COPILOT_HAIKU_MODEL` | `claude-haiku-4.5` | `haiku` alias target |
| `COPILOT_API_PORT` | `4141` | gateway port |
| `COPILOT_SHIM_PORT` | `4142` | shim port, unique per parallel run |
| `COPILOT_API_CMD` | `npx copilot-api@0.7.0 start --port $COPILOT_API_PORT` | gateway start command |

Files:

- `$TMPDIR/copilot-api.log`: gateway + shim log
- `$TMPDIR/claude-copilot.<port>.run` / `.pid`: run markers and gateway PID, used for the last-one-out cleanup

## Tested models

Checked 2026-10-01 with copilot-api 0.7.0 and Claude Code 2.1.286. Each model got a plain "reply ok" check and a Bash tool-call check.

- **Work:** claude-opus-4.7, claude-opus-4.8, claude-opus-5.5, claude-opus-5, claude-sonnet-5.5, claude-sonnet-5, claude-haiku-4.5, gemini-3.7-flash, gemini-3.8-flash, kimi-k3, gpt-5-mini, gpt-4.1, gpt-4o, gpt-4o-mini, gpt-4, gpt-3.5-turbo
- **Partial:** kimi-k2.7-code. Plain ok, tool test failed (said the command ran, never reported the output).
- **Fail:**
    - gpt-5.3-codex, gpt-5.4-mini, gpt-5.5, gpt-5.6-luna/-sol/-terra, gpt-6-luna/-sol, gpt-6.1-sol, grok-4.7, mai-code-1.1-flash: 400 "not accessible via the /chat/completions endpoint" (Copilot serves them only on `/responses`)
    - gpt-5.4: 400 Bad Request, cause unknown
    - gpt-41-copilot: 400 model_not_supported

Not tested: embeddings, trajectory-compaction (not chat models), `auto`. The model list depends on your Copilot plan: see "Available models" in the gateway output at startup, or `curl localhost:4141/v1/models`.

## Known issues / limits

- **Security:** copilot-api listens on all interfaces. While it runs, other machines on your network could use your Copilot session. Trusted networks only.
- **Unofficial:** copilot-api is a reverse-engineered proxy. It may break when GitHub changes things, and may be against GitHub's terms. Your call.
- **Billing:** premium requests count against your Copilot plan.
- **Non-Claude models:** GPT/Gemini/Kimi go through a translation layer, tool calling can be less reliable than Claude.
- **`auto` model:** Copilot's auto-routing is done client-side by Copilot's own apps. copilot-api doesn't list or route it. Use Copilot CLI for that.
- **Catalog warning:** Claude Code warns the model isn't in its catalog and assumes a 200k context. The script sets `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` to relax that. Auto-mode classifier billing note is harmless.
- **`claude --bare`** also avoids the system-message problem, but drops hooks and CLAUDE.md. The shim is the better fix.
- **Reused gateway:** if `:4141` is already open, the script assumes it's copilot-api and doesn't check.

## Troubleshooting

- **Port busy:** set `COPILOT_SHIM_PORT` (or `COPILOT_API_PORT`).
- **Leftover processes after `kill -9`:**
  ```sh
  lsof -ti tcp:4141 -sTCP:LISTEN | xargs kill
  lsof -ti tcp:4142 -sTCP:LISTEN | xargs kill
  ```
- **Gateway didn't come up:** check `$TMPDIR/copilot-api.log`.
- **400 `model_not_supported`:** the model ID doesn't exist on Copilot. Check `/v1/models`.
- **400 "prefill" on a Claude 5.x model:** the shim isn't in the path, or the request shape changed. Look at the log.

## Upgrade path (not done on purpose)

- npm `copilot-api` is stuck at 0.7.0 (last published 2025-10-05).
- Upstream PR [ericc-ch/copilot-api#274](https://github.com/ericc-ch/copilot-api/pull/274) adds `/responses` translation for the failing GPT-5.x/6 models. Open and unmerged as of 2026-10-01.
- npm forks that claim `/responses` support: `@jeffreycao/copilot-api`, `xiaodcs-copilot-api`, `@dianshuv/copilot-api`, `copilot-api-plus`, `copilot-api-node20`. **Not vetted.** The gateway sees your GitHub token, so read the code and `npm view <pkg> scripts` first. To try one: set `COPILOT_API_CMD`, rerun the model tests, see if the shim is still needed.

## How the JetBrains Copilot plugin does the same

Observed locally, plugin is closed source. Why this script runs its own gateway instead of reusing the IDE's:

- The IDE runs `copilot-language-server --stdio` (native binary in the plugin), which spawns your `claude` (path from Settings > GitHub Copilot > Chat > "Enable Claude Code CLI") through the Claude Agent SDK, speaking stream-json over stdin/stdout. Permission prompts go to the IDE UI.
- The language server runs its own Anthropic-compatible local endpoint (`127.0.0.1:<random port>`, bearer token required) that forwards to Copilot, plus an MCP gateway exposing the IDE's `github` and `intellij` MCP servers.
- The token is per session and unreadable from outside. A standalone `claude` against that endpoint worked only with that session's token, so it's a dead end for terminal use.

## Credits

Made by [Dikran Seropian](https://github.com/seropian). Built on [copilot-api](https://github.com/ericc-ch/copilot-api) by ericc-ch.

## License

[Apache 2.0](LICENSE)
