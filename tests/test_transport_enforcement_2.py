"""The rulebook in transport space, part 2 (admin, 24 Oct): push rules, outbound checks and
the PR rules, each checked by code and counted as drift."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from swarmly.github.client import GitHubClient
from swarmly.github.watch import GitHubWatcher
from swarmly.telegram import drift as dr
from swarmly.telegram import push_checks as pc
from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from tests.test_github_watch import make_test_config
from tests.test_telegram_channel import GROUP, event, make_channel, make_config, run

A, B, M = "a" * 40, "b" * 40, "c" * 40


def fake_git(answers):
    """A git that answers by its arguments' first words."""
    def run(*args):
        for prefix, out in answers.items():
            if args[:len(prefix)] == prefix:
                return out
        return ""
    return run


class PushCheckTests(unittest.TestCase):

    def line(self, branch="feat/x", local=A, remote=B):
        return [f"refs/heads/{branch} {local} refs/heads/{branch} {remote}"]

    def test_a_secret_in_the_pushed_commits_is_refused(self):
        token = "123456789:" + "A" * 35
        git = fake_git({("log", "-p"): f"+BOT_TOKEN = '{token}'\n", ("merge-base",): B})
        kind, why = pc.check(self.line(), "developer", "DeepSeek", git)
        self.assertEqual(kind, "secret_in_push")
        self.assertIn("Telegram bot token", why)

    def test_dropping_another_agents_commits_is_refused_but_your_own_is_not(self):
        git = fake_git({("merge-base",): M, ("log", "-1"): "Reasonix\n", ("log", "-p"): ""})
        kind, _ = pc.check(self.line(), "developer", "DeepSeek", git)
        self.assertEqual(kind, "foreign_force_push")
        mine = fake_git({("merge-base",): M, ("log", "-1"): "DeepSeek\n", ("log", "-p"): ""})
        self.assertIsNone(pc.check(self.line(), "developer", "DeepSeek", mine))

    def test_a_fast_forward_is_fine(self):
        git = fake_git({("merge-base",): B, ("log", "-p"): "+ok\n"})
        self.assertIsNone(pc.check(self.line(), "developer", "DeepSeek", git))

    def test_the_coordinator_pushes_merges_to_main_only(self):
        ff = {("merge-base",): B, ("log", "-p"): ""}
        kind, _ = pc.check(self.line("feat/x"), "coordinator", "Claude", fake_git(ff))
        self.assertEqual(kind, "coordinator_code_push")
        single = fake_git({**ff, ("rev-list",): f"{A} {B}\n"})
        self.assertEqual(pc.check(self.line("main"), "coordinator", "Claude", single)[0],
                         "coordinator_code_push")
        merge = fake_git({**ff, ("rev-list",): f"{A} {B} {M}\n"})
        self.assertIsNone(pc.check(self.line("main"), "coordinator", "Claude", merge))
        # A no-op push (already up to date) and the bus branch are not code pushes.
        self.assertIsNone(pc.check(self.line("main", local=B, remote=B), "coordinator",
                                   "Claude", single))
        self.assertIsNone(pc.check(self.line("agents-bus"), "coordinator", "Claude", single))

    def test_the_coordinator_may_create_main_on_an_empty_remote(self):
        """#249 review: a new repository's first push has nothing to merge into."""
        single = fake_git({("log", "-p"): "", ("rev-list",): f"{A}\n"})
        self.assertIsNone(pc.check(self.line("main", remote=pc.ZERO), "coordinator", "Claude",
                                   single))


class Recording(EchoAdapter):
    def __init__(self, replies):
        super().__init__("Hermes")
        self.replies = list(replies)

    async def respond(self, message):
        return AdapterReply(text=self.replies.pop(0))


