"""The WAIT line: a reply says what it waits on, and the transport opens the row (#165).

The lines here are the ones the round produced, plus every way a line can be wrong -- each
wrong one is said back to the bot, never posted as prose.
"""

import unittest

from swarmly.telegram import owed as ow
from swarmly.telegram import wait_line as wl
from tests.test_telegram_channel import GROUP


class TestAKindEach(unittest.TestCase):

    def test_a_review_verdict_row(self):
        row = wl.parse_line("WAIT review #133 head 38f1977 from Reasonix", topic="tg-3556",
                            at=1000.0)
        self.assertEqual(row.kind, "verdict")
        self.assertEqual(row.debtor, "Reasonix")
        self.assertEqual(row.item, "#133")
        self.assertEqual(row.head, "38f1977")
        self.assertEqual(row.topic, "tg-3556")
        self.assertTrue(row.is_open)

    def test_a_qa_verdict_row(self):
        row = wl.parse_line("WAIT qa #195 head cc57d8a from Antigravity", at=1000.0)
        self.assertEqual((row.kind, row.debtor, row.head), ("verdict", "Antigravity", "cc57d8a"))

    def test_a_ci_row_is_owed_by_the_transport(self):
        row = wl.parse_line("WAIT ci #173 head 9fb2f21", at=1000.0)
        self.assertEqual((row.kind, row.debtor), ("ci", ow.CI))

    def test_a_merge_row(self):
        row = wl.parse_line("WAIT merge #173 head b40f3ae from Claudebot", at=1000.0)
        self.assertEqual((row.kind, row.debtor), ("merge", "Claudebot"))

    def test_a_progress_row(self):
        row = wl.parse_line("WAIT progress #166 from Antigravity", at=1000.0)
        self.assertEqual((row.kind, row.debtor, row.head), ("progress", "Antigravity", ""))

    def test_an_answer_is_the_admins(self):
        row = wl.parse_line("WAIT answer on the Drive credentials", at=1000.0)
        self.assertEqual((row.kind, row.debtor), ("admin", ow.ADMIN))
        self.assertIn("Drive credentials", row.owes)

    def test_the_shapes_the_swarm_actually_wrote(self):
        """Every WAIT this round produced parses into a row, so the migration can start."""
        lines = [
            "WAIT review #191 head e326779 from Reasonix",
            "WAIT qa #208 head 9bed565 from Antigravity",
            "WAIT merge #207 head fc9c810 from Claudebot",
            "WAIT answer on the #189 ruling",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(wl.parse_line(line, at=1000.0).is_open)


class TestTwoQuestionsAreTwoDebts(unittest.TestCase):
    """An admin row has no item, so it is keyed by the question -- two questions, two rows.

    Without the key both rows write the id ``admin:-:admin`` and the table keeps one: the second
    question the bot said it waits on is never owed (#209 review).
    """

    def test_two_questions_in_one_topic_key_apart(self):
        first = wl.parse_line("WAIT answer on the Drive credentials", topic="tg-3556", at=1000.0)
        second = wl.parse_line("WAIT answer on the CT100 network", topic="tg-3556", at=1000.0)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(ow.fold([first, second])), 2)

    def test_the_same_question_asked_again_is_one_debt(self):
        said = "WAIT answer on the Drive credentials"
        self.assertEqual(wl.parse_line(said, topic="tg-3556", at=1000.0).id,
                         wl.parse_line(said, topic="tg-3556", at=2000.0).id)

    def test_the_admin_still_settles_it_by_answering(self):
        row = wl.parse_line("WAIT answer on the Drive credentials", topic="tg-3556", at=1000.0)
        closed = ow.settle([row], ow.Fact(kind="reply", item="tg-3556", who=ow.ADMIN),
                           now=2000.0)
        self.assertEqual(len(closed), 1)                     # the key does not trap the row


class TestARefusalSaysWhy(unittest.TestCase):
    """A line that cannot become a row is refused with the shape that would work."""

    def refused(self, line):
        with self.assertRaises(wl.WaitError) as caught:
            wl.parse_line(line, at=1000.0)
        return str(caught.exception)

    def test_a_head_bound_kind_without_a_head(self):
        self.assertIn("needs the head", self.refused("WAIT review #133 from Reasonix"))

    def test_a_verdict_without_a_bot(self):
        self.assertIn("needs the bot that owes it", self.refused("WAIT review #133 head 38f1977"))

    def test_an_unknown_kind_lists_the_ones_there_are(self):
        said = self.refused("WAIT sometime #133")
        self.assertIn("does not know", said)
        self.assertIn("review", said)

    def test_ci_may_not_be_owed_by_a_bot(self):
        self.assertIn("owed by the transport",
                      self.refused("WAIT ci #173 head 9fb2f21 from Reasonix"))

    def test_a_bare_marker_is_not_a_line(self):
        self.assertIn("needs a kind", self.refused("WAIT"))


