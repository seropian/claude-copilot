#!/usr/bin/env python3
"""Build the self-contained claude-copilot.sh artifact."""
import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "src" / "launcher.sh.in"
MANIFEST = ROOT / "src" / "shim" / "MANIFEST"
VERSION_FILE = ROOT / "VERSION"
OUTPUT = ROOT / "dist" / "claude-copilot.sh"
MARKER = "__CLAUDE_COPILOT_SHIM__"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def read_version():
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_RE.match(version):
        raise SystemExit("invalid VERSION: %s" % version)
    return version


def read_shim():
    names = [line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not names:
        raise SystemExit("shim manifest is empty")
    parts = []
    for name in names:
        path = ROOT / name
        if path.suffix != ".py" or not path.is_file():
            raise SystemExit("missing shim source: %s" % name)
        text = path.read_text(encoding="utf-8")
        if "'" in text:
            raise SystemExit("shim source contains a single quote: %s" % name)
        parts.append(text.rstrip("\n"))
    return "\n".join(parts) + "\n"


def build_bytes():
    version = read_version()
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(MARKER) != 1:
        raise SystemExit("launcher template must contain exactly one build marker")
    # Keep the version visible without changing the launcher/shim runtime.
    header = "# claude-copilot version %s (generated; edit src/, not this file)\n" % version
    if not template.startswith("#!/"):
        raise SystemExit("launcher template must start with a shebang")
    output = template.replace("\n", "\n").replace(MARKER, read_shim())
    output = output.split("\n", 1)[0] + "\n" + header + output.split("\n", 1)[1]
    return output.encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the artifact is out of date")
    args = parser.parse_args()
    data = build_bytes()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_bytes() != data:
            print("claude-copilot.sh is out of date; run: python3 build.py")
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".claude-copilot.", dir=str(OUTPUT.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        os.replace(name, str(OUTPUT))
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print("built %s (v%s)" % (OUTPUT, read_version()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
