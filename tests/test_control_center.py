"""The pinned dashboard message and its refresh (issue #117).

What these pin: the first refresh sends and pins, the next ones edit in place, an identical
refresh sends nothing, a new coordinator adopts the message Telegram already has pinned
instead of pinning a second one, and no failure of the Telegram call can raise out of a beat
-- the heartbeat that carries the dashboard also carries this bot's liveness.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from swarmly.telegram import control_center as cc
from swarmly.telegram import control as ct
from tests.test_telegram_channel import GROUP, make_channel, make_config, run

CHAT = GROUP
TEXT = "SWARM round=5 items=3/9 coord=Claude"


class FakeBot:
    """The four Telegram calls the dashboard makes, recorded instead of sent."""

    def __init__(self, pinned=None, next_id: int = 500):
        self.sent = []
        self.edits = []
        self.pins = []
        self.unpins = []
        self.pinned = pinned
        self.next_id = next_id
        self.send_error = None
        self.edit_error = None
        self.pin_error = None

    async def send_message(self, **kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append(kwargs)
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id, **kwargs)

    async def edit_message_text(self, **kwargs):
        if self.edit_error:
            raise self.edit_error
        self.edits.append(kwargs)
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def pin_chat_message(self, **kwargs):
        if self.pin_error:
            raise self.pin_error
        self.pins.append(kwargs)
        return True

    async def unpin_chat_message(self, **kwargs):
        self.unpins.append(kwargs)
        return True

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, pinned_message=self.pinned)


def centre(bot, text=TEXT):
    return cc.ControlCenter(bot, chat_id=CHAT, thread_id=307, render=lambda: text)


class TestTheFirstMessage(unittest.TestCase):
    def test_the_first_refresh_sends_one_message_and_pins_it(self):
        bot = FakeBot()
        result = run(centre(bot).refresh())
        self.assertEqual(result.status, cc.CREATED)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0]["text"], TEXT)
        self.assertEqual(bot.sent[0]["message_thread_id"], 307)     # the control topic
        self.assertEqual(bot.pins[0]["message_id"], result.message_id)

    def test_a_pin_that_is_refused_does_not_fail_the_refresh(self):
        bot = FakeBot()
        bot.pin_error = RuntimeError("not enough rights to pin")
        result = run(centre(bot).refresh())
        self.assertEqual(result.status, cc.CREATED)                 # the message still exists
        self.assertIsNotNone(result.message_id)

    def test_an_unpinned_dashboard_is_still_the_dashboard(self):
        bot = FakeBot()
        patch = mock.patch.object(bot, "pin_chat_message", side_effect=AssertionError("no pin"))
        with patch:
            result = run(centre(bot).refresh())
        self.assertEqual(result.status, cc.CREATED)


class TestLaterRefreshes(unittest.TestCase):
    def test_a_second_refresh_edits_the_same_message(self):
        bot = FakeBot()
        row = centre(bot)
        first = run(row.refresh())
        result = run(row.refresh("SWARM round=5 items=4/9 coord=Claude"))
        self.assertEqual(result.status, cc.EDITED)
        self.assertEqual(result.message_id, first.message_id)
        self.assertEqual(len(bot.sent), 1)                          # no second message
        self.assertEqual(bot.edits[0]["message_id"], first.message_id)

    def test_an_identical_refresh_sends_nothing_at_all(self):
        bot = FakeBot()
        row = centre(bot)
        run(row.refresh())
        result = run(row.refresh(TEXT))
        self.assertEqual(result.status, cc.UNCHANGED)
        self.assertEqual(bot.edits, [])

    def test_not_modified_is_an_unchanged_message_not_a_failure(self):
        bot = FakeBot()
        row = centre(bot)
        run(row.refresh())
        bot.edit_error = RuntimeError("Bad Request: message is not modified")
        result = run(row.refresh("SWARM something else"))
        self.assertEqual(result.status, cc.UNCHANGED)
        self.assertEqual(len(bot.sent), 1)

    def test_a_deleted_message_is_recreated_and_pinned_again(self):
        bot = FakeBot()
        row = centre(bot)
        first = run(row.refresh())
        bot.edit_error = RuntimeError("Bad Request: message to edit not found")
        result = run(row.refresh("SWARM round=5 items=5/9 coord=Claude"))
        self.assertEqual(result.status, cc.CREATED)
        self.assertNotEqual(result.message_id, first.message_id)
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(bot.pins[-1]["message_id"], result.message_id)


class TestFailover(unittest.TestCase):
    def test_a_new_coordinator_adopts_the_pinned_message(self):
        """The message id is never on disk: Telegram's pin is the state (#117 failover)."""
        pinned = SimpleNamespace(message_id=777, text=TEXT, message_thread_id=307)
        bot = FakeBot(pinned=pinned)
        row = centre(bot)
        result = run(row.refresh())
        self.assertEqual(result.message_id, 777)
        self.assertEqual(bot.sent, [])                              # nothing new was posted
        self.assertEqual(bot.pins, [])                              # and nothing re-pinned

    def test_an_adopted_message_is_edited_when_the_text_moved(self):
        pinned = SimpleNamespace(message_id=777, text="SWARM round=5 items=1/9", message_thread_id=307)
        bot = FakeBot(pinned=pinned)
        result = run(centre(bot).refresh())
        self.assertEqual(result.message_id, 777)
        self.assertEqual(result.status, cc.TAKEN)
        self.assertEqual(bot.edits[0]["message_id"], 777)

    def test_a_pinned_message_that_is_not_ours_is_left_alone(self):
        pinned = SimpleNamespace(message_id=778, text="Welcome to the swarm!", message_thread_id=307)
        bot = FakeBot(pinned=pinned)
        result = run(centre(bot).refresh())
        self.assertEqual(result.status, cc.CREATED)
        self.assertNotEqual(result.message_id, 778)
        self.assertEqual(bot.edits, [])

    def test_another_topics_dashboard_is_not_adopted(self):
        pinned = SimpleNamespace(message_id=779, text=TEXT, message_thread_id=999)
        bot = FakeBot(pinned=pinned)
        result = run(centre(bot).refresh())
        self.assertEqual(result.status, cc.CREATED)
        self.assertNotEqual(result.message_id, 779)


