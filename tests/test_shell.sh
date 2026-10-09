#!/usr/bin/env bash
# Shell tests: prerequisite check, cleanup, source-vs-exec, the full launcher flow (with a fake claude), install.sh.
# Run: bash tests/test_shell.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/dist/claude-copilot.sh"
INSTALL="$ROOT/install.sh"
TMP="$(mktemp -d)"
cleanup() { # launcher shims are nohup'd and outlive the test, so kill any serving out of $TMP before deleting it
  for f in $(find "$TMP" -name shim.state 2>/dev/null); do read -r p _ < "$f"; kill "$p" 2>/dev/null; done
  ps eww -axo pid=,command= 2>/dev/null | grep -F "$TMP/" | grep -F ' serve ' | awk '{print $1}' | xargs kill 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP
pass=0 fail=0

ok()   { pass=$((pass + 1)); echo "ok   - $1"; }
bad()  { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && echo "       $2"; }
check() { # name, condition exit code
  if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1" "${3:-}"; fi
}
contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

PY="$(command -v python3)"
CURL="$(command -v curl)"

# stub dir with chosen tools. args: dir, tools...
mkbin() {
  local d="$1"; shift
  mkdir -p "$d"
  for t in "$@"; do
    case "$t" in
      python3) ln -sf "$PY" "$d/python3" ;;
      curl)    ln -sf "$CURL" "$d/curl" ;;
      *)       printf '#!/bin/sh\nexit 0\n' > "$d/$t"; chmod +x "$d/$t" ;;
    esac
  done
}

# ---------- _cc_check ----------

mkbin "$TMP/all" python3 curl claude
out=$(PATH="$TMP/all" /bin/bash -c ". '$SCRIPT'; _cc_check" 2>&1); rc=$?
check "_cc_check passes with all tools" $rc "$out"

mkbin "$TMP/nopy" curl claude
out=$(PATH="$TMP/nopy" /bin/bash -c ". '$SCRIPT'; _cc_check" 2>&1); rc=$?
check "_cc_check fails without python3" $((rc == 1 ? 0 : 1))
contains "$out" "missing required tools: python3"; check "_cc_check names python3" $?
contains "$out" "python.org"; check "_cc_check gives python3 hint" $?

mkbin "$TMP/none"
out=$(PATH="$TMP/none" /bin/bash -c ". '$SCRIPT'; _cc_check" 2>&1); rc=$?
check "_cc_check fails with nothing installed" $((rc == 1 ? 0 : 1))
contains "$out" "python3" && contains "$out" "curl" && contains "$out" "claude"; check "_cc_check lists all missing tools" $?
contains "$out" "brew install curl"; check "_cc_check gives curl hint" $?
contains "$out" "code.claude.com"; check "_cc_check gives claude hint" $?

mkdir -p "$TMP/oldpy"
printf '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "Python 3.5.0"; exit 0; fi\nexit 1\n' > "$TMP/oldpy/python3"
chmod +x "$TMP/oldpy/python3"; mkbin "$TMP/oldpy" curl claude
out=$(PATH="$TMP/oldpy" /bin/bash -c ". '$SCRIPT'; _cc_check" 2>&1); rc=$?
check "_cc_check rejects python < 3.6" $((rc == 1 ? 0 : 1))
contains "$out" "3.6 or newer"; check "_cc_check explains version requirement" $?

# ---------- _cc_cleanup ----------

sleep 30 & victim=$!
echo x > "$TMP/pf"
/bin/bash -c ". '$SCRIPT'; _cc_cleanup $victim '$TMP/pf'"; rc=$?
sleep 0.2
kill -0 $victim 2>/dev/null && alive=1 || alive=0
check "_cc_cleanup kills the shim pid" $alive
[ ! -e "$TMP/pf" ]; check "_cc_cleanup removes the port file" $?
check "_cc_cleanup returns 0" $rc
wait $victim 2>/dev/null
/bin/bash -c ". '$SCRIPT'; _cc_cleanup '' ''"; check "_cc_cleanup tolerates empty args" $?

# ---------- exit-time updater ----------

UPD="$TMP/update"; mkdir -p "$UPD"
printf old > "$UPD/app"
printf new > "$UPD/download"
printf '%s\n' "$UPD/download" > "$UPD/state"
/bin/bash -c ". '$SCRIPT'; _cc_schedule_update \"\$1\" \"\$2\"" _ "$UPD/app" "$UPD/state"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ "$(cat "$UPD/app")" = new ] && break
  sleep 0.1
done
[ "$(cat "$UPD/app")" = new ]; check "updater replaces the file after launcher exit" $?

# ---------- update check version comparison ----------

