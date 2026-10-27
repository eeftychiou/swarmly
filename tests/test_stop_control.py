"""The dashboard's Stop, per bot (#437): yield the lease, say OFFLINE, then exit.

A pause keeps the bot online *and* holding the lease, so the failover drill (#423, check 7)
could not be run from the page: a peer only took over once the lease lapsed. These pin the
four things the control promises -- admin only, the lease given up at once, the OFFLINE line
out, and the exit -- and the one thing it refuses: a yield that did not land still letting
the bot go offline while the swarm thinks a lease is held.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from swarmly.telegram import control as ct
from swarmly.telegram import control_press as cp
from swarmly.telegram.channel import ChannelEvent, ControlRefused
from tests.test_control_center import FakeBot
from tests.test_telegram_channel import ADMIN_ID, make_channel, make_config, run

NOW = 1_800_000_000.0
CONTROL_CHAT = -1009876543210


class Store:
    """A lease store that records instead of pushing: the wiring is what is under test."""

    def __init__(self, term=15, holder="Hermes", fail=False):
        self.term, self.holder, self.fail = term, holder, fail
        self.yielded = []

    def state(self, *, now=None):
        from swarmly.telegram.lease import LeaseState
        return LeaseState(status="ok", term=self.term, holder=self.holder, expires_at=1e12)

    def yield_lease(self, *, reason="", now=None, term=None):
        if self.fail:
            raise RuntimeError("the bus refused the push")
        self.yielded.append((reason, term))
        self.holder = ""


class Dispatcher:
    def __init__(self):
        self.stopped = False

    async def stop_polling(self):
        self.stopped = True


def lines(channel, needle):
    return [entry["text"] for entry in channel._test_sender.sent if needle in entry["text"]]


class TestTheTableOffersIt(unittest.TestCase):
    def test_the_button_is_offered_with_a_second_tap_and_says_what_it_costs(self):
        stop = ct.control("bot.offline")
        self.assertIsNotNone(stop)
        self.assertEqual((stop.label, stop.confirm), ("Stop", True))
        self.assertIn("lease", stop.note)
        self.assertIn("start it on the host", stop.note)

    def test_this_build_carries_it_out_so_the_page_draws_no_dead_button(self):
        channel = make_channel(make_config())
        self.assertIn("bot.offline", channel.control_actions())


class TestStopping(unittest.TestCase):
    def channel(self, holder="Hermes", fail=False):
        channel = make_channel(make_config(
            control={"chat_id": CONTROL_CHAT, "topic_id": 307},
            bus={"transport": "folder", "repo": "/nonexistent-bus"}))
        channel.bot = FakeBot()
        channel.store = Store(holder=holder, fail=fail)
        channel.lease_store = lambda: channel.store
        channel.current_coordinator = lambda now=None: holder or None
        return channel

    def test_a_holder_gives_the_lease_up_at_once_says_offline_and_exits(self):
        channel = self.channel()
        said = run(channel.go_offline(presser=str(ADMIN_ID)))

        self.assertEqual(channel.store.yielded, [("offline (dashboard Stop)", 15)])
        self.assertIn("offline", said)
        self.assertIn("Start it again on the host", said)
        self.assertEqual(len(lines(channel, "Hermes offline")), 1)
        self.assertTrue(channel.stop_requested)
        self.assertFalse(channel.restart_requested)

    def test_a_bot_that_does_not_hold_the_lease_yields_nothing_and_still_stops(self):
        channel = self.channel(holder="Codex")
        said = run(channel.go_offline())

        self.assertEqual(channel.store.yielded, [], "nothing to give up, and nothing written")
        self.assertIn("offline", said)
        self.assertTrue(channel.stop_requested)

    def test_a_yield_that_did_not_land_refuses_the_stop_in_words(self):
        """Better a bot that stays up saying why than a swarm waiting on a dead lease."""
        channel = self.channel(fail=True)
        with self.assertRaises(ControlRefused) as caught:
            run(channel.go_offline())
        self.assertIn("could not give the lease away", str(caught.exception))
        self.assertFalse(channel.stop_requested)
        self.assertEqual(lines(channel, "Hermes offline"), [])

    def test_the_exit_is_polling_stopped_and_not_a_restart(self):
        channel = self.channel()
        channel.dispatcher = Dispatcher()

        async def go():
            channel.request_stop()
            await asyncio.sleep(0)

        run(go())
        self.assertTrue(channel.dispatcher.stopped)
        self.assertTrue(channel.stop_requested)
        self.assertFalse(channel.restart_requested, "the launcher must not start it again")

    def test_the_offline_line_is_not_posted_twice_on_the_way_out(self):
        """`stop()` posts OFFLINE when the transport shuts down; a Stop already has."""
        channel = self.channel()
        run(channel.go_offline())
        asked = []

        async def on_stop():
            asked.append("on_stop")

        channel._started_wall = NOW                       # a started transport, as live
        channel.membership.on_stop = on_stop
        channel.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        run(channel.stop())
        self.assertEqual(asked, [], "the line went out once, in go_offline")
        self.assertEqual(len(lines(channel, "Hermes offline")), 1)


class TestAPeersPress(unittest.TestCase):
    """A press on another bot's card travels as the CONTROL note that bot applies to itself."""

    def channel(self):
        channel = make_channel(make_config(
            control={"chat_id": CONTROL_CHAT, "topic_id": 307},
            bus={"transport": "folder", "repo": "/nonexistent-bus"}))
        channel.bot = FakeBot()
        channel.store = Store()
        channel.lease_store = lambda: channel.store
        channel.current_coordinator = lambda now=None: "Codex"
        return channel

    def note(self, action="bot.offline"):
        return cp.PressPolicy.log_line(action, "Hermes", str(ADMIN_ID), NOW, done=True)

    def test_the_note_path_answers_and_asks_for_the_exit(self):
        channel = self.channel()
        said = channel.apply_control_note(self.note(), sender="Codex")
        self.assertIn("accepted: bot.offline", said)
        self.assertTrue(channel.stop_requested)

    def test_a_note_from_a_bot_without_the_dashboard_is_refused(self):
        """Only the lease holder's notes are obeyed, so a Stop cannot arrive from a stranger."""
        channel = self.channel()
        said = channel.apply_control_note(cp.PressPolicy.log_line(
            "bot.offline", "Hermes", str(ADMIN_ID), NOW, done=True), sender="Antigravity")
        self.assertIn("does not hold the dashboard", said)
        self.assertFalse(channel.stop_requested)


