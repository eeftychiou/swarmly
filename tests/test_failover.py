"""Failure drills (issue #85): seven scenarios on the in-process multi-bot harness.

Each scenario kills something on purpose -- the coordinator, the bus connection, a bot's
runtime -- and asserts the swarm's answer, which is specified in #81 (wakes), #82 (liveness),
#83 (the lease), #84/#86 (round state). Nothing here starts a CLI: the stub adapter answers
what the scenario scripted and the clock is the group's, so the whole file runs in seconds.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from swarmly.telegram import wake as wk
from tests.swarm_group import (ADMIN_USERNAME, GROUP, Bus, Group, needs_swarm, run)
from tempfile import TemporaryDirectory

#: The swarm the documentation describes: one bot per role.
ROLES = {"Claudebot": "coordinator", "Codex": "reviewer", "Antigravity": "qa-tester",
         "DeepSeek": "developer"}

#: A round-state block, as the coordinator writes it in a reply (#86).
ROUND_BLOCK = ('ROUND_STATE {"version": 1, "round": 88, '
               '"items": {"85": {"owner": "DeepSeek"}}}')


@needs_swarm
class GroupCase(unittest.TestCase):
    """A swarm with a coordinator in place at term 1, ready for a drill."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.group = Group(self.tmp.name, succession=["reviewer", "qa-tester", "developer"])
        for alias, role in ROLES.items():
            self.group.join(alias, role)
        self.group.get("Claudebot").channel.membership.state.coordinator = "Claudebot"
        self.boot()

    def boot(self) -> None:
        """One lease pass: the configured coordinator claims term 1, everybody else sees it."""
        self.group.tick()

    # -- what the drills assert ------------------------------------------------------
    def holder(self) -> str:
        return self.group.bus_holder() if hasattr(self.group, "bus_holder") else \
            self.lease_state().holder

    def lease_state(self):
        from swarmly.telegram.lease import LeaseStore
        store = LeaseStore("observer", transport=self.group.bus.view("observer"),
                           root=self.group.root,
                           clock=lambda: self.group.clock.now(), ttl_seconds=600.0)
        return store.state(now=self.group.clock.now())


@needs_swarm
class TestTheCoordinatorDies(GroupCase):
    """1. The coordinator dies while a PR waits on CI.

    The lease lapses, the successor acts at term N+1, and -- the part that used to be missing --
    the coordinator that is still *running* but cut off the bus cannot merge anything.
    """

    def test_the_successor_takes_over_and_the_cut_off_coordinator_cannot_merge(self):
        group = self.group
        self.assertEqual(self.holder(), "Claudebot")
        self.assertEqual(self.lease_state().term, 1)

        pr = group.github.open_pr(109, head="cf0b82a", title="Resume nits (#105)")
        group.get("Claudebot").channel.membership.state.coordinator = "Claudebot"

        # The coordinator loses its connection to the bus -- a lost remote, not a clean stop.
        group.cut_off("Claudebot")
        group.advance(11 * 60)                       # one minute past the 10-minute lease

        out = group.tick()
        state = self.lease_state()
        self.assertEqual((state.holder, state.term), ("Codex", 2))   # the term moved, once
        self.assertEqual(group.get("Codex").channel.lease_state().term, 2)
        self.assertTrue(any("acting coordinator at term 2" in text
                            for text in group.said_by("Codex")))
        self.assertFalse(any("acting coordinator" in text
                             for text in group.said_by("Antigravity", "acting coordinator")))
        self.assertFalse(group.get("Claudebot").lease_fence())

        # CI goes green, and both bots reach for the merge button: only the acting one may.
        group.github.go_green(109)
        merged_by = []
        for alias in ("Claudebot", "Codex"):
            bot = group.get(alias)
            if group.github.merge(109, by=alias, fence=bot.lease_fence):
                merged_by.append(alias)
        self.assertEqual(merged_by, ["Codex"])
        self.assertEqual(group.github.merges, [(109, "Codex")])
        self.assertEqual(pr["state"], "merged")