class TestNobodyWaitsOnABotThatCannotAct(unittest.TestCase):
    """#246: the transport accepted a WAIT on a peer whose own pulse said `paused`."""

    def test_a_wait_on_a_paused_peer_is_refused_with_one_line(self):
        with self.assertRaises(wl.WaitError) as caught:
            wl.parse_line("WAIT qa #245 head cefd20a from Antigravity", at=1000.0,
                          unable={"antigravity": "paused"})
        self.assertEqual(str(caught.exception),
                         "Antigravity is paused: name a peer who can act")

    def test_the_alias_is_matched_however_it_is_written(self):
        for said in ("Antigravity", "antigravity", "@Antigravity"):
            with self.subTest(said=said):
                with self.assertRaises(wl.WaitError):
                    wl.parse_line(f"WAIT progress #246 from {said}", at=1000.0,
                                  unable={"antigravity": "paused"})

    def test_a_peer_that_can_act_is_left_alone(self):
        row = wl.parse_line("WAIT qa #245 head cefd20a from Reasonix", at=1000.0,
                            unable={"antigravity": "paused"})
        self.assertEqual(row.debtor, "Reasonix")

    def test_the_extraction_says_it_back_and_records_nothing(self):
        body, rows, reasons = wl.extract_waits(
            "Reviewed it.\n"
            "WAIT qa #245 head cefd20a from Antigravity", topic="tg-1",
            at=1000.0, unable={"antigravity": "paused"})
        self.assertEqual(rows, [])
        self.assertEqual(len(reasons), 1)
        self.assertIn("Antigravity is paused", reasons[0])
        self.assertNotIn("WAIT qa", body)                     # never posted as prose


