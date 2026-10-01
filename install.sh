#!/usr/bin/env bash
# Install claude-copilot. See README.md.
# Usage: curl -fsSL https://raw.githubusercontent.com/seropian/claude-copilot/main/install.sh | bash
# Env: CLAUDE_COPILOT_REF (branch/tag/commit, default main), INSTALL_DIR (default ~/.local/bin)

set -eu

url="https://raw.githubusercontent.com/seropian/claude-copilot/${CLAUDE_COPILOT_REF:-main}/claude-copilot.sh"
dir="${INSTALL_DIR:-$HOME/.local/bin}"
dest="$dir/claude-copilot"

mkdir -p "$dir"
curl -fsSL "$url" -o "$dest.tmp" || { rm -f "$dest.tmp"; echo "install failed: couldn't download $url" >&2; exit 1; }
chmod +x "$dest.tmp"
mv "$dest.tmp" "$dest"
echo "installed: $dest"

case ":$PATH:" in
  *":$dir:"*) ;;
  *) echo "note: $dir isn't on your PATH, add it to your shell profile:"
     echo "  export PATH=\"$dir:\$PATH\"" ;;
esac

for c in node npx python3 nc lsof claude; do
  command -v "$c" >/dev/null 2>&1 || echo "warning: '$c' not found, claude-copilot needs it"
done

echo "run: claude-copilot"
