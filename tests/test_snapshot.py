"""The per-bot status snapshot a heartbeat carries (issue #124).

What these pin: the record says what the bot is doing and what is waiting, it is taken in code
with no adapter call, it carries no identifier a bot must not publish, and a snapshot that
stopped being renewed is reported as stale instead of looking current.
"""

import asyncio
import json
import re
import unittest

from swarmly.telegram import snapshot as sn
from swarmly.telegram.presence import Peer, Presence, Pulse
from swarmly.telegram.redact import Redactor
from tests.test_telegram_channel import (ADMIN_ID, GROUP, event, make_channel, make_config, run)

NOW = 1_800_000_000.0

#: Anything that would be an identifier in the record: a chat id, a bot token, a path.
IDENTIFIERS = re.compile(r"\d{4,}|:\w+$|[A-Za-z]:\\|/(?:home|Users|tmp)/|token|secret", re.I)


def live_channel(**limits):
    """A channel whose clocks a test moves: presence time, the monotonic clock, the wall."""
    channel = make_channel(make_config(limits={"heartbeat_minutes": 2, "peer_down_after": 3,
                                               **limits}))
    wall = {"t": NOW}
    mono = {"t": NOW}
    channel.presence_clock = lambda: wall["t"]
    channel._wall = lambda: wall["t"]
    channel._clock = lambda: mono["t"]
    channel._test_wall, channel._test_mono = wall, mono
    return channel


class CountingAdapter:
    """Wraps the channel's adapter and records every prompt: a snapshot must give it none."""

    def __init__(self, inner):
        self.inner = inner
        self.turns = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def respond(self, message):
        self.turns.append(message.text)
        return await self.inner.respond(message)


