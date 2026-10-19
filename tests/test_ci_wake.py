"""CI wake: a PR's checks are watched, and the waiting agent is woken once when they finish.

Issue #24. A transport that pushed a pull request waits on its checks; the GitHub poll asks
for them, and when they reach a terminal state the waiting agent gets exactly one turn with
the result — once per head commit, and not again after a restart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from swarmly.github.client import PRChecks, checks_from_rollup
from swarmly.github.watch import (CI_WATCH_TTL_SECONDS, CIWatchState, GitHubWatcher,
                                  register_ci_watch)
from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.config import AgentConfig

GROUP = -1001234567890
TOPIC = 12
REPO = "example-org/webapp"


class FakeAdapter:
    def __init__(self) -> None:
        self.received: list[IncomingMessage] = []

    async def respond(self, message: IncomingMessage):
        self.received.append(message)
        reply = MagicMock()
        reply.text = f"ACK {message.text[:20]}"
        reply.ok = True
        return reply

    async def close(self) -> None:
        pass


class FakeGitHub:
    """Answers like :class:`GitHubClient`, with the check results a test queues up."""

    def __init__(self, checks: list = None) -> None:
        self.checks = list(checks or [])
        self.calls: list = []

    def notifications(self, repo: str = None) -> list:
        return []

    def pr_checks(self, repo: str, number: int):
        self.calls.append((repo, number))
        return self.checks.pop(0) if self.checks else None


def make_config(cwd: str, **github_kwargs) -> AgentConfig:
    return AgentConfig.from_dict({
        "agent": {"name": "AntigravityBot", "alias": "Antigravity", "bot_token": "123:TEST",
                  "home_channel_id": GROUP, "admin_user_id": 99999},
        "projects": {"webapp": {"repo": REPO, "cwd": cwd, "topics": [TOPIC],
                                "worktrees": False}},
        "github": {"login": "test-bot", "poll_minutes": 5, **github_kwargs},
    })


def pending(total: int = 2, sha: str = "aaa111") -> PRChecks:
    return PRChecks(number=20, head_sha=sha, state="pending", total=total,
                    pending=["build", "test"])


def passed(sha: str = "aaa111") -> PRChecks:
    return PRChecks(number=20, head_sha=sha, state="success", total=2, passed=2,
                    url=f"https://github.com/{REPO}/pull/20", title="Add the thing")


def failed(sha: str = "aaa111") -> PRChecks:
    return PRChecks(number=20, head_sha=sha, state="failure", total=3, passed=1,
                    failed=["build", "e2e"], url=f"https://github.com/{REPO}/pull/20")


def fake_channel() -> tuple:
    channel = MagicMock()
    adapter = FakeAdapter()
    channel._adapter_for.return_value = adapter
    channel.adapter = adapter
    channel.send = AsyncMock()
    channel.has_recent_ref.return_value = False
    return channel, adapter


def watcher_for(tmp: str, client: FakeGitHub, channel=None,
                state_path: Path = None) -> GitHubWatcher:
    cfg = make_config(tmp)
    return GitHubWatcher(cfg, client=client,
                         state_path=state_path or Path(tmp) / "state.json",
                         channel=channel if channel is not None else fake_channel()[0])


def watch_pr(watcher: GitHubWatcher) -> None:
    watcher.ci_state.watch(REPO, 20, GROUP, TOPIC, project="webapp")
    watcher.ci_state.save(watcher.ci_state_path)


class TestChecksFromRollup(unittest.TestCase):
    def test_a_running_check_keeps_the_run_pending(self):
        checks = checks_from_rollup(20, "abc", "", "", [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
             "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "test", "status": "IN_PROGRESS"},
        ])
        self.assertEqual(checks.state, "pending")
        self.assertEqual((checks.total, checks.passed, checks.pending), (2, 1, ["test"]))
        self.assertFalse(checks.terminal)

    def test_all_checks_done_is_terminal(self):
        checks = checks_from_rollup(20, "abc", "url", "title", [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
             "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"},
        ])
        self.assertEqual(checks.state, "success")
        self.assertTrue(checks.terminal)
        self.assertIn("all 2 check(s) passed", checks.summary())

    def test_a_failure_names_the_failing_checks(self):
        checks = checks_from_rollup(20, "abc", "", "", [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
             "conclusion": "FAILURE"},
            {"__typename": "StatusContext", "context": "e2e", "state": "ERROR"},
            {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED",
             "conclusion": "SKIPPED"},
        ])
        self.assertEqual(checks.state, "failure")
        self.assertEqual(checks.failed, ["build", "e2e"])
        self.assertIn("build", checks.summary())

    def test_an_empty_rollup_is_pending_not_a_wake(self):
        # A PR pushed a moment ago may have no checks yet: waiting is right, a wake saying
        # "no checks" is noise -- and it would land before the run it waits for exists.
        checks = checks_from_rollup(20, "abc", "", "", [])
        self.assertEqual(checks.state, "pending")
        self.assertFalse(checks.terminal)
        self.assertIn("no check runs", checks.summary())

    def test_an_expected_status_context_is_still_running(self):
        """Maintainer review of #24: ``EXPECTED`` is a StatusContext that has not reported
        yet, so it is pending — not a passed check that would declare the run terminal."""
        checks = checks_from_rollup(20, "abc", "", "", [
            {"__typename": "StatusContext", "context": "qa", "state": "EXPECTED"},
        ])
        self.assertEqual(checks.state, "pending")
        self.assertFalse(checks.terminal)
        self.assertEqual(checks.pending, ["qa"])

    def test_a_merged_pr_is_not_open(self):
        """Maintainer review of #24: the rollup says nothing about whether the PR still waits
        on its checks, so the PR's own state is read too."""
        checks = checks_from_rollup(20, "abc", "", "", [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
             "conclusion": "SUCCESS"},
        ], pr_state="MERGED")
        self.assertEqual(checks.state, "success")          # the checks themselves did pass
        self.assertEqual(checks.pr_state, "MERGED")
        self.assertFalse(checks.is_open)
        self.assertTrue(checks_from_rollup(20, "abc", "", "", []).is_open)   # unknown/open

    def test_pr_checks_asks_for_the_pr_state(self):
        """Maintainer review of #24: one call carries the state, so the watch can tell a
        merged PR from an open one without a second request."""
        from swarmly.github.client import GitHubClient

        calls = []

        def fake_run(args, timeout=30.0):
            calls.append(args)
            return 0, json.dumps({"number": 20, "state": "MERGED", "headRefOid": "abc",
                                  "url": "u", "title": "t",
                                  "statusCheckRollup": [
                                      {"__typename": "CheckRun", "name": "build",
                                       "status": "COMPLETED", "conclusion": "SUCCESS"}]}), ""

        client = GitHubClient()
        with patch.object(client, "_run", fake_run):
            checks = client.pr_checks(REPO, 20)
        self.assertIn("state", calls[0][-1])                # the field is requested
        self.assertEqual(checks.pr_state, "MERGED")
        self.assertFalse(checks.is_open)