class TestOnlyTheAdmin(unittest.TestCase):
    def test_a_stop_is_admin_only_and_asks_twice(self):
        admin = cp.PressPolicy(admin_id=ADMIN_ID, clock=lambda: NOW,
                               confirm_seconds=cp.CONFIRM_SECONDS, code=lambda: "a1b2",
                               available=("bot.offline",))
        self.assertEqual(admin.decide(cp.Press(user_id=42, data="ctl:bot.offline",
                                               at=NOW)).reason, cp.NOT_ADMIN)
        self.assertEqual(admin.decide(cp.Press(user_id=ADMIN_ID, data="ctl:bot.offline",
                                               at=NOW)).verdict, cp.CONFIRM)


class TestTheDrillRunsFromThePage(unittest.TestCase):
    """The press the drill makes: the dashboard's own write path, end to end (#437)."""

    def test_a_confirmed_press_of_stop_yields_and_stops(self):
        channel = make_channel(make_config(
            control={"chat_id": CONTROL_CHAT, "topic_id": 307},
            bus={"transport": "folder", "repo": "/nonexistent-bus"}))
        channel.bot = FakeBot()
        channel.store = Store()
        channel.lease_store = lambda: channel.store
        channel.current_coordinator = lambda now=None: "Hermes"
        channel.control_round = lambda: None

        asked = run(channel.dashboard_action({"action": "bot.offline"}))
        code = channel.control_policy().pending()[0].code
        done = run(channel.dashboard_action({"action": "bot.offline", "code": code}))

        self.assertEqual(asked["verdict"], "confirm")
        self.assertIn("Tap again", asked["text"])
        self.assertEqual(channel.store.yielded, [("offline (dashboard Stop)", 15)])
        self.assertTrue(channel.stop_requested)
        self.assertTrue(done)


if __name__ == "__main__":
    unittest.main()
