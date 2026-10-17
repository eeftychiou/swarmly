"""The configs and commands the user-facing docs tell people to run must work.

- The README's "minimal agent config" passes config validation (an ``admin_user_id: 0``
  there used to fail it), and ``swarmly-telegram check`` accepts it as a subprocess once the
  adapter is one this host has (``echo``).
- The two demo scripts the README points at run to completion (POSIX shells only).
- Every relative link and #anchor in tracked Markdown resolves
  (``scripts/check_docs_links.py``, which CI also runs on its own).
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests import envguard
from tests.gitenv import git_env

ROOT = Path(__file__).resolve().parent.parent

try:
    import yaml
except ImportError:  # pragma: no cover - the telegram extra brings PyYAML
    yaml = None


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)


def readme_minimal_config() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"A minimal agent config:\s*```yaml\n(.*?)```", text, re.S)
    if not match:
        raise AssertionError("README no longer has a 'A minimal agent config:' yaml block")
    return match.group(1)


@unittest.skipIf(yaml is None, "needs PyYAML")
class TestReadmeMinimalConfig(unittest.TestCase):
    def test_the_readme_config_passes_check(self):
        from swarmly.telegram.config import AgentConfig
        data = yaml.safe_load(readme_minimal_config())
        token_env = data["agent"]["bot_token_env"]
        with mock.patch.dict(os.environ, {token_env: "123456789:AA" + "x" * 33}):
            config = AgentConfig.from_dict(data, source_path=ROOT / "config.yaml")
            self.assertEqual(config.validate(require_token=True), [])

    def test_a_zero_admin_id_is_rejected(self):
        """DISCRIMINATING: the README's old value fails the same check."""
        from swarmly.telegram.config import AgentConfig
        data = yaml.safe_load(readme_minimal_config())
        data["agent"]["admin_user_id"] = 0
        token = {data["agent"]["bot_token_env"]: "123456789:AA" + "x" * 33}
        with mock.patch.dict(os.environ, token):
            config = AgentConfig.from_dict(data, source_path=ROOT / "config.yaml")
            problems = config.validate(require_token=True)
        self.assertTrue(any("admin_user_id" in p for p in problems))


@unittest.skipIf(yaml is None, "needs PyYAML")
class TestReadmeCommands(unittest.TestCase):
    def test_check_accepts_the_readme_config(self):
        """`swarmly-telegram --config config.yaml check`, as the README runs it."""
        data = yaml.safe_load(readme_minimal_config())
        with tempfile.TemporaryDirectory() as tmp:
            data["adapter"] = {"type": "echo", "cwd": tmp}    # hermes is not on this host
            config = Path(tmp, "config.yaml")
            config.write_text(yaml.safe_dump(data), encoding="utf-8")
            env = {**os.environ, data["agent"]["bot_token_env"]: "123456789:AA" + "x" * 33,
                   "PYTHONIOENCODING": "utf-8"}
            result = subprocess.run(
                [sys.executable, "-m", "swarmly.telegram.cli", "--config", str(config), "check",
                 "--offline"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])


@unittest.skipIf(yaml is None, "needs PyYAML")
class TestQuickstartSigningKey(unittest.TestCase):
    def test_the_quickstart_creates_the_key_post_needs(self):
        """`post` signs as the alias and refuses without its key: the README must set it."""
        alias = yaml.safe_load(readme_minimal_config())["agent"]["alias"]
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = text[text.index("swarmly-telegram init --alias"):]
        quickstart = quickstart[:quickstart.index("## Messages")]
        agent = alias.lower()
        self.assertIn(f"swarmly-keygen --agent {agent} --env-file", quickstart)
        self.assertIn("--registry", quickstart)
        self.assertLess(quickstart.index("swarmly-keygen"), quickstart.index(" post "))
        # The key lands in the env file every swarmly-telegram command reads by itself.
        self.assertIn("--env-file ~/.a2a-telegram.env", quickstart)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None or shutil.which("git") is None,
                 "the demos are bash scripts")
class TestDemoScripts(unittest.TestCase):
    def run_demo(self, name):
        with tempfile.TemporaryDirectory() as tmp:
            env = git_env(PYTHON=sys.executable)
            result = subprocess.run(["bash", str(ROOT / "examples" / name), tmp],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        return result.stdout

    def test_minimal_bus_demo(self):
        self.run_demo("minimal-bus-demo.sh")

    def test_folder_bus_demo(self):
        self.run_demo("folder-bus-demo.sh")


class TestMarkdownLinks(unittest.TestCase):
    def test_every_relative_link_and_anchor_resolves(self):
        spec = importlib.util.spec_from_file_location(
            "check_docs_links", ROOT / "scripts" / "check_docs_links.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.check(ROOT), [])

    def test_the_checker_flags_a_broken_anchor(self):
        """DISCRIMINATING: the checker is not vacuous."""
        spec = importlib.util.spec_from_file_location(
            "check_docs_links", ROOT / "scripts" / "check_docs_links.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            own = "https://github.com/eeftychiou/swarmly/blob/main/"
            Path(tmp, "a.md").write_text("# Real heading\n\n[ok](#real-heading) [bad](#nope)"
                                         " [gone](missing.md)"
                                         f" [own-ok]({own}a.md#real-heading)"
                                         f" [own-gone]({own}missing.md)\n", encoding="utf-8")
            problems = module.check(Path(tmp))
        self.assertEqual(len(problems), 3, problems)


if __name__ == "__main__":
    unittest.main()
