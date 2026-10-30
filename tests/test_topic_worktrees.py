"""#486: every turn a bot runs at the same time gets its own working copy of the code.

Two CLI instances never work in the same cwd. Above ``limits.max_parallel_turns`` 1 every
forum topic works in a git worktree of its own (the project's, or one made from
``adapter.cwd`` for a topic with no project); General keeps the shared folder and runs alone
under the shared lock; a topic whose copy cannot be made also runs alone there, never beside
another turn.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.gitenv import git_env
from tests.test_projects import GROUP, FakeSender, event, make_config

from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.config import ConfigError

PARALLEL = {"max_parallel_turns": 2}


def git_repo(folder: str) -> str:
    """A checkout with one commit, so ``git worktree add ... HEAD --detach`` works in it."""
    subprocess.run(["git", "init", "-b", "main", folder], capture_output=True, check=True,
                   env=git_env())
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
        subprocess.run(["git", "-C", folder, "config", key, value], check=True, env=git_env())
    Path(folder, "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", folder, "add", "README.md"], check=True, env=git_env())
    subprocess.run(["git", "-C", folder, "commit", "-m", "init"], check=True, env=git_env())
    return folder


class TestOneWorkingCopyPerTopic(unittest.TestCase):
    def test_two_unbound_topics_get_two_copies(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            first = channel._workdir_for(GROUP, 12)
            second = channel._workdir_for(GROUP, 15)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir() and second.is_dir())
            self.assertEqual(first.parent, Path(repo).resolve() / ".swarmly-worktrees")
            self.assertEqual(first.name, "topic-12")
            # And each folder gets its own adapter, so no two topics share a CLI session.
            a12, a15 = channel._adapter_for(GROUP, 12), channel._adapter_for(GROUP, 15)
            self.assertIsNot(a12, a15)
            self.assertNotEqual(a12.settings.cwd, a15.settings.cwd)

    def test_a_project_topic_without_worktrees_still_gets_a_copy(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as elsewhere:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(
                    adapter={"type": "echo", "cwd": elsewhere},
                    limits=PARALLEL,
                    projects={"webapp": {"repo": "example-org/webapp", "cwd": repo,
                                         "topics": [12, 15], "worktrees": False}}),
                sender=FakeSender())
            first = channel._workdir_for(GROUP, 12)
            second = channel._workdir_for(GROUP, 15)
            self.assertEqual(first, Path(repo).resolve() / ".swarmly-worktrees" / "topic-12")
            self.assertEqual(second, Path(repo).resolve() / ".swarmly-worktrees" / "topic-15")
            self.assertEqual(len(channel._project_adapters), 0)   # nothing built yet

    def test_conversations_are_keyed_per_topic(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            suffixes = []
            channel._topic_adapter = lambda workdir, suffix: suffixes.append(suffix) or object()
            channel._adapter_for(GROUP, 12)
            channel._adapter_for(GROUP, 15)
            self.assertEqual(suffixes, ["topic-12", "topic-15"])

    def test_general_keeps_the_shared_folder_and_runs_alone(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            self.assertEqual(channel._workdir_for(GROUP, None), Path(repo).resolve())
            self.assertIs(channel._adapter_for(GROUP, None), channel.adapter)
            self.assertIs(channel._lock_for_key(GROUP, None), channel._turn_lock)
            self.assertIsNot(channel._lock_for_key(GROUP, 12), channel._turn_lock)

    def test_a_topic_without_a_copy_runs_alone_in_the_shared_folder(self):
        """A failed worktree never yields a turn beside another one in the shared cwd."""
        with TemporaryDirectory() as tmp:
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": tmp}, limits=PARALLEL),
                sender=FakeSender())
            self.assertEqual(channel._workdir_for(GROUP, 12), Path(tmp).resolve())
            first = channel._lock_for_key(GROUP, 12)
            self.assertIs(first, channel._turn_lock)
            self.assertIs(channel._lock_for_key(GROUP, 15), first)   # the same, so serialized

    def test_n1_is_unchanged(self):
        with TemporaryDirectory() as tmp:
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": tmp}),
                sender=FakeSender())
            self.assertIs(channel._lock_for_key(GROUP, 12), channel._turn_lock)
            self.assertEqual(channel._workdir_for(GROUP, 12), Path(tmp).resolve())
            self.assertIs(channel._adapter_for(GROUP, 12), channel.adapter)
            self.assertFalse((Path(tmp) / ".swarmly-worktrees").exists())

    def test_where_names_an_unbound_topics_copy_without_making_it(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            reply = channel._command_reply("where", event("/where@HermesBot", thread_id=99))
            target = Path(repo).resolve() / ".swarmly-worktrees" / "topic-99"
            self.assertIn(f"workdir={target}", reply)
            self.assertFalse(target.exists())


class TestConfigForParallelCopies(unittest.TestCase):
    def test_a_git_checkout_needs_no_shared_cwd_consent(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            config = make_config(adapter={"type": "claude_thread", "cwd": repo},
                                 limits={"max_parallel_turns": 2})
            self.assertEqual(config.limits.max_parallel_turns, 2)
            self.assertFalse(config.limits.allow_shared_cwd)

    def test_a_non_git_folder_is_refused_with_the_reason(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as caught:
                make_config(adapter={"type": "claude_thread", "cwd": tmp},
                            limits={"max_parallel_turns": 2})
            self.assertIn("allow_shared_cwd", str(caught.exception))
            self.assertIn("not a git checkout", str(caught.exception))
            # The explicit opt-in keeps its meaning: sharing is accepted, and it loads.
            shared = make_config(adapter={"type": "claude_thread", "cwd": tmp},
                                 limits={"max_parallel_turns": 2, "allow_shared_cwd": True})
            self.assertTrue(shared.limits.allow_shared_cwd)


if __name__ == "__main__":
    unittest.main()
