import hashlib
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BuildTests(unittest.TestCase):
    def run_build(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "build.py")] + list(args), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_artifact_is_current(self):
        result = self.run_build("--check")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_build_is_deterministic(self):
        artifact = Path(ROOT) / "dist" / "claude-copilot.sh"
        before = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.assertEqual(self.run_build().returncode, 0)
        after = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_shell_syntax(self):
        result = subprocess.run(["bash", "-n", os.path.join(ROOT, "dist", "claude-copilot.sh")], cwd=ROOT)
        self.assertEqual(result.returncode, 0)

    def test_bundle_is_readable_source(self):
        src = Path(ROOT, "dist", "claude-copilot.sh").read_text()
        shim = re.search(r"local shim='\n(.*?)\n'\n", src, re.S).group(1)
        compile(shim, "embedded-shim", "exec")
        self.assertNotIn("b64decode", shim)
        self.assertNotIn("zipfile", shim)
        self.assertNotIn("NamedTemporaryFile", shim)
        self.assertNotIn("from .", shim)
        self.assertLess(len(shim), sum(p.stat().st_size for p in Path(ROOT, "src", "claude_copilot_shim").glob("*.py")))
