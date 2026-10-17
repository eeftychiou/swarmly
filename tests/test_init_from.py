"""``init --from`` moves an existing agent to another project (and group): the bus, the
working folder and the group change; the bot, its peers and its settings stay."""

import os
import re
import subprocess
import unittest
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from unittest import mock

from tests.gitenv import TEST_IDENTITY, git_env
from swarmly.telegram.cli import main
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.init import InitError, retarget_config, write_from, write_starter

ENV = dict(os.environ, **TEST_IDENTITY)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=git_env()).stdout.strip()


def project(base: Path, name: str) -> Path:
    remote = base / f"{name}.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(remote))
    repo = base / name
    git(base, "clone", "-q", str(remote), str(repo))
    git(repo, "checkout", "-q", "-b", "main")
    (repo / "README.md").write_text(name + "\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "first")
    git(repo, "push", "-q", "origin", "main")
    return repo


class TestInitFrom(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {k: v for k, v in ENV.items()
                                               if k.startswith("GIT_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.old = project(self.base, "old")
        self.new = project(self.base, "new")
        self.source = self.base / "codex.yaml"
        write_starter(self.source, "Codex", "codex", bus_repo=self.old, role="qa-tester")
        text = self.source.read_text(encoding="utf-8")
        text = text.replace("example_codex_bot", "my_codex_bot")
        text = text.replace("timeout_seconds: 900", "timeout_seconds: 1800   # long QA runs")
        self.source.write_text(text, encoding="utf-8")

    def test_the_bus_and_folder_move_and_the_rest_stays(self):
        target = self.base / "codex-new.yaml"
        lines = write_from(target, self.source, self.new,
                           home_channel="https://t.me/c/1234567890/1/2")
        moved = AgentConfig.load(target)
        self.assertEqual(Path(moved.bus.repo), self.new / ".agents" / "bus")
        self.assertEqual(moved.bus.ref, "origin/agents-bus")
        self.assertEqual(Path(moved.adapter.cwd), self.new)
        self.assertEqual(moved.identity.home_channel_id, -1001234567890)
        self.assertEqual(moved.identity.name, "my_codex_bot")
        self.assertEqual(moved.adapter.type, "codex")
        self.assertEqual(moved.adapter.timeout_seconds, 1800)
        self.assertIn("# long QA runs", target.read_text(encoding="utf-8"))
        self.assertTrue((self.new / ".agents" / "bus" / "config" / "a2a_config.json").exists())
        self.assertTrue(any("swarmly-keygen --agent codex" in line for line in lines))
        self.assertEqual(git(self.new, "status", "--porcelain"), "")   # project tree clean

    def test_without_a_group_the_group_stays(self):
        text = self.source.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^(  home_channel_id:) *0", r"\1 -1009876543210", text)
        self.source.write_text(text, encoding="utf-8")
        target = self.base / "codex-new.yaml"
        write_from(target, self.source, self.new)
        self.assertEqual(AgentConfig.load(target).identity.home_channel_id, -1009876543210)

    def test_refuses_to_overwrite_the_source_or_an_existing_file(self):
        with self.assertRaises(InitError):
            write_from(self.source, self.source, self.new, force=True)
        target = self.base / "exists.yaml"
        target.write_text("x")
        with self.assertRaises(InitError):
            write_from(target, self.source, self.new)

    def test_a_bad_group_is_an_error(self):
        with self.assertRaises(InitError):
            write_from(self.base / "x.yaml", self.source, self.new, home_channel="my group")

    def test_cli_needs_a_bus_repo_with_from(self):
        code = main(["--config", str(self.base / "x.yaml"), "init", "--from",
                     str(self.source)])
        self.assertEqual(code, 2)
        code = main(["--config", str(self.base / "x.yaml"), "init", "--from",
                     str(self.source), "--bus-repo", str(self.new)])
        self.assertEqual(code, 0)


class TestRetarget(unittest.TestCase):

    def test_missing_keys_and_sections_are_added(self):
        text = "agent:\n  alias: A\n\nadapter:\n  type: echo\n"
        out = retarget_config(text, PurePosixPath("/p/.agents/bus"), "origin/agents-bus",
                              PurePosixPath("/p"), "-1001")
        self.assertIn("adapter:\n  cwd: /p\n  type: echo", out)
        self.assertIn("agent:\n  home_channel_id: -1001\n  alias: A", out)
        self.assertTrue(out.endswith("bus:\n  ref: origin/agents-bus\n  repo: /p/.agents/bus\n"))


if __name__ == "__main__":
    unittest.main()
