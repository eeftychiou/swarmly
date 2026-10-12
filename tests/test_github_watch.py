"""Tests for GitHub watcher and backstop polling (swarmly/github/watch.py)."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from swarmly.github.client import GitHubClient, GitHubError, Issue, IssueComment, Notification, PR
from swarmly.github.watch import GitHubWatcher, NotificationState, github_poll_once
from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.config import AgentConfig


class FakeAdapter:
    def __init__(self) -> None:
        self.received: list[IncomingMessage] = []

    async def respond(self, message: IncomingMessage):
        self.received.append(message)
        reply = MagicMock()
        reply.text = f"ACK {message.text[:20]}"
        return reply

    async def close(self) -> None:
        pass


def make_test_config(cwd: str, **github_kwargs) -> AgentConfig:
    data = {
        "agent": {
            "name": "AntigravityBot",
            "alias": "Antigravity",
            "bot_token": "123:TEST",
            "home_channel_id": -1001234567890,
            "admin_user_id": 99999,
        },
        "projects": {
            "webapp": {
                "repo": "example-org/webapp",
                "cwd": cwd,
                "topics": [12],
                "worktrees": False,
            }
        },
        "github": {
            "login": "test-bot",
            "poll_minutes": 5,
            **github_kwargs,
        },
    }
    return AgentConfig.from_dict(data)


class TestGitHubWatch(unittest.IsolatedAsyncioTestCase):
    async def test_watcher_disabled_when_poll_minutes_zero(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp, poll_minutes=0)
            mock_client = MagicMock(spec=GitHubClient)
            watcher = GitHubWatcher(cfg, client=mock_client)
            outcome = await watcher.poll_once()
            self.assertEqual(outcome.turns_queued, 0)
            mock_client.notifications.assert_not_called()

    async def test_watcher_produces_turns_for_mention_review_assignment(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp, poll_minutes=5)
            mock_client = MagicMock(spec=GitHubClient)
            notifs = [
                Notification(
                    id="n1",
                    reason="mention",
                    repo="example-org/webapp",
                    subject_title="Fix styling",
                    subject_type="Issue",
                    subject_url="https://api.github.com/repos/example-org/webapp/issues/10",
                    number=10,
                    updated_at="2026-10-10T12:00:00Z",
                ),
                Notification(
                    id="n2",
                    reason="review_requested",
                    repo="example-org/webapp",
                    subject_title="Add new api",
                    subject_type="PullRequest",
                    subject_url="https://api.github.com/repos/example-org/webapp/pulls/20",
                    number=20,
                    updated_at="2026-10-10T12:05:00Z",
                ),
                Notification(
                    id="n3",
                    reason="assign",
                    repo="example-org/webapp",
                    subject_title="Refactor auth",
                    subject_type="Issue",
                    subject_url="https://api.github.com/repos/example-org/webapp/issues/30",
                    number=30,
                    updated_at="2026-10-10T12:10:00Z",
                ),
            ]
            mock_client.notifications.return_value = notifs
            mock_client.issue_view.side_effect = lambda repo, num: Issue(
                number=num,
                title=f"Issue {num}",
                body="Body text",
                author="alice",
                state="OPEN",
                comments=[IssueComment(author="bob", body="Please review line 10", created_at="...")],
            )
            mock_client.pr_view.side_effect = lambda repo, num: PR(
                number=num,
                title=f"PR {num}",
                body="PR Body",
                author="carol",
                state="OPEN",
            )
            mock_client.mark_notification_read.return_value = True

            fake_channel = MagicMock()
            fake_adapter = FakeAdapter()
            fake_channel._adapter_for.return_value = fake_adapter
            fake_channel.adapter = fake_adapter
            fake_channel.send = AsyncMock()
            fake_channel.has_recent_ref.return_value = False

            state_file = Path(tmp) / "state.json"
            watcher = GitHubWatcher(cfg, client=mock_client, state_path=state_file, channel=fake_channel)
            outcome = await watcher.poll_once()

            self.assertEqual(outcome.turns_queued, 3)
            self.assertEqual(outcome.notifications_seen, 3)
            self.assertEqual(len(fake_adapter.received), 3)

            # Check prompt framings
            t1 = fake_adapter.received[0].text
            self.assertIn("GitHub notification: bob mentioned you on example-org/webapp#10", t1)
            self.assertEqual(fake_adapter.received[0].project, "webapp")
            self.assertEqual(fake_adapter.received[0].context_id, "tg-12")

            t2 = fake_adapter.received[1].text
            self.assertIn("GitHub review requested on example-org/webapp#20 by carol", t2)

            t3 = fake_adapter.received[2].text
            self.assertIn("GitHub assignment: example-org/webapp#30 by bob", t3)

            # Verify notifications were marked read
            self.assertEqual(mock_client.mark_notification_read.call_count, 3)

            # Verify channel.send was called with valid thread_id keyword arg
            self.assertEqual(fake_channel.send.call_count, 3)
            first_call_kwargs = fake_channel.send.call_args_list[0].kwargs
            self.assertEqual(first_call_kwargs.get("chat_id"), -1001234567890)
            self.assertEqual(first_call_kwargs.get("thread_id"), 12)
            self.assertNotIn("message_thread_id", first_call_kwargs)

            # Verify state was saved to disk
            state_reloaded = NotificationState.load(state_file)
            self.assertTrue(state_reloaded.is_processed("n1"))
            self.assertTrue(state_reloaded.is_processed("n2"))
            self.assertTrue(state_reloaded.is_processed("n3"))

    async def test_watcher_deduplicates_via_state_file(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp, poll_minutes=5)
            mock_client = MagicMock(spec=GitHubClient)
            mock_client.notifications.return_value = [
                Notification(
                    id="n1",
                    reason="mention",
                    repo="example-org/webapp",
                    subject_title="Fix styling",
                    subject_type="Issue",
                    subject_url="https://api.github.com/repos/example-org/webapp/issues/10",
                    number=10,
                    updated_at="2026-10-10T12:00:00Z",
                )
            ]
            state_file = Path(tmp) / "state.json"
            pre_state = NotificationState(processed_ids={"n1": "2026-10-10T12:00:00Z"})
            pre_state.save(state_file)

            fake_channel = MagicMock()
            fake_adapter = FakeAdapter()
            fake_channel._adapter_for.return_value = fake_adapter

            watcher = GitHubWatcher(cfg, client=mock_client, state_path=state_file, channel=fake_channel)
            outcome = await watcher.poll_once()

            self.assertEqual(outcome.turns_queued, 0)
            self.assertEqual(len(fake_adapter.received), 0)

    async def test_watcher_skips_notifications_handled_in_topic(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp, poll_minutes=5)
            mock_client = MagicMock(spec=GitHubClient)
            mock_client.notifications.return_value = [
                Notification(
                    id="n42",
                    reason="mention",
                    repo="example-org/webapp",
                    subject_title="Check issue 42",
                    subject_type="Issue",
                    subject_url="https://api.github.com/repos/example-org/webapp/issues/42",
                    number=42,
                    updated_at="2026-10-10T12:00:00Z",
                )
            ]
            state_file = Path(tmp) / "state.json"
            fake_channel = MagicMock()
            fake_adapter = FakeAdapter()
            fake_channel._adapter_for.return_value = fake_adapter
            # Simulate that channel recently saw issue 42 in the topic
            fake_channel.has_recent_ref.return_value = True

            watcher = GitHubWatcher(cfg, client=mock_client, state_path=state_file, channel=fake_channel)
            outcome = await watcher.poll_once()

            self.assertEqual(outcome.turns_queued, 0)
            self.assertEqual(outcome.turns_skipped, 1)
            fake_channel.has_recent_ref.assert_called_once_with(-1001234567890, 12, "example-org/webapp", 42)
            # Notification marked processed so it won't be retried
            reloaded = NotificationState.load(state_file)
            self.assertTrue(reloaded.is_processed("n42"))

    def test_channel_has_recent_ref_detection(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp)
            channel = AgentChannel(cfg)
            GROUP = -1001234567890
            TOPIC = 12

            # Initially no ref
            self.assertFalse(channel.has_recent_ref(GROUP, TOPIC, "example-org/webapp", 42))

            # Record turn with "#42"
            channel.record_topic_turn(GROUP, TOPIC, "Hey @Antigravity can you check PR #42 please?")
            self.assertTrue(channel.has_recent_ref(GROUP, TOPIC, "example-org/webapp", 42))
            self.assertFalse(channel.has_recent_ref(GROUP, TOPIC, "example-org/webapp", 99))
            self.assertFalse(channel.has_recent_ref(GROUP, 999, "example-org/webapp", 42))

            # Test #42 at start of string
            ch_start = AgentChannel(cfg)
            ch_start.record_topic_turn(GROUP, TOPIC, "#42 is merged")
            self.assertTrue(ch_start.has_recent_ref(GROUP, TOPIC, "example-org/webapp", 42))

            # Test space before #42 ("check #42")
            ch_space = AgentChannel(cfg)
            ch_space.record_topic_turn(GROUP, TOPIC, "please check #42")
            self.assertTrue(ch_space.has_recent_ref(GROUP, TOPIC, "example-org/webapp", 42))

            # Negative: random number without issue/PR/# prefix
            ch_neg = AgentChannel(cfg)
            ch_neg.record_topic_turn(GROUP, TOPIC, "changed on line 42 of file.py")
            self.assertFalse(ch_neg.has_recent_ref(GROUP, TOPIC, "example-org/webapp", 42))

    def test_channel_status_command_reports_github(self):
        with TemporaryDirectory() as tmp:
            # Config with github
            cfg_gh = make_test_config(tmp, login="antigravity-bot", poll_minutes=5)
            ch1 = AgentChannel(cfg_gh)
            ch1._github_last_poll = "2026-10-10T15:00:00Z"
            rep1 = ch1._command_reply("status")
            self.assertIn("github: @antigravity-bot (backstop: 5m, last: 2026-10-10T15:00:00Z)", rep1)

            # Config without github
            cfg_no_gh = AgentConfig.from_dict({
                "agent": {
                    "name": "Bot",
                    "alias": "Test",
                    "bot_token": "123:TEST",
                    "home_channel_id": -100123,
                    "admin_user_id": 999,
                }
            })
            ch2 = AgentChannel(cfg_no_gh)
            rep2 = ch2._command_reply("status")
            self.assertIn("github: disabled", rep2)

    async def test_watcher_standalone_mode_builds_adapter_and_runs(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp, poll_minutes=5)
            mock_client = MagicMock(spec=GitHubClient)
            mock_client.notifications.return_value = [
                Notification(
                    id="n99",
                    reason="comment",
                    repo="example-org/webapp",
                    subject_title="New discussion",
                    subject_type="Issue",
                    subject_url="https://api.github.com/repos/example-org/webapp/issues/99",
                    number=99,
                    updated_at="2026-10-10T12:00:00Z",
                )
            ]
            mock_client.issue_view.return_value = Issue(
                number=99,
                title="New discussion",
                body="Hello world",
                author="bob",
                state="OPEN",
            )
            state_file = Path(tmp) / "state.json"
            # channel is None -> exercises the standalone branch that calls build_adapter(cfg, transport=False)
            watcher = GitHubWatcher(cfg, client=mock_client, state_path=state_file, channel=None)
            outcome = await watcher.poll_once()

            self.assertEqual(outcome.turns_queued, 1)
            self.assertEqual(outcome.delivered_topics, [(cfg.identity.home_channel_id, 12)])
            reloaded = NotificationState.load(state_file)
            self.assertTrue(reloaded.is_processed("n99"))

    async def test_cli_gh_poll_once(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp)
            from swarmly.telegram.cli import _gh_poll

            # Without --once: returns exit code 2
            args_bad = argparse.Namespace(once=False, json=False, quiet=False)
            code_bad = await _gh_poll(cfg, args_bad)
            self.assertEqual(code_bad, 2)

            # With --once and mocked outcome
            with patch("swarmly.github.watch.github_poll_once") as mock_poll:
                from swarmly.github.watch import GitHubPollOutcome
                mock_poll.return_value = GitHubPollOutcome(
                    polled_repos=["example-org/webapp"],
                    notifications_seen=1,
                    turns_queued=1,
                    turns_skipped=0,
                )
                args_good = argparse.Namespace(once=True, json=True, quiet=False)
                code_good = await _gh_poll(cfg, args_good)
                self.assertEqual(code_good, 0)
                mock_poll.assert_called_once_with(cfg)



def _notif(nid="n1", updated="2026-10-10T12:00:00Z", number=10, reason="mention"):
    return Notification(id=nid, reason=reason, repo="example-org/webapp",
                        subject_title="t", subject_type="Issue",
                        subject_url=f"https://api.github.com/repos/example-org/webapp/issues/{number}",
                        number=number, updated_at=updated)


class TestBackstopFixes(unittest.IsolatedAsyncioTestCase):
    def watcher(self, tmp, client, channel=None):
        cfg = make_test_config(tmp)
        return GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json", channel=channel)

    def channel(self):
        ch = MagicMock()
        ch.adapter = ch._adapter_for.return_value = FakeAdapter()
        ch.send = AsyncMock()
        ch.has_recent_ref.return_value = False
        return ch

    async def test_a_failed_gh_call_is_an_error_not_an_empty_inbox(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.side_effect = GitHubError("HTTP 401: Bad credentials")
            outcome = await self.watcher(tmp, client).poll_once()
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("Bad credentials", outcome.errors[0])

    async def test_gh_poll_exits_nonzero_when_github_is_unreachable(self):
        from swarmly.github.watch import GitHubPollOutcome
        from swarmly.telegram import cli
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp)
            failed = GitHubPollOutcome(polled_repos=["example-org/webapp"], errors=["x: boom"])
            with patch("swarmly.github.watch.github_poll_once", AsyncMock(return_value=failed)):
                code = await cli._gh_poll(cfg, argparse.Namespace(once=True, json=False,
                                                                  quiet=True))
        self.assertEqual(code, 1)

    async def test_an_updated_thread_fires_again(self):
        """GitHub keeps one thread per issue; a new mention on it bumps updated_at."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.issue_view.return_value = None
            ch = self.channel()
            client.notifications.return_value = [_notif()]
            w = self.watcher(tmp, client, ch)
            self.assertEqual((await w.poll_once()).turns_queued, 1)
            self.assertEqual((await w.poll_once()).turns_queued, 0)        # unchanged
            client.notifications.return_value = [_notif(updated="2999-01-01T00:00:00Z")]
            self.assertEqual((await w.poll_once()).turns_queued, 1)        # new activity

    async def test_a_followed_thread_does_not_fire_again(self):
        """Two agents commenting on one issue must not wake each other every poll."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.issue_view.return_value = None
            client.notifications.return_value = [_notif(reason="comment")]
            w = self.watcher(tmp, client, self.channel())
            self.assertEqual((await w.poll_once()).turns_queued, 1)
            client.notifications.return_value = [_notif(reason="comment",
                                                        updated="2999-01-01T00:00:00Z")]
            self.assertEqual((await w.poll_once()).turns_queued, 0)

    def test_the_ledger_keeps_githubs_clock_not_the_hosts(self):
        """A host clock running ahead must not swallow the next event on the thread."""
        state = NotificationState()
        state.mark_processed("n1", "2026-10-10T12:00:00Z")
        self.assertEqual(state.processed_ids["n1"], "2026-10-10T12:00:00Z")
        self.assertFalse(state.is_processed("n1", "2026-10-10T12:00:01Z"))

    def test_old_ledger_entries_stay_processed(self):
        """Entries written before updated_at tracking hold the processing time."""
        state = NotificationState(processed_ids={"n1": "2026-10-10T12:30:00Z"})
        self.assertTrue(state.is_processed("n1", "2026-10-10T12:00:00Z"))
        self.assertFalse(state.is_processed("n1", "2026-10-10T13:00:00Z"))

    async def test_dedupe_checks_every_topic_of_the_project(self):
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp)
            cfg.projects["webapp"].topics.append((None, 15))
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = [_notif(number=14)]
            ch = self.channel()
            ch.has_recent_ref.side_effect = lambda c, t, repo, n: t == 15
            w = GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json", channel=ch)
            outcome = await w.poll_once()
        self.assertEqual((outcome.turns_queued, outcome.turns_skipped), (0, 1))

    async def test_backstop_turns_take_the_topic_lock(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = [_notif()]
            client.issue_view.return_value = None
            ch = self.channel()
            entered = []

            class Admit:
                def __init__(self, chat_id, thread_id):
                    entered.append((chat_id, thread_id))

                async def __aenter__(self):
                    return None

                async def __aexit__(self, *exc):
                    return False

            ch.admitted = Admit
            await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(entered, [(-1001234567890, 12)])


class TestAgentGitHubToken(unittest.TestCase):
    def test_the_agent_runtime_gets_its_own_token_as_gh_token(self):
        from swarmly.telegram.adapters import build_adapter
        with TemporaryDirectory() as tmp:
            data = {"agent": {"name": "b", "alias": "Antigravity", "bot_token": "1:x",
                              "home_channel_id": -1, "admin_user_id": 1},
                    "adapter": {"type": "echo", "cwd": tmp},
                    "github": {"login": "agy-bot", "token_env": "GH_TOKEN_AGY"}}
            adapter = build_adapter(AgentConfig.from_dict(data))
        env = adapter.environment({"GH_TOKEN_AGY": "ghp_agent", "GH_TOKEN": "ghp_owner",
                                   "PATH": "/bin"})
        self.assertEqual(env["GH_TOKEN"], "ghp_agent")
        self.assertNotIn("GH_TOKEN_AGY", env)
        for risky in ("A2A_KEY_ANTIGRAVITY", "A2A_TELEGRAM_BOT_TOKEN_X"):
            bad = AgentConfig.from_dict({**data, "github": {"login": "a", "token_env": risky}})
            self.assertTrue(any("not a GitHub token" in p for p in bad.validate()))
        without = build_adapter(AgentConfig.from_dict({**data, "github": None}))
        self.assertNotIn("GH_TOKEN", without.environment({"GH_TOKEN": "ghp_owner"}))


class TestBackstopAlert(unittest.IsolatedAsyncioTestCase):
    async def test_an_unreachable_github_is_announced_at_most_hourly(self):
        from swarmly.github.watch import GitHubPollOutcome
        with TemporaryDirectory() as tmp:
            channel = AgentChannel(make_test_config(tmp), sender=MagicMock())
        channel.send = AsyncMock()
        clock = [1000.0]
        channel._clock = lambda: clock[0]
        failed = GitHubPollOutcome(errors=["example-org/webapp: HTTP 401"])
        with patch("swarmly.github.watch.github_poll_once", AsyncMock(return_value=failed)):
            await channel.check_github()
            clock[0] += 600
            await channel.check_github()
            clock[0] += 3600
            await channel.check_github()
        self.assertEqual(channel.send.await_count, 2)
        self.assertIn("HTTP 401", channel.send.await_args.args[0])

if __name__ == "__main__":
    unittest.main()
