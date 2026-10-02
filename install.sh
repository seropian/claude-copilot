#!/usr/bin/env bash
# Install claude-copilot. See README.md.
# Usage: curl -fsSL https://raw.githubusercontent.com/seropian/claude-copilot/main/install.sh | bash
# Env: CLAUDE_COPILOT_REF (branch/tag/commit, default main), INSTALL_DIR (default ~/.local/bin)

# Everything lives in main() and is called on the last line, so a truncated download can't run half a script.
main() {
  set -eu

  local ref="${CLAUDE_COPILOT_REF:-main}"
  case "$ref" in
    ''|*[!A-Za-z0-9._/-]*|*..*) echo "install failed: CLAUDE_COPILOT_REF has odd characters: $ref" >&2; return 1 ;;
  esac
  local url="https://raw.githubusercontent.com/seropian/claude-copilot/$ref/claude-copilot.sh"
  local dir="${INSTALL_DIR:-$HOME/.local/bin}"
  case "$dir" in "~"|"~/"*) dir="$HOME${dir#\~}" ;; esac
  local dest="$dir/claude-copilot" c

  mkdir -p "$dir"
  tmp=$(mktemp "$dir/.claude-copilot.XXXXXX")
  trap 'rm -f "$tmp"' EXIT
  curl -fsSL "$url" -o "$tmp" || { echo "install failed: couldn't download $url" >&2; return 1; }
  if [ ! -s "$tmp" ] || [ "$(head -c 2 "$tmp")" != "#!" ]; then
    echo "install failed: $url didn't return a script" >&2
    return 1
  fi
  chmod +x "$tmp"
  mv "$tmp" "$dest"
  echo "installed: $dest"

  case ":$PATH:" in
    *":$dir:"*) ;;
    *) echo "note: $dir isn't on your PATH, add it to your shell profile (~/.zshrc or ~/.bashrc):"
       echo "  export PATH=\"$dir:\$PATH\"" ;;
  esac

  for c in python3 claude; do
    command -v "$c" >/dev/null 2>&1 || echo "warning: '$c' not found, claude-copilot needs it"
  done

  echo "run: claude-copilot"
}

main "$@"