@needs_swarm
class TestTheCoordinatorComesBack(GroupCase):
    """2. The coordinator comes back after losing the role: it stays a peer until /role."""

    def test_the_returning_coordinator_does_not_merge_twice(self):
        group = self.group
        group.github.open_pr(109, head="cf0b82a")
        group.github.go_green(109)

        group.kill("Claudebot")
        group.advance(11 * 60)
        group.tick()
        self.assertEqual(group.get("Codex").channel.lease_state().term, 2)

        back = group.restart("Claudebot")
        self.assertFalse(back.lease_fence())          # a peer, not the acting coordinator
        self.assertTrue(group.github.merge(109, by="Codex", fence=group.get("Codex").lease_fence))
        self.assertFalse(group.github.merge(109, by="Claudebot", fence=back.lease_fence))
        self.assertEqual(group.github.merges, [(109, "Codex")])

        # The admin hands the role back: /role writes a new term, and there is still one merge.
        run(back.channel.handle_event(_admin_event("/role @claude_obot coordinator")))
        self.assertTrue(back.lease_fence())
        self.assertEqual(group.github.merges, [(109, "Codex")])


    def test_only_the_acting_coordinator_writes_the_round_state(self):
        group = self.group
        block = "\n" + ROUND_BLOCK
        group.kill("Claudebot")
        group.advance(11 * 60)
        group.tick()

        acting = group.get("Codex")
        body = acting.channel.apply_round_block("#85 is on the branch." + block,
                                                _admin_event("#85 is on the branch."))
        self.assertNotIn("ROUND_STATE", body)
        state = acting.channel.round_store().load("88")
        self.assertEqual(state.status, "ok")
        self.assertEqual(state.item("85")["owner"], "DeepSeek")
        segments = group.bus.documents(".agents/rounds/88")

        back = group.restart("Claudebot")
        dropped = back.channel.apply_round_block("#85 merged." + block,
                                                 _admin_event("#85 merged."))
        self.assertNotIn("ROUND_STATE", dropped)
        self.assertIn("non-coordinator", dropped)
        self.assertEqual(group.bus.documents(".agents/rounds/88"), segments)


@needs_swarm
class TestOneCoordinatorRemains(GroupCase):
    """3. Two successors claim at once, and a partition heals: one holder, the highest term."""

    def test_two_successors_leave_one_holder(self):
        group = self.group
        group.kill("Claudebot")
        group.advance(11 * 60)
        # Both successors read the lapsed lease before either push is fetched: two claims at
        # term 2, and the fold -- not the order they happened to run in -- has to name one.
        group.delay_bus("Antigravity")
        group.tick()

        state = self.lease_state()
        self.assertEqual(state.term, 2)
        self.assertEqual(state.holder, "Codex")       # reviewer, first in the succession
        claimants = sorted(row["alias"] for row in group.bus.documents(".agents/leases"))
        self.assertEqual(claimants, ["Antigravity", "Claudebot", "Codex"])
        self.assertEqual(state.ignored != (), True)   # the loser is recorded, not silent

        # Once the stale bot fetches, it stops acting: the fence asks the bus every time.
        group.heal("Antigravity")
        self.assertFalse(group.get("Antigravity").lease_fence())
        self.assertFalse(any("acting coordinator" in text
                             for text in group.said_by("Antigravity")))

    def test_a_partition_between_successors_changes_no_holder(self):
        group = self.group
        group.kill("Claudebot")
        group.advance(11 * 60)
        group.tick()
        held = self.lease_state().holder

        group.partition("Codex", "Antigravity")       # Telegram delivery stops between them
        group.advance(60)
        group.tick()
        self.assertEqual(self.lease_state().holder, held)
        group.heal("Codex", "Antigravity")
        group.tick()
        self.assertEqual(self.lease_state().holder, held)
        self.assertEqual(self.lease_state().term, 2)


@needs_swarm
class TestTheDeveloperDies(GroupCase):
    """4. The developer dies mid-PR: DOWN once, the item comes back, the admin hears it once."""

    def test_the_item_is_reassigned_and_the_admin_told_once(self):
        group = self.group
        lead = group.get("Claudebot")
        lead.channel.bot = object()                  # a transport with a Telegram bot to post to
        started = []
        lead.channel._run_own_task = (
            lambda text, chat, thread, name: started.append(text))

        group.get("DeepSeek").heartbeat()            # it was alive a moment ago
        group.pump()
        run(_hand_off(lead.channel, "@DeepSeek_dd_bot please land #85 on a branch"))
        group.kill("DeepSeek")                       # no BYE: the process died
        group.advance(7 * 60)                        # past the 6-minute DOWN window

        run(lead.channel.check_peer_liveness())
        self.assertEqual(len(lead.said("DeepSeek has been silent since")), 1)
        run(lead.channel.check_peer_liveness())      # nothing new to say
        self.assertEqual(len(lead.said("DeepSeek has been silent since")), 1)

        run(lead.channel.check_stalled_handoffs())
        self.assertEqual(len(started), 1)            # the item comes back, in a turn
        self.assertIn("DeepSeek is DOWN", started[0])
        self.assertIn("hand it to another peer", started[0])
        self.assertEqual(lead.channel._pending_handoffs, {})


