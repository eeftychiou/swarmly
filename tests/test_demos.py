"""The examples/ demos run end to end and show alice's message as verified (review R5)."""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.gitenv import git_env

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "needs bash")
class TestFolderDemo(unittest.TestCase):

    def test_alice_verifies(self):
        with TemporaryDirectory() as shared:
            env = {k: v for k, v in git_env().items() if not k.startswith("A2A_")}
            env["PYTHON"] = sys.executable
            result = subprocess.run(["bash", str(ROOT / "examples" / "folder-bus-demo.sh"),
                                     shared], capture_output=True, text=True, env=env,
                                    timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Valid signature", result.stdout)


class TestRegistryFlag(unittest.TestCase):

    def test_a_missing_registry_is_an_error(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_")}
        env.update(PYTHONPATH=str(ROOT), A2A_CONFIG=str(ROOT / "no-such-registry.json"))
        with TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "a2a_bus.py"), "--transport",
                 "folder", "--folder", folder, "inbox"],
                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
