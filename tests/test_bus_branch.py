"""Bus mail goes to its own branch: ``init`` checks out ``agents-bus`` (at ``.agents/bus``
when the project checkout is on another branch), so agents never commit to main."""

import os
import subprocess
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.gitenv import TEST_IDENTITY, git_env
from swarmly.telegram.cli import _bus_on_main_branch
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.init import InitError, write_starter
from swarmly.transports.git import GitTransport

#: The identity the transport's own commits run with (it commits with the process env).
ENV = dict(os.environ, **TEST_IDENTITY)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=git_env()).stdout.strip()


class TestBusBranch(unittest.TestCase):

    def setUp(self):
        # The transport commits with the process environment: a CI runner has no git identity.
        patcher = mock.patch.dict(os.environ, {k: v for k, v in ENV.items()
                                               if k.startswith("GIT_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.remote = base / "remote.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.repo = base / "project"
        git(base, "clone", "-q", str(self.remote), str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "README.md").write_text("project\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "first")
        git(self.repo, "push", "-q", "origin", "main")
        self.worktree = self.repo / ".agents" / "bus"

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_checks_out_the_bus_branch_beside_the_project(self):
        config = self.repo.parent / "hermes.yaml"
        lines = write_starter(config, "Hermes", "echo", bus_repo=self.repo)
        loaded = AgentConfig.load(config)
        self.assertEqual(Path(loaded.bus.repo), self.worktree)
        self.assertEqual(loaded.bus.ref, "origin/agents-bus")
        self.assertEqual(Path(loaded.adapter.cwd), self.repo)      # the agent works on the code
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "agents-bus")
        self.assertTrue((self.worktree / "config" / "a2a_config.json").exists())
        # Local-only ignores: the project's tracked tree stays clean.
        exclude = (self.repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertIn("/.agents/bus/", exclude)
        self.assertFalse((self.repo / ".gitignore").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(any("push -u origin agents-bus" in line for line in lines))
        self.assertEqual(_bus_on_main_branch(loaded), "")

        # A second agent on the same project shares the bus checkout.
        other = self.repo.parent / "codex.yaml"
        write_starter(other, "Codex", "echo", bus_repo=self.repo)
        self.assertEqual(Path(AgentConfig.load(other).bus.repo), self.worktree)

    def test_mail_is_pushed_to_the_bus_branch_not_main(self):
        write_starter(self.repo.parent / "hermes.yaml", "Hermes", "echo", bus_repo=self.repo)
        main_before = git(self.remote, "rev-parse", "main")
        GitTransport(self.worktree).post("20261011T000000Z-hermes-codex-x.md", "hello\n")
        self.assertEqual(git(self.remote, "rev-parse", "main"), main_before)
        self.assertIn(".agents/inbox/20261011T000000Z-hermes-codex-x.md",
                      git(self.remote, "ls-tree", "-r", "--name-only", "agents-bus"))

    def test_a_config_in_the_project_stays_out_of_git(self):
        write_starter(self.repo / "config.yaml", "Hermes", "echo", bus_repo=self.repo)
        ignored = (self.repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertIn("/config.yaml", ignored)
        self.assertIn(".agents/state/", (self.worktree / ".gitignore").read_text().splitlines())

    def test_a_checkout_already_on_the_branch_is_used_as_is(self):
        git(self.repo, "checkout", "-q", "-b", "agents-bus")
        config = self.repo.parent / "hermes.yaml"
        write_starter(config, "Hermes", "echo", bus_repo=self.repo)
        self.assertEqual(Path(AgentConfig.load(config).bus.repo), self.repo)
        self.assertFalse(self.worktree.exists())

    def test_a_second_clone_attaches_to_the_pushed_bus_branch(self):
        write_starter(self.repo.parent / "hermes.yaml", "Hermes", "echo", bus_repo=self.repo)
        git(self.worktree, "push", "-q", "-u", "origin", "agents-bus")
        other = self.repo.parent / "other"
        git(self.repo.parent, "clone", "-q", "-b", "main", str(self.remote), str(other))
        git(other, "update-ref", "-d", "refs/remotes/origin/agents-bus")   # cloned before the push
        write_starter(self.repo.parent / "codex.yaml", "Codex", "echo", bus_repo=other)
        bus = other / ".agents" / "bus"
        self.assertEqual(git(bus, "rev-parse", "HEAD"), git(self.remote, "rev-parse", "agents-bus"))
        self.assertEqual(git(bus, "rev-parse", "--abbrev-ref", "@{u}"), "origin/agents-bus")

    def test_an_existing_remote_branch_is_tracked(self):
        git(self.repo, "push", "-q", "origin", "main:agents-bus")
        git(self.repo, "fetch", "-q", "origin")
        write_starter(self.repo.parent / "hermes.yaml", "Hermes", "echo", bus_repo=self.repo)
        self.assertEqual(git(self.worktree, "rev-parse", "--abbrev-ref", "@{u}"),
                         "origin/agents-bus")

    def test_opting_out_keeps_main_and_check_warns(self):
        config = self.repo.parent / "hermes.yaml"
        write_starter(config, "Hermes", "echo", bus_repo=self.repo, bus_branch=None)
        loaded = AgentConfig.load(config)
        self.assertEqual(Path(loaded.bus.repo), self.repo)
        self.assertEqual(loaded.bus.ref, "origin/main")
        self.assertEqual(_bus_on_main_branch(loaded), "main")

    def test_an_empty_repository_is_refused_with_advice(self):
        empty = self.repo.parent / "empty"
        git(self.repo.parent, "init", "-q", str(empty))
        with self.assertRaises(InitError) as caught:
            write_starter(self.repo.parent / "x.yaml", "Hermes", "echo", bus_repo=empty)
        self.assertIn("first commit", str(caught.exception))

    def test_a_folder_that_is_not_git_is_left_alone(self):
        plain = self.repo.parent / "plain"
        plain.mkdir()
        config = self.repo.parent / "x.yaml"
        write_starter(config, "Hermes", "echo", bus_repo=plain)
        self.assertEqual(Path(AgentConfig.load(config).bus.repo), plain)


if __name__ == "__main__":
    unittest.main()
