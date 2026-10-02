#!/usr/bin/env bash
# Uninstall claude-copilot. See README.md.
# Env: INSTALL_DIR (default ~/.local/bin), KEEP_LOGIN=1 to keep the saved token

set -eu

dest="${INSTALL_DIR:-$HOME/.local/bin}/claude-copilot"
data="$HOME/.local/share/claude-copilot"

if [ -e "$dest" ]; then rm -f "$dest" && echo "removed: $dest"; else echo "not found: $dest"; fi

if [ "${KEEP_LOGIN:-}" = 1 ]; then
  echo "kept: $data"
elif [ -d "$data" ]; then
  rm -rf "$data" && echo "removed: $data"
fi

rm -f "${TMPDIR:-/tmp}/claude-copilot.log"
echo "note: ~/.local/share/copilot-api (if any) belongs to copilot-api, left alone."
