import ast
import hashlib
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([sys.executable, os.path.join(ROOT, "build.py")], cwd=ROOT, check=True, stdout=subprocess.PIPE)

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
        for node in ast.walk(ast.parse(shim)):
            self.assertFalse(isinstance(node, ast.ImportFrom) and node.level > 0)

    def bundled_shim(self):
        src = Path(ROOT, "dist", "claude-copilot.sh").read_text()
        return re.search(r"local shim='\n(.*?)\n'\n", src, re.S).group(1)

    def test_bundle_no_args_exits_2(self):
        r = subprocess.run([sys.executable, "-c", self.bundled_shim()], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(r.returncode, 2)

    def test_bundle_serves(self):
        import signal, tempfile, time, urllib.error, urllib.request
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "state")
            proc = subprocess.Popen([sys.executable, "-c", self.bundled_shim(), "serve", "0", pf], stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if os.path.exists(pf):
                        break
                    time.sleep(0.1)
                pid, port = open(pf).read().split()
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen("http://127.0.0.1:%s/nope" % port, timeout=10)
                self.assertEqual(cm.exception.code, 404)
            finally:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
                proc.stderr.close()

    def test_python_dash_m_runs_cli(self):
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
        r = subprocess.run([sys.executable, "-m", "claude_copilot_shim"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(r.returncode, 2)
