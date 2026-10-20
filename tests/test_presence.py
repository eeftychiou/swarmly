"""Peer liveness (issue #82): a heartbeat, and DOWN as an advisory fact.

What these pin is what the design review asked for: DOWN means silence on *both* planes, a busy
peer is never DOWN, a restart never turns "not heard yet" into "everyone is down", and the admin
notice happens once per episode.
"""

import unittest

from swarmly.telegram import presence as pr


class TestPulse(unittest.TestCase):

    def test_round_trip(self):
        pulse = pr.Pulse(alias="Claudebot", at=1000.0, rev="abc123", role="coordinator", term=3,
                         busy_since=990.0, frozen=False)
        again = pr.Pulse.from_dict(pulse.to_dict())
        self.assertEqual((again.alias, again.at, again.term, again.busy_since),
                         ("Claudebot", 1000.0, 3, 990.0))

    def test_a_bad_pulse_is_refused(self):
        for data in ({}, {"alias": "x"}, {"alias": "x", "at": "soon"},
                     {"alias": "x", "at": 1, "term": -1}, {"alias": "", "at": 1}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                pr.Pulse.from_dict(data)


class TestDown(unittest.TestCase):

    def presence(self, *, down_after=90.0, grace=None, base=1000.0):
        return pr.Presence(base, down_after=down_after, grace=grace)

    def test_silence_for_the_window_marks_a_peer_down(self):
        table = self.presence(down_after=90.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        self.assertFalse(table.is_down("DeepSeek", 1080.0))
        self.assertTrue(table.is_down("DeepSeek", 1090.0))
        self.assertEqual(table.status("DeepSeek", 1090.0), "down")

    def test_a_pulse_brings_a_peer_back_up(self):
        table = self.presence()
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        self.assertTrue(table.is_down("DeepSeek", 1200.0))
        table.note(pr.Pulse(alias="DeepSeek", at=1200.0))
        self.assertEqual(table.status("DeepSeek", 1200.0), "up")

    def test_any_message_counts_as_liveness(self):
        """DOWN is silence on both planes: a Telegram message keeps a peer up with no pulse."""
        table = self.presence()
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        table.seen("DeepSeek", 1080.0)
        self.assertFalse(table.is_down("DeepSeek", 1080.0))

    def test_a_busy_peer_is_never_down(self):
        table = self.presence(down_after=90.0)
        for stamp in (1000.0, 1090.0, 1180.0):
            table.note(pr.Pulse(alias="DeepSeek", at=stamp, busy_since=stamp - 5))
        self.assertFalse(table.is_down("DeepSeek", 1180.0))
        self.assertEqual(table.status("DeepSeek", 1180.0), "busy")

    def test_a_peer_never_heard_from_is_unknown_not_down(self):
        table = self.presence()
        self.assertFalse(table.is_down("Codex", 5000.0))
        self.assertEqual(table.status("Codex", 5000.0), "unknown")

    def test_a_restart_gets_a_grace_period(self):
        table = self.presence(down_after=90.0, base=10_000.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))     # a stale last-seen from a file
        self.assertFalse(table.is_down("DeepSeek", 10_010.0))
        self.assertTrue(table.is_down("DeepSeek", 10_090.0))


class TestOperatorView(unittest.TestCase):

    def test_the_status_line_says_how_long(self):
        table = pr.Presence(1000.0, down_after=90.0)
        table.note(pr.Pulse(alias="Claudebot", at=1100.0))
        table.note(pr.Pulse(alias="DeepSeek", at=400.0))
        line = table.line(1120.0, order=["Claudebot", "DeepSeek"])
        self.assertIn("Claudebot:up", line)
        self.assertIn("DeepSeek:down(", line)

    def test_busy_duration_is_visible(self):
        table = pr.Presence(1000.0, down_after=90.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1180.0, busy_since=1000.0))
        self.assertIn("DeepSeek:busy(3m)", table.line(1180.0))

    def test_the_admin_notice_fires_once_per_episode(self):
        table = pr.Presence(1000.0, down_after=90.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        self.assertTrue(table.mark_notified("DeepSeek", 1100.0))
        self.assertFalse(table.mark_notified("DeepSeek", 1200.0))
        # ... and a peer that comes back and goes quiet again is a new episode.
        table.note(pr.Pulse(alias="DeepSeek", at=1300.0))
        self.assertTrue(table.mark_notified("DeepSeek", 1500.0))
