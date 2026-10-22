"""The transport half of waking on news (#163): where a row's debt is, and what holds it.

The decision is a pure function (`owed_wake.plan`); this file pins the half that knows the
world: which chat and thread a row's discussion is, that a topic already running a turn is
held, and that what woke whom is written down before the next tick.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import owed as ow
from tests.test_telegram_channel import GROUP, make_channel, make_config


def ci_row(**fields):
    """One CI row with the fields these tests all need, so each test names only its own."""
    fields.setdefault("kind", "ci")
    fields.setdefault("item", "#146")
    fields.setdefault("debtor", ow.CI)
    fields.setdefault("owes", "a result on c166987")
    fields.setdefault("head", "c166987")
    return ow.Row(**fields)


def channel_with_table():
    channel = make_channel(make_config())
    channel.bot = object()
    directory = TemporaryDirectory()
    store = ow.OwedStore("Hermes", state_dir=Path(directory.name), clock=lambda: 1000.0)
    channel.owed_store = lambda: store
    channel._test_table = (directory, store)
    turns = []
    channel._run_own_task = lambda text, chat, thread, name: turns.append(
        (text, chat, thread, name))
    channel._test_turns = turns
    return channel


class TestWhereTheWakeGoes(unittest.TestCase):

    def channel(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        return channel

    def open_row(self, channel, **fields):
        fields.setdefault("observed", "green")
        row = ci_row(**fields)
        channel._test_table[1].save_all([row])
        return row

    def test_a_row_wakes_in_the_discussion_it_names(self):
        channel = self.channel()
        channel.topics.remember(GROUP, 5122, "round 5")
        token = channel.topics.context(GROUP, 5122)
        self.open_row(channel, topic=token, woken="pending")
        told = channel.owed_wake_tick(now=1000.0)
        self.assertEqual(len(channel._test_turns), 1)
        text, chat_id, thread_id, name = channel._test_turns[0]
        self.assertEqual((chat_id, thread_id), (GROUP, 5122))
        self.assertEqual(name, "round 5")
        self.assertIn("pending to green", text)         # the delta, not "check again"
        self.assertIn("Owed-row wake", text)
        self.assertTrue(told)                           # and the tick says what it did

    def test_a_row_with_no_discussion_wakes_in_general(self):
        channel = self.channel()
        self.open_row(channel, topic="")
        channel.owed_wake_tick(now=1000.0)
        self.assertEqual([(chat, thread) for _t, chat, thread, _n in channel._test_turns],
                         [(GROUP, None)])

    def test_a_token_this_bot_cannot_place_wakes_nobody(self):
        channel = self.channel()
        self.open_row(channel, topic="tg-999999")
        done = channel.owed_wake_tick(now=1000.0)
        self.assertEqual(channel._test_turns, [])
        self.assertIn("no discussion to wake in", " ".join(done))


class TestNoNewsNoTurnInTheTransport(unittest.TestCase):

    def channel(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        channel.topics.remember(GROUP, 5122, "round 5")
        row = ci_row(topic=channel.topics.context(GROUP, 5122), observed="green")
        channel._test_table[1].save_all([row])
        return channel, row

    def test_the_same_news_wakes_once(self):
        channel, _row = self.channel()
        channel.owed_wake_tick(now=1000.0)
        self.assertEqual(len(channel._test_turns), 1)
        # The wake is written to the row, so the next tick sees no news -- even after a
        # restart, which is the same table read fresh.
        channel.owed_wake_tick(now=1060.0)
        self.assertEqual(len(channel._test_turns), 1)
        self.assertEqual(channel._test_table[1].load()[0].woken, "green")

    def test_closed_rows_wake_nobody(self):
        channel, row = self.channel()
        row.state = ow.CLOSED
        channel._test_table[1].save_all([row])
        self.assertEqual(channel.owed_wake_tick(now=1000.0), [])
        self.assertEqual(channel._test_turns, [])


class TestTheBusyHoldAndTheBudget(unittest.TestCase):

    def channel(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        channel.topics.remember(GROUP, 5122, "round 5")
        return channel

    def open_row(self, channel, item, debtor, observed="green", thread=5122):
        channel.topics.remember(GROUP, thread, f"topic {thread}")
        token = channel.topics.context(GROUP, thread)
        channel._test_table[1].save_all([ci_row(item=item, debtor=debtor, topic=token,
                                                observed=observed)])

    def test_a_busy_topic_holds_the_wake_and_keeps_it_armed(self):
        """#160 applies to these wakes too: a turn already running here is not queued behind."""
        channel = self.channel()
        self.open_row(channel, "#146", ow.CI)
        channel._busy_since[(GROUP, 5122)] = 990.0
        done = channel.owed_wake_tick(now=1000.0)
        self.assertEqual(channel._test_turns, [])
        self.assertIn("held: turn running", " ".join(done))
        self.assertEqual(channel._test_table[1].load()[0].woken, "")   # still news, later
        # ... and once that turn ends the same row wakes.
        channel._busy_since.clear()
        channel.owed_wake_tick(now=1060.0)
        self.assertEqual(len(channel._test_turns), 1)

    def test_one_tick_starts_at_most_the_budget(self):
        channel = self.channel()
        from swarmly.telegram.owed_wake import MAX_WAKES_PER_TICK
        for index in range(MAX_WAKES_PER_TICK + 2):
            self.open_row(channel, f"#{500 + index}", ow.CI, thread=5200 + index)
        done = channel.owed_wake_tick(now=1000.0)
        self.assertEqual(len(channel._test_turns), MAX_WAKES_PER_TICK)
        self.assertIn("wake budget", " ".join(done))
        # The rows it did not reach are still news: the next tick picks them up.
        channel.owed_wake_tick(now=1060.0)
        self.assertEqual(len(channel._test_turns), MAX_WAKES_PER_TICK + 2)

    def test_only_one_turn_per_topics_per_tick(self):
        channel = self.channel()
        self.open_row(channel, "#146", ow.CI)
        self.open_row(channel, "#147", ow.CI)
        done = channel.owed_wake_tick(now=1000.0)
        self.assertEqual(len(channel._test_turns), 1)      # one turn, not two in one topic
        self.assertIn("held: turn running", " ".join(done))


class TestAnEscalationWakesTheAdminOnce(unittest.TestCase):

    def test_a_row_that_keeps_repeating_is_escalated_once(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        channel.topics.remember(GROUP, 5122, "round 5")
        token = channel.topics.context(GROUP, 5122)
        row = ci_row(topic=token, observed="green", woken="green", wakes=2)
        channel._test_table[1].save_all([row])
        channel.owed_wake_tick(now=1000.0)
        self.assertEqual(len(channel._test_turns), 1)
        self.assertIn("Owed-row escalation", channel._test_turns[0][0])
        self.assertIn("admin", channel._test_turns[0][0])
        # Marked, so it never comes back.
        self.assertEqual(channel.owed_wake_tick(now=1060.0), [])
        self.assertTrue(channel._test_table[1].load()[0].escalated)