class TestCIWake(unittest.IsolatedAsyncioTestCase):
    async def test_a_pending_run_is_not_a_wake(self):
        with TemporaryDirectory() as tmp:
            client = FakeGitHub([pending()])
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)

            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 0)
            self.assertEqual(outcome.ci_watched, 1)
            self.assertEqual(adapter.received, [])
            reloaded = CIWatchState.load(watcher.ci_state_path)
            self.assertIn(f"{REPO}#20", reloaded.watches)     # still waiting

    async def test_the_terminal_run_wakes_the_agent_once(self):
        with TemporaryDirectory() as tmp:
            client = FakeGitHub([pending(), passed()])
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)

            await watcher.poll_once()
            self.assertEqual(adapter.received, [])            # checks still running
            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 1)
            self.assertEqual(len(adapter.received), 1, adapter.received)
            message = adapter.received[0]
            self.assertIn(f"CI finished on {REPO}#20", message.text)
            self.assertIn("all 2 check(s) passed", message.text)
            self.assertEqual(message.project, "webapp")
            self.assertEqual(message.context_id, f"tg-{TOPIC}")
            self.assertEqual(channel.send.call_count, 1)      # the turn's reply, posted
            # The watch is spent: no further poll asks for it.
            self.assertEqual(list(CIWatchState.load(watcher.ci_state_path).watches), [])

    async def test_a_failing_run_wakes_with_the_failing_checks(self):
        with TemporaryDirectory() as tmp:
            client = FakeGitHub([failed()])
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)

            await watcher.poll_once()

            self.assertEqual(len(adapter.received), 1)
            self.assertIn("CI finished on", adapter.received[0].text)
            self.assertIn("failing: build, e2e", adapter.received[0].text)
            self.assertIn("Fix the failing checks", adapter.received[0].text)

    async def test_the_same_run_is_never_reported_twice_across_a_restart(self):
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            channel, adapter = fake_channel()
            first = watcher_for(tmp, FakeGitHub([passed()]), channel, state_path)
            watch_pr(first)
            await first.poll_once()
            self.assertEqual(len(adapter.received), 1)

            # A restart: a new watcher reads the same state, and the same run is registered
            # again (a re-posted PR link). It must not wake a second time.
            second = watcher_for(tmp, FakeGitHub([passed()]), channel, state_path)
            second.ci_state.watch(REPO, 20, GROUP, TOPIC, project="webapp")
            second.ci_state.save(second.ci_state_path)
            outcome = await second.poll_once()

            self.assertEqual(outcome.ci_wakes, 0)
            self.assertEqual(outcome.ci_skipped, 1)
            self.assertEqual(len(adapter.received), 1)

    async def test_a_new_push_is_reported_again(self):
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            channel, adapter = fake_channel()
            first = watcher_for(tmp, FakeGitHub([passed("aaa111")]), channel, state_path)
            watch_pr(first)
            await first.poll_once()

            second = watcher_for(tmp, FakeGitHub([passed("bbb222")]), channel, state_path)
            second.ci_state.watch(REPO, 20, GROUP, TOPIC, project="webapp")
            second.ci_state.save(second.ci_state_path)
            outcome = await second.poll_once()

            self.assertEqual(outcome.ci_wakes, 1)
            self.assertEqual(len(adapter.received), 2)

    async def test_a_merged_pr_wakes_nobody(self):
        """Maintainer review of #24: a reply that names an already-merged PR (a merge notice,
        an upgrade call) must not wake the agent to "review and QA" work that already landed."""
        with TemporaryDirectory() as tmp:
            merged = passed()
            merged.pr_state = "MERGED"
            client = FakeGitHub([merged])
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)

            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 0)
            self.assertEqual(adapter.received, [])
            self.assertEqual(outcome.errors, [])
            self.assertEqual(list(CIWatchState.load(watcher.ci_state_path).watches), [])

    async def test_a_closed_pr_with_running_checks_is_dropped_too(self):
        """A closed PR's checks will never finish, so the watch goes on the first poll rather
        than waiting out the 12 h TTL."""
        with TemporaryDirectory() as tmp:
            closed = pending()
            closed.pr_state = "CLOSED"
            watcher = watcher_for(tmp, FakeGitHub([closed]), fake_channel()[0])
            watch_pr(watcher)

            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 0)
            self.assertEqual(list(CIWatchState.load(watcher.ci_state_path).watches), [])

    async def test_an_open_pr_is_still_watched_and_woken(self):
        """The state is read, but an open PR waits on its checks as before."""
        with TemporaryDirectory() as tmp:
            still_open = passed()
            still_open.pr_state = "OPEN"
            client = FakeGitHub([still_open])
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)

            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 1)
            self.assertEqual(len(adapter.received), 1)

    async def test_a_watch_gh_has_no_checks_for_is_dropped_quietly(self):
        with TemporaryDirectory() as tmp:
            client = FakeGitHub([None])
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)

            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 0)
            self.assertEqual(outcome.errors, [])
            self.assertEqual(adapter.received, [])
            self.assertEqual(list(CIWatchState.load(watcher.ci_state_path).watches), [])

    async def test_a_watch_that_never_finishes_is_dropped_after_its_ttl(self):
        with TemporaryDirectory() as tmp:
            client = FakeGitHub([pending(), pending()])
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)
            # Backdate the watch past its lifetime instead of sleeping.
            for entry in watcher.ci_state.watches.values():
                entry["created_at"] -= CI_WATCH_TTL_SECONDS + 1
            await watcher.poll_once()

            self.assertEqual(client.calls, [])                # not even polled
            self.assertEqual(list(CIWatchState.load(watcher.ci_state_path).watches), [])
            self.assertEqual(adapter.received, [])

    async def test_a_watch_registered_during_a_wake_survives_the_poll(self):
        # The wake turn's reply names a second PR, so the channel writes that watch to the
        # same file this poll is holding a stale snapshot of. Saving the snapshot back would
        # erase it: the watch must still be there when the poll ends.
        with TemporaryDirectory() as tmp:
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, FakeGitHub([passed()]), channel)
            watch_pr(watcher)

            async def registers_another(message):
                register_ci_watch(watcher.config, REPO, 21, GROUP, TOPIC, project="webapp",
                                  state_path=watcher.ci_state_path)
                reply = MagicMock()
                reply.text = "ACK"
                reply.ok = True
                return reply

            adapter.respond = registers_another
            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 1)
            self.assertEqual(list(CIWatchState.load(watcher.ci_state_path).watches),
                             [f"{REPO}#21"])

    async def test_a_watch_registered_between_polls_is_still_polled(self):
        # A long-lived watcher loaded its state before the channel wrote this watch, so the
        # poll has to read the file back instead of trusting the snapshot in memory.
        with TemporaryDirectory() as tmp:
            client = FakeGitHub([passed()])
            watcher = watcher_for(tmp, client, fake_channel()[0])
            register_ci_watch(watcher.config, REPO, 20, GROUP, TOPIC, project="webapp",
                              state_path=watcher.ci_state_path)
            self.assertEqual(watcher.ci_state.watches, {})   # nothing in the snapshot

            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 1)
            self.assertEqual(client.calls, [(REPO, 20)])

    async def test_a_wake_in_a_topic_without_a_context_id_is_refused(self):
        # Same guard as the notification turns: a bound topic outside the home group with no
        # shared secret has no context id, so its wake must be refused rather than run into
        # the shared General session.
        with TemporaryDirectory() as tmp:
            channel, adapter = fake_channel()
            watcher = watcher_for(tmp, FakeGitHub([passed()]), channel)
            other_group = GROUP - 1
            watcher.ci_state.watch(REPO, 20, other_group, TOPIC, project="webapp")
            watcher.ci_state.save(watcher.ci_state_path)
            with patch.dict(os.environ, {"SWARMLY_REDACTION_SECRET": ""}):
                outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 0)
            self.assertEqual(adapter.received, [])
            self.assertEqual(outcome.delivered_topics, [])
            self.assertTrue(any("refused" in err for err in outcome.errors), outcome.errors)

    async def test_a_failed_turn_is_reported_and_still_not_repeated(self):
        with TemporaryDirectory() as tmp:
            client = FakeGitHub([passed()])
            channel, adapter = fake_channel()

            async def broke(message):
                adapter.received.append(message)
                reply = MagicMock()
                reply.text = ""
                reply.ok = False
                reply.error = "the runtime exited"
                return reply

            adapter.respond = broke
            watcher = watcher_for(tmp, client, channel)
            watch_pr(watcher)
            outcome = await watcher.poll_once()

            self.assertEqual(outcome.ci_wakes, 0)
            self.assertTrue(any("the runtime exited" in err for err in outcome.errors),
                            outcome.errors)

            again = watcher_for(tmp, FakeGitHub([passed()]), channel, watcher.state_path)
            again.ci_state.watch(REPO, 20, GROUP, TOPIC, project="webapp")
            again.ci_state.save(again.ci_state_path)
            self.assertEqual((await again.poll_once()).ci_wakes, 0)


