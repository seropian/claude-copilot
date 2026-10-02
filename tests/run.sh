#!/usr/bin/env bash
# Run all tests: python unittest for the shim, bash for the launcher and installer.
# E2E=1 also runs the real e2e (real claude + real Copilot, needs a stored login).
cd "$(dirname "$0")/.." || exit 1
rc=0
python3 -m unittest discover -s tests -p 'test_*.py' -v || rc=1
bash tests/test_shell.sh || rc=1
[ "${E2E:-0}" = 1 ] && { bash tests/test_e2e.sh || rc=1; }
exit $rc
