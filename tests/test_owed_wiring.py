"""The wiring: the transport's own events open and close the rows (#162, PR #173).

The row model is only as good as its sources. These tests pin the two that exist as facts in
this transport today: a hand-off made in a discussion opens one row per peer addressed, and a
peer's reply **in that discussion** closes it -- and a reply anywhere else does not.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import owed as ow
from tests.test_telegram_channel import GROUP, event, make_channel, make_config


def channel_with_table():
    """A channel whose expectations table is a temporary directory, not the checkout."""
    channel = make_channel(make_config())
    channel.bot = object()
    directory = TemporaryDirectory()
    store = ow.OwedStore("Hermes", state_dir=Path(directory.name), clock=lambda: 1000.0)
    channel.owed_store = lambda: store
    channel._test_table = (directory, store)          # keep both alive for the test
    channel.addCleanupTable = directory.cleanup
    return channel


def rows(channel):
    return channel._test_table[1].load()


class TestOnePushPerEvent(unittest.TestCase):
    """The wiring must not put one bus commit on the wire per row (#173 review)."""

    def channel(self):
        pushed = []

        class Bus:
            ref = "refs/heads/main"

            def commit_and_push(self, message, paths=None):
                pushed.append(list(paths or []))

        channel = make_channel(make_config())
        channel.bot = object()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ow.OwedStore("Hermes", state_dir=Path(directory.name), transport=Bus(),
                             root=Path(directory.name), clock=lambda: 1000.0)
        channel.owed_store = lambda: store
        channel._test_table = (directory, store)
        channel._test_pushed = pushed
        return channel

    def test_a_reply_to_two_peers_is_one_push(self):
        channel = self.channel()
        channel._mirror_handoffs("@CodexBot: review #173.\n- @AntigravityBot: QA it.",
                                 GROUP, 307, 55)
        self.assertEqual(len(rows(channel)), 2)
        self.assertEqual(len(channel._test_pushed), 1)        # one commit, both documents
        self.assertEqual(len(channel._test_pushed[0]), 2)

    def test_a_reply_that_closes_two_rows_is_one_push(self):
        channel = self.channel()
        store = channel._test_table[1]
        place = channel._place_key(GROUP, 307)
        store.save_all([*ow.handoff_rows(asked=["Codex"], item="#173", asker="Hermes",
                                         topic=place, place=place, at=1000.0),
                        *ow.handoff_rows(asked=["Codex"], item="#174", asker="Hermes",
                                         topic=place, place=place, at=1000.0)])
        channel._test_pushed.clear()
        channel._close_owed_handoff("Codex", GROUP, 307)
        self.assertEqual(len(channel._test_pushed), 1)        # one commit for both closes
        self.assertEqual(len(channel._test_pushed[0]), 2)
        self.assertEqual([row for row in rows(channel) if row.is_open], [])


class TestAHandOffOpensARow(unittest.TestCase):

    def channel(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        return channel

    def test_every_peer_addressed_owes_an_answer(self):
        """The multi-recipient case, which missed DeepSeek on 22 Oct (#133)."""
        channel = self.channel()
        channel._mirror_handoffs("@CodexBot: review #173.\n- @AntigravityBot: QA it.",
                                 GROUP, 307, 55)
        table = rows(channel)
        self.assertEqual(sorted(row.debtor for row in table), ["Antigravity", "Codex"])
        self.assertTrue(all(row.kind == "handoff" and row.is_open for row in table))
        self.assertTrue(all(row.waiting_on == "Hermes" for row in table))

    def test_the_row_carries_the_discussion_it_was_made_in(self):
        channel = self.channel()
        token = channel._topic_token(GROUP, 307)
        channel._mirror_handoffs("@CodexBot please run the suite.", GROUP, 307, 55)
        self.assertEqual([row.topic for row in rows(channel)], [token])
        self.assertEqual(rows(channel)[0].id, f"handoff:-@{token}:Codex")

    def test_a_hand_off_in_two_topics_is_two_rows(self):
        channel = self.channel()
        channel._mirror_handoffs("@CodexBot please run the suite.", GROUP, 307, 55)
        channel._mirror_handoffs("@CodexBot now check the docs.", GROUP, 308, 56)
        self.assertEqual(len(rows(channel)), 2)
        self.assertNotEqual(rows(channel)[0].topic, rows(channel)[1].topic)

    def test_the_same_hand_off_seen_twice_is_one_row(self):
        channel = self.channel()
        channel._mirror_handoffs("@CodexBot please run the suite.", GROUP, 307, 55)
        channel._mirror_handoffs("@CodexBot please run the suite.", GROUP, 307, 55)
        self.assertEqual(len(rows(channel)), 1)

    def test_a_book_keeping_failure_does_not_drop_the_hand_off(self):
        channel = self.channel()

        def broken():
            raise ow.OwedError("the table cannot be read")

        channel.owed_store = broken
        with self.assertLogs("swarmly.telegram", level="WARNING"):
            channel._mirror_handoffs("@CodexBot please run the suite.", GROUP, 307, 55)
        self.assertIn((GROUP, 307, "Codex"), channel._pending_handoffs)


class TestAReplyClosesTheRowWhereItWasMade(unittest.TestCase):

    def channel(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        channel._mirror_handoffs("@CodexBot please run the suite.", GROUP, 307, 55)
        return channel

    def test_the_peers_reply_in_that_topic_answers_it(self):
        channel = self.channel()
        channel._handoff_answered(event("on it", username="CodexBot", thread_id=307,
                                        message_id=56))
        row = rows(channel)[0]
        self.assertFalse(row.is_open)
        self.assertEqual(row.closed_by, "Codex answered")

    def test_a_reply_in_another_topic_answers_nothing(self):
        channel = self.channel()
        channel._handoff_answered(event("on it", username="CodexBot", thread_id=308,
                                        message_id=57))
        self.assertTrue(rows(channel)[0].is_open)

    def test_another_peers_reply_answers_nothing(self):
        channel = self.channel()
        channel._handoff_answered(event("mine", username="AntigravityBot", thread_id=307,
                                        message_id=58))
        self.assertTrue(rows(channel)[0].is_open)

    def test_a_busy_line_is_not_an_answer(self):
        channel = self.channel()
        channel._handoff_answered(event("[Codex] ⏳ Codex is busy with a turn running since "
                                        "14:58 UTC; 1 message(s) here wait",
                                        username="CodexBot", thread_id=307, message_id=59))
        self.assertTrue(rows(channel)[0].is_open)


class TestTheOwedCommand(unittest.TestCase):

    def channel(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        return channel

    def test_it_lists_what_is_open(self):
        channel = self.channel()
        self.assertEqual(channel._owed_command(None), "owed: nothing")
        channel._mirror_handoffs("@CodexBot please run the suite.", GROUP, 307, 55)
        line = channel._owed_command(None)
        self.assertIn("owed=1", line)
        self.assertIn("Codex owes an answer", line)
        self.assertIn("Hermes waiting", line)

    def test_it_is_answered_by_the_transport_without_a_turn(self):
        channel = self.channel()
        self.assertIsNotNone(channel._handle_command(
            event("/owed@HermesBot", author_id=202, username="CodexBot", thread_id=307)))

    def test_a_table_it_cannot_read_is_not_reported_as_nothing(self):
        channel = self.channel()

        def broken():
            raise ow.OwedError("no")

        channel.owed_store = broken
        with self.assertLogs("swarmly.telegram", level="WARNING"):
            line = channel._owed_command(None)
        self.assertIn("cannot read the table", line)
        self.assertNotIn("owed: nothing", line)


class TestTheLeadAndUntokenedPlaces(unittest.TestCase):
    """#173 review: a hand-off to the coordinator still owes a row, and places without a token
    must not collapse into one debt."""

    def channel(self):
        channel = channel_with_table()
        self.addCleanup(channel._test_table[0].cleanup)
        return channel

    def test_a_hand_off_to_the_coordinator_opens_a_row(self):
        channel = self.channel()
        channel.membership.state.coordinator = "Codex"
        channel.current_coordinator = lambda *a, **k: "Codex"
        # The body answers Codex's own hand-off, so it is not mirrored to it -- but the
        # coordinator is still waited on, which is exactly the debt that had no row.
        channel._mirror_handoffs("@CodexBot: #173 is ready, merge it.", GROUP, 307, 55,
                                 asker="Codex")
        self.assertEqual([row.debtor for row in rows(channel)], ["Codex"])

    def test_two_places_without_a_topic_token_stay_apart(self):
        channel = self.channel()
        first = channel._place_key(GROUP, None)
        second = channel._place_key(GROUP + 1, None)
        self.assertNotEqual(first, second)
        self.assertFalse(first.lstrip("x").isdigit())     # a digest, never a chat id
        channel._mirror_handoffs("@CodexBot: take #173.", GROUP, None, 55)
        channel._mirror_handoffs("@CodexBot: take #173.", GROUP + 1, None, 56)
        self.assertEqual(len(rows(channel)), 2)

