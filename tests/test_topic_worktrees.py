"""#486: every turn a bot runs at the same time gets its own working copy of the code.

Two CLI instances never work in the same cwd. Above ``limits.max_parallel_turns`` 1 every
forum topic works in a git worktree of its own (the project's, or one made from
``adapter.cwd`` for a topic with no project); General keeps the shared folder and runs alone
under the shared lock; a topic whose copy cannot be made also runs alone there, never beside
another turn.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.gitenv import git_env
from tests.test_cli_passthrough import CountingAdapter, fake_proc
from tests.test_projects import ADMIN_ID, GROUP, FakeSender, event, make_config
from tests.test_telegram_channel import make_channel, run

from swarmly.telegram import channel as channel_mod
from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.config import ConfigError

PARALLEL = {"max_parallel_turns": 2}
#: A second group: its topic 12 is another discussion from the home chat's topic 12.
OTHER = -1009999999999


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


def resolved(channel, *places) -> None:
    """Resolve each ``(chat_id, thread_id)`` the way a turn does: awaited, off the loop.
    Only a resolved topic has a folder of its own; an unresolved one answers the shared
    folder and the shared lock (the safe pair)."""
    for chat_id, thread_id in places:
        run(channel.resolve_topic_folder(chat_id, thread_id))


class TestOneWorkingCopyPerTopic(unittest.TestCase):
    def test_two_unbound_topics_get_two_copies(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            resolved(channel, (GROUP, 12), (GROUP, 15))
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
            resolved(channel, (GROUP, 12), (GROUP, 15))
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
            resolved(channel, (GROUP, 12), (GROUP, 15))
            channel._adapter_for(GROUP, 12)
            channel._adapter_for(GROUP, 15)
            self.assertEqual(suffixes, ["topic-12", "topic-15"])

    def test_general_keeps_the_shared_folder_and_runs_alone(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            resolved(channel, (GROUP, 12))
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
            resolved(channel, (GROUP, 12), (GROUP, 15))
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


class TestTwoChatsAreTwoFolders(unittest.TestCase):
    """Review blocker 1: the same topic id in two chats is two discussions, not one folder."""

    def test_one_topic_id_in_two_chats_gets_two_copies(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            resolved(channel, (GROUP, 12), (OTHER, 12))
            home = channel._workdir_for(GROUP, 12)
            other = channel._workdir_for(OTHER, 12)
            self.assertNotEqual(home, other)
            self.assertEqual(home.name, "topic-12")
            self.assertEqual(other.name, f"topic-{OTHER}-12")
            self.assertTrue(home.is_dir() and other.is_dir())
            # Two folders, so two locks: neither chat's turn waits behind the other's.
            self.assertIsNot(channel._lock_for_key(GROUP, 12),
                             channel._lock_for_key(OTHER, 12))
            # And two adapters, so no session is shared across the two discussions.
            self.assertIsNot(channel._adapter_for(GROUP, 12),
                             channel._adapter_for(OTHER, 12))

    def test_two_chats_do_not_share_one_session_state_file(self):
        """Two chats' topic 12 are two discussions: two folders *and* two state files."""
        with TemporaryDirectory() as tmp, TemporaryDirectory() as cfg_dir:
            repo = git_repo(tmp)
            config = make_config(adapter={"type": "claude_thread", "cwd": repo},
                                 limits=PARALLEL)
            config_file = Path(cfg_dir) / "config.yaml"
            config_file.write_text("agent:\n  name: TestBot\n  alias: hermes\n",
                                   encoding="utf-8")
            config.source_path = config_file
            channel = AgentChannel(config, sender=FakeSender())
            resolved(channel, (GROUP, 12), (OTHER, 12))
            home, other = channel._adapter_for(GROUP, 12), channel._adapter_for(OTHER, 12)
            self.assertIsNotNone(home._state_path)
            self.assertNotEqual(home._state_path, other._state_path)
            self.assertTrue(str(home._state_path).endswith(
                f"claude-sessions-hermes-topic-12.json"), home._state_path)
            self.assertIn(f"topic-{OTHER}-12", str(other._state_path))
            # The child registry is per discussion for the same reason.
            self.assertNotEqual(home.children.path, other.children.path)

    def test_an_unresolved_topic_uses_the_shared_pair_without_blocking(self):
        """The loop never creates (nor waits on another topic's creation): a topic nobody
        resolved yet answers the shared folder, and so the shared lock."""
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            with mock.patch.object(channel_mod, "ensure_topic_worktree") as made:
                self.assertEqual(channel._workdir_for(GROUP, 12), Path(repo).resolve())
                self.assertIs(channel._lock_for_key(GROUP, 12), channel._turn_lock)
            self.assertEqual(made.call_count, 0)

    def test_a_project_bound_topic_in_another_chat_is_named_for_that_chat(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(
                    adapter={"type": "echo", "cwd": repo}, limits=PARALLEL,
                    projects={"webapp": {"repo": "example-org/webapp", "cwd": repo,
                                         "topics": [12, 15]}}),
                sender=FakeSender())
            resolved(channel, (GROUP, 12), (OTHER, 12))
            # project_for binds by (chat_id, thread_id): topic 12 in the *other* chat is not
            # this project's, so it copies adapter.cwd -- a different folder either way.
            self.assertNotEqual(channel._workdir_for(GROUP, 12),
                                channel._workdir_for(OTHER, 12))