class TestTheRecord(unittest.TestCase):

    def test_it_round_trips(self):
        snap = sn.Snapshot(alias="DeepSeek", state=sn.BUSY, since=1000.0, at=1010.0, every=120.0,
                           topic="tg-307", turn_elapsed=10.0, turn_timeout=900.0, queue=2,
                           wakes=(("abc", 2000.0),), model="sonnet", effort="high",
                           rev="abc1234", uptime=500.0, restarts=1, term=3, lease_ends=1600.0)
        again = sn.Snapshot.from_dict(snap.to_dict())
        self.assertEqual(again, snap)

    def test_a_malformed_record_is_refused(self):
        for data in ({}, {"alias": "x"}, {"alias": "x", "state": "sleeping"},
                     {"alias": "", "state": "idle"}, [], "idle"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                sn.Snapshot.from_dict(data)

    def test_a_fresh_snapshot_is_not_stale_and_an_old_one_is(self):
        snap = sn.Snapshot(alias="DeepSeek", state=sn.IDLE, at=NOW, every=120.0)
        self.assertFalse(snap.is_stale(NOW + 100))
        self.assertTrue(snap.is_stale(NOW + 300))            # more than two missed beats
        self.assertEqual(snap.age(NOW + 300), 300.0)

    def test_a_channel_with_no_heartbeat_is_never_stale(self):
        snap = sn.Snapshot(alias="DeepSeek", state=sn.IDLE, at=NOW, every=0.0)
        self.assertFalse(snap.is_stale(NOW + 10_000))        # nothing to be late for

    def test_the_line_says_state_and_age(self):
        snap = sn.Snapshot(alias="DeepSeek", state=sn.BUSY, since=NOW - 600, at=NOW - 30,
                           every=120.0, topic="tg-307", turn_elapsed=600.0, turn_timeout=900.0,
                           queue=1, wakes=(("abc", NOW + 60),), term=3)
        line = snap.line(NOW)
        self.assertIn("DeepSeek:busy", line)
        self.assertIn("tg-307", line)
        self.assertIn("queue=1", line)
        self.assertIn("wakes=1", line)
        self.assertIn("term=3", line)
        self.assertIn("age=30s", line)

    def test_the_freshest_snapshot_wins(self):
        older = sn.Snapshot(alias="Codex", state=sn.IDLE, at=NOW - 500)
        newer = sn.Snapshot(alias="Codex", state=sn.BUSY, at=NOW)
        self.assertIs(sn.freshest([older, newer], "codex"), newer)
        self.assertIsNone(sn.freshest([older, newer], "Reasonix"))


class TestOnTheChannel(unittest.TestCase):

    def test_an_idle_bot_reports_idle(self):
        channel = live_channel()
        snap = channel.status_snapshot(NOW)
        self.assertEqual((snap.alias, snap.state), ("Hermes", sn.IDLE))
        self.assertEqual(snap.topic, "general")
        self.assertEqual(snap.every, 120.0)
        self.assertEqual(snap.queue, 0)

    def test_a_frozen_bot_says_frozen(self):
        channel = live_channel()
        channel._set_frozen(True, "quota", NOW + 600)
        self.assertEqual(channel.status_snapshot(NOW).state, sn.FROZEN)

    def test_a_paused_bot_says_paused(self):
        channel = live_channel()
        channel._pause_pending = True
        self.assertEqual(channel.status_snapshot(NOW).state, sn.PAUSED)

    def test_a_running_turn_is_busy_with_its_topic_and_elapsed_time(self):
        channel = live_channel()
        channel._busy_since[(GROUP, 307)] = channel._clock() - 45
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.state, sn.BUSY)
        self.assertEqual(snap.turn_elapsed, 45.0)
        self.assertTrue(snap.topic.startswith("tg-307"), snap.topic)
        self.assertEqual(snap.since, NOW - 45)

    def test_a_turn_past_its_timeout_is_stuck(self):
        channel = live_channel()
        channel.config.adapter.timeout_seconds = 60
        channel._busy_since[(GROUP, 307)] = channel._clock() - 120
        self.assertEqual(channel.status_snapshot(NOW).state, sn.STUCK)

    def test_a_waiting_queue_is_counted(self):
        channel = live_channel()
        channel._busy_waiting = 3
        channel._cooldown_waiting[(GROUP, 307)] = 2
        self.assertEqual(channel.status_snapshot(NOW).queue, 5)

    def test_no_timers_are_reported_because_none_exist(self):
        """#215: the snapshot keeps its `wakes` field for older readers, and it is always empty."""
        channel = live_channel()
        self.assertEqual(channel.status_snapshot(NOW).wakes, ())
    def test_a_bot_without_the_lease_reports_no_term(self):
        channel = live_channel()
        snap = channel.status_snapshot(NOW)
        self.assertEqual((snap.term, snap.lease_ends), (0, 0.0))

    def test_the_holder_reports_its_term_and_expiry(self):
        """#124 review: the lease fields were only ever tested for the *non*-holder."""
        from swarmly.telegram.lease import LeaseState

        channel = live_channel()
        channel.lease_state = lambda now=None: LeaseState(
            status="ok", term=4, holder=channel.config.identity.alias, expires_at=NOW + 540.0)
        snap = channel.status_snapshot(NOW)
        self.assertEqual((snap.term, snap.lease_ends), (4, NOW + 540.0))
        self.assertIn("term=4", snap.line(NOW))

    def test_a_lease_held_by_someone_else_is_not_this_bots_term(self):
        from swarmly.telegram.lease import LeaseState

        channel = live_channel()
        channel.lease_state = lambda now=None: LeaseState(
            status="ok", term=4, holder="Claudebot", expires_at=NOW + 540.0)
        self.assertEqual(channel.status_snapshot(NOW).term, 0)

    def test_effort_resolution_matches_online_notice(self):
        """Issue #166: Reasonix and Antigravity fill effort from the same source online_notice uses."""
        # Antigravity defaults to "the model default" when effort is unset
        agy_chan = make_channel(make_config(adapter={"type": "antigravity"}))
        snap = agy_chan.status_snapshot(NOW)
        self.assertEqual(snap.effort, "the model default")

        # Antigravity with tiered model extracts effort from model name
        agy_tiered = make_channel(make_config(adapter={"type": "antigravity", "model": "Gemini 3.1 Pro (High)"}))
        snap_tiered = agy_tiered.status_snapshot(NOW)
        self.assertEqual(snap_tiered.effort, "high")

        # Reasonix defaults to "the model default" when effort is unset
        reasonix_chan = make_channel(make_config(adapter={"type": "reasonix", "model": "deepseek-reasoner"}))
        self.assertEqual(reasonix_chan.status_snapshot(NOW).effort, "the model default")

        # Echo adapter has no effort support, so effort stays blank
        echo_chan = make_channel(make_config(adapter={"type": "echo"}))
        self.assertEqual(echo_chan.status_snapshot(NOW).effort, "")

    def test_uptime_and_restarts_come_from_the_process(self):
        channel = live_channel()
        channel._booted_at = channel._clock() - 7200
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.uptime, 7200.0)
        self.assertGreaterEqual(snap.restarts, 0)

    def test_status_snapshot_reports_drift_and_round_trips_safely(self):
        """#452: status_snapshot reports drift score, standing, and kind without evidence/keys."""
        channel = live_channel()
        channel.record_violation("merge_gate_refused",
                                 r"secret path C:\Users\eftyc\secret\notes and chat -100123456789",
                                 chat_id=-100123456789, thread_id=19286)
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.drift_score, 3)
        self.assertEqual(snap.drift_standing, "a reminder rides the next turn")
        self.assertEqual(snap.drift_kind, "merge_gate_refused")
        self.assertIsNotNone(snap.drift_age)

        # Round-trip through dict
        data = snap.to_dict()
        again = sn.Snapshot.from_dict(data)
        self.assertEqual(again.drift_score, snap.drift_score)
        self.assertEqual(again.drift_hard, snap.drift_hard)
        self.assertEqual(again.drift_resets_today, snap.drift_resets_today)
        self.assertEqual(again.drift_standing, snap.drift_standing)
        self.assertEqual(again.drift_kind, snap.drift_kind)
        self.assertEqual(again.drift_age, snap.drift_age)
        self.assertEqual(again, snap)

        # Safety: neither evidence nor session keys leak (#349)
        payload = json.dumps(data)
        self.assertNotIn("Users", payload)
        self.assertNotIn("notes", payload)
        self.assertNotIn("100123456789", payload)
        self.assertNotIn("19286", payload)


