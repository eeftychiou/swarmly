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

    def test_armed_wakes_are_listed_with_their_next_fire(self):
        """The timers this bot has promised to fire, with when: read from its own store."""
        from swarmly.telegram import wake as wk

        channel = live_channel()
        live = wk.WakeTimer(argv=("git", "status"), then="continue", next_at=NOW + 600,
                            expires_at=NOW + 3600, every_seconds=300, chat_id=GROUP,
                            alias="Hermes")
        done = wk.WakeTimer(argv=("git", "status"), then="done", next_at=NOW - 60,
                            expires_at=NOW + 3600, every_seconds=0, chat_id=GROUP,
                            alias="Hermes")
        done.state = "done"

        class Store:
            def load(self):
                return [live, done]

        channel.wake_store = lambda: Store()          # a store of its own: nothing is written
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.wakes, ((live.id, NOW + 600),))

    def test_the_lease_fields_appear_only_for_the_holder(self):
        channel = live_channel()
        snap = channel.status_snapshot(NOW)
        self.assertEqual((snap.term, snap.lease_ends), (0, 0.0))

    def test_uptime_and_restarts_come_from_the_process(self):
        channel = live_channel()
        channel._booted_at = channel._clock() - 7200
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.uptime, 7200.0)
        self.assertGreaterEqual(snap.restarts, 0)


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
