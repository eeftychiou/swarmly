"""Tests for GitHub watcher and backstop polling (swarmly/github/watch.py)."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from swarmly.github.client import GitHubClient, GitHubError, Issue, IssueComment, Notification, PR
from swarmly.github.watch import (MAX_TURN_ATTEMPTS, GitHubWatcher, NotificationState,
                                  github_poll_once, item_blocker, tracking_item_refs)
from swarmly.telegram.adapters.base import AdapterReply, IncomingMessage
from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.config import AgentConfig


def _stamp(seconds_ago: float = 0.0) -> str:
    """A notification stamp this recent.

    Issue #87 gives a notification older than ``github.backlog_hours`` no turn, so a fixture
    that wants one has to be recent: a literal date is a backlog item the day after it is
    written.
    """
    import datetime
    import time
    moment = datetime.datetime.fromtimestamp(
        time.time() - seconds_ago, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def make_test_config(cwd: str, role: str | None = None, **github_kwargs) -> AgentConfig:
    data = {
        "agent": {
            "name": "AntigravityBot",
            "alias": "Antigravity",
            "bot_token": "123:TEST",
            "home_channel_id": -1001234567890,
            "admin_user_id": 99999,
            **({"role": role} if role else {}),
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
                    updated_at=_stamp(0),
                ),
                Notification(
                    id="n2",
                    reason="review_requested",
                    repo="example-org/webapp",
                    subject_title="Add new api",
                    subject_type="PullRequest",
                    subject_url="https://api.github.com/repos/example-org/webapp/pulls/20",
                    number=20,
                    updated_at=_stamp(60),
                ),
                Notification(
                    id="n3",
                    reason="assign",
                    repo="example-org/webapp",
                    subject_title="Refactor auth",
                    subject_type="Issue",
                    subject_url="https://api.github.com/repos/example-org/webapp/issues/30",
                    number=30,
                    updated_at=_stamp(120),
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
                    updated_at=_stamp(0),
                )
            ]
            state_file = Path(tmp) / "state.json"
            pre_state = NotificationState(processed_ids={"n1": _stamp(0)})
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
                    updated_at=_stamp(0),
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

    async def test_a_notification_goes_to_the_topic_already_about_its_item(self):
        """Round 5.5 (23 Oct): an answer to #230 landed in the project's default place while
        the PR's work ran in its round topic. Issue #87 keeps the rule for what a person
        says -- a CI event there is a notice, not a turn (see the quiet tests below)."""
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp, poll_minutes=5)
            mock_client = MagicMock(spec=GitHubClient)
            mock_client.notifications.return_value = [
                Notification(id="n230", reason="mention", repo="example-org/webapp",
                             subject_title="CI failed", subject_type="Issue",
                             subject_url="https://api.github.com/repos/example-org/webapp/issues/230",
                             number=230, updated_at=_stamp(180))]
            mock_client.issue_view.return_value = Issue(number=230, title="CI failed", body="x",
                                                        author="bob", state="OPEN")
            fake_channel = MagicMock()
            fake_channel._adapter_for.return_value = FakeAdapter()
            fake_channel.has_recent_ref.return_value = False
            fake_channel.topic_for_text.return_value = (-1001234567890, 9400)
            fake_channel.send = AsyncMock()
            watcher = GitHubWatcher(cfg, client=mock_client, state_path=Path(tmp) / "s.json",
                                    channel=fake_channel)
            outcome = await watcher.poll_once()
            fake_channel.topic_for_text.assert_called_with("#230")
            self.assertEqual(outcome.delivered_topics, [(-1001234567890, 9400)])

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
                    updated_at=_stamp(0),
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



def _notif(nid="n1", updated=_stamp(0), number=10, reason="mention"):
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

    # -- issue #87: not every notification is a reply ---------------------------------
    async def test_a_backlog_of_old_notifications_produces_no_chat_messages(self):
        """First enable: three-day-old runs were answered in the group, one reply each."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = [
                _notif(nid="n1", updated=_stamp(3 * 86400)),
                _notif(nid="n2", updated=_stamp(2 * 86400), number=11),
            ]
            ch = self.channel()
            w = self.watcher(tmp, client, ch)
            outcome = await w.poll_once()
        self.assertEqual(outcome.turns_queued, 0)
        self.assertEqual(outcome.backlog_skipped, 2)
        self.assertEqual(ch._adapter_for.return_value.received, [])
        ch.send.assert_not_called()                       # not even a notice: nothing happened
        self.assertTrue(w.state.is_processed("n1"))
        client.mark_notification_read.assert_any_call("n1")

    async def test_a_recent_notification_still_gets_its_turn(self):
        """The window (#87) must not eat what just happened."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.issue_view.return_value = None
            client.notifications.return_value = [_notif(nid="n1", updated=_stamp(60))]
            w = self.watcher(tmp, client, self.channel())
            self.assertEqual((await w.poll_once()).turns_queued, 1)

    async def test_a_ci_notification_posts_one_line_and_no_turn(self):
        """A CI completion on a watched PR: the coordinator is woken by #24, not by a reply."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.issue_view.return_value = Issue(number=230, title="CI failed", body="x",
                                                   author="bob", state="OPEN")
            client.notifications.return_value = [
                Notification(id="n230", reason="ci_activity", repo="example-org/webapp",
                             subject_title="CI failed", subject_type="Issue",
                             subject_url="https://api.github.com/repos/example-org/webapp/issues/230",
                             number=230, updated_at=_stamp(60))]
            ch = self.channel()
            ch.topic_for_text.return_value = (-1001234567890, 9400)
            outcome = await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(outcome.turns_queued, 0)
        self.assertEqual(outcome.quiet_skipped, 1)
        self.assertEqual(ch._adapter_for.return_value.received, [])
        said = ch.send.await_args
        self.assertEqual((said.kwargs["chat_id"], said.kwargs["thread_id"]),
                         (-1001234567890, 9400))
        self.assertTrue(said.kwargs["notice"])
        self.assertIn("#230", said.args[0])

    async def test_a_comment_from_one_of_the_swarm_is_not_a_turn(self):
        """Two bots commenting on one issue must not answer each other in public."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.issue_view.return_value = Issue(
                number=10, title="t", body="x", author="DeepSeek", state="OPEN",
                comments=[IssueComment(author="Claudebot", body="merging it now", created_at="")])
            client.notifications.return_value = [_notif(nid="n1", updated=_stamp(60),
                                                         reason="comment")]
            ch = self.channel()
            # Claudebot is a configured peer, by alias and by handle: either name counts.
            cfg = make_test_config(tmp)
            cfg.peers["Claudebot"] = "@Claude_obot"
            watcher = GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json",
                                    channel=ch)
            outcome = await watcher.poll_once()
        self.assertEqual(outcome.turns_queued, 0)
        self.assertEqual(outcome.quiet_skipped, 1)
        self.assertIn("one of the swarm's bots", ch.send.await_args.args[0])

    async def test_a_comment_from_a_person_still_gets_a_turn(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.issue_view.return_value = Issue(
                number=10, title="t", body="x", author="bob", state="OPEN",
                comments=[IssueComment(author="alice", body="please fix the lint", created_at="")])
            client.notifications.return_value = [_notif(nid="n1", updated=_stamp(60),
                                                         reason="comment")]
            ch = self.channel()
            outcome = await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(outcome.turns_queued, 1)
        self.assertEqual(outcome.quiet_skipped, 0)

    async def test_a_persons_comment_on_a_swarm_opened_pr_is_still_a_turn(self):
        """#252 review: the actor is the last commenter, not the PR's author.

        Reproduced there as `turns 0, notice "eeftychiou is one of the swarm's bots"`: the PR
        was opened by the agent's own login, so reading the *author* quieted the very reply it
        owed. The author only decides when nobody has spoken since.
        """
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.pr_view.return_value = PR(
                number=252, title="a swarm PR", body="x", author="test-bot", state="OPEN",
                comments=[IssueComment(author="eeftychiou",
                                       body="one blocker: fix the actor", created_at="")])
            client.notifications.return_value = [
                Notification(id="n252", reason="comment", repo="example-org/webapp",
                             subject_title="a swarm PR", subject_type="PullRequest",
                             subject_url="https://api.github.com/repos/example-org/webapp/pulls/252",
                             number=252, updated_at=_stamp(60))]
            ch = self.channel()
            outcome = await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(outcome.turns_queued, 1, outcome.errors)
        self.assertEqual(outcome.quiet_skipped, 0)
        said = ch._adapter_for.return_value.received[0].text
        self.assertIn("eeftychiou", said)

    async def test_a_swarm_comment_on_its_own_pr_is_still_quiet(self):
        """... and a bot answering on that same PR stays quiet."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.pr_view.return_value = PR(
                number=252, title="a swarm PR", body="x", author="test-bot", state="OPEN",
                comments=[IssueComment(author="DeepSeek", body="pushed a fix", created_at="")])
            client.notifications.return_value = [
                Notification(id="n252", reason="comment", repo="example-org/webapp",
                             subject_title="a swarm PR", subject_type="PullRequest",
                             subject_url="https://api.github.com/repos/example-org/webapp/pulls/252",
                             number=252, updated_at=_stamp(60))]
            ch = self.channel()
            cfg = make_test_config(tmp)
            cfg.peers["DeepSeek"] = "@DeepSeek_dd_bot"
            w = GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json", channel=ch)
            outcome = await w.poll_once()
        self.assertEqual(outcome.turns_queued, 0)
        self.assertEqual(outcome.quiet_skipped, 1)

    async def test_a_pr_with_no_comments_still_names_its_author(self):
        """The old reading, kept for the case it was right about."""
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.pr_view.return_value = PR(number=20, title="Add new api", body="x",
                                             author="alice", state="OPEN")
            client.notifications.return_value = [
                Notification(id="n20", reason="review_requested", repo="example-org/webapp",
                             subject_title="Add new api", subject_type="PullRequest",
                             subject_url="https://api.github.com/repos/example-org/webapp/pulls/20",
                             number=20, updated_at=_stamp(60))]
            ch = self.channel()
            outcome = await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(outcome.turns_queued, 1, outcome.errors)
        self.assertIn("alice", ch._adapter_for.return_value.received[0].text)

    def test_backlog_hours_zero_answers_everything(self):
        """An operator who wants the whole backlog answered can say so."""
        with TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp)
            cfg.github.backlog_hours = 0
            w = GitHubWatcher(cfg, client=MagicMock(spec=GitHubClient),
                              state_path=Path(tmp) / "s.json")
        self.assertFalse(w._in_backlog(_notif(updated=_stamp(9 * 86400))))
        self.assertTrue(w._in_backlog(_notif(updated=_stamp(9 * 86400))) is False)

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

    # -- 2.12 / issue #12: a failed turn must not consume the notification ----------------

    def failing_channel(self, error="the runtime exited 1"):
        """A channel whose adapter fails the turn instead of answering it."""
        class FailingAdapter:
            def __init__(self):
                self.calls = 0

            async def respond(self, message):
                self.calls += 1
                return AdapterReply(text="", ok=False, error=error)

            async def close(self):
                pass

        ch = MagicMock()
        ch.adapter = ch._adapter_for.return_value = FailingAdapter()
        ch.send = AsyncMock()
        ch.has_recent_ref.return_value = False
        return ch

    async def test_a_successful_turn_is_still_marked_read(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = [_notif()]
            client.issue_view.return_value = None
            outcome = await self.watcher(tmp, client, self.channel()).poll_once()
            reloaded = NotificationState.load(Path(tmp) / "s.json")
        self.assertEqual((outcome.turns_queued, outcome.errors), (1, []))
        client.mark_notification_read.assert_called_once_with("n1")
        self.assertTrue(reloaded.is_processed("n1"))

    async def test_a_failed_turn_leaves_the_notification_unread(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = [_notif()]
            client.issue_view.return_value = None
            outcome = await self.watcher(tmp, client, self.failing_channel()).poll_once()
            reloaded = NotificationState.load(Path(tmp) / "s.json")
        client.mark_notification_read.assert_not_called()
        self.assertFalse(reloaded.is_processed("n1"))
        self.assertEqual(outcome.turns_queued, 0)
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("left unread", outcome.errors[0])
        self.assertEqual(reloaded.attempts("n1"), 1)

    async def test_a_failed_turn_backs_off_instead_of_hot_looping(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = [_notif()]
            client.issue_view.return_value = None
            ch = self.failing_channel()
            # poll_minutes=5 -> the first retry waits one poll interval, so the next poll must
            # not run the turn again.
            first = await self.watcher(tmp, client, ch).poll_once()
            second = await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(ch.adapter.calls, 1)
        self.assertEqual((first.turns_skipped, second.turns_skipped), (0, 1))
        client.mark_notification_read.assert_not_called()

    async def test_a_failed_turn_is_given_up_loudly_after_the_bound(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = [_notif()]
            client.issue_view.return_value = None
            state_path = Path(tmp) / "s.json"
            state = NotificationState()
            state.failures["n1"] = {"attempts": MAX_TURN_ATTEMPTS - 1,
                                    "retry_after": "2000-01-01T00:00:00Z"}
            state.save(state_path)
            outcome = await self.watcher(tmp, client, self.failing_channel()).poll_once()
            reloaded = NotificationState.load(state_path)
        client.mark_notification_read.assert_called_once_with("n1")
        self.assertTrue(reloaded.is_processed("n1"))
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("gave up", outcome.errors[0])


def _tracking(number=34, state="OPEN", title="Round 2 tracking: whole messages", body=""):
    return Issue(number=number, title=title, body=body, author="claude", state=state,
                 labels=["tracking"])


class TestRoundEndReport(unittest.IsolatedAsyncioTestCase):
    """Issue #25: closing the tracking issue wakes the coordinator to post the round report.

    Closing sends no GitHub notification, so the poller remembers which labelled issues were
    open and reports the number that is now closed -- once per closure, in the topic bound to
    the project, and only for the coordinator."""

    def channel(self):
        ch = MagicMock()
        ch.adapter = ch._adapter_for.return_value = FakeAdapter()
        ch.send = AsyncMock()
        ch.has_recent_ref.return_value = False
        return ch

    def watcher(self, tmp, client, channel, role="coordinator"):
        cfg = make_test_config(tmp, role=role)
        # #83: the coordinator gate is the bus lease, asked through the channel. A mock channel
        # has to answer it explicitly; a developer's lease says no.
        channel.is_coordinator.return_value = role == "coordinator"
        return GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json",
                             channel=channel)

    def client(self, *states):
        client = MagicMock(spec=GitHubClient)
        client.notifications.return_value = []
        client.issues_with_label.side_effect = [[_tracking(state=s)] for s in states]
        return client

    async def test_closing_the_tracking_issue_asks_for_the_round_report_now(self):
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED")
            ch = self.channel()
            first = await self.watcher(tmp, client, ch).poll_once()
            second = await self.watcher(tmp, client, ch).poll_once()
        adapter = ch._adapter_for.return_value
        self.assertEqual((first.turns_queued, second.turns_queued), (0, 1))
        self.assertEqual(len(adapter.received), 1)
        turn = adapter.received[0]
        self.assertIn("post the round report now", turn.text)
        self.assertIn("example-org/webapp#34", turn.text)
        self.assertIn("Round 2 tracking", turn.text)
        self.assertEqual(turn.project, "webapp")
        self.assertEqual(turn.context_id, "tg-12")
        ch.send.assert_called_once()
        kwargs = ch.send.call_args.kwargs
        self.assertEqual((kwargs.get("chat_id"), kwargs.get("thread_id")), (-1001234567890, 12))
        # The label is what marks a tracking issue, and the poll asks for it.
        client.issues_with_label.assert_called_with("example-org/webapp", "tracking")

    async def test_an_issue_that_stays_closed_reports_once(self):
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", "CLOSED", "CLOSED")
            ch = self.channel()
            for _ in range(4):
                await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(len(ch._adapter_for.return_value.received), 1)

    async def test_an_issue_closed_before_the_first_poll_is_not_reported(self):
        """A repo's old labelled issues must not each wake the coordinator on the first poll."""
        with TemporaryDirectory() as tmp:
            client = self.client("CLOSED")
            ch = self.channel()
            outcome = await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual((outcome.turns_queued, len(ch._adapter_for.return_value.received)),
                         (0, 0))

    async def test_a_developer_is_not_woken_by_a_closed_tracking_issue(self):
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED")
            ch = self.channel()
            await self.watcher(tmp, client, ch, role="developer").poll_once()
            outcome = await self.watcher(tmp, client, ch, role="developer").poll_once()
        self.assertEqual(outcome.turns_queued, 0)
        client.issues_with_label.assert_not_called()

    async def test_a_reopened_tracking_issue_reports_again_when_it_closes(self):
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", "OPEN", "CLOSED")
            ch = self.channel()
            for _ in range(4):
                await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(len(ch._adapter_for.return_value.received), 2)

    async def test_a_recent_mention_in_the_topic_does_not_suppress_the_report(self):
        """The coordinator closed the issue from a turn in this topic; that is not the report."""
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED")
            ch = self.channel()
            ch.has_recent_ref.return_value = True
            await self.watcher(tmp, client, ch).poll_once()
            outcome = await self.watcher(tmp, client, ch).poll_once()
        self.assertEqual(outcome.turns_queued, 1)

    async def test_a_failed_report_turn_is_retried_and_not_recorded_as_posted(self):
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", "CLOSED")
            failing = MagicMock()
            failing.adapter = failing._adapter_for.return_value = MagicMock()
            failing.adapter.respond = AsyncMock(
                return_value=AdapterReply(text="", ok=False, error="the runtime exited 1"))
            failing.send = AsyncMock()
            failing.has_recent_ref.return_value = False
            await self.watcher(tmp, client, failing).poll_once()          # still open
            failed = await self.watcher(tmp, client, failing).poll_once()  # closes: turn fails
            waiting = await self.watcher(tmp, client, failing).poll_once()  # backed off
            state = NotificationState.load(Path(tmp) / "s.json")
        self.assertEqual(failed.turns_queued, 0)
        self.assertIn("retry 1/", failed.errors[0])
        # Backed off, so the next poll does not burn another failed turn.
        self.assertEqual(waiting.turns_skipped, 1)
        self.assertEqual(state.round_reports, {})
        self.assertEqual(state.attempts("tracking:example-org/webapp#34"), 1)
        self.assertEqual(list(state.pending_reports), ["example-org/webapp#34"])

    async def test_a_report_lost_to_a_failed_turn_is_posted_on_a_later_poll(self):
        """The closure stays queued: a broken runtime must not swallow the round end."""
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", "CLOSED")
            failing = self.channel()
            failing.adapter = failing._adapter_for.return_value = MagicMock()
            failing.adapter.respond = AsyncMock(
                return_value=AdapterReply(text="", ok=False, error="the runtime exited 1"))
            await self.watcher(tmp, client, failing).poll_once()          # still open
            await self.watcher(tmp, client, failing).poll_once()          # closes: turn fails
            state = NotificationState.load(Path(tmp) / "s.json")
            state.failures.clear()                                        # the backoff window passes
            state.save(Path(tmp) / "s.json")
            ch = self.channel()
            outcome = await self.watcher(tmp, client, ch).poll_once()
            final = NotificationState.load(Path(tmp) / "s.json")
        self.assertEqual(outcome.turns_queued, 1)
        self.assertIn("post the round report now", ch._adapter_for.return_value.received[0].text)
        self.assertEqual(final.pending_reports, {})

    async def test_the_tracking_label_can_be_configured(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = []
            client.issues_with_label.return_value = []
            cfg = make_test_config(tmp, role="coordinator", tracking_label="round")
            watcher = GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json",
                                    channel=self.channel())
            await watcher.poll_once()
        client.issues_with_label.assert_called_with("example-org/webapp", "round")

    async def test_an_unreachable_github_is_an_error_not_a_quiet_round_end(self):
        with TemporaryDirectory() as tmp:
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = []
            client.issues_with_label.side_effect = GitHubError("HTTP 401: Bad credentials")
            outcome = await self.watcher(tmp, client, self.channel()).poll_once()
        self.assertTrue(any("Bad credentials" in e for e in outcome.errors))


class TestARoundEndsOnlyWhenEveryItemIsDone(unittest.IsolatedAsyncioTestCase):
    """Issue #65: a tracking issue closed with work still open is not a round end.

    Round 2 closed its tracking issue while two items were still open, and the coordinator
    posted a report over unfinished work. The close is now refused -- no report turn -- and
    the coordinator is told, once, which items are still open."""

    BODY = "- [x] #65\n- [ ] #68\n- [ ] #71\n"

    def channel(self):
        ch = MagicMock()
        ch.adapter = ch._adapter_for.return_value = FakeAdapter()
        ch.send = AsyncMock()
        ch.has_recent_ref.return_value = False
        return ch

    def watcher(self, tmp, client, channel):
        cfg = make_test_config(tmp, role="coordinator")
        return GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json",
                             channel=channel)

    def client(self, *states, body=None, items=None):
        """A client whose tracking issue closes on the states given, and whose items are
        the ``items`` map ``{number: ("issue"|"pr", state)}``."""
        client = MagicMock(spec=GitHubClient)
        client.notifications.return_value = []
        client.issues_with_label.side_effect = [
            [_tracking(state=s, body=self.BODY if body is None else body)] for s in states]
        items = items or {}

        def issue_view(repo, number):
            kind, state = items.get(int(number), ("issue", "CLOSED"))
            if kind == "pr":
                return None
            return Issue(number=int(number), title=f"#{number}", body="", author="claude",
                         state=state)

        def pr_view(repo, number):
            kind, state = items.get(int(number), ("issue", "CLOSED"))
            if kind != "pr":
                return None
            return PR(number=int(number), title=f"#{number}", body="", author="claude",
                      state=state)

        def ref_state(repo, number):
            """``gh api``'s one-call answer: an issue's or a PR's state (#227)."""
            kind, state = items.get(int(number), ("issue", "CLOSED"))
            word = str(state).strip().lower()
            return "merged" if kind == "pr" and word == "merged" else word

        client.issue_view.side_effect = issue_view
        client.pr_view.side_effect = pr_view
        client.ref_state.side_effect = ref_state
        return client

    async def test_a_close_with_an_open_item_gets_no_report_and_names_the_item(self):
        items = {65: ("issue", "CLOSED"), 68: ("issue", "OPEN"), 71: ("pr", "MERGED")}
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", "CLOSED", items=items)
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()          # open
            closing = await self.watcher(tmp, client, ch).poll_once()  # closes with #68 open
            again = await self.watcher(tmp, client, ch).poll_once()    # no repeat
            state = NotificationState.load(Path(tmp) / "s.json")
        sent = ch._adapter_for.return_value.received
        self.assertEqual(len(sent), 1)
        self.assertIn("the round is not over", sent[0].text)
        self.assertIn("#68 open", sent[0].text)
        self.assertNotIn("#71", sent[0].text)                       # merged: not a blocker
        self.assertNotIn("post the round report now", sent[0].text)
        self.assertEqual((closing.turns_queued, again.turns_queued), (1, 0))
        self.assertEqual(state.round_reports, {})                   # no round report recorded
        self.assertEqual(list(state.blocked_reported), ["example-org/webapp#34"])

    async def test_an_unmerged_pull_request_blocks_the_report(self):
        items = {65: ("issue", "CLOSED"), 68: ("issue", "CLOSED"), 71: ("pr", "OPEN")}
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", items=items)
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
            await self.watcher(tmp, client, ch).poll_once()
        text = ch._adapter_for.return_value.received[0].text
        self.assertIn("#71 open", text)
        self.assertNotIn("post the round report now", text)

    async def test_a_close_with_every_item_merged_asks_for_the_report_once(self):
        items = {65: ("issue", "CLOSED"), 68: ("issue", "CLOSED"), 71: ("pr", "MERGED")}
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", "CLOSED", "CLOSED", items=items)
            ch = self.channel()
            for _ in range(4):
                await self.watcher(tmp, client, ch).poll_once()
        sent = ch._adapter_for.return_value.received
        self.assertEqual(len(sent), 1)
        self.assertIn("post the round report now", sent[0].text)
        self.assertNotIn("the round is not over", sent[0].text)

    async def test_a_pull_request_closed_without_merging_blocks_the_report(self):
        """Review of PR #76: gh says CLOSED for a PR that never merged, and that is not done."""
        items = {65: ("issue", "CLOSED"), 68: ("issue", "CLOSED"), 71: ("pr", "CLOSED")}
        with TemporaryDirectory() as tmp:
            client = self.client("OPEN", "CLOSED", items=items)
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
            await self.watcher(tmp, client, ch).poll_once()
        text = ch._adapter_for.return_value.received[0].text
        self.assertIn("the round is not over", text)
        self.assertIn("#71 closed, unmerged", text)

    async def test_an_item_github_cannot_read_counts_as_not_done(self):
        client = self.client("OPEN", "CLOSED", body="- [x] #65\n")
        client.issue_view.side_effect = None
        client.issue_view.return_value = None
        client.pr_view.return_value = None
        client.ref_state.side_effect = None
        client.ref_state.return_value = None          # gh could not answer for the item
        with TemporaryDirectory() as tmp:
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
            await self.watcher(tmp, client, ch).poll_once()
        self.assertIn("#65 unchecked", ch._adapter_for.return_value.received[0].text)

    async def test_a_reopen_after_a_refused_close_reports_on_the_next_valid_close(self):
        open_items = {65: ("issue", "OPEN")}
        done_items = {65: ("issue", "CLOSED")}
        with TemporaryDirectory() as tmp:
            ch = self.channel()
            # closed with #65 open, reopened, then closed with everything merged
            for state, items in (("OPEN", open_items), ("CLOSED", open_items),
                                 ("OPEN", done_items), ("CLOSED", done_items)):
                client = MagicMock(spec=GitHubClient)
                client.notifications.return_value = []
                client.issues_with_label.return_value = [_tracking(state=state, body=self.BODY)]
                client.issue_view.side_effect = lambda repo, number, items=items: Issue(
                    number=int(number), title="", body="", author="claude",
                    state=items.get(int(number), ("issue", "CLOSED"))[1])
                client.pr_view.return_value = None
                client.ref_state.side_effect = (
                    lambda repo, number, items=items:
                    items.get(int(number), ("issue", "CLOSED"))[1].lower())
                await self.watcher(tmp, client, ch).poll_once()
        sent = ch._adapter_for.return_value.received
        self.assertEqual(len(sent), 2)
        self.assertIn("the round is not over", sent[0].text)
        self.assertIn("post the round report now", sent[1].text)


class TestAParentClosesWithItsLastSlice(unittest.IsolatedAsyncioTestCase):
    """#227 item 2: a parent issue closes once every slice it lists has landed.

    The 22 Oct case: #117 stayed open after #133, #153 and #154 had all merged. The transport
    already reads a tracking issue's checklist to refuse an early close; it now uses the same
    check to close the parent when nothing is missing — with one line naming the slices, and
    the state of each slice deciding, never a ticked box.
    """

    BODY = "- [ ] #65\n- [ ] #68\n"

    def channel(self):
        ch = MagicMock()
        ch.adapter = ch._adapter_for.return_value = FakeAdapter()
        ch.send = AsyncMock()
        ch.has_recent_ref.return_value = False
        return ch

    def watcher(self, tmp, client, channel):
        cfg = make_test_config(tmp, role="coordinator")
        return GitHubWatcher(cfg, client=client, state_path=Path(tmp) / "s.json",
                             channel=channel)

    def client(self, states, items, body=None):
        """A client whose labelled tracking issue reads ``states`` in turn, and whose listed
        items have the states in ``items`` (``{number: ("issue"|"pr", state)}``)."""
        client = MagicMock(spec=GitHubClient)
        client.notifications.return_value = []
        client.issues_with_label.side_effect = [
            [_tracking(state=state, body=self.BODY if body is None else body)]
            for state in states]

        def ref_state(repo, number):
            kind, state = items.get(int(number), ("issue", "CLOSED"))
            word = str(state).strip().lower()
            return "merged" if kind == "pr" and word == "merged" else word

        client.ref_state.side_effect = ref_state
        client.pr_view.side_effect = lambda repo, number: (
            PR(number=int(number), title="", body="", author="claude",
               state=items[int(number)][1])
            if items.get(int(number), ("issue", ""))[0] == "pr" else None)
        client.issue_view.side_effect = lambda repo, number: Issue(
            number=int(number), title="", body="", author="claude",
            state=items.get(int(number), ("issue", "CLOSED"))[1])
        return client

    async def test_a_parent_whose_slices_all_merged_closes_itself(self):
        items = {65: ("pr", "MERGED"), 68: ("issue", "CLOSED")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN", "CLOSED"], items)
            ch = self.channel()
            first = await self.watcher(tmp, client, ch).poll_once()
            second = await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_called_once()
        repo, number, comment = client.close_issue.call_args.args
        self.assertEqual((repo, number), ("example-org/webapp", 34))
        self.assertIn("#65", comment)                    # the line names the slices...
        self.assertIn("#68", comment)
        # ...and the close is the round end: one report turn, not two.
        self.assertEqual((first.turns_queued, second.turns_queued), (1, 0))
        self.assertIn("post the round report now", ch._adapter_for.return_value.received[0].text)

    async def test_a_parent_with_an_open_slice_stays_open(self):
        items = {65: ("pr", "MERGED"), 68: ("pr", "OPEN")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN", "OPEN"], items)
            ch = self.channel()
            first = await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_not_called()
        self.assertEqual(first.turns_queued, 0)

    async def test_a_ticked_box_is_not_proof(self):
        """The coordinator ticked both boxes, but #68 is still open: the parent stays open."""
        items = {65: ("pr", "MERGED"), 68: ("pr", "OPEN")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN"], items, body="- [x] #65\n- [x] #68\n")
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_not_called()

    async def test_a_closed_pull_request_is_not_a_landed_slice(self):
        items = {65: ("pr", "MERGED"), 68: ("pr", "CLOSED")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN"], items)
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_not_called()

    async def test_an_item_github_cannot_read_keeps_the_parent_open(self):
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN"], {})
            client.ref_state.side_effect = None
            client.ref_state.return_value = None        # no answer is not "landed"
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_not_called()

    async def test_a_prose_body_that_merely_mentions_issues_does_not_close(self):
        """#227 review blocker: #189's own shape.

        Its body describes items 2-4 in prose and names eight other issues — all merged or
        closed — so the broad reading (`tracking_item_refs`) says "nothing open". Closing on
        that would close the parent of *this* round two slices early.
        """
        # #189's body, in the shape that matters: numbers mentioned, no listing.
        body = chr(10).join([
            'Found in the 22 Oct exports:',
            '- **Parent issues left open:** #117 stayed open after all its slices (#133, #153, #154) merged.',
            '- **Reviewers dispatching work:** Reasonix sent Codex to #170 (18:03).',
            '**Decide and do:**',
            "1. **Codex's role:** full re-reviews of #173 and #175.",
            "4. **Identities:** one login, so a merge claim (#155) can't be checked.",
        ]) + chr(10)
        items = {117: ("issue", "CLOSED"), 133: ("pr", "MERGED"), 153: ("pr", "MERGED"),
                 154: ("pr", "MERGED"), 170: ("issue", "CLOSED"), 173: ("pr", "MERGED"),
                 175: ("pr", "MERGED"), 155: ("pr", "MERGED")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN"], items, body=body)
            ch = self.channel()
            outcome = await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_not_called()          # a mention is not a listing...
        self.assertEqual(outcome.turns_queued, 0)       # ...and no round end was invented
        # The narrow reading is what stops it: the broad one would have said "all landed".
        self.assertEqual(len(tracking_item_refs(body, 34)), 8)

    async def test_a_split_into_line_states_the_slices(self):
        """The other listing form #227 names: "split into #a, #b" is a statement, so it closes."""
        items = {65: ("pr", "MERGED"), 68: ("issue", "CLOSED")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN"], items, body="Split into #65 and #68." + chr(10))
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_called_once()

    async def test_a_failed_close_posts_its_line_once(self):
        """#227 review nit: a close that keeps failing must not re-post the same comment."""
        items = {65: ("pr", "MERGED"), 68: ("issue", "CLOSED")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN", "OPEN", "OPEN"], items)
            client.close_issue.return_value = False
            ch = self.channel()
            for _ in range(3):
                await self.watcher(tmp, client, ch).poll_once()
            state = NotificationState.load(Path(tmp) / "s.json")
        comments = [call.args[2] for call in client.close_issue.call_args_list]
        self.assertEqual(len(comments), 3)               # the close is retried every poll...
        self.assertEqual(sum(1 for text in comments if text), 1)   # ...the line goes once
        self.assertEqual(list(state.parent_notes), ["example-org/webapp#34"])

    async def test_a_close_that_lands_clears_the_note(self):
        items = {65: ("pr", "MERGED"), 68: ("issue", "CLOSED")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN", "CLOSED"], items)
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
            state = NotificationState.load(Path(tmp) / "s.json")
        self.assertEqual(state.parent_notes, {})

    async def test_an_issue_that_lists_nothing_is_not_a_parent(self):
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN"], {}, body="Round 6 will be planned here.")
            ch = self.channel()
            await self.watcher(tmp, client, ch).poll_once()
        client.close_issue.assert_not_called()

    async def test_a_failed_close_is_an_error_not_a_silent_success(self):
        items = {65: ("pr", "MERGED"), 68: ("issue", "CLOSED")}
        with TemporaryDirectory() as tmp:
            client = self.client(["OPEN"], items)
            client.close_issue.return_value = False
            ch = self.channel()
            outcome = await self.watcher(tmp, client, ch).poll_once()
            state = NotificationState.load(Path(tmp) / "s.json")
        self.assertTrue(any("could not close" in e for e in outcome.errors), outcome.errors)
        # Not closed, so no round report was invented for it.
        self.assertEqual(state.round_reports, {})
        self.assertEqual(ch._adapter_for.return_value.received, [])


class TestTrackingItemRefs(unittest.TestCase):
    def test_the_checklist_is_the_item_list(self):
        self.assertEqual(tracking_item_refs("- [x] #65\n- [ ] #68\n", own=69), [65, 68])

    def test_without_a_checklist_every_reference_counts(self):
        self.assertEqual(tracking_item_refs("items: #65, #68 and #71", own=69), [65, 68, 71])

    def test_the_tracking_issue_itself_is_never_an_item(self):
        self.assertEqual(tracking_item_refs("- [ ] #69\n- [ ] #68\n", own=69), [68])

    def test_a_closed_issue_is_done_but_a_closed_pull_request_is_not(self):
        # Review of PR #76: a PR closed without merging also reads CLOSED, so the kind decides.
        self.assertEqual(item_blocker("MERGED"), None)
        self.assertEqual(item_blocker("CLOSED"), None)                       # an issue
        self.assertEqual(item_blocker("CLOSED", is_pr=True), "closed, unmerged")
        self.assertEqual(item_blocker("MERGED", is_pr=True), None)
        self.assertEqual(item_blocker("OPEN", is_pr=True), "open")
        self.assertEqual(item_blocker(""), "unchecked")
        self.assertEqual(item_blocker(None, is_pr=True), "unchecked")



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