UC="$TMP/uc"; mkdir -p "$UC/bin"
printf '#!/bin/sh\n' > "$UC/payload"
UC_SUM=$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$UC/payload")
cat > "$UC/bin/curl" <<EOF
#!/bin/sh
case "\$*" in
  *api.github.com*) echo "{\"tag_name\": \"v\$FAKE_LATEST\"}" ;;
  *.sha256*) echo "$UC_SUM  claude-copilot.sh" ;;
  *) while [ \$# -gt 0 ]; do [ "\$1" = -o ] && cp "$UC/payload" "\$2"; shift; done ;;
esac
EOF
chmod +x "$UC/bin/curl"
ln -sf "$PY" "$UC/bin/python3"
printf '#!/bin/sh\n' > "$UC/app"
uc_staged() { # running version, latest published
  rm -f "$UC/state"
  PATH="$UC/bin:/usr/bin:/bin" FAKE_LATEST="$2" /bin/bash -c ". '$SCRIPT'; _cc_version='$1'; _cc_check_update '$UC/app' '$UC/state' \$\$" >/dev/null 2>&1
  [ -s "$UC/state" ]
}
uc_staged 1.0.0 1.1.0; check "update check stages a newer release" $?
uc_staged 1.1.0 1.1.0; [ $? -ne 0 ]; check "update check ignores the same version" $?
uc_staged 1.2.0 1.1.0; [ $? -ne 0 ]; check "update check ignores an older release" $?
uc_staged 1.9.0 1.10.0; check "update check compares numerically (1.10.0 > 1.9.0)" $?
rm -f "$UC"/.claude-copilot-update.*
rm -f "$UC/state"; PATH="$UC/bin:/usr/bin:/bin" FAKE_LATEST=1.1.0 /bin/bash -c ". '$SCRIPT'; _cc_version=1.0.0; _cc_check_update '$UC/app' '$UC/state' 999999" >/dev/null 2>&1
[ ! -e "$UC/state" ] && [ -z "$(ls -A "$UC" | grep '^.claude-copilot-update')" ]; check "update check drops its download when the launcher is gone" $?
uc_state() { # extra env assignment
  env -i HOME="$UC" PATH="$UC/bin:/usr/bin:/bin" FAKE_LATEST= "$@" /bin/bash -c ". '$SCRIPT'; _cc_update_enabled=1; _cc_start_update_check '$UC/app'; wait; printf '[%s]' \"\${_cc_update_state:-}\""
}
case "$(uc_state X=1)" in "[]") false ;; *) true ;; esac; check "update check is on by default" $?
[ "$(uc_state COPILOT_AUTO_UPDATE=0)" = "[]" ]; check "COPILOT_AUTO_UPDATE=0 turns the update check off" $?
rm -f "$UC"/.claude-copilot-update-state.*

# ---------- sourcing vs executing ----------

out=$(/bin/bash -c ". '$SCRIPT'; type claude-copilot" 2>&1); rc=$?
check "sourcing defines claude-copilot" $rc
contains "$out" "function"; check "claude-copilot is a function when sourced" $?

out=$(PATH="$TMP/none" /bin/bash "$SCRIPT" 2>&1); rc=$?
check "executing runs the launcher (and exits non-zero on missing tools)" $((rc == 1 ? 0 : 1))
contains "$out" "missing required tools"; check "executing reaches _cc_check" $?

if command -v zsh >/dev/null 2>&1; then
  out=$(zsh -c ". '$SCRIPT'; whence -w claude-copilot" 2>&1); rc=$?
  check "sourcing in zsh defines the function without running it" $rc "$out"
  out=$(PATH="$TMP/none" /bin/zsh "$SCRIPT" 2>&1 || PATH="$TMP/none" zsh "$SCRIPT" 2>&1)
  contains "$out" "missing required tools"; check "executing in zsh runs the launcher" $?
fi

# ---------- full launcher flow with a fake claude ----------

FLOW="$TMP/flow"; mkdir -p "$FLOW/bin" "$FLOW/tmp"
ln -sf "$PY" "$FLOW/bin/python3"; ln -sf "$CURL" "$FLOW/bin/curl"
cat > "$FLOW/bin/claude" <<EOF
#!/bin/sh
{
  echo "BASE=\$ANTHROPIC_BASE_URL"
  echo "TOKEN=\$ANTHROPIC_AUTH_TOKEN"
  echo "MODEL=\$ANTHROPIC_MODEL"
  echo "SONNET=\$ANTHROPIC_DEFAULT_SONNET_MODEL"
  echo "OPUS=\$ANTHROPIC_DEFAULT_OPUS_MODEL"
  echo "HAIKU=\$ANTHROPIC_DEFAULT_HAIKU_MODEL"
  echo "NONESS=\$CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
  echo "ARGS=\$*"
  printf '%s\n' "\$@" > "$FLOW/claude.args"
} > "$FLOW/claude.out"
# prove the shim is alive while claude runs
port=\${ANTHROPIC_BASE_URL##*:}
curl -s -o /dev/null -w "%{http_code}" -H "x-api-key: \$ANTHROPIC_AUTH_TOKEN" "http://127.0.0.1:\$port/nope" > "$FLOW/shim.status"
curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:\$port/nope" > "$FLOW/shim.nokey"
curl -s -o /dev/null -w "%{http_code}" -H "x-api-key: wrong" "http://127.0.0.1:\$port/nope" > "$FLOW/shim.badkey"
exit \${FAKE_CLAUDE_RC:-0}
EOF
chmod +x "$FLOW/bin/claude"
echo "fake-gh-token" > "$FLOW/token"

run_flow() { # env assignments via "$@" prefix, returns launcher rc
  env -i HOME="${FLOW_HOME:-$FLOW}" PATH="$FLOW/bin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" "$@" \
    /bin/bash "$SCRIPT" --extra-flag value 2>"$FLOW/stderr"
}

run_flow; rc=$?
check "launcher exits 0 when claude exits 0" $rc "$(cat "$FLOW/stderr")"
out=$(cat "$FLOW/claude.out" 2>/dev/null)
contains "$out" "BASE=http://127.0.0.1:"; check "launcher points ANTHROPIC_BASE_URL at the local shim" $?
case "$out" in *TOKEN=????????????????????????*) t=0 ;; *) t=1 ;; esac; check "launcher sets a random per-run auth token" $t
! contains "$out" "TOKEN=placeholder"; check "auth token is not the old placeholder" $?
contains "$out" "MODEL="; check "default leaves model selection to Claude Code" $?
contains "$out" "SONNET=claude-sonnet-5.5"; check "default sonnet mapping" $?
contains "$out" "OPUS=claude-opus-5.5"; check "default opus mapping" $?
contains "$out" "HAIKU=claude-haiku-4.5"; check "default haiku mapping" $?
contains "$out" "NONESS=1"; check "nonessential traffic disabled" $?
case "$out" in *"ARGS=--model"*) t=1 ;; *) t=0 ;; esac
check "default does not pass --model to claude" $?