class TestRegistration(unittest.IsolatedAsyncioTestCase):
    def test_registration_is_recorded_on_disk_and_off_when_the_watcher_is(self):
        with TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            self.assertTrue(register_ci_watch(cfg, REPO, 20, GROUP, TOPIC, project="webapp",
                                              state_path=Path(tmp) / "ci.json"))
            state = CIWatchState.load(Path(tmp) / "ci.json")
            self.assertEqual(state.watches[f"{REPO}#20"]["number"], 20)
            self.assertEqual(state.watches[f"{REPO}#20"]["thread_id"], TOPIC)

            off = make_config(tmp, poll_minutes=0)
            self.assertFalse(register_ci_watch(off, REPO, 21, GROUP, TOPIC,
                                               state_path=Path(tmp) / "none.json"))
            self.assertFalse((Path(tmp) / "none.json").exists())

    def test_watching_twice_keeps_one_watch(self):
        state = CIWatchState()
        state.watch(REPO, 20, GROUP, TOPIC, project="webapp", now=1000.0)
        state.watch(REPO, 20, GROUP, TOPIC, project="webapp", now=2000.0)
        self.assertEqual(len(state.watches), 1)
        # It keeps its original clock, so re-posting a PR link never extends the watch.
        self.assertEqual(state.watches[f"{REPO}#20"]["created_at"], 1000.0)

    def test_reported_results_are_bounded(self):
        state = CIWatchState()
        for number in range(600):
            state.mark_reported(f"{REPO}#{number}", "sha:success")
        self.assertEqual(len(state.reported), 500)
        self.assertIn(f"{REPO}#599", state.reported)          # newest kept
        self.assertNotIn(f"{REPO}#0", state.reported)