class TestWhenTheStateBegan(unittest.TestCase):
    """#124 review: `since` is when the state began, and it holds across beats.

    A snapshot is taken every heartbeat. If `since` were "now" each time, a bot paused for an
    hour would report `for 0s` for ever, and a frozen bot reported the *expiry* of its freeze as
    the moment it began.
    """

    def beats(self, channel, count=3, every=120.0):
        """Take *count* heartbeats, moving the presence clock between them."""
        snaps = []
        for _ in range(count):
            channel._test_wall["t"] += every
            snaps.append(channel.status_snapshot())
        return snaps

    def test_idle_since_holds_across_beats(self):
        channel = live_channel()
        started = channel.status_snapshot().at                # the first beat in this state
        snaps = self.beats(channel)
        self.assertEqual([snap.since for snap in snaps], [started] * len(snaps))
        self.assertEqual(snaps[-1].state, sn.IDLE)
        self.assertEqual(snaps[-1].age(snaps[-1].at), 0.0)
        # ...and the line says how long, which is the number a reader actually wants.
        self.assertIn("for 6m", snaps[-1].line(snaps[-1].at))   # three 2m beats

    def test_paused_since_holds_across_beats(self):
        channel = live_channel()
        channel._pause_pending = True                     # /pause, at this moment
        channel._note_state(paused=True)
        paused_at = channel._test_wall["t"]
        snaps = self.beats(channel)
        self.assertEqual({snap.state for snap in snaps}, {sn.PAUSED})
        self.assertEqual({snap.since for snap in snaps}, {paused_at})

    def test_frozen_since_is_when_the_freeze_began_not_when_it_ends(self):
        channel = live_channel()
        channel._set_frozen(True, "quota", NOW + 7200)    # the expiry is two hours away
        frozen_at = channel._test_wall["t"]
        snap = channel.status_snapshot()
        self.assertEqual(snap.state, sn.FROZEN)
        self.assertEqual(snap.since, frozen_at)
        self.assertLess(snap.since, NOW + 7200)           # never the expiry
        later = self.beats(channel)[-1]
        self.assertEqual(later.since, frozen_at)          # and it does not drift per beat

    def test_a_resumed_bot_is_idle_since_it_resumed(self):
        channel = live_channel()
        channel._set_frozen(True, "pause", None)
        channel._test_wall["t"] += 600
        channel._set_frozen(False, None)
        resumed_at = channel._test_wall["t"]
        snap = channel.status_snapshot()
        self.assertEqual(snap.state, sn.IDLE)
        self.assertEqual(snap.since, resumed_at)

    def test_a_turn_that_ends_restarts_the_idle_clock(self):
        channel = live_channel()
        channel._busy_since[(GROUP, 307)] = channel._clock() - 30
        busy = channel.status_snapshot()
        self.assertEqual(busy.state, sn.BUSY)
        self.assertEqual(busy.since, busy.at - 30)        # the turn's own start
        channel._test_wall["t"] += 30
        channel._busy_since.clear()
        channel._note_state(busy=False)                   # the turn finished
        idle = channel.status_snapshot()
        self.assertEqual(idle.state, sn.IDLE)
        self.assertEqual(idle.since, channel._test_wall["t"])