class TestOneResolutionPerTopic(unittest.TestCase):
    """Review should-fix: the failure cache and the folder resolution are one answer."""

    def test_a_failure_is_not_retried_and_the_lock_follows_it(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as not_git:
            git_repo(tmp)
            channel = AgentChannel(
                make_config(
                    adapter={"type": "echo", "cwd": tmp}, limits=PARALLEL,
                    projects={"webapp": {"repo": "example-org/webapp", "cwd": not_git,
                                         "topics": [12], "worktrees": False}}),
                sender=FakeSender())
            with mock.patch.object(channel_mod, "ensure_topic_worktree",
                                   return_value=None) as made:
                resolved(channel, (GROUP, 12))
                self.assertEqual(channel._workdir_for(GROUP, 12), Path(not_git).resolve())
                self.assertIs(channel._lock_for_key(GROUP, 12), channel._turn_lock)
                self.assertEqual(channel._workdir_for(GROUP, 12), Path(not_git).resolve())
                self.assertIs(channel._lock_for_key(GROUP, 12), channel._turn_lock)
            self.assertEqual(made.call_count, 1)     # resolved once, never retried per turn

    def test_the_copy_is_made_off_the_event_loop(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            channel = AgentChannel(
                make_config(adapter={"type": "echo", "cwd": repo}, limits=PARALLEL),
                sender=FakeSender())
            threads = []
            real = channel_mod.ensure_topic_worktree

            def record(base, name, *, create=True):
                threads.append(threading.get_ident())
                return real(base, name, create=create)

            with mock.patch.object(channel_mod, "ensure_topic_worktree", record):
                run(channel.resolve_topic_folder(GROUP, 12))
                folder = channel._workdir_for(GROUP, 12)     # cached: no second creation
            self.assertEqual(len(threads), 1)
            self.assertNotEqual(threads[0], threading.get_ident())   # not the loop's thread
            self.assertTrue(folder.is_dir())


class TestThePassthroughUsesTheTopicsFolder(unittest.TestCase):
    """Review blocker 2: `/cli` ran in `adapter.cwd` while holding only the topic's lock."""

    def passthrough(self, cwd: str, **limits):
        made = make_channel(
            make_config(adapter={"type": "claude",
                                 "command": f"{sys.executable} {{prompt}}", "cwd": cwd},
                        limits=limits),
            adapter=CountingAdapter())
        made.bot = mock.Mock(id=555)                 # a bot with an id, and an editor
        made.bot.edit_message_text = mock.AsyncMock()

        async def send(payload, reply_to_message_id=None):
            made._test_sender.sent.append({"text": payload, "reply_to": reply_to_message_id})
            return mock.Mock(message_id=4000 + len(made._test_sender.sent))

        made._sender = send
        return made

    def test_cli_runs_in_the_topics_own_copy(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            made = self.passthrough(repo, **PARALLEL)
            run(made.resolve_topic_folder(GROUP, 12))    # the copy, before Popen is faked
            with mock.patch("swarmly.telegram.native.subprocess.Popen",
                            fake_proc(stdout="ok\n")) as spawned:
                run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                            username="alice", is_bot=False, thread_id=12)))
            copy = Path(repo).resolve() / ".swarmly-worktrees" / "topic-12"
            self.assertEqual(Path(spawned.call_args.kwargs["cwd"]), copy)

    def test_a_general_passthrough_keeps_the_shared_folder(self):
        with TemporaryDirectory() as tmp:
            repo = git_repo(tmp)
            made = self.passthrough(repo, **PARALLEL)
            with mock.patch("swarmly.telegram.native.subprocess.Popen",
                            fake_proc(stdout="ok\n")) as spawned:
                run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                            username="alice", is_bot=False)))
            self.assertEqual(Path(spawned.call_args.kwargs["cwd"]), Path(repo).resolve())

    def test_a_shared_folder_passthrough_refuses_while_general_runs(self):
        """Above N=1 a topic with no copy takes the shared lock, so the passthrough waits."""
        with TemporaryDirectory() as not_git:
            made = self.passthrough(not_git, max_parallel_turns=2, allow_shared_cwd=True)

            async def scenario():
                await made._turn_lock.acquire()
                try:
                    return await made.handle_event(event(
                        "/cli@HermesBot --version", author_id=ADMIN_ID, username="alice",
                        is_bot=False, thread_id=12))
                finally:
                    made._turn_lock.release()

            run(scenario())
            self.assertIn("a turn is running", made._test_sender.last)


if __name__ == "__main__":
    unittest.main()