MOVED = "SWARM round=5 items=4/9 coord=Claude"


class TestAFailoverRetiresTheOldPin(unittest.TestCase):
    """A bot may edit only its own messages, so a failover cannot adopt the old dashboard.

    Telegram answers "message can't be edited" for another bot's message. Reading that as
    "gone" (as this code did) pinned a *second* dashboard and left the first one live with its
    buttons -- presses then landed on a bot that no longer coordinated (review, #133).
    """

    def pinned(self, author=None, message_id=777):
        row = SimpleNamespace(message_id=message_id, text=TEXT, message_thread_id=307)
        if author:
            row.from_user = SimpleNamespace(username=author)
        return row

    def test_the_previous_coordinators_dashboard_is_retired_not_adopted(self):
        bot = FakeBot(pinned=self.pinned(author="ClaudeBot"))
        row = cc.ControlCenter(bot, chat_id=CHAT, thread_id=307, render=lambda: TEXT,
                               me_username="HermesBot")
        result = run(row.refresh())
        self.assertEqual(bot.unpins[0]["message_id"], 777)          # its keyboard goes with it
        self.assertEqual(len(bot.sent), 1)                          # exactly one dashboard now
        self.assertNotEqual(result.message_id, 777)
        self.assertEqual(bot.pins[0]["message_id"], result.message_id)

    def test_a_restart_adopts_the_pins_own_dashboard_and_posts_nothing(self):
        bot = FakeBot(pinned=self.pinned(author="HermesBot"))
        row = cc.ControlCenter(bot, chat_id=CHAT, thread_id=307, render=lambda: TEXT,
                               me_username="HermesBot")
        result = run(row.refresh())
        self.assertEqual(result.message_id, 777)
        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.unpins, [])

    def test_a_refused_edit_retires_the_message_instead_of_duplicating_it(self):
        """The fallback, for when Telegram does not tell us who sent the pin."""
        bot = FakeBot(pinned=self.pinned(author=None))
        bot.edit_error = RuntimeError("Bad Request: message can't be edited")
        result = run(centre(bot, MOVED).refresh())    # a moved dashboard: it tries to edit
        self.assertEqual(bot.unpins[0]["message_id"], 777)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(result.status, cc.CREATED)

    def test_a_deleted_message_is_recreated_and_nothing_is_unpinned(self):
        bot = FakeBot(pinned=self.pinned(author=None))
        bot.edit_error = RuntimeError("Bad Request: message to edit not found")
        result = run(centre(bot, MOVED).refresh())
        self.assertEqual(bot.unpins, [])                            # gone is not "not ours"
        self.assertEqual(result.status, cc.CREATED)


