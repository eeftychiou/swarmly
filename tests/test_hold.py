"""/hold and /release: the swarm answers but starts nothing, across restarts (admin, 23 Oct).

Live, 23 Oct: the admin held round 5.5 with a chat message; the coordinator restarted into a
fresh session, the message was gone, and the round started again. A hold is state now.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run


class HoldTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def channel(self, coordinator=True):
        config = make_config(fallback={"gitbus_root": self.tmp.name},
                             limits={})
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel.is_coordinator = lambda now=None: coordinator
        self.tasks = []
        channel._run_own_task = lambda text, chat, thread, name: self.tasks.append(text)
        return channel

    def admin(self, text):
        return event(text, author_id=ADMIN_ID, username="alice", is_bot=False)

    def test_the_admin_holds_and_releases_and_only_the_coordinator_says_so(self):
        coordinator, peer = self.channel(), self.channel(coordinator=False)
        said = coordinator._handle_hold(self.admin("/hold until the drill is done"))
        self.assertIn("on hold: until the drill is done", said)
        self.assertEqual(peer._handle_hold(self.admin("/hold until the drill is done")), "")
        self.assertTrue(coordinator.held() and peer.held())
        self.assertIn("until the drill is done", coordinator._hold_note())
        self.assertIn("released", coordinator._handle_hold(self.admin("/release")))
        self.assertFalse(coordinator.held())
        self.assertEqual(coordinator._hold_note(), "")

    def test_only_the_admin_holds(self):
        channel = self.channel()
        self.assertEqual(channel._handle_hold(event("/hold", author_id=77, is_bot=True)), "")
        self.assertFalse(channel.held())
        self.assertIsNone(channel._handle_hold(self.admin("/holdings")))

    def test_a_hold_survives_a_restart(self):
        self.channel()._handle_hold(self.admin("/hold"))
        self.assertTrue(self.channel().held())
        self.channel()._handle_hold(self.admin("/release"))
        self.assertFalse(self.channel().held())

    def test_held_nothing_makes_work(self):
        channel = self.channel()
        channel._handle_hold(self.admin("/hold"))
        self.assertEqual(channel.owed_guard_tick(), [])
        self.assertEqual(channel.owed_wake_tick(),
                         ["held: no owed wake while the swarm is on hold"])
        self.assertEqual(self.tasks, [])


if __name__ == "__main__":
    unittest.main()
