"""The coordinator's watchdog: an idle coordinator with no WAKE gets one turn to check the round.

Live, round 4: a hand-off to the coordinator was lost in a restart, nothing else could wake
it, and the swarm waited all night.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from swarmly.telegram import wake as wk
from tests.test_telegram_channel import GROUP, make_channel, make_config

NOW = 1_800_000_000.0


class CoordinatorWatchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def channel(self, coordinator=True, **limits):
        config = make_config(fallback={"gitbus_root": self.tmp.name}, limits=limits)
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel.is_coordinator = lambda now=None: coordinator
        channel.wake_store = lambda: wk.WakeStore(
            channel.config.identity.alias, state_dir=Path(self.tmp.name) / "wakes",
            transport=None, root=Path(self.tmp.name), clock=time.time)
        self.tasks = []
        channel._run_own_task = lambda text, chat, thread, name: self.tasks.append(
            (text, chat, thread))
        return channel

    def last_turn(self, channel, at, thread_id=1336):
        channel._watch = {"chat_id": GROUP, "thread_id": thread_id, "topic": "Round 4",
                          "at": at, "backoff": 1}

    def test_an_idle_coordinator_checks_the_round_in_its_last_topic(self):
        channel = self.channel()
        self.last_turn(channel, NOW)
        self.assertIsNone(channel.check_coordinator_watch(now=NOW + 29 * 60))
        self.assertEqual(channel.check_coordinator_watch(now=NOW + 31 * 60), "checked")
        text, chat, thread = self.tasks[0]
        self.assertTrue(text.startswith("Coordinator check:"))
        self.assertIn("NO_REPLY", text)
        self.assertEqual((chat, thread), (GROUP, 1336))

    def test_quiet_checks_back_off(self):
        channel = self.channel()
        self.last_turn(channel, NOW)
        channel.check_coordinator_watch(now=NOW + 31 * 60)
        self.assertIsNone(channel.check_coordinator_watch(now=NOW + 31 * 60 + 59 * 60))
        self.assertEqual(channel.check_coordinator_watch(now=NOW + 31 * 60 + 61 * 60), "checked")

    def test_another_turn_resets_the_back_off(self):
        channel = self.channel()
        self.last_turn(channel, NOW)
        channel.check_coordinator_watch(now=NOW + 31 * 60)
        from swarmly.telegram.channel import ChannelEvent
        channel._note_turn_for_watch(ChannelEvent(text="@HermesBot go", chat_id=GROUP,
                                                  thread_id=1336, message_id=9))
        self.assertEqual(channel._watch["backoff"], 1)

    def test_not_the_coordinator_never_checks(self):
        channel = self.channel(coordinator=False)
        self.last_turn(channel, NOW)
        self.assertIsNone(channel.check_coordinator_watch(now=NOW + 120 * 60))

    def test_an_armed_wake_is_enough(self):
        channel = self.channel()
        self.last_turn(channel, NOW)
        channel.wake_store().add(wk.WakeTimer(
            argv=("gh", "pr", "checks", "94"), then="merge it", next_at=NOW + 9999,
            expires_at=time.time() + 3600, every_seconds=60, chat_id=GROUP,
            alias=channel.config.identity.alias), max_timers=10)
        self.assertIsNone(channel.check_coordinator_watch(now=NOW + 120 * 60))

    def test_a_swarm_idle_for_a_day_is_left_alone(self):
        channel = self.channel()
        self.last_turn(channel, NOW)
        self.assertIsNone(channel.check_coordinator_watch(now=NOW + 25 * 3600))

    def test_off_with_zero(self):
        channel = self.channel(coordinator_watch_minutes=0)
        self.last_turn(channel, NOW)
        self.assertIsNone(channel.check_coordinator_watch(now=NOW + 120 * 60))

    def test_the_last_turn_survives_a_restart(self):
        channel = self.channel()
        from swarmly.telegram.channel import ChannelEvent
        channel._note_turn_for_watch(ChannelEvent(text="@HermesBot go", chat_id=GROUP,
                                                  thread_id=1336, message_id=9))
        restarted = self.channel()
        self.assertEqual(restarted._watch["thread_id"], 1336)


if __name__ == "__main__":
    unittest.main()