class OutboundTests(unittest.TestCase):

    def channel(self, *replies):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        channel = make_channel(make_config(), adapter=Recording(replies))
        channel.bot = object()
        channel.drift = dr.DriftLedger(Path(self.tmp.name) / "drift.json")
        channel.topics.remember(GROUP, 9400, "Round")
        return channel

    def test_a_repeat_of_the_last_post_is_dropped_and_charged(self):
        text = ("@CodexBot #245 is at cefd20a with CI 7/7 and Reasonix's merge verdict; I merge "
                "it the moment the QA verdict lands on that same head.")
        channel = self.channel(text, text.replace("lands", "lands."))
        run(channel.handle_event(event("@HermesBot status?", thread_id=9400, message_id=1)))
        run(channel.handle_event(event("@HermesBot status??", thread_id=9400, message_id=2)))
        posts = [s["text"] for s in channel._test_sender.sent if "cefd20a" in s["text"]]
        self.assertEqual(len(posts), 1)
        self.assertEqual([r.kind for r in channel.drift.live()], ["duplicate_reply"])

    def test_telling_a_peer_to_upgrade_is_charged(self):
        channel = self.channel("@CodexBot please pipx upgrade swarmly and restart the bot.")
        run(channel.handle_event(event("@HermesBot next?", thread_id=9400, message_id=3)))
        self.assertIn("deploy_instruction", [r.kind for r in channel.drift.live()])


class PRCheckTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_new_pr_is_checked_once_per_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_test_config(tmp)
            client = MagicMock(spec=GitHubClient)
            client.notifications.return_value = []
            client.issues_with_label.return_value = []
            client.open_pr_checks.return_value = []
            client.open_prs.return_value = [
                {"number": 250, "headRefOid": A, "body": "Adds a thing."},
                {"number": 251, "headRefOid": B, "body": "Closes #87"},
                {"number": 252, "headRefOid": M, "body": "Fixes #87"},
                {"number": 253, "headRefOid": A, "body": "Part of #260. Phase 3."},
                {"number": 254, "headRefOid": B, "body": "part of #260: phase 9"},
                {"number": 255, "headRefOid": M, "body": "Builds on work done as part of #120."}]
            client.pr_diff.side_effect = lambda repo, n: (
                "-    def test_old(self):\n+    x = 1\n" if n == 251 else
                "+    @unittest.skip('later')\n+    def test_new(self):\n" if n == 252 else "")
            channel = MagicMock()
            channel.send = AsyncMock()
            channel.has_recent_ref.return_value = False
            channel.topic_for_text.return_value = (GROUP, 9400)
            watcher = lambda: GitHubWatcher(cfg, client=client,  # noqa: E731
                                            state_path=Path(tmp) / "s.json", channel=channel)
            await watcher().poll_once()
            said = {call.args[1]: call.args[2] for call in client.comment.call_args_list}
            self.assertIn("names no issue", said[250])
            self.assertIn("#87 already has open PR(s) #252", said[251])
            self.assertIn("removes 1 more test function", said[251])
            self.assertIn("adds a test skip", said[252])
            # One step of a larger issue names it with "Part of", and several steps of one
            # issue are not "one issue, one pull request" (live, 25 Oct: #298 was flagged).
            self.assertNotIn(253, said)
            self.assertNotIn(254, said)
            self.assertIn("names no issue", said[255])     # "part of" in passing is not one
            await watcher().poll_once()                       # same heads: not again
            self.assertEqual(client.comment.call_count, 4)

    def test_part_of_is_read_in_the_forms_pr_bodies_use(self):
        """Review of #307: bold, a quote or a colon around "Part of #N" still names it."""
        for body in ("Part of #260.", "**Part of #260**", "> Part of #260", "Part of: #260",
                     "- part of #260, phase 3", "## Part of #260", "1. Part of #260"):
            with self.subTest(body=body):
                self.assertIsNotNone(GitHubWatcher._PART_OF.search(body))
        for body in ("Builds on work done as part of #120.", "Part of the plan, see #260"):
            with self.subTest(body=body):
                self.assertIsNone(GitHubWatcher._PART_OF.search(body))


if __name__ == "__main__":
    unittest.main()