class TestAFailureNeverRaises(unittest.TestCase):
    def test_a_send_that_fails_is_a_status_not_an_exception(self):
        bot = FakeBot()
        bot.send_error = RuntimeError("Bad Request: chat not found")
        result = run(centre(bot).refresh())
        self.assertEqual(result.status, cc.FAILED)
        self.assertIn("chat not found", result.detail)

    def test_an_edit_that_fails_for_another_reason_is_a_status_too(self):
        bot = FakeBot()
        row = centre(bot)
        run(row.refresh())
        bot.edit_error = RuntimeError("Bad Request: chat_write_forbidden")
        result = run(row.refresh("SWARM moved on"))
        self.assertEqual(result.status, cc.FAILED)
        self.assertIsNotNone(result.message_id)                     # the id is kept for a retry

    def test_an_unreadable_pin_falls_back_to_creating(self):
        bot = FakeBot()

        async def boom(chat_id):
            raise RuntimeError("Bad Request: chat not found")

        bot.get_chat = boom
        result = run(centre(bot).refresh())
        self.assertEqual(result.status, cc.CREATED)


class TestTheChannelWiresIt(unittest.TestCase):
    def test_no_control_section_means_no_dashboard(self):
        channel = make_channel(make_config())
        self.assertIsNone(channel.control_center())

    def test_a_control_section_builds_the_dashboard_on_its_chat(self):
        channel = make_channel(make_config(control={"chat_id": CHAT, "topic_id": 307}))
        channel.bot = FakeBot()
        row = channel.control_center()
        self.assertIsNotNone(row)
        self.assertEqual((row.chat_id, row.thread_id), (CHAT, 307))

    def test_a_refresh_edits_the_message_and_never_calls_the_adapter(self):
        channel = make_channel(make_config(control={"chat_id": CHAT, "topic_id": 307}))
        channel.bot = FakeBot()
        channel.control_round = lambda: None
        turns = []
        channel.adapter.respond = lambda message: turns.append(message.text) or None
        result = run(channel.refresh_control_center())
        self.assertEqual(result.status, cc.CREATED)
        self.assertTrue(channel.bot.sent[0]["text"].startswith(ct.MARKER))
        self.assertEqual(turns, [])
        self.assertEqual(channel.usage_ledger().entries, [])        # and no turn was recorded

    def test_only_the_coordinator_refreshes_it(self):
        channel = make_channel(make_config(control={"chat_id": CHAT, "topic_id": 307},
                                           limits={"heartbeat_minutes": 2}))
        channel.bot = FakeBot()
        channel.control_round = lambda: None
        asyncio.run(channel._heartbeat_tick(now=1000.0))
        self.assertEqual(channel.bot.sent, [])                      # not the coordinator: silent

        channel.is_coordinator = lambda now=None: True
        asyncio.run(channel._heartbeat_tick(now=1000.0))
        self.assertEqual(len(channel.bot.sent), 1)
        self.assertTrue(channel.bot.sent[0]["text"].startswith(ct.MARKER))