run_flow ANTHROPIC_MODEL=caller-model
out=$(cat "$FLOW/claude.out")
contains "$out" "MODEL="; check "default clears caller model override" $?

run_flow COPILOT_CLAUDE_MODEL=
out=$(cat "$FLOW/claude.out")
contains "$out" "MODEL=" && ! contains "$out" "ARGS=--model"; check "empty model override uses Claude Code selection" $?
contains "$out" "--extra-flag value"; check "forwards user args to claude" $?
[ "$(cat "$FLOW/shim.status")" = "404" ]; check "shim is serving while claude runs" $?
[ "$(cat "$FLOW/shim.nokey")" = "401" ] && [ "$(cat "$FLOW/shim.badkey")" = "401" ]; check "shim rejects requests without the right key" $?
[ -z "$(ls "$FLOW/tmp" | grep -v claude-copilot.log)" ]; check "port file removed after exit" $?
[ -f "$FLOW/.local/share/claude-copilot/claude-copilot.log" ] && [ ! -e "$FLOW/tmp/claude-copilot.log" ]; check "shim log is written to the state dir" $?

port=$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")
sleep 0.3
code=$("$CURL" -s -m 2 -o /dev/null -w '%{http_code}' -H "x-api-key: $(sed -n 's/^TOKEN=//p' "$FLOW/claude.out")" "http://127.0.0.1:$port/nope")
[ "$code" = 404 ]; check "shim stays up during idle grace" $? "code=$code"
[ -e "$FLOW/.local/share/claude-copilot/shim.state" ]; check "idle shim keeps state" $?

run_flow; rc=$?
[ "$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")" = "$port" ] && [ "$(sed -n 's/^TOKEN=//p' "$FLOW/claude.out")" != "" ]
check "next run reuses the shared shim" $? "rc=$rc"

IDLE_STATE="$FLOW/idle-home/.local/share/claude-copilot"
FLOW_HOME="$FLOW/idle-home" run_flow COPILOT_SHIM_IDLE_GRACE=1; idle_port=$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")
sleep 4
code=$($CURL -s -m 2 -o /dev/null -w '%{http_code}' -H "x-api-key: $(sed -n 's/^TOKEN=//p' "$FLOW/claude.out")" "http://127.0.0.1:$idle_port/nope")
[ ! -e "$IDLE_STATE/shim.state" ]; check "shim stops after idle grace" $? "code=$code"
[ ! -e "$IDLE_STATE/shim.state" ]; check "idle cleanup removes shim state" $?

run_flow; rc=$?
[ "$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")" = "$port" ] && [ "$(sed -n 's/^TOKEN=//p' "$FLOW/claude.out")" != "" ]
check "shared shim remains available after idle cleanup" $? "rc=$rc"

# shim killed behind our back: next run starts another shim
read -r kp _ < "$FLOW/.local/share/claude-copilot/shim.state" 2>/dev/null || kp=""
[ -n "$kp" ] && kill "$kp"; sleep 0.5
run_flow; rc=$?
[ -n "$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")" ] && [ "$(sed -n 's/^TOKEN=//p' "$FLOW/claude.out")" != "" ]
check "dead shim is restarted" $? "rc=$rc"
[ "$(cat "$FLOW/shim.status")" = 404 ]; check "restarted shim accepts its key" $?

# The following model and cleanup checks use the restarted normal shim.

run_flow COPILOT_CLAUDE_MODEL=my-model COPILOT_SONNET_MODEL=s1 COPILOT_OPUS_MODEL=o1 COPILOT_HAIKU_MODEL=h1
out=$(cat "$FLOW/claude.out")
contains "$out" "MODEL=my-model" && contains "$out" "--model my-model"; check "COPILOT_CLAUDE_MODEL overrides model" $?
contains "$out" "SONNET=s1" && contains "$out" "OPUS=o1" && contains "$out" "HAIKU=h1"; check "COPILOT_*_MODEL overrides tier mappings" $?

# Keep the existing process-restart coverage below on a fresh normal shim.
read -r kp _ < "$FLOW/.local/share/claude-copilot/shim.state" 2>/dev/null || kp=""
[ -n "$kp" ] && kill "$kp"; sleep 0.5
run_flow; rc=$?
[ "$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")" != "" ]; check "second dead shim is restarted" $? "rc=$rc"
[ "$(cat "$FLOW/shim.status")" = 404 ]; check "second restarted shim accepts its key" $?

run_flow COPILOT_CLAUDE_MODEL=my-model COPILOT_SONNET_MODEL=s1 COPILOT_OPUS_MODEL=o1 COPILOT_HAIKU_MODEL=h1
out=$(cat "$FLOW/claude.out")
contains "$out" "MODEL=my-model" && contains "$out" "--model my-model"; check "COPILOT_CLAUDE_MODEL overrides model" $?
contains "$out" "SONNET=s1" && contains "$out" "OPUS=o1" && contains "$out" "HAIKU=h1"; check "COPILOT_*_MODEL overrides tier mappings" $?

run_flow FAKE_CLAUDE_RC=7; rc=$?
check "launcher propagates claude's exit code" $((rc == 7 ? 0 : 1)) "rc=$rc"

