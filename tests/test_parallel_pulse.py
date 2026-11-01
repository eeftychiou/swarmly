"""A bot running two topic turns at once reports both (#488).

With ``limits.max_parallel_turns`` above 1 the pulse snapshot used to describe one turn: the
first it found. What these pin: every running turn is listed with its own clock and silence, a
stuck turn beside a healthy one makes the bot STUCK, the dashboard draws a row per turn, an
older peer's record (single fields only) still renders, and ``/topics`` names who is working
where.
"""

from __future__ import annotations

import time
import unittest

from swarmly.telegram import snapshot as sn
from swarmly.telegram import webui
from swarmly.telegram.registry_shadow import RegistryShadow
from tests.test_snapshot import GROUP, NOW, live_channel
from tests import test_topics_filters as filters_tests
from tests.test_registry_shadow import Clock, channel_for


def two_turns(channel, *, quiet_307=0.0, quiet_308=0.0):
    """Topic 307 running 300 s and topic 308 running 120 s, each with its own output clock."""
    channel._busy_since[(GROUP, 307)] = channel._clock() - 300
    channel._busy_since[(GROUP, 308)] = channel._clock() - 120
    heard = {"tg-307": time.monotonic() - quiet_307, "tg-308": time.monotonic() - quiet_308}
    channel.adapter.last_output_at = lambda key=None: heard.get(key)
    return channel


class TestTheSnapshotListsEveryTurn(unittest.TestCase):

    def test_both_topics_are_listed_each_with_its_own_elapsed_time(self):
        snap = two_turns(live_channel()).status_snapshot(NOW)
        self.assertEqual(snap.state, sn.BUSY)
        self.assertEqual([(t, e) for t, e, _ in snap.turns],
                         [("tg-307", 300.0), ("tg-308", 120.0)])
        # The single fields an older peer reads stay filled: the longest turn.
        self.assertEqual((snap.topic, snap.turn_elapsed), ("tg-307", 300.0))
        self.assertIn("2 turns: tg-307 5m, tg-308 2m", snap.line(NOW))

    def test_a_stuck_turn_beside_a_healthy_one_is_stuck(self):
        """The hung turn is the *second* one: it must not hide behind the healthy first."""
        snap = two_turns(live_channel(turn_silence_minutes=1),
                         quiet_308=600).status_snapshot(NOW)
        self.assertEqual(snap.state, sn.STUCK)
        # The single fields name the stuck turn, so an older reader sees the right one.
        self.assertEqual(snap.topic, "tg-308")
        self.assertAlmostEqual(snap.silent, 120, delta=2)    # silent since it started
        quiet = dict((t, s) for t, _, s in snap.turns)
        self.assertLess(quiet["tg-307"], 2)

    def test_an_idle_bot_lists_no_turns(self):
        self.assertEqual(live_channel().status_snapshot(NOW).turns, ())

    def test_the_list_round_trips_and_carries_no_identifier(self):
        snap = two_turns(live_channel()).status_snapshot(NOW)
        record_ = snap.to_dict()
        self.assertEqual(sn.Snapshot.from_dict(record_).turns, snap.turns)
        for row in record_["turns"]:
            self.assertEqual(set(row), {"topic", "elapsed", "silent"})
            self.assertTrue(row["topic"].startswith("tg-"), row)
            self.assertNotIn(str(GROUP), row["topic"])

    def test_an_older_peers_record_reads_as_no_list(self):
        old = {"alias": "Codex", "state": "busy", "at": NOW, "every": 120.0,
               "topic": "tg-307", "turn_elapsed": 45.0, "silent": 3.0}
        snap = sn.Snapshot.from_dict(old)
        self.assertEqual(snap.turns, ())
        self.assertIn("tg-307", snap.line(NOW))

    def test_a_malformed_list_is_dropped_row_by_row(self):
        data = {"alias": "Codex", "state": "busy", "at": NOW,
                "turns": ["tg-1", {"topic": "tg-2", "elapsed": "x", "silent": 1.0},
                          *({"topic": f"tg-{i}"} for i in range(20))]}
        turns = sn.Snapshot.from_dict(data).turns
        self.assertEqual(turns[0], ("tg-2", 0.0, 1.0))
        self.assertEqual(len(turns), sn.MAX_TURNS)


class TestTheDashboardDrawsARowPerTurn(unittest.TestCase):

    def test_two_busy_topics_are_two_rows_with_the_queue(self):
        snap = two_turns(live_channel()).status_snapshot(NOW)
        snap = sn.Snapshot.from_dict({**snap.to_dict(), "queue": 2})
        card = webui.bot_cards(now=NOW, snapshots=[snap])[0]
        self.assertEqual([(row["topic"], row["seconds"]) for row in card["running"]],
                         [("tg-307", 300.0), ("tg-308", 120.0)])
        self.assertEqual(card["queue"], 2)

    def test_an_older_peers_record_still_renders_its_one_turn(self):
        old = sn.Snapshot.from_dict({"alias": "Codex", "state": "busy", "at": NOW,
                                     "every": 120.0, "topic": "tg-307", "turn_elapsed": 45.0})
        card = webui.bot_cards(now=NOW, snapshots=[old])[0]
        self.assertEqual(card["running"], [{"topic": "tg-307", "seconds": 45.0, "silent": 0.0}])

    def test_an_idle_bot_has_no_rows(self):
        idle = sn.Snapshot(alias="Codex", state=sn.IDLE, at=NOW, every=120.0)
        self.assertEqual(webui.bot_cards(now=NOW, snapshots=[idle])[0]["running"], [])

    def test_the_card_draws_one_line_per_running_turn(self):
        """The page's own script reads `running`, and a busy bot is told by its badge."""
        page = webui._PAGE
        self.assertIn("bot.running", page)
        self.assertIn('bot.badge === "working"', page)


class TestSlashTopicsNamesWhoIsWorking(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.channel = channel_for("Claudebot", self.clock)
        self.shadow = RegistryShadow(self.channel, clock=self.clock)

    def test_a_bot_with_two_turns_is_named_under_both_topics(self):
        now = time.time()
        rows = [sn.Snapshot(alias="Codex", state=sn.BUSY, at=now, every=120.0, topic="tg-11",
                            turns=(("tg-11", 30.0, 1.0), ("tg-12", 10.0, 1.0))),
                sn.Snapshot(alias="DeepSeek", state=sn.BUSY, at=now, every=120.0,
                            topic="tg-12"),
                sn.Snapshot(alias="Antigravity", state=sn.IDLE, at=now, every=120.0)]
        self.channel.presence_clock = lambda: now
        self.channel.control_snapshots = lambda moment=None: rows
        store = filters_tests.TestSlashTopicsFilters.store(
            self, filters_tests.record(11, "first"), filters_tests.record(12, "second"))
        text = self.shadow.describe(store)
        first = next(line for line in text.splitlines() if "first" in line)
        second = next(line for line in text.splitlines() if "second" in line)
        self.assertIn("working: Codex", first)
        self.assertNotIn("DeepSeek", first)
        self.assertIn("working: Codex, DeepSeek", second)
        self.assertNotIn("Antigravity", text)

    def test_no_pulses_lists_the_topics_as_before(self):
        self.channel.control_snapshots = lambda moment=None: 1 / 0
        store = filters_tests.TestSlashTopicsFilters.store(self, filters_tests.record(11, "first"))
        text = self.shadow.describe(store)
        self.assertIn("first", text)
        self.assertNotIn("working:", text)


if __name__ == "__main__":
    unittest.main()
