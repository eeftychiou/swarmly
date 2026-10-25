"""CI on every open PR of the repo, every poll: a finished run wakes the coordinator once.

Admin, 23 Oct: "Why would the CI watch arm only based on a token-space trigger, which
typically drifts?" Live, round 5.5: the last merge waited an hour because no reply had named
the PR the way the per-PR watch needed. What is watched now comes from GitHub, not a model.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from swarmly.github.client import GitHubClient, PRChecks
from swarmly.github.watch import GitHubWatcher
from tests.test_github_watch import FakeAdapter, make_test_config

GROUP = -1001234567890


def checks(number, sha, state):
    done = state != "pending"
    return PRChecks(number=number, head_sha=sha, state=state, total=7,
                    passed=7 if state == "success" else (6 if state == "failure" else 3),
                    failed=["lint"] if state == "failure" else [],
                    pending=[] if done else ["unittest"], url="", title=f"PR {number}",
                    pr_state="OPEN")


class RepoCITests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = FakeAdapter()

    def watcher(self, polls, **github):
        cfg = make_test_config(self.tmp.name, **github)
        client = MagicMock(spec=GitHubClient)
        client.notifications.return_value = []
        client.issues_with_label.return_value = []
        client.open_pr_checks.side_effect = list(polls)
        channel = MagicMock()
        channel._adapter_for.return_value = self.adapter
        channel.send = AsyncMock()
        channel.has_recent_ref.return_value = False
        channel.topic_for_text.return_value = (GROUP, 9400)
        state = Path(self.tmp.name) / "state.json"
        return lambda: GitHubWatcher(cfg, client=client, state_path=state, channel=channel)

    async def poll(self, make):
        return await make().poll_once()       # a fresh watcher each time, like a restart

    async def test_a_run_seen_running_wakes_the_coordinator_once_when_it_finishes(self):
        make = self.watcher([[checks(230, "474778a", "pending")],
                             [checks(230, "474778a", "success")],
                             [checks(230, "474778a", "success")]])
        await self.poll(make)
        self.assertEqual(self.adapter.received, [])
        outcome = await self.poll(make)
        self.assertEqual(len(self.adapter.received), 1)
        self.assertIn("#230", self.adapter.received[0].text)
        self.assertEqual(outcome.delivered_topics, [(GROUP, 9400)])   # the PR's own topic
        await self.poll(make)
        self.assertEqual(len(self.adapter.received), 1, "one wake per run, not per poll")

    async def test_a_failure_wakes_too(self):
        make = self.watcher([[checks(230, "aaaaaaa", "pending")],
                             [checks(230, "aaaaaaa", "failure")]])
        await self.poll(make)
        await self.poll(make)
        self.assertEqual(len(self.adapter.received), 1)
        self.assertIn("Fix the failing checks", self.adapter.received[0].text)

    async def test_a_run_already_finished_when_first_seen_wakes_nobody(self):
        """The first poll after a deploy must not replay every old result."""
        make = self.watcher([[checks(229, "7f23954", "success"), checks(231, "50fc5ae", "failure")],
                             [checks(229, "7f23954", "success"), checks(231, "50fc5ae", "failure")]])
        await self.poll(make)
        await self.poll(make)
        self.assertEqual(self.adapter.received, [])

    async def test_a_new_push_is_a_new_run(self):
        make = self.watcher([[checks(230, "2c46748", "pending")],
                             [checks(230, "2c46748", "failure")],
                             [checks(230, "474778a", "pending")],
                             [checks(230, "474778a", "success")]])
        for _ in range(4):
            await self.poll(make)
        self.assertEqual(len(self.adapter.received), 2)

    async def test_it_can_be_turned_off(self):
        make = self.watcher([[checks(230, "474778a", "pending")],
                             [checks(230, "474778a", "success")]], watch_repo_ci=False)
        await self.poll(make)
        await self.poll(make)
        self.assertEqual(self.adapter.received, [])


if __name__ == "__main__":
    unittest.main()