# fixed port
fp=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
read -r original_pid original_port _ < "$FLOW/.local/share/claude-copilot/shim.state"
rm -f "$FLOW/claude.out"
run_flow COPILOT_SHIM_PORT=$fp; rc=$?
check "different port cannot start a second shim" $((rc == 1 ? 0 : 1))
contains "$(cat "$FLOW/stderr")" "shared shim already uses port"; check "port conflict explains shared shim policy" $?
read -r current_pid current_port _ < "$FLOW/.local/share/claude-copilot/shim.state"
[ "$original_pid:$original_port" = "$current_pid:$current_port" ] && [ ! -e "$FLOW/claude.out" ]; check "port conflict preserves the existing shim and does not run claude" $?
run_flow COPILOT_SHIM_PORT=$original_port; check "matching port reuses shared shim" $?
run_flow COPILOT_STATE_DIR="$FLOW/custom-state"; rc=$?
[ "$rc" = 1 ] && [ ! -e "$FLOW/custom-state" ]; check "custom state directory is rejected without creating it" $?
contains "$(cat "$FLOW/stderr")" "Unset COPILOT_STATE_DIR"; check "custom state rejection gives migration guidance" $?
UNHEALTHY_HOME="$FLOW/unhealthy-home"
mkdir -p "$UNHEALTHY_HOME/.local/share/claude-copilot"
sleep 30 & unhealthy_pid=$!
printf '%s %s bad-key old\n' "$unhealthy_pid" "$fp" > "$UNHEALTHY_HOME/.local/share/claude-copilot/shim.state"
FLOW_HOME="$UNHEALTHY_HOME" run_flow; rc=$?
check "live unhealthy shim PID blocks a second shim" $((rc == 1 ? 0 : 1))
read -r recorded_pid _ < "$UNHEALTHY_HOME/.local/share/claude-copilot/shim.state"
[ "$recorded_pid" = "$unhealthy_pid" ]; check "unhealthy shim retains its recorded state" $?
kill "$unhealthy_pid"; wait "$unhealthy_pid" 2>/dev/null
env -i HOME="$FLOW" PATH="$FLOW/bin:/usr/bin:/bin" /bin/bash "$SCRIPT" --stop-shim >/dev/null
run_flow COPILOT_SHIM_PORT=$fp
contains "$(cat "$FLOW/claude.out")" "BASE=http://127.0.0.1:$fp"; check "COPILOT_SHIM_PORT is honored" $?

# busy fixed port (a fresh port: the previous one may still be in TIME_WAIT and refuse a plain bind)
bp=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
env -i HOME="$FLOW" PATH="$FLOW/bin:/usr/bin:/bin" /bin/bash "$SCRIPT" --stop-shim >/dev/null
"$PY" -c 'import socket,time,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.listen(1); time.sleep(20)' "$bp" & holder=$!
sleep 0.5
rm -f "$FLOW/claude.out"
run_flow COPILOT_SHIM_PORT=$bp; rc=$?
kill $holder 2>/dev/null; wait $holder 2>/dev/null
check "busy COPILOT_SHIM_PORT fails" $((rc == 1 ? 0 : 1))
contains "$(cat "$FLOW/stderr")" "busy"; check "busy port explains itself" $?
[ ! -e "$FLOW/claude.out" ]; check "claude is not started when port is busy" $?

# update notice: silent on first run and same version, says so once when the version changed
FLOW_HOME="$FLOW/st-ver-home" run_flow
! contains "$(cat "$FLOW/stderr")" "updated"; check "no update notice on first run" $?
FLOW_HOME="$FLOW/st-ver-home" run_flow
! contains "$(cat "$FLOW/stderr")" "updated"; check "no update notice on same version" $?
echo 0.0.1 > "$FLOW/st-ver-home/.local/share/claude-copilot/last-version"
FLOW_HOME="$FLOW/st-ver-home" run_flow
contains "$(cat "$FLOW/stderr")" "updated 0.0.1 -> "; check "update notice when the version changed" $?
FLOW_HOME="$FLOW/st-ver-home" run_flow
! contains "$(cat "$FLOW/stderr")" "updated"; check "update notice shows only once" $?

