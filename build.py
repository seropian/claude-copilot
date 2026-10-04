#!/usr/bin/env python3
"""Build the self-contained claude-copilot.sh artifact."""
import argparse
import io
import os
import re
import stat
import tempfile
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "src" / "launcher.sh.in"
PACKAGE = ROOT / "src" / "claude_copilot_shim"
VERSION_FILE = ROOT / "VERSION"
OUTPUT = ROOT / "dist" / "claude-copilot.sh"
MARKER = "__CLAUDE_COPILOT_SHIM__"
VERSION_MARKER = "__CLAUDE_COPILOT_VERSION__"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
MODULE_ORDER = ("config.py", "helpers.py", "auth.py", "models.py", "transforms.py", "stream.py", "server.py", "cli.py")


def read_version():
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_RE.match(version):
        raise SystemExit("invalid VERSION: %s" % version)
    return version


def minify_source(source):
    """Remove comments and blank lines without changing Python token spacing."""
    lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        row, col = token.start
        end_row, end_col = token.end
        if row == end_row:
            lines[row - 1] = lines[row - 1][:col] + lines[row - 1][end_col:]
        else:
            lines[row - 1] = lines[row - 1][:col]
            for line in range(row, end_row - 1):
                lines[line] = ""
            lines[end_row - 1] = lines[end_row - 1][end_col:]
    return "\n".join(line.rstrip() for line in lines if line.strip()) + "\n"


def read_shim():
    if not PACKAGE.is_dir():
        raise SystemExit("missing shim package: %s" % PACKAGE)
    parts = []
    for name in MODULE_ORDER:
        path = PACKAGE / name
        if not path.is_file():
            raise SystemExit("missing shim module: %s" % name)
        source = path.read_text(encoding="utf-8")
        source = re.sub(r"^from \.\w* import .*\n", "", source, flags=re.MULTILINE)
        parts.append(minify_source(source).rstrip("\n"))
    source = "\n".join(parts) + "\n"
    source += "\nimport sys\nsys.exit(main(sys.argv[1:]))\n"
    try:
        compile(source, "<claude-copilot-shim>", "exec")
    except SyntaxError as e:
        raise SystemExit("invalid bundled shim: %s" % e)
    if "'" in source:
        raise SystemExit("shim source contains a single quote")
    return source


def build_bytes():
    version = read_version()
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(MARKER) != 1:
        raise SystemExit("launcher template must contain exactly one build marker")
    if template.count(VERSION_MARKER) != 1:
        raise SystemExit("launcher template must contain exactly one version marker")
    header = "# claude-copilot version %s (generated; edit src/, not this file)\n" % version
    if not template.startswith("#!/"):
        raise SystemExit("launcher template must start with a shebang")
    output = template.replace(MARKER, read_shim()).replace(VERSION_MARKER, version)
    shebang, rest = output.split("\n", 1)
    output = shebang + "\n" + header + rest
    return output.encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the artifact is out of date")
    args = parser.parse_args()
    data = build_bytes()
    version = read_version()
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
    print("built %s (v%s)" % (OUTPUT, version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
