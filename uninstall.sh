#!/usr/bin/env bash
# Uninstall claude-copilot. See README.md.
# Env: INSTALL_DIR (default ~/.local/bin), KEEP_LOGIN=1 to keep the saved token

set -eu

dir="${INSTALL_DIR:-$HOME/.local/bin}"
case "$dir" in "~"|"~/"*) dir="$HOME${dir#\~}" ;; esac
dest="$dir/claude-copilot"
data="$HOME/.local/share/claude-copilot"

if [ -e "$dest" ]; then rm -f "$dest" && echo "removed: $dest"; else echo "not found: $dest"; fi

if [ -f "$data/shim.state" ]; then
  read -r spid _ < "$data/shim.state" || true
  case "${spid:-}" in ''|*[!0-9]*) ;; *) kill "$spid" 2>/dev/null && echo "stopped shim: $spid" ;; esac
fi

if [ "${KEEP_LOGIN:-}" = 1 ]; then
  echo "kept: $data"
elif [ -d "$data" ]; then
  rm -rf "$data" && echo "removed: $data"
fi

rm -f "${TMPDIR:-/tmp}/claude-copilot.log"