# Deferred legacy cleanup is retried on later launches and does not advance its marker.
LR="$FLOW/legacy-cleanup"
mkdir -p "$LR/.local/share/claude-copilot"
echo 0.0.1 > "$LR/.local/share/claude-copilot/legacy-cleanup-version"
out=$(HOME="$LR" /bin/bash -c "
  . '$SCRIPT'
  _cc_version=2.0
  shim=unused
  cleanup_calls=0
  python3() {
    if [ \"\$3\" = cleanup ]; then cleanup_calls=\$((cleanup_calls + 1)); return 1; fi
    command python3 \"\$@\"
  }
  _cc_legacy_cleanup '$LR/.local/share/claude-copilot' '$SCRIPT' 0.0.1 0.0.1
  _cc_legacy_cleanup '$LR/.local/share/claude-copilot' '$SCRIPT' 0.0.1 2.0
  [ \"\$cleanup_calls\" = 2 ] && [ \"\$_cc_legacy_cleanup_deferred\" = 1 ] &&
    [ \"\$(cat '$LR/.local/share/claude-copilot/legacy-cleanup-version')\" = 0.0.1 ]
  echo \$?
")
[ "$out" = 0 ]; check "deferred legacy cleanup retries without advancing its marker" $?

# Slow legacy cleanup runs after the session in the background: it never delays claude or a concurrent launch.
SC="$FLOW/slow-cleanup"; SC_STATE="$SC/home/.local/share/claude-copilot"
mkdir -p "$SC/bin" "$SC/tmp" "$SC_STATE"
ln -sf "$CURL" "$SC/bin/curl"
cat > "$SC/bin/python3" <<EOF
#!/bin/sh
if [ "\$3" = cleanup ]; then
  echo \$\$ >> "$SC/cleanup.started"
  sleep 6
  echo \$\$ >> "$SC/cleanup.done"
  exit 1
fi
exec "$PY" "\$@"
EOF
cat > "$SC/bin/claude" <<EOF
#!/bin/sh
echo "\$ANTHROPIC_BASE_URL" >> "$SC/claude.log"
EOF
chmod +x "$SC/bin/python3" "$SC/bin/claude"
echo 0.0.1 > "$SC_STATE/last-version"
sc_run() { env -i HOME="$SC/home" PATH="$SC/bin:/usr/bin:/bin" TMPDIR="$SC/tmp" COPILOT_TOKEN_FILE="$FLOW/token" /bin/bash "$SCRIPT" >/dev/null 2>&1; }
sc_t0=$(date +%s); sc_run; sc_rc=$?; sc_t1=$(date +%s)
[ "$sc_rc" = 0 ] && [ "$(wc -l < "$SC/claude.log")" -eq 1 ] && [ $((sc_t1 - sc_t0)) -lt 5 ] && [ ! -e "$SC/cleanup.done" ]
check "slow legacy cleanup does not delay the launcher" $? "rc=$sc_rc took=$((sc_t1 - sc_t0))s"
for i in $(seq 1 50); do [ -s "$SC/cleanup.started" ] && break; sleep 0.1; done
[ -s "$SC/cleanup.started" ]; check "legacy cleanup is scheduled after the session exits" $?
sc_t0=$(date +%s); sc_run; sc_rc=$?; sc_t1=$(date +%s)
[ "$sc_rc" = 0 ] && [ "$(wc -l < "$SC/claude.log")" -eq 2 ] && [ $((sc_t1 - sc_t0)) -lt 5 ] && [ ! -e "$SC/cleanup.done" ]
check "launch during a running legacy cleanup starts claude without waiting" $? "rc=$sc_rc took=$((sc_t1 - sc_t0))s"
[ ! -e "$SC_STATE/shim.lock" ]; check "background legacy cleanup does not hold the shim lock" $?
[ "$(sort -u "$SC/claude.log" | wc -l)" -eq 1 ]; check "concurrent launch keeps the shared shim" $?
[ "$(cat "$SC_STATE/legacy-cleanup-version" 2>/dev/null)" != "$(cat "$ROOT/VERSION")" ]; check "deferred background cleanup keeps its retry marker" $?
for p in $(cat "$SC/cleanup.started"); do kill "$p" 2>/dev/null; done

# login failure stops the launcher
rm -f "$FLOW/claude.out"
env -i HOME="$FLOW/emptyhome" PATH="$FLOW/bin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/nothing/token" \
  /bin/bash -c "
    . '$SCRIPT'
    # make the device-code request fail fast: no network needed, bad host
    python3() { if [ \"\$1\" = -c ] && [ \"\${3:-}\" = login ]; then return 1; fi; command python3 \"\$@\"; }
    claude-copilot
  " 2>/dev/null; rc=$?
[ "$rc" -eq 1 ] && [ ! -e "$FLOW/claude.out" ]
check "login failure aborts before claude" $? "rc=$rc"

# non-numeric port is rejected up front
# a configured port is reusable after the previous client stopped
fp=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
run_flow COPILOT_SHIM_PORT="$fp"; rc=$?
[ "$rc" -eq 0 ] && [ "$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")" = "$fp" ]; check "shim starts on the configured port after cleanup" $? "rc=$rc $(cat "$FLOW/stderr")"
rm -f "$FLOW/claude.out"
run_flow COPILOT_SHIM_PORT=abc; rc=$?
check "non-numeric COPILOT_SHIM_PORT fails" $((rc == 1 ? 0 : 1))
contains "$(cat "$FLOW/stderr")" "must be a port number"; check "non-numeric port explains itself" $?
[ ! -e "$FLOW/claude.out" ]; check "claude is not started on a bad port" $?

# symlinked log path is refused
mkdir -p "$FLOW/st2-home/.local/share/claude-copilot"; ln -sf "$FLOW/victim" "$FLOW/st2-home/.local/share/claude-copilot/claude-copilot.log"
env -i HOME="$FLOW/st2-home" PATH="$FLOW/bin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" /bin/bash "$SCRIPT" 2>"$FLOW/stderr"; rc=$?
[ "$rc" -eq 1 ] && [ ! -e "$FLOW/victim" ]; check "symlinked log is refused, target untouched" $? "rc=$rc"

# works under set -u (empty --settings array on old bash)
rm -f "$FLOW/claude.out"
env -i HOME="$FLOW" PATH="$FLOW/bin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" \
  /bin/bash -c "set -u; . '$SCRIPT'; claude-copilot" 2>"$FLOW/stderr"; rc=$?
check "launcher works under set -u" $rc "$(cat "$FLOW/stderr")"

# fake python3: the real one, except `serve` either hangs (never writes the port file) or dies at once
mkdir -p "$FLOW/pybin"; ln -sf "$CURL" "$FLOW/pybin/curl"; cp "$FLOW/bin/claude" "$FLOW/pybin/claude"
cat > "$FLOW/pybin/python3" <<FAKEPY
#!/bin/sh
if [ "\$3" = serve ]; then
  case "\$FAKE_SERVE" in hang) exec sleep 30 ;; die) exit 1 ;; esac
fi
exec "$PY" "\$@"
FAKEPY
chmod +x "$FLOW/pybin/python3"

# shim never comes up: launcher gives up with a message, no claude, nothing left behind
rm -f "$FLOW/claude.out"
env -i HOME="$FLOW/st-die-home" PATH="$FLOW/pybin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" FAKE_SERVE=die \
  /bin/bash "$SCRIPT" 2>"$FLOW/stderr"; rc=$?
[ "$rc" -eq 1 ]; check "launcher fails when the shim dies at startup" $? "rc=$rc"
contains "$(cat "$FLOW/stderr")" "shim didn't start"; check "startup failure is reported" $?
[ ! -e "$FLOW/claude.out" ]; check "claude is not started when the shim dies" $?
[ -z "$(ls "$FLOW/tmp" | grep -v claude-copilot.log)" ]; check "startup failure leaves no temp files" $?
[ -z "$(ls "$FLOW/st-die-home/.local/share/claude-copilot" | grep '^start\.')" ]; check "startup failure leaves nothing in the state dir" $?

# signal during startup: shim is killed, files removed, right exit code
for sig in TERM INT HUP; do
  case $sig in TERM) want=143 ;; INT) want=130 ;; HUP) want=129 ;; esac
  env -i HOME="$FLOW/st-hang-$sig-home" PATH="$FLOW/pybin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" FAKE_SERVE=hang \
    perl -e '$SIG{INT} = "DEFAULT"; exec @ARGV' /bin/bash "$SCRIPT" 2>/dev/null & lp=$!
  spid=""
  for _ in $(seq 1 50); do spid=$(cat "$FLOW/st-hang-$sig-home/.local/share/claude-copilot"/start.*.pid 2>/dev/null); [ -n "$spid" ] && break; sleep 0.1; done
  kill -$sig $lp; wait $lp 2>/dev/null; rc=$?
  [ "$rc" -eq "$want" ]; check "SIG$sig during startup returns $want" $? "rc=$rc"
  sleep 0.2
  [ -n "$spid" ] && ! kill -0 "$spid" 2>/dev/null; check "SIG$sig during startup kills the shim" $? "spid=$spid"
  [ -z "$(ls "$FLOW/tmp" | grep -v claude-copilot.log)" ]; check "SIG$sig during startup removes temp files" $?
  [ -n "$spid" ] && kill "$spid" 2>/dev/null
done

# ---------- install.sh ----------

IN="$TMP/inst"; mkdir -p "$IN/bin"
cat > "$IN/bin/curl" <<EOF
#!/bin/sh
# fake curl: records the url, writes a script to the -o target; FAKE_CURL_FAIL=1 fails
echo "\$@" > "$IN/curl.args"
[ "\${FAKE_CURL_FAIL:-}" = 1 ] && exit 22
while [ \$# -gt 0 ]; do [ "\$1" = -o ] && out="\$2"; shift; done
echo '#!/bin/sh' > "\$out"
EOF
chmod +x "$IN/bin/curl"
mkbin "$IN/bin" python3 claude

out=$(env -i HOME="$IN/home" PATH="$IN/bin:/usr/bin:/bin" /bin/bash "$INSTALL" 2>&1); rc=$?
check "install.sh succeeds" $rc "$out"
[ -x "$IN/home/.local/bin/claude-copilot" ]; check "install.sh installs an executable to ~/.local/bin" $?
[ -z "$(ls -A "$IN/home/.local/bin" | grep -v '^claude-copilot$')" ]; check "install.sh leaves no temp file" $?
contains "$(cat "$IN/curl.args")" "github.com/seropian/claude-copilot/releases/latest/download/claude-copilot.sh"; check "install.sh downloads the latest release by default" $?
contains "$out" "isn't on your PATH"; check "install.sh warns when dir is not on PATH" $?
contains "$out" "run: claude-copilot"; check "install.sh prints how to run" $?

env -i HOME="$IN/home" PATH="$IN/bin:/usr/bin:/bin" CLAUDE_COPILOT_VERSION=9.0.0 INSTALL_DIR="$IN/custom" /bin/bash "$INSTALL" >/dev/null 2>&1
[ -x "$IN/custom/claude-copilot" ]; check "INSTALL_DIR is honored" $?
contains "$(cat "$IN/curl.args")" "github.com/seropian/claude-copilot/releases/latest/download/claude-copilot.sh"; check "installer always downloads the latest release" $?

out=$(env -i HOME="$IN/home" PATH="$IN/custom:$IN/bin:/usr/bin:/bin" INSTALL_DIR="$IN/custom" /bin/bash "$INSTALL" 2>&1)
contains "$out" "isn't on your PATH"; [ $? -ne 0 ]; check "install.sh stays quiet about PATH when dir is on it" $?

mkdir -p "$IN/nopy"; cp "$IN/bin/curl" "$IN/nopy/curl"; mkbin "$IN/nopy" claude
out=$(env -i HOME="$IN/home" PATH="$IN/nopy:/usr/bin:/bin" INSTALL_DIR="$IN/nopy-out" /bin/bash "$INSTALL" 2>&1)
if ! /usr/bin/env -i PATH=/usr/bin:/bin sh -c 'command -v python3' >/dev/null 2>&1; then
  contains "$out" "warning: 'python3' not found"; check "install.sh warns about missing prerequisites" $?
fi

rm -rf "$IN/failhome"
out=$(env -i HOME="$IN/failhome" PATH="$IN/bin:/usr/bin:/bin" FAKE_CURL_FAIL=1 /bin/bash "$INSTALL" 2>&1); rc=$?
check "install.sh fails when download fails" $((rc == 1 ? 0 : 1))
contains "$out" "install failed"; check "install.sh reports the failed download" $?
[ -z "$(ls -A "$IN/failhome/.local/bin")" ]; check "failed install leaves nothing behind" $?

# a download that is empty or not a script is rejected and the old install survives
cp "$IN/bin/curl" "$IN/bin/curl.good"
cat > "$IN/bin/curl" <<EOF
#!/bin/sh
while [ \$# -gt 0 ]; do [ "\$1" = -o ] && out="\$2"; shift; done
printf '%s' "\${FAKE_BODY:-}" > "\$out"
EOF
mkdir -p "$IN/bad/bin"; echo keep > "$IN/bad/bin/claude-copilot"
for body in "" "<html>proxy error</html>"; do
  out=$(env -i HOME="$IN/bad" PATH="$IN/bin:/usr/bin:/bin" INSTALL_DIR="$IN/bad/bin" FAKE_BODY="$body" /bin/bash "$INSTALL" 2>&1); rc=$?
  [ "$rc" -eq 1 ] && contains "$out" "didn't return a script"; check "install.sh rejects a non-script download ('${body:-empty}')" $? "rc=$rc $out"
done
[ "$(cat "$IN/bad/bin/claude-copilot")" = keep ]; check "bad download leaves the existing install alone" $?
[ "$(ls -A "$IN/bad/bin")" = claude-copilot ]; check "bad download leaves no temp file" $?
mv "$IN/bin/curl.good" "$IN/bin/curl"

# ~ in INSTALL_DIR is expanded, existing install is replaced
env -i HOME="$IN/tilde" PATH="$IN/bin:/usr/bin:/bin" INSTALL_DIR='~/bin' /bin/bash "$INSTALL" >/dev/null 2>&1
[ -x "$IN/tilde/bin/claude-copilot" ] && [ ! -e "$PWD/~" ]; check "install.sh expands ~ in INSTALL_DIR" $?
echo old > "$IN/home/.local/bin/claude-copilot"
env -i HOME="$IN/home" PATH="$IN/bin:/usr/bin:/bin" /bin/bash "$INSTALL" >/dev/null 2>&1
[ "$(head -c 2 "$IN/home/.local/bin/claude-copilot")" = "#!" ]; check "install.sh replaces an existing install" $?

# claude gets CLAUDE_CODE_PROCESS_WRAPPER pointing at the launcher
CW="$TMP/cw"; mkdir -p "$CW/bin" "$CW/tmp" "$CW/home"
ln -sf "$PY" "$CW/bin/python3"; ln -sf "$CURL" "$CW/bin/curl"
cat > "$CW/bin/claude" <<FAKE
#!/bin/sh
echo "\$ANTHROPIC_BASE_URL \$CLAUDE_CODE_PROCESS_WRAPPER \$CLAUDE_COPILOT_WRAP" >> "$CW/claude.log"
sleep 0.3
FAKE
chmod +x "$CW/bin/claude"
cat > "$CW/bin/child" <<FAKE
#!/bin/sh
echo "\$ANTHROPIC_BASE_URL \$ANTHROPIC_AUTH_TOKEN|\$*" > "$CW/child.out"
exit 5
FAKE
chmod +x "$CW/bin/child"
echo tok > "$CW/token"
cw_run() { env -i HOME="$CW/home" PATH="$CW/bin:/usr/bin:/bin" TMPDIR="$CW/tmp" COPILOT_TOKEN_FILE="$CW/token" "$@"; }

# Launchers in different projects still share the per-user shim.
for i in 1 2 3 4 5 6; do
  mkdir -p "$CW/project-$i"
  cw_run /bin/bash -c "cd '$CW/project-$i' && /bin/bash '$SCRIPT'" >/dev/null 2>&1 &
done
wait
[ "$(wc -l < "$CW/claude.log")" -eq 6 ]; check "all concurrent launchers ran claude" $? "$(cat "$CW/claude.log")"
[ "$(awk '{print $1}' "$CW/claude.log" | sort -u | wc -l)" -eq 1 ]; check "concurrent launchers share one shim" $? "$(cat "$CW/claude.log")"
[ ! -e "$CW/home/.local/share/claude-copilot/shim.lock" ]; check "shim lock released" $?

# stale lock from a dead pid is cleared
mkdir "$CW/home/.local/share/claude-copilot/shim.lock"; echo 999999 > "$CW/home/.local/share/claude-copilot/shim.lock/pid"
cw_run /bin/bash "$SCRIPT" >/dev/null 2>&1; check "stale shim lock is cleared" $?

# a stale break lock (breaker died) is cleared too
mkdir "$CW/home/.local/share/claude-copilot/shim.lock" "$CW/home/.local/share/claude-copilot/shim.lock.break"
echo 999999 > "$CW/home/.local/share/claude-copilot/shim.lock/pid"; echo 999999 > "$CW/home/.local/share/claude-copilot/shim.lock.break/pid"
cw_run /bin/bash "$SCRIPT" >/dev/null 2>&1; check "stale break lock is cleared" $?
[ ! -e "$CW/home/.local/share/claude-copilot/shim.lock.break" ]; check "break lock released" $?

# a live owner's lock is never broken
OWNERLESS="$TMP/ownerless"
mkdir -p "$OWNERLESS/shim.lock"
touch -t 200001010000 "$OWNERLESS/shim.lock"
/bin/bash -c ". '$SCRIPT'; _cc_lock_take '$OWNERLESS/shim.lock' && _cc_lock_drop"
check "old ownerless shim lock is recovered" $?
mkdir "$OWNERLESS/shim.lock" "$OWNERLESS/shim.lock.break"
touch -t 200001010000 "$OWNERLESS/shim.lock" "$OWNERLESS/shim.lock.break"
/bin/bash -c ". '$SCRIPT'; _cc_lock_take '$OWNERLESS/shim.lock' && _cc_lock_drop"
check "old ownerless breaker lock is recovered" $?
mkdir "$OWNERLESS/shim.lock"
/bin/bash -c ". '$SCRIPT'; _cc_lock_break '$OWNERLESS/shim.lock'"
[ -d "$OWNERLESS/shim.lock" ]; check "new ownerless lock is protected during PID publication" $?
rmdir "$OWNERLESS/shim.lock"

LK="$TMP/lk"; mkdir -p "$LK/shim.lock"; sleep 30 & lp=$!; echo $lp > "$LK/shim.lock/pid"
/bin/bash -c ". '$SCRIPT'; _cc_lock_break '$LK/shim.lock'"
[ -d "$LK/shim.lock" ]; check "live lock owner keeps its lock" $?
kill $lp 2>/dev/null; wait $lp 2>/dev/null

awk '{print $2, $3}' "$CW/claude.log" | sort -u | grep -qx "$SCRIPT 1"; check "claude gets CLAUDE_CODE_PROCESS_WRAPPER=<launcher>" $? "$(cat "$CW/claude.log")"

# wrapper mode: runs the given command (not claude) against the shared shim, keeps its exit code
: > "$CW/claude.log"
cw_run /bin/bash "$SCRIPT" "$CW/bin/child" a b >/dev/null 2>&1; rc=$?
check "wrapper mode propagates the command's exit code" $((rc == 5 ? 0 : 1)) "rc=$rc"
contains "$(cat "$CW/child.out")" "|a b"; check "wrapper mode forwards the command args unchanged" $?
contains "$(cat "$CW/child.out")" "http://127.0.0.1:"; check "wrapper mode points the command at the shim" $?
[ ! -s "$CW/claude.log" ]; check "wrapper mode does not start claude" $?

# Spare wrappers must clean up even when the shim has already disappeared.
cat > "$CW/bin/spare" <<FAKE
#!/bin/bash
if [ "\$1" = agents ]; then cat "$CW/registry"; exit 0; fi
if [ "\$1" = --bg-pty-host ]; then
  shift 5
  "\$@" &
  wait
  exit \$?
fi
echo \$\$ > "$CW/spare.pid"
trap 'exit 0' TERM
sleep 60 &
wait
FAKE
chmod +x "$CW/bin/spare"
echo '[{"state":"running"}]' > "$CW/registry"
cw_run env COPILOT_SHIM_IDLE_GRACE=1 /bin/bash "$SCRIPT" "$CW/bin/spare" --bg-spare >/dev/null 2>&1 & spare_launcher=$!
for i in $(seq 1 100); do [ -f "$CW/spare.pid" ] && break; sleep 0.1; done
read -r spare_pid < "$CW/spare.pid"
sleep 3
kill -0 "$spare_pid" 2>/dev/null; check "registered session keeps spare worker alive" $?
cw_run /bin/bash "$SCRIPT" --stop-shim >/dev/null 2>&1
echo '[]' > "$CW/registry"
for i in $(seq 1 100); do kill -0 "$spare_launcher" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$spare_launcher" 2>/dev/null; then
  check "idle spare wrapper exits without a running shim" 1
  kill "$spare_launcher" "$spare_pid" 2>/dev/null
else
  check "idle spare wrapper exits without a running shim" 0
fi
wait "$spare_launcher" 2>/dev/null
kill -0 "$spare_pid" 2>/dev/null
check "idle spare child is terminated" $(( $? == 0 ? 1 : 0 ))

rm -f "$CW/spare.pid"
cw_run env COPILOT_SHIM_IDLE_GRACE=1 /bin/bash "$SCRIPT" "$CW/bin/spare" --bg-pty-host socket 200 50 -- "$SCRIPT" "$CW/bin/spare" --bg-spare >"$CW/spare-host.log" 2>&1 & spare_host=$!
for i in $(seq 1 100); do [ -f "$CW/spare.pid" ] && break; sleep 0.1; done
read -r spare_pid < "$CW/spare.pid"
for i in $(seq 1 150); do kill -0 "$spare_host" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$spare_host" 2>/dev/null; then
  check "nested spare PTY host does not retain an ordinary client lease" 1 "$(cat "$CW/spare-host.log")"
  kill "$spare_host" "$spare_pid" 2>/dev/null
else
  check "nested spare PTY host does not retain an ordinary client lease" 0
fi
wait "$spare_host" 2>/dev/null
kill -0 "$spare_pid" 2>/dev/null
check "nested spare worker is terminated with its host" $(( $? == 0 ? 1 : 0 ))

cw_run /bin/bash "$SCRIPT" child >/dev/null 2>&1
[ -s "$CW/claude.log" ]; check "relative command name is not wrapper mode" $?

# the shim key must not show up in claude's argv (visible to other local users via ps)
cat > "$CW/bin/claude" <<FAKE
#!/bin/sh
printf '%s\\n' "\$@" > "$CW/claude.args"
FAKE
rm -f "$CW/bin/curl"
cat > "$CW/bin/curl" <<FAKE
#!/bin/sh
case "\$*" in *claude-settings*) cat >/dev/null; echo '{}' ;; *) exec "$CURL" "\$@" ;; esac
FAKE
chmod +x "$CW/bin/curl"
cw_run /bin/bash "$SCRIPT" -p hi >/dev/null 2>&1
sfile=$(sed -n '/^--settings$/{n;p;}' "$CW/claude.args")
[ -f "$sfile" ]; check "--settings is a file path" $? "$(cat "$CW/claude.args")"
key=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["env"]["ANTHROPIC_AUTH_TOKEN"])' "$sfile" 2>/dev/null)
[ -n "$key" ] && ! grep -q "$key" "$CW/claude.args"; check "shim key is not in claude's argv" $?
grep -q "$key" "$sfile"; check "settings file carries the shim key" $?
[ "$(ls -l "$sfile" | cut -c1-10)" = "-rw-------" ]; check "settings file is mode 600" $?

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
