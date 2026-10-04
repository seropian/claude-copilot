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
  env -i HOME="$FLOW" PATH="$FLOW/bin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" "$@" \
    /bin/bash "$SCRIPT" --extra-flag value 2>"$FLOW/stderr"
}

run_flow; rc=$?
check "launcher exits 0 when claude exits 0" $rc "$(cat "$FLOW/stderr")"
out=$(cat "$FLOW/claude.out" 2>/dev/null)
contains "$out" "BASE=http://127.0.0.1:"; check "launcher points ANTHROPIC_BASE_URL at the local shim" $?
case "$out" in *TOKEN=????????????????????????*) t=0 ;; *) t=1 ;; esac; check "launcher sets a random per-run auth token" $t
! contains "$out" "TOKEN=placeholder"; check "auth token is not the old placeholder" $?
contains "$out" "MODEL=claude-sonnet-5.5"; check "default model is claude-sonnet-5.5" $?
contains "$out" "SONNET=claude-sonnet-5.5"; check "default sonnet mapping" $?
contains "$out" "OPUS=claude-opus-5.5"; check "default opus mapping" $?
contains "$out" "HAIKU=claude-haiku-4.5"; check "default haiku mapping" $?
contains "$out" "NONESS=1"; check "nonessential traffic disabled" $?
contains "$out" "ARGS=--model claude-sonnet-5.5"; check "passes --model to claude" $?
contains "$out" "--extra-flag value"; check "forwards user args to claude" $?
[ "$(cat "$FLOW/shim.status")" = "404" ]; check "shim is serving while claude runs" $?
[ "$(cat "$FLOW/shim.nokey")" = "401" ] && [ "$(cat "$FLOW/shim.badkey")" = "401" ]; check "shim rejects requests without the right key" $?
[ -z "$(ls "$FLOW/tmp" | grep -v claude-copilot.log)" ]; check "port file removed after exit" $?
[ -f "$FLOW/tmp/claude-copilot.log" ]; check "shim log is written to TMPDIR" $?

port=$(sed -n 's/^BASE=.*://p' "$FLOW/claude.out")
sleep 0.3
"$CURL" -s -m 2 "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; check_rc=$?
check "shim is killed after claude exits" $((check_rc != 0 ? 0 : 1))

run_flow COPILOT_CLAUDE_MODEL=my-model COPILOT_SONNET_MODEL=s1 COPILOT_OPUS_MODEL=o1 COPILOT_HAIKU_MODEL=h1
out=$(cat "$FLOW/claude.out")
contains "$out" "MODEL=my-model" && contains "$out" "--model my-model"; check "COPILOT_CLAUDE_MODEL overrides model" $?
contains "$out" "SONNET=s1" && contains "$out" "OPUS=o1" && contains "$out" "HAIKU=h1"; check "COPILOT_*_MODEL overrides tier mappings" $?

run_flow FAKE_CLAUDE_RC=7; rc=$?
check "launcher propagates claude's exit code" $((rc == 7 ? 0 : 1)) "rc=$rc"

# fixed port
fp=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
run_flow COPILOT_SHIM_PORT=$fp
contains "$(cat "$FLOW/claude.out")" "BASE=http://127.0.0.1:$fp"; check "COPILOT_SHIM_PORT is honored" $?

# busy fixed port (a fresh port: the previous one may still be in TIME_WAIT and refuse a plain bind)
bp=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
"$PY" -c 'import socket,time,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.listen(1); time.sleep(20)' "$bp" & holder=$!
sleep 0.5
rm -f "$FLOW/claude.out"
run_flow COPILOT_SHIM_PORT=$bp; rc=$?
kill $holder 2>/dev/null; wait $holder 2>/dev/null
check "busy COPILOT_SHIM_PORT fails" $((rc == 1 ? 0 : 1))
contains "$(cat "$FLOW/stderr")" "busy"; check "busy port explains itself" $?
[ ! -e "$FLOW/claude.out" ]; check "claude is not started when port is busy" $?

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
rm -f "$FLOW/claude.out"
run_flow COPILOT_SHIM_PORT=abc; rc=$?
check "non-numeric COPILOT_SHIM_PORT fails" $((rc == 1 ? 0 : 1))
contains "$(cat "$FLOW/stderr")" "must be a port number"; check "non-numeric port explains itself" $?
[ ! -e "$FLOW/claude.out" ]; check "claude is not started on a bad port" $?

# symlinked log path is refused
mkdir -p "$FLOW/tmp2"; ln -sf "$FLOW/victim" "$FLOW/tmp2/claude-copilot.log"
env -i HOME="$FLOW" PATH="$FLOW/bin:/usr/bin:/bin" TMPDIR="$FLOW/tmp2" COPILOT_TOKEN_FILE="$FLOW/token" /bin/bash "$SCRIPT" 2>"$FLOW/stderr"; rc=$?
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
env -i HOME="$FLOW" PATH="$FLOW/pybin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" FAKE_SERVE=die \
  /bin/bash "$SCRIPT" 2>"$FLOW/stderr"; rc=$?
[ "$rc" -eq 1 ]; check "launcher fails when the shim dies at startup" $? "rc=$rc"
contains "$(cat "$FLOW/stderr")" "shim didn't start"; check "startup failure is reported" $?
[ ! -e "$FLOW/claude.out" ]; check "claude is not started when the shim dies" $?
[ -z "$(ls "$FLOW/tmp" | grep -v claude-copilot.log)" ]; check "startup failure leaves no temp files" $?

# signal during startup: shim is killed, files removed, right exit code
for sig in TERM INT HUP; do
  case $sig in TERM) want=143 ;; INT) want=130 ;; HUP) want=129 ;; esac
  env -i HOME="$FLOW" PATH="$FLOW/pybin:/usr/bin:/bin" TMPDIR="$FLOW/tmp" COPILOT_TOKEN_FILE="$FLOW/token" FAKE_SERVE=hang \
    perl -e '$SIG{INT} = "DEFAULT"; exec @ARGV' /bin/bash "$SCRIPT" 2>/dev/null & lp=$!
  spid=""
  for _ in $(seq 1 50); do spid=$(cat "$FLOW"/tmp/claude-copilot.*.pid 2>/dev/null); [ -n "$spid" ] && break; sleep 0.1; done
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

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