class TestChannelRegistersAPRItTalksAbout(unittest.IsolatedAsyncioTestCase):
    """The link from a turn to the watch: a reply naming a PR registers it for the poll."""

    def channel(self, tmp: str):
        """A channel whose config puts its state (notifications, CI watches) under *tmp*."""
        from tests.test_telegram_channel import make_channel
        cfg = make_config(tmp)
        cfg.bus.repo = tmp                                  # state files under the temp dir
        self.cfg = cfg
        return make_channel(cfg)

    def watched(self, tmp: str) -> CIWatchState:
        from swarmly.github.watch import ci_state_file_for
        return CIWatchState.load(ci_state_file_for(self.cfg))

    def test_a_pr_link_in_a_reply_is_watched(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            from tests.test_telegram_channel import run
            run(channel.send(f"Opened https://github.com/{REPO}/pull/20 for #24"))

            state = self.watched(tmp)
            self.assertEqual(list(state.watches), [f"{REPO}#20"])
            self.assertEqual(state.watches[f"{REPO}#20"]["chat_id"], GROUP)

    def test_a_bare_ref_in_a_project_topic_is_watched_there(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            from tests.test_telegram_channel import run
            run(channel.send("pushed the fix to PR #20", chat_id=GROUP, thread_id=TOPIC))

            watch = self.watched(tmp).watches.get(f"{REPO}#20")
            self.assertIsNotNone(watch)
            self.assertEqual(watch["thread_id"], TOPIC)
            self.assertEqual(watch["project"], "webapp")

    def test_a_pr_link_in_a_callable_reply_is_watched(self):
        # The text the channel sends is what a callable resolves to, so that is what may
        # name a PR -- the callable's source never would.
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            from tests.test_telegram_channel import run
            run(channel.send(lambda: f"Opened https://github.com/{REPO}/pull/20"))

            state = self.watched(tmp)
            self.assertEqual(list(state.watches), [f"{REPO}#20"])

    def test_a_reply_without_a_pr_registers_nothing(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            from tests.test_telegram_channel import run
            run(channel.send("ran the suite, all green"))
            self.assertEqual(list(self.watched(tmp).watches), [])

    def test_nothing_is_watched_when_github_polling_is_off(self):
        with TemporaryDirectory() as tmp:
            from tests.test_telegram_channel import make_channel, run
            cfg = make_config(tmp, poll_minutes=0)
            cfg.bus.repo = tmp
            channel = make_channel(cfg)
            run(channel.send("Opened https://github.com/example-org/webapp/pull/20"))
            from swarmly.github.watch import ci_state_file_for
            self.assertFalse(ci_state_file_for(cfg).exists())


if __name__ == "__main__":
    unittest.main()