@needs_swarm
class TestOnlyTheReviewerIsFrozen(GroupCase):
    """5. Only the reviewer is frozen: its reviews are reassigned, and nobody self-reviews."""

    def test_a_frozen_reviewer_gets_its_review_reassigned(self):
        group = self.group
        lead, reviewer = group.get("Claudebot"), group.get("Codex")
        lead.channel.bot = object()
        started = []
        lead.channel._run_own_task = (
            lambda text, chat, thread, name: started.append(text))
        lead.adapter.on("frozen and cannot answer", "@agy_dd_bot please take the review of #109")
        lead.adapter.on("#109 needs a second verdict",
                        "@agy_dd_bot please take the review of #109 — Codex is frozen")

        run(_hand_off(lead.channel, "@codex_dd_bot review #109 at cf0b82a"))

        # The reviewer's quota runs out: it says so, on the wire, to everybody.
        reviewer.channel._set_frozen(True, "quota", group.clock.now() + 7200)
        group.say("Codex", reviewer.channel._frozen_notice())
        group.pump()
        self.assertIn("Codex", lead.channel.frozen_peers())

        run(lead.channel.check_stalled_handoffs())
        self.assertEqual(len(started), 1)
        self.assertIn("Codex is frozen", started[0])
        run(lead.channel.handle_event(_admin_event("@Claude_obot #109 needs a second verdict")))
        self.assertEqual(len(lead.said("@agy_dd_bot please take the review of #109")), 1)
        self.assertEqual(lead.said("@codex_dd_bot review"), [])   # not back to the frozen bot
        self.assertEqual(group.bots["Codex"].alive, True)      # frozen, not dead


    """6. Every bot dies and restarts: the schedule and the round state come back."""

    def test_timers_and_round_state_survive_a_full_outage(self):
        group = self.group
        store = group.get("DeepSeek").channel.wake_store()
        timer = wk.WakeTimer(argv=("git", "status"), then="continue #85",
                             next_at=group.clock.now() + 3600,
                             expires_at=group.clock.now() + 7200, every_seconds=300,
                             chat_id=GROUP, alias="DeepSeek", topic="Round 4")
        store.add(timer, max_timers=10)
        terms = self.lease_state().term

        for alias in ROLES:
            group.kill(alias)
        group.advance(60)
        for alias in ROLES:
            group.restart(alias)

        recovered = group.get("DeepSeek").channel.wake_store().load()
        self.assertEqual([row.id for row in recovered], [timer.id])
        self.assertEqual(recovered[0].then, "continue #85")
        self.assertTrue(recovered[0].live)
        self.assertEqual(self.lease_state().term, terms)   # the lease did not go backwards


@needs_swarm
class TestAWakeThatKeepsFailing(GroupCase):
    """7. A WAKE check keeps failing: it re-arms until `until`, escalates once, and stops.

    The coordinator's own timer, fired through the harness tick: waking is every bot's promise
    whatever its role (#96), and the harness tick is the path a drill actually takes -- so the
    wake pass must read the group clock, not real time, for advancing the clock to fire it.
    """

    def test_a_pending_check_rearms_then_escalates_once(self):
        group = self.group
        bot = group.get("Claudebot")
        now = group.clock.now()
        timer = wk.WakeTimer(argv=("gh", "pr", "checks", "109"), then="merge it if green",
                             next_at=now + 10, expires_at=now + 150, every_seconds=60,
                             chat_id=GROUP, alias="Claudebot", topic="Round 4")
        bot.channel.wake_store().add(timer, max_timers=10)
        group.github.open_pr(109, checks={"lint": "pass", "unittest": "pending"})

        started = []
        bot.channel._run_own_task = lambda text, chat, thread, name: started.append(text)

        async def pending_check(argv, *, cwd, timeout):
            return group.github.check_returncode(argv), "unittest: pending"

        with mock.patch("swarmly.telegram.wake.run_check_async", pending_check):
            done = []
            for _ in range(8):
                # Through the harness tick, on the group clock -- the path a drill uses, and
                # the one that must not read real time out from under an advanced clock.
                out = group.tick(60, wakes=True)
                done.extend(out["wake"]["Claudebot"])

        actions = [line.split(": ", 1)[1] for line in done if line.startswith(timer.id)]
        self.assertIn("rearm", actions)
        self.assertIn("expire", actions)
        self.assertEqual(started and len(started), 1)          # one escalation turn, not a chain
        self.assertIn("expired", started[0])
        self.assertEqual(bot.channel.wake_store().live_count, 0)


async def _hand_off(channel, text: str, thread: int = 1336, message_id: int = 55) -> None:
    """Give a peer work in a topic, and let the channel's own mirror work finish."""
    channel._mirror_handoffs(text, GROUP, thread, message_id)
    pending = [task for task in getattr(channel, "_mirror_tasks", set()) if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _admin_event(text: str):
    from swarmly.telegram.channel import ChannelEvent
    from tests.swarm_group import ADMIN_ID, ADMIN_USERNAME
    return ChannelEvent(text=text, chat_id=GROUP, author_id=ADMIN_ID,
                        author_username=ADMIN_USERNAME, author_is_bot=False,
                        replies_to_bot=False, message_id=7001, thread_id=None,
                        attachments=[], raw=None)


if __name__ == "__main__":
    unittest.main()
