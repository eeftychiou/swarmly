"""adapter.git_sync: the transport keeps a sandboxed agent's checkout current."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import TEST_IDENTITY, git_env
from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.config import AdapterSettings
from swarmly.telegram.git_sync import sync_checkout
from tests.test_telegram_channel import make_channel, make_config, run

ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
           **TEST_IDENTITY)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True, env=git_env()).stdout.strip()


def commit(path, name):
    (Path(path) / name).write_text(name)
    git(path, "add", name)
    git(path, "commit", "-qm", name)
    return git(path, "rev-parse", "--short", "HEAD")


class Repos:
    def __init__(self, tmp):
        self.origin = Path(tmp) / "origin"
        self.author = Path(tmp) / "author"
        self.agent = Path(tmp) / "agent"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp, "clone", "-q", str(self.origin), str(self.author))
        git(self.author, "checkout", "-q", "-b", "main")
        commit(self.author, "a")
        git(self.author, "push", "-q", "origin", "main")
        git(tmp, "clone", "-q", str(self.origin), str(self.agent))


class GitEnv(unittest.TestCase):

    def setUp(self):
        old = os.environ.copy()

        def restore():
            os.environ.clear()
            os.environ.update(old)
        self.addCleanup(restore)
        os.environ.update({k: v for k, v in ENV.items() if k.startswith("GIT_")})


class TestSyncCheckout(GitEnv):

    def test_a_clean_checkout_is_fast_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(tmp)
            new = commit(repos.author, "b")
            git(repos.author, "push", "-q", "origin", "main")
            line = sync_checkout(str(repos.agent))
            self.assertEqual(git(repos.agent, "rev-parse", "--short", "HEAD"), new)
            self.assertIn(f"main at {new}", line)
            self.assertIn("fast-forwarded from", line)
            self.assertIn("fetches before every turn", line)

    def test_local_changes_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(tmp)
            old = git(repos.agent, "rev-parse", "--short", "HEAD")
            commit(repos.author, "b")
            git(repos.author, "push", "-q", "origin", "main")
            (repos.agent / "a").write_text("edited")
            line = sync_checkout(str(repos.agent))
            self.assertEqual(git(repos.agent, "rev-parse", "--short", "HEAD"), old)
            self.assertIn("local changes", line)
            self.assertIn("1 commit(s) behind origin/main", line)
            self.assertEqual((repos.agent / "a").read_text(), "edited")

    def test_a_branch_main_already_holds_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(tmp)
            git(repos.agent, "checkout", "-q", "-b", "feat/old")
            self.assertNotIn("already in", sync_checkout(str(repos.agent)))
            commit(repos.author, "b")
            git(repos.author, "push", "-q", "origin", "main")
            line = sync_checkout(str(repos.agent))
            self.assertIn("every change on this branch is already in origin/main", line)
            commit(repos.agent, "c")
            self.assertNotIn("already in", sync_checkout(str(repos.agent)))

    def test_a_squash_merged_branch_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(tmp)
            git(repos.agent, "checkout", "-q", "-b", "feat/x")
            commit(repos.agent, "x1")
            commit(repos.agent, "x2")
            for name in ("x1", "x2"):                  # main gets both as one commit
                (repos.author / name).write_text(name)
            git(repos.author, "add", "x1", "x2")
            git(repos.author, "commit", "-qm", "squash")
            git(repos.author, "push", "-q", "origin", "main")
            line = sync_checkout(str(repos.agent))
            self.assertIn("every change on this branch is already in origin/main", line)

    def test_not_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sync_checkout(tmp))
        self.assertIsNone(sync_checkout(""))


class TestWhoSyncs(GitEnv):

    def test_codex_syncs_by_default(self):
        self.assertTrue(AdapterSettings(type="codex").syncs_git)
        self.assertTrue(AdapterSettings(type="codex", permissions="strict").syncs_git)
        self.assertFalse(AdapterSettings(type="codex", command="codex {prompt}").syncs_git)
        self.assertTrue(AdapterSettings(type="claude").syncs_git)
        self.assertTrue(AdapterSettings(type="antigravity").syncs_git)
        self.assertFalse(AdapterSettings(type="echo").syncs_git)
        self.assertFalse(AdapterSettings(type="claude", git_sync=False).syncs_git)
        self.assertTrue(AdapterSettings(type="claude", git_sync=True).syncs_git)
        self.assertFalse(AdapterSettings(type="codex", git_sync=False).syncs_git)

    def test_the_line_reaches_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(tmp)
            config = make_config(adapter={"type": "codex", "cwd": str(repos.agent)})
            channel = make_channel(config)
            incoming = IncomingMessage(text="QA #8", author_alias="admin")
            got = run(channel._with_git_state(incoming, None))
            self.assertTrue(got.text.startswith("QA #8\n\nGit, checked by the transport just now"))
            config.adapter.git_sync = False
            self.assertIs(run(channel._with_git_state(incoming, None)), incoming)


if __name__ == "__main__":
    unittest.main()
