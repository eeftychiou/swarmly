"""/hold and /release: the swarm answers but starts nothing, across restarts (admin, 23 Oct).

Live, 23 Oct: the admin held round 5.5 with a chat message; the coordinator restarted into a
fresh session, the message was gone, and the round started again. A hold is state now.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from swarmly.telegram import presence as pr
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run

#: A fixed presence clock: the reconciliation compares stamps, never the wall clock.
NOW = 1_800_000_000.0


def pulse_line(pulse: pr.Pulse) -> str:
    """One ``STATUS intent=PULSE`` line, as a peer publishes it on Telegram."""
    return (f"[A2A v1.1] STATUS to=* intent=PULSE ref={pulse.alias} rev=abc1234\n\n"
            + json.dumps(pulse.to_dict()))


class FakePulseBus:
    """A pulse ref that holds what a peer wrote, without git (the beat needs only read)."""

    def __init__(self, pulses=None):
        self.pulses = dict(pulses or {})
        self.published = []

    def publish(self, pulse):
        self.published.append(pulse)
        return True

    def read(self):
        return dict(self.pulses)


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

    def test_a_named_hold_still_holds_every_bot(self):
        """#218 review (Reasonix): /hold@bot picks who answers, not who is held."""
        channel = self.channel(coordinator=False)
        self.assertEqual(channel._handle_hold(self.admin("/hold@OtherBot drill")), "")
        self.assertTrue(channel.held())
        self.assertIn("on hold", channel._handle_hold(self.admin("/hold@HermesBot drill")))
        self.assertEqual(channel._handle_hold(self.admin("/release@OtherBot")), "")
        self.assertFalse(channel.held())

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
        # The tick's own result, read only by tests (the caller drops it): not a notice.
        self.assertEqual(channel.owed_wake_tick(),
                         ["held: no owed wake while the swarm is on hold"])
        self.assertEqual(self.tasks, [])


class TestTheHoldOnThePulse(unittest.TestCase):
    """#234: the hold rides the pulse, so a bot that missed the command still obeys it.

    Live, round 5.5: `/hold` reached four bots and nothing else. A bot that was down at that
    moment -- or that joined after it -- started work while the rest of the swarm was held, and
    no machine line said so. The beat now carries the hold, and the release carries its time.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wall = {"t": 1_700_000_000.0}

    def channel(self, *, plane="telegram", bus=None):
        limits = {"heartbeat_plane": plane} if plane == "bus" else {}
        config = make_config(fallback={"gitbus_root": self.tmp.name}, limits=limits)
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel.is_coordinator = lambda now=None: False
        channel._run_own_task = lambda *args, **kwargs: None
        channel.presence_clock = lambda: NOW
        channel._wall = lambda: self.wall["t"]
        if bus is not None:
            channel._pulsebus = bus
            channel._pulsebus_checked = True
        return channel

    def admin(self, text):
        return event(text, author_id=ADMIN_ID, username="alice", is_bot=False)

    def hear(self, channel, pulse, alias="Codex", username="CodexBot"):
        run(channel.handle_event(event(pulse_line(pulse), username=username, message_id=1)))

    # -- the two halves of the acceptance ---------------------------------------------
    def test_a_bot_started_after_the_hold_is_held_within_one_pulse(self):
        held = {"at": NOW - 600, "reason": "drill"}
        bus = FakePulseBus({"Codex": pr.Pulse(alias="Codex", at=NOW, held=held).to_dict()})
        channel = self.channel(plane="bus", bus=bus)
        self.assertFalse(channel.held())                    # it was down for the /hold
        run(channel._heartbeat_tick())
        self.assertTrue(channel.held())
        self.assertIn("drill", channel._hold_note())

    def test_a_bot_that_missed_the_release_is_released_within_one_pulse(self):
        channel = self.channel()
        channel._handle_hold(self.admin("/hold drill"))
        self.assertTrue(channel.held())
        released = pr.Pulse(alias="Codex", at=NOW,
                            released_at=channel._hold_action_at() + 60)
        self.hear(channel, released)
        self.assertFalse(channel.held())
        self.assertEqual(channel._hold_note(), "")

    def test_an_older_hold_never_overrides_a_newer_release(self):
        channel = self.channel()
        channel._handle_hold(self.admin("/hold drill"))
        channel._handle_hold(self.admin("/release"))
        stamp = channel._hold_action_at()
        self.assertEqual(channel._adopt_hold({"alias": "Codex", "at": NOW,
                                              "held": {"at": stamp - 5, "reason": "drill"}}),
                         "")
        self.assertFalse(channel.held())

    # -- what makes the reconciliation safe ------------------------------------------
    def test_my_beat_carries_the_hold_and_then_the_release(self):
        channel = self.channel()
        channel._handle_hold(self.admin("/hold drill"))
        beat = channel.own_pulse(NOW).to_dict()
        self.assertEqual(beat["held"], {"at": self.wall["t"], "reason": "drill"})
        self.assertNotIn("released_at", beat)
        channel._handle_hold(self.admin("/release"))
        after = pr.Pulse.from_dict(channel.own_pulse(NOW).to_dict())
        self.assertIsNone(after.held)
        self.assertEqual(after.released_at, self.wall["t"])

    def test_a_release_survives_a_restart_with_its_time(self):
        self.channel()._handle_hold(self.admin("/release"))
        restarted = self.channel()
        self.assertEqual(restarted._hold_action_at(), self.wall["t"])
        # A stale hold from a peer that never heard the release cannot bring it back ...
        self.assertEqual(restarted._adopt_hold({"alias": "Codex", "at": NOW,
                                                "held": {"at": self.wall["t"] - 1}}), "")
        self.assertFalse(restarted.held())
        # ... and the newer release of a peer is adopted, even as a bot is still held.
        restarted._handle_hold(self.admin("/hold later"))
        self.assertTrue(restarted.held())
        self.assertEqual(restarted._adopt_hold({"alias": "Codex", "at": NOW,
                                                "released_at": self.wall["t"] + 1}),
                         "released")
        self.assertFalse(restarted.held())

    def test_the_presence_line_names_who_is_held(self):
        channel = self.channel()
        self.hear(channel, pr.Pulse(alias="Codex", at=NOW,
                                    held={"at": NOW - 60, "reason": "drill"}))
        line = channel.presence_line(now=NOW)
        self.assertIn("Codex:up+held", line)
        self.assertNotIn("Hermes:up+held", line)            # this bot is not held

    def test_status_names_this_bots_own_hold(self):
        """#234 review nit: /status reads for the operator, so it says this bot is held too.

        #245 review (Reasonix): a free-text reason is quoted, and an empty one is `_`, so the
        token never swallows the rest of the line.
        """
        channel = self.channel()
        self.assertNotIn(" held=", channel._command_reply("status"))
        channel._handle_hold(self.admin("/hold drill"))
        self.assertIn(' held="drill"', channel._command_reply("status"))
        channel._handle_hold(self.admin("/hold until the drill is done"))
        self.assertIn(' held="until the drill is done"', channel._command_reply("status"))
        channel._handle_hold(self.admin("/hold"))
        self.assertIn(" held=_", channel._command_reply("status"))

    def test_a_beat_with_no_hold_leaves_the_hold_alone(self):
        """A peer on an older build carries neither field: that is silence, not a release."""
        channel = self.channel()
        channel._handle_hold(self.admin("/hold drill"))
        self.hear(channel, pr.Pulse(alias="Codex", at=NOW))
        self.assertTrue(channel.held())


if __name__ == "__main__":
    unittest.main()
