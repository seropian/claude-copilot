#!/usr/bin/env bash
# Real e2e: real launcher, real shim, real `claude`, real GitHub Copilot. No fakes.
# Needs a stored GitHub login (~/.local/share/claude-copilot) and `claude` on PATH.
# Skips (exit 0) when either is missing. Spends a few small Copilot requests per model.
# Run: bash tests/test_e2e.sh
# Models per route (override as needed): E2E_NATIVE, E2E_RESPONSES, E2E_CHAT. Missing ones are skipped.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/dist/claude-copilot.sh"
TMP="$(mktemp -d)"
dpid=""
trap '[ -n "$dpid" ] && kill "$dpid" 2>/dev/null; rm -rf "$TMP"' EXIT
pass=0 fail=0 skip=0

ok()   { pass=$((pass + 1)); echo "ok   - $1"; }
bad()  { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && echo "       $2" | head -c 600; }
skip() { skip=$((skip + 1)); echo "skip - $1"; }
contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

if ! command -v claude >/dev/null 2>&1; then echo "skip - e2e: claude not on PATH"; exit 0; fi
if [ ! -s "$HOME/.local/share/claude-copilot/github_token" ]; then
  echo "skip - e2e: no GitHub login stored, run claude-copilot once first"; exit 0
fi

NATIVE="${E2E_NATIVE:-claude-haiku-4.5}"
RESPONSES="${E2E_RESPONSES:-gpt-5-mini}"
CHAT="${E2E_CHAT:-gemini-3.7-flash}"

# Run the launcher with a timeout (perl is on every mac/linux box, `timeout` is not).
# args: seconds, then launcher args. Output in $OUT, exit code in $RC.
run() {
  local secs="$1"; shift
  OUT=$(cd "$TMP" && TMPDIR="$TMP" CLAUDE_CONFIG_DIR="$TMP/claude-config" perl -e 'alarm shift; exec @ARGV' "$secs" /bin/bash "$SCRIPT" "$@" 2>&1); RC=$?
}

# Shim processes left over from our runs (their command line holds the port file under $TMP).
shims() { pgrep -f "$TMP/claude-copilot\.[A-Za-z0-9]+" 2>/dev/null | wc -l | tr -d ' '; }

# ---------- model discovery: which of our models does this Copilot plan have? ----------

src=$(cat "$SCRIPT")
shim_py="${src#*local shim=\'}"; shim_py="${shim_py%%
\'
*}"
pf="$TMP/disc.pf"
COPILOT_SHIM_KEY=e2e-disc python3 -c "$shim_py" serve 0 "$pf" >"$TMP/disc.log" 2>&1 &
dpid=$!
port=""
for _ in $(seq 1 50); do read -r _ port 2>/dev/null < "$pf"; [ -n "$port" ] && break; sleep 0.1; done
avail=$(curl -sf -m 30 -H "x-api-key: e2e-disc" "http://127.0.0.1:$port/v1/models" 2>/dev/null)
kill "$dpid" 2>/dev/null
if [ -z "$avail" ]; then bad "shim lists models from real Copilot" "$(cat "$TMP/disc.log")"; echo "$pass passed, $fail failed"; exit 1; fi
ok "shim lists models from real Copilot"
has_model() { contains "$avail" "\"id\": \"$1\"" || contains "$avail" "\"id\":\"$1\""; }

before=$(shims)

# ---------- one prompt per route ----------

for entry in "native:$NATIVE" "responses:$RESPONSES" "chat:$CHAT"; do
  route="${entry%%:*}"; model="${entry#*:}"
  if ! has_model "$model"; then skip "$route route ($model not on this plan)"; continue; fi

  COPILOT_CLAUDE_MODEL="$model" run 180 -p "Reply with exactly the word PONG and nothing else."
  if [ $RC -eq 0 ] && contains "$OUT" "PONG"; then ok "$route route ($model): plain prompt"; else bad "$route route ($model): plain prompt (rc=$RC)" "$OUT"; fi

  COPILOT_CLAUDE_MODEL="$model" run 240 -p "Run this bash command and tell me what it printed: echo E2E_TOOL_\$((6*7))" \
    --allowedTools Bash --permission-mode acceptEdits
  if [ $RC -eq 0 ] && contains "$OUT" "E2E_TOOL_42"; then ok "$route route ($model): tool call round trip"; else bad "$route route ($model): tool call round trip (rc=$RC)" "$OUT"; fi

  COPILOT_CLAUDE_MODEL="$model" run 180 -p "Count from 1 to 5, one number per line." --output-format stream-json --verbose
  if [ $RC -eq 0 ] && contains "$OUT" '"type":"result"' && contains "$OUT" '"is_error":false'; then ok "$route route ($model): streaming completes cleanly"; else bad "$route route ($model): streaming (rc=$RC)" "$OUT"; fi
done

# ---------- launcher behavior against the real thing ----------

# exit code of claude comes through (bad flag value makes claude fail)
run 60 --permission-mode definitely-not-a-mode -p hi
[ $RC -ne 0 ] && ok "claude's failing exit code is passed through" || bad "claude's failing exit code is passed through" "$OUT"

# bogus model: Copilot's error reaches claude, launcher still exits and cleans up
COPILOT_CLAUDE_MODEL="no-such-model-xyz" run 120 -p "hi"
if [ $RC -ne 0 ] && [ $RC -ne 142 ] && contains "$OUT" "no-such-model-xyz"; then ok "unknown model fails visibly, no hang"; else bad "unknown model fails visibly, no hang (rc=$RC)" "$OUT"; fi

# two launchers at once don't fight over a port
( COPILOT_CLAUDE_MODEL="$NATIVE" run 180 -p "Reply with exactly: A1"; echo "$RC $OUT" > "$TMP/a" ) &
( COPILOT_CLAUDE_MODEL="$NATIVE" run 180 -p "Reply with exactly: B2"; echo "$RC $OUT" > "$TMP/b" ) &
wait
if has_model "$NATIVE"; then
  a=$(cat "$TMP/a"); b=$(cat "$TMP/b")
  if [ "${a%% *}" = 0 ] && [ "${b%% *}" = 0 ] && contains "$a" "A1" && contains "$b" "B2"; then ok "parallel launchers each get their own shim"; else bad "parallel launchers each get their own shim" "$a | $b"; fi
else
  skip "parallel launchers ($NATIVE not on this plan)"
fi

# no shim left behind
sleep 1
after=$(shims)
[ "$after" -eq 0 ] && ok "no shim processes left after runs" || bad "no shim processes left after runs" "before=$before after=$after"

echo
echo "$pass passed, $fail failed, $skip skipped"
[ "$fail" -eq 0 ]