class TestNoTurnAndNoIdentifiers(unittest.TestCase):
    """The acceptance criteria: no adapter call, and nothing a bot must not publish."""

    def test_the_heartbeat_takes_no_model_turn(self):
        channel = live_channel()
        counting = CountingAdapter(channel.adapter)
        channel.adapter = counting
        run(channel._heartbeat_tick(now=NOW))
        self.assertEqual(counting.turns, [])

    def test_the_record_carries_no_chat_id_token_or_path(self):
        channel = live_channel()
        channel._busy_since[(GROUP, 307)] = channel._clock() - 5
        record = channel.status_snapshot(NOW).to_dict()
        blob = json.dumps(record, sort_keys=True)
        self.assertNotIn(str(GROUP), blob)                   # no chat id
        # The topic is its discussion token (`tg-<thread>`, the #22 convention), not an id the
        # record would otherwise have to carry.
        self.assertRegex(record["topic"], r"^(general|tg-[0-9a-fA-F-]+)$")
        for value in (record["alias"], record["model"]):
            self.assertIsNone(IDENTIFIERS.search(value), value)
        # `rev` is the revision a bot is running: a sha or "unknown", as /diag already says.
        self.assertRegex(record["rev"], r"^([0-9a-f]{7,40}|unknown)?$")

    def test_the_redactor_has_nothing_to_remove(self):
        """The record is already public-shaped: what the redactor would hide, it does not hold."""
        channel = live_channel()
        channel.config.adapter.cwd = "C:/Users/eftyc/swarm/pr3-deepseek"
        blob = json.dumps(channel.status_snapshot(NOW).to_dict(), sort_keys=True)
        self.assertEqual(Redactor.from_config(channel.config).text(blob), blob)

    def test_a_broken_lease_still_yields_a_snapshot(self):
        channel = live_channel()

        def boom(*args, **kwargs):
            raise RuntimeError("the bus is gone")

        channel.lease_state = boom
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.state, sn.IDLE)
        self.assertEqual(snap.term, 0)


class TestOnThePulse(unittest.TestCase):
    """The snapshot rides the heartbeat, so a peer that missed a beat can still render it."""

    def test_the_pulse_carries_the_snapshot(self):
        channel = live_channel()
        pulse = channel.own_pulse(NOW)
        self.assertIsNotNone(pulse.status)
        self.assertEqual(pulse.status["state"], sn.IDLE)
        self.assertEqual(pulse.status["alias"], "Hermes")
        self.assertEqual(sn.Snapshot.from_dict(pulse.status).every, 120.0)

    def test_a_peer_keeps_the_snapshot_it_was_sent(self):
        table = Presence(NOW, down_after=360)
        pulse = Pulse(alias="Codex", at=NOW,
                      status=sn.Snapshot(alias="Codex", state=sn.BUSY, at=NOW,
                                         every=120).to_dict())
        peer = table.note(pulse)
        self.assertEqual(peer.snapshot["state"], sn.BUSY)

    def test_an_older_beat_does_not_overwrite_a_newer_record(self):
        table = Presence(NOW, down_after=360)
        newer = Pulse(alias="Codex", at=NOW,
                      status=sn.Snapshot(alias="Codex", state=sn.BUSY, at=NOW).to_dict())
        older = Pulse(alias="Codex", at=NOW - 500,
                      status=sn.Snapshot(alias="Codex", state=sn.IDLE, at=NOW - 500).to_dict())
        table.note(newer)
        peer = table.note(older)
        self.assertEqual(peer.snapshot["state"], sn.BUSY)

    def test_a_pulse_without_a_snapshot_is_still_a_pulse(self):
        """An older build's beat is not refused: it carries liveness and no record."""
        table = Presence(NOW, down_after=360)
        peer = table.note(Pulse(alias="Codex", at=NOW))
        self.assertIsNone(peer.snapshot)
        self.assertEqual(table.status("Codex", NOW), "up")


class TestAcrossARestart(unittest.TestCase):

    def test_the_peer_record_keeps_its_snapshot(self):
        peer = Peer(alias="Codex", last_seen=NOW,
                    snapshot={"alias": "Codex", "state": "busy", "at": NOW})
        again = Peer.from_dict(peer.to_dict())
        self.assertEqual(again.snapshot, peer.snapshot)


if __name__ == "__main__":
    unittest.main()