class TestExtraction(unittest.TestCase):

    def test_the_line_leaves_the_reply_and_becomes_a_row(self):
        text = ("Reviewed #191 at e326779 and it is fine.\n"
                "WAIT merge #191 head e326779 from Claudebot\n"
                "I will look again when it lands.")
        body, rows, reasons = wl.extract_waits(text, topic="tg-3556", at=1000.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(reasons, [])
        self.assertNotIn("WAIT", body)                       # never posted as prose
        self.assertIn("Reviewed #191", body)
        self.assertIn("I will look again", body)

    def test_a_refused_line_is_removed_too_and_said_back(self):
        text = "Here is the verdict.\nWAIT review #133\nDone."
        body, rows, reasons = wl.extract_waits(text, at=1000.0)
        self.assertEqual(rows, [])
        self.assertEqual(len(reasons), 1)
        self.assertNotIn("WAIT review", body)
        self.assertIn("Here is the verdict.", body)

    def test_prose_that_starts_with_the_word_is_left_alone(self):
        text = "WAITING for the bus to come back, no row for this.\nWAIT progress #166 from Agy"
        body, rows, reasons = wl.extract_waits(text, at=1000.0)
        self.assertIn("WAITING for the bus", body)
        self.assertEqual(len(rows), 1)
        self.assertEqual(reasons, [])

    def test_text_without_the_marker_is_untouched(self):
        text = "No rows here, just a sentence about the bus."
        self.assertEqual(wl.extract_waits(text), (text, [], []))

    def test_the_ack_names_the_row(self):
        row = wl.parse_line("WAIT review #133 head 38f1977 from Reasonix", at=1000.0)
        said = wl.ack(row)
        self.assertIn("Reasonix", said)
        self.assertIn("38f1977", said)
        self.assertIn("recorded", said)


class TestARetiredWakeBecomesARow(unittest.TestCase):
    """#165: the timer goes, the promise stays -- a ``WAKE`` line is the row that replaces it."""

    def spec(self, line):
        from swarmly.telegram import wake as wk
        return wk.parse_line(line)

    def test_a_checks_wake_is_progress_on_that_pr(self):
        row = wl.from_wake(self.spec('WAKE in=10m check="gh pr checks 209" then="merge it"'),
                           alias="DeepSeek", topic="tg-3556", at=1000.0)
        self.assertEqual((row.kind, row.item, row.debtor), ("progress", "#209", "DeepSeek"))
        self.assertIn("gh pr checks 209", row.owes)          # the promise, still written down
        self.assertIn("merge it", row.opened_by)
        self.assertEqual(row.topic, "tg-3556")
        self.assertEqual(row.deadline, 1600.0)               # when the timer would have fired

    def test_a_check_on_nothing_is_progress_with_no_item(self):
        row = wl.from_wake(self.spec('WAKE in=1m check="git status" then="go"'),
                           alias="DeepSeek", at=1000.0)
        self.assertEqual((row.kind, row.item), ("progress", ""))
        self.assertEqual(row.deadline, 1060.0)

    def test_the_row_always_has_the_moment_the_line_named(self):
        """`in=` is required by the grammar, so every row carries its own deadline: the flag for
        a row nothing could act on was unreachable and is gone (#213 review)."""
        from swarmly.telegram import wake as wk
        with self.assertRaises(wk.WakeError):
            wk.parse_line('WAKE check="git status" then="go"')      # no `in=` never parses
        row = wl.from_wake(self.spec('WAKE in=2m check="git status" then="go"'),
                           alias="DeepSeek", at=1000.0)
        self.assertEqual(row.deadline, 1120.0)
        self.assertEqual(row.flag, "")

    def test_two_checks_on_one_pr_are_two_promises(self):
        same = wl.from_wake(self.spec('WAKE in=1m check="gh pr checks 209" then="go"'),
                            alias="DeepSeek", at=1000.0)
        again = wl.from_wake(self.spec('WAKE in=1m check="gh pr checks 209" then="go"'),
                             alias="DeepSeek", at=2000.0)
        other = wl.from_wake(self.spec('WAKE in=1m check="gh pr diff 209" then="go"'),
                             alias="DeepSeek", at=1000.0)
        self.assertEqual(same.id, again.id)                  # re-arming one check is one row
        self.assertNotEqual(same.id, other.id)


class TestTheTransportRecordsIt(unittest.TestCase):
    """`apply_wait_lines`: the row is saved, the ack is posted, the line never is."""

    def channel(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from tests.test_telegram_channel import make_channel, make_config
        channel = make_channel(make_config())
        channel.bot = object()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ow.OwedStore("Hermes", state_dir=Path(directory.name), clock=lambda: 1000.0)
        channel.owed_store = lambda: store
        channel._test_table = (directory, store)
        channel.topics.remember(GROUP, 307, "round 5.5")   # the home group: it has a token
        return channel

    def event(self):
        from tests.test_telegram_channel import event
        return event("anything", chat_id=GROUP, thread_id=307, message_id=90)

    def test_a_wait_line_becomes_a_row_in_that_discussion(self):
        channel = self.channel()
        said = channel.apply_wait_lines(
            "Reviewed the head.\nWAIT review #191 head e326779 from Reasonix", self.event())
        rows = channel._test_table[1].load()
        self.assertEqual([row.kind for row in rows], ["verdict"])
        self.assertEqual(rows[0].debtor, "Reasonix")
        self.assertEqual(rows[0].topic, channel.topics.context(GROUP, 307))
        self.assertIn("Reasonix", said)
        self.assertIn("e326779", said)
        self.assertNotIn("WAIT review", said)                 # never posted as prose
        self.assertIn("Reviewed the head.", said)

    def test_a_refused_line_says_why_and_records_nothing(self):
        channel = self.channel()
        said = channel.apply_wait_lines("Here it is.\nWAIT review #191", self.event())
        self.assertEqual(channel._test_table[1].load(), [])
        self.assertIn("WAIT refused", said)
        self.assertIn("needs the head", said)

    def test_two_questions_to_the_admin_are_two_rows(self):
        channel = self.channel()
        channel.apply_wait_lines(
            "Two things I need.\nWAIT answer on the Drive credentials\n"
            "WAIT answer on the CT100 network", self.event())
        rows = channel._test_table[1].load()
        said = sorted(row.owes for row in rows)
        self.assertEqual(len(rows), 2, said)                 # one row per question, not one

    def test_a_reply_with_no_wait_line_is_untouched(self):
        channel = self.channel()
        text = "Nothing owed here, just a sentence."
        self.assertEqual(channel.apply_wait_lines(text, self.event()), text)

    def test_a_table_that_refuses_says_so_instead_of_pretending(self):
        channel = self.channel()

        def broken():
            raise ow.OwedError("the bus rejected the push")

        channel.owed_store = broken
        with self.assertLogs("swarmly.telegram", level="WARNING"):
            said = channel.apply_wait_lines("Done.\nWAIT progress #166 from Agy", self.event())
        self.assertIn("WAIT not recorded", said)
        self.assertIn("the bus rejected the push", said)