class TestWhatEachBotSpent(unittest.TestCase):
    """Every bot's tokens and cost, without asking any bot for a turn (issue #117).

    Usage rides the pulse's status record (the shape #117's heartbeat carries): each bot's own
    ledger, briefed to today plus the beat. The dashboard reads peers' numbers from there and
    its own from the ledger directly, never by asking anyone for a turn.
    """

    def channel(self):
        channel = make_channel(make_config(control={"chat_id": GROUP, "topic_id": 307},
                                           limits={"heartbeat_minutes": 2}))
        channel.bot = FakeBot()
        channel.control_round = lambda: None
        channel.presence_clock = lambda: 1_800_000_000.0
        return channel

    def test_todays_totals_come_from_the_ledger_with_their_coverage(self):
        from swarmly.telegram import usage as us
        channel = self.channel()
        day = 1_800_000_000.0 - (1_800_000_000.0 % 86400)
        channel.usage_ledger().append(us.Entry(alias="Hermes", started=day + 10, ended=day + 20,
                                               tokens_in=1200, tokens_out=340,
                                               cost_usd=0.42, outcome=us.OK))
        today = channel.own_pulse(1_800_000_000.0).status["usage"]["today"]
        self.assertEqual((today["tokens_in"], today["tokens_out"]), (1200, 340))
        self.assertEqual((today["tokens_in_known"], today["turns"]), (1, 1))
        self.assertEqual(today["cost_usd"], 0.42)

    def test_reading_the_dashboard_does_not_move_the_beats_window(self):
        channel = self.channel()
        before = dict(channel._pulse_usage(1_800_000_000.0))
        channel.control_text()
        after = dict(channel._pulse_usage(1_800_000_000.0))
        self.assertEqual(before["beat"], after["beat"])

    def test_a_bot_with_no_turns_spent_nothing_and_a_silent_peer_stays_unknown(self):
        channel = self.channel()
        # An empty ledger is a *known* zero, never a silent runtime: main's #147 rule, which
        # this dashboard inherits by reading ``totals()`` rather than counting rows itself.
        mine = channel.control_usage()["Hermes"]
        self.assertEqual((mine["turns"], mine["tokens_in"], mine["tokens_out"]), (0, 0, 0))
        # A peer that has said nothing is absent from the map, so it renders unknown -- not a
        # zero it never reported.
        self.assertNotIn("Codex", channel.control_usage())
        self.assertEqual(ct.usage_line("Codex", None),
                         "Codex: usage unknown (no turns recorded)")

    def test_a_peers_spend_arrives_on_its_pulse(self):
        from swarmly.telegram.presence import Pulse
        channel = self.channel()
        channel.presence().note(Pulse(alias="Codex", at=1_800_000_010.0, rev="abc1234",
                                      status={"at": 1_800_000_010.0, "alias": "Codex",
                                              "state": "busy",
                                              "usage": {"today": {
                                                  "turns": 3, "tokens_in": 900,
                                                  "tokens_out": None, "tokens_in_known": 3,
                                                  "tokens_out_known": 0, "cost_usd": 0.10,
                                                  "cost_known": 3}}}))
        text = channel.control_text()
        self.assertIn("Codex:", text)
        self.assertIn("900", text)                     # its input tokens
        self.assertIn("unknown", text)                 # output was never reported: not a zero

    def test_the_snapshot_carries_the_usage_and_a_peer_keeps_the_newest(self):
        from swarmly.telegram.presence import Pulse
        channel = self.channel()
        table = channel.presence()
        table.note(Pulse(alias="Codex", at=1_800_000_020.0,
                         status={"at": 1_800_000_020.0, "usage": {"today": {
                             "turns": 5, "tokens_in": 5000, "tokens_out": 900,
                             "tokens_in_known": 5, "tokens_out_known": 5, "cost_usd": 1.0,
                             "cost_known": 5}}}))
        table.note(Pulse(alias="Codex", at=1_800_000_001.0,
                         status={"at": 1_800_000_001.0, "usage": {"today": {
                             "turns": 1, "tokens_in": 10, "tokens_out": 2,
                             "tokens_in_known": 1}}}))
        usage = (table.peers["Codex"].snapshot or {}).get("usage") or {}
        self.assertEqual(usage["today"]["tokens_in"], 5000)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
