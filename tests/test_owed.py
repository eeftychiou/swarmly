"""The expectations table (round 5.5, #162): who owes what, opened and closed by facts.

What these pin: a row's identity is its settlement rule (the same fact updates one row, never
opens a second); each of the six inferences opens the debt the fact implies and no other; a row
closes only on the fact that answers it -- an approval on older code answers nothing, and no
amount of a bot saying "done" closes anything; rows survive on disk and on the bus; and the two
round-5 stalls this item is drawn from (#146 never merged, #133's hand-off to DeepSeek) end with
a row that says who owes what.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import owed as ow


def timer_clock(start=1000.0):
    """A clock a test moves: rows compare deadlines to it, never to the wall."""
    state = {"t": float(start)}
    return state, lambda: state["t"]


class TestTheRow(unittest.TestCase):

    def row(self, **fields):
        fields.setdefault("kind", "verdict")
        fields.setdefault("item", "#170")
        fields.setdefault("debtor", "Reasonix")
        fields.setdefault("owes", "a review verdict on 22e68c5")
        fields.setdefault("head", "22e68c5")
        return ow.Row(**fields)

    def test_the_id_is_the_settlement_rule(self):
        first = self.row()
        again = self.row(owes="a review verdict on 22e68c5", observed="no verdict yet")
        self.assertEqual(first.id, again.id)
        self.assertEqual(first.id, "verdict:#170@22e68c5:Reasonix")
        # A different head is a different debt: the reviewer owes a verdict on the new code.
        self.assertNotEqual(first.id, self.row(head="504d426").id)

    def test_an_unknown_kind_or_state_is_refused(self):
        with self.assertRaises(ow.OwedError):
            self.row(kind="nonsense")
        with self.assertRaises(ow.OwedError):
            self.row(state="maybe")
        with self.assertRaises(ow.OwedError):
            self.row(debtor="")

    def test_it_round_trips(self):
        row = self.row(waiting_on="the round", opened_by="head 22e68c5 pushed",
                       observed="pending", deadline=2000.0, topic="tg-5122")
        again = ow.Row.from_dict(row.to_dict())
        self.assertEqual(again, row)
        self.assertEqual(again.id, row.id)

    def test_a_version_it_does_not_know_is_refused(self):
        data = self.row().to_dict()
        data["version"] = 99
        with self.assertRaises(ow.OwedError):
            ow.Row.from_dict(data)

    def test_the_line_says_who_owes_what_and_how_late(self):
        row = self.row(deadline=1900.0, observed="no verdict", waiting_on="Claudebot")
        self.assertEqual(row.line(now=1000.0),
                         "Reasonix owes a review verdict on 22e68c5 on #170 "
                         "(Claudebot waiting) [no verdict] 15m left")
        self.assertIn("overdue", self.row(deadline=900.0).line(now=1000.0))


class TestOpeningRowsFromFacts(unittest.TestCase):

    def test_a_hand_off_opens_a_row_for_every_peer_addressed(self):
        """The 22 Oct miss: a line addressed to two peers left #117 with DeepSeek."""
        rows = ow.handoff_rows(asked=["Reasonix", "@Antigravity"], asker="Claudebot",
                              item="#117", topic="tg-5122", at=1000.0)
        self.assertEqual(sorted(row.debtor for row in rows), ["Antigravity", "Reasonix"])
        for row in rows:
            self.assertEqual(row.kind, "handoff")
            self.assertEqual(row.owes, "an answer")
            self.assertEqual(row.waiting_on, "Claudebot")
            self.assertEqual(row.item, "#117")

    def test_a_bot_does_not_owe_itself_an_answer(self):
        rows = ow.handoff_rows(asked=["Reasonix", "Reasonix"], asker="reasonix", at=1000.0)
        self.assertEqual(rows, [])

    def test_a_new_head_opens_ci_and_both_verdicts_on_that_head(self):
        rows = ow.head_rows(item="#170", head="22e68c5",
                            roles={"review": "Reasonix", "qa": "Antigravity"}, at=1000.0)
        self.assertEqual([row.kind for row in rows], ["ci", "verdict", "verdict"])
        self.assertEqual([row.debtor for row in rows], [ow.CI, "Reasonix", "Antigravity"])
        self.assertTrue(all(row.head == "22e68c5" for row in rows))

    def test_a_role_with_no_owner_opens_nothing(self):
        rows = ow.head_rows(item="#170", head="22e68c5", roles={"review": "Reasonix"}, at=1000.0)
        self.assertEqual([row.debtor for row in rows], [ow.CI, "Reasonix"])

    def test_a_fix_first_verdict_makes_the_developer_owe_a_head(self):
        rows = ow.fix_rows(item="#169", head="09c2c23", developer="DeepSeek",
                           verdicts={"review": "fix-first", "qa": "pass"}, at=1000.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].debtor, "DeepSeek")
        self.assertIn("new head", rows[0].owes)

    def test_a_pass_or_a_merge_verdict_owes_the_developer_nothing(self):
        self.assertEqual(ow.fix_rows(item="#169", head="09c2c23", developer="DeepSeek",
                                     verdicts={"review": "merge", "qa": "pass"}), [])

    def test_merge_is_owed_only_when_all_three_facts_are_on_this_head(self):
        row = ow.merge_row(item="#172", head="54960ac", coordinator="Claudebot", ci_green=True,
                           verdicts={"review": "merge", "qa": "pass"}, at=1000.0)
        self.assertIsNotNone(row)
        self.assertEqual(row.debtor, "Claudebot")
        self.assertEqual(row.id, "merge:#172@54960ac:Claudebot")
        self.assertIsNone(ow.merge_row(item="#172", head="54960ac", coordinator="Claudebot",
                                       ci_green=False, verdicts={"review": "merge",
                                                                 "qa": "pass"}))
        self.assertIsNone(ow.merge_row(item="#172", head="54960ac", coordinator="Claudebot",
                                       ci_green=True, verdicts={"review": "merge",
                                                                "qa": "pending"}))

    def test_an_unticked_item_with_an_owner_owes_progress(self):
        rows = ow.progress_rows(items=[{"key": "162", "owner": "DeepSeek"},
                                      {"key": "163", "owner": "Claude", "done": True},
                                      {"key": "164", "owner": ""}], at=1000.0)
        self.assertEqual([row.item for row in rows], ["#162"])
        self.assertEqual(rows[0].debtor, "DeepSeek")

    def test_a_question_to_the_admin_owes_an_answer_and_nothing_more(self):
        row = ow.admin_row(question="Do I deploy round 5.5?", asked_by="Claudebot", at=1000.0)
        self.assertEqual(row.kind, "admin")
        self.assertEqual(row.debtor, ow.ADMIN)
        self.assertIn("Do I deploy", row.opened_by)
        # Only a reminder ever comes of this kind: no inference opens it as anything else.
        self.assertEqual(row.state, ow.OPEN)


class TestTheReviewPins(unittest.TestCase):
    """What the review of the design pinned down (#162): identity, vocabulary, two writers."""

    def test_a_debt_about_a_head_cannot_exist_without_one(self):
        for kind in ow.HEAD_BOUND:
            with self.subTest(kind=kind), self.assertRaises(ow.OwedError):
                ow.Row(kind=kind, item="#170", debtor="Reasonix", owes="something")
        # A debt that is not about a head needs none.
        self.assertEqual(ow.Row(kind="handoff", item="", debtor="Reasonix",
                                owes="an answer").head, "")

    def test_a_head_is_normalized_and_an_impossible_one_is_refused(self):
        row = ow.Row(kind="verdict", item="#170", debtor="Reasonix", owes="a verdict",
                     head="22E68C5")
        self.assertEqual(row.head, "22e68c5")
        self.assertEqual(row.id, "verdict:#170@22e68c5:Reasonix")
        with self.assertRaises(ow.OwedError):
            ow.Row(kind="verdict", item="#170", debtor="Reasonix", owes="a verdict",
                   head="not-a-sha")

    def test_the_verdict_vocabulary_is_what_bots_actually_write(self):
        self.assertEqual(ow.normalize_verdict("Changes requested"), ow.VERDICT_FIX)
        self.assertEqual(ow.normalize_verdict("LGTM"), ow.VERDICT_MERGE)
        self.assertEqual(ow.normalize_verdict("passed"), ow.VERDICT_PASS)
        rows = ow.fix_rows(item="#169", head="09c2c23", developer="DeepSeek",
                           verdicts={"review": "changes requested"})
        self.assertEqual(len(rows), 1)          # "changes requested" still opens the fix row
        self.assertIsNotNone(ow.merge_row(item="#172", head="54960ac", coordinator="Claudebot",
                                         ci_green=True,
                                         verdicts={"review": "approved", "qa": "green"}))

    def test_two_writers_fold_to_one_row_and_closed_beats_open(self):
        row = ow.handoff_rows(asked=["DeepSeek"], asker="Claudebot", at=1000.0)[0]
        mine = ow.Row(**{**row.to_dict(), "writer": "Claudebot", "updated_at": 1000.0})
        theirs = ow.Row(**{**row.to_dict(), "writer": "Reasonix", "state": ow.CLOSED,
                           "closed_by": "DeepSeek answered", "updated_at": 1000.0})
        folded = ow.fold([mine, theirs])
        self.assertEqual(len(folded), 1)
        self.assertFalse(folded[0].is_open)     # an answered debt is answered
        self.assertEqual(folded[0].writer, "Reasonix")

    def test_two_opens_fold_to_the_one_that_saw_more(self):
        row = ow.handoff_rows(asked=["DeepSeek"], asker="Claudebot", at=1000.0)[0]
        quiet = ow.Row(**{**row.to_dict(), "writer": "Claudebot", "updated_at": 1000.0})
        loud = ow.Row(**{**row.to_dict(), "writer": "Reasonix", "updated_at": 1200.0,
                         "observed": "no reply for 20m"})
        folded = ow.fold([quiet, loud])
        self.assertEqual(folded[0].observed, "no reply for 20m")

    def test_the_store_stamps_its_writer(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ow.OwedStore("DeepSeek", state_dir=Path(tmp.name))
        row = store.save(ow.handoff_rows(asked=["Reasonix"], asker="Claudebot", at=1000.0)[0])
        self.assertEqual(row.writer, "DeepSeek")
        self.assertEqual(store.load()[0].writer, "DeepSeek")


class TestClosingOnFacts(unittest.TestCase):

    def rows(self):
        return [*ow.head_rows(item="#170", head="22e68c5",
                              roles={"review": "Reasonix", "qa": "Antigravity"}, at=1000.0),
                ow.admin_row(question="deploy?", at=1000.0),
                *ow.handoff_rows(asked=["Reasonix"], asker="Claudebot", at=1000.0)]

    def test_a_merge_closes_everything_bound_to_the_item(self):
        rows = self.rows()
        closed = ow.settle(rows, ow.Fact(kind="merge", item="#170", head="22e68c5"))
        self.assertEqual(sorted(row.kind for row in closed), ["ci", "verdict", "verdict"])
        self.assertTrue(all(not row.is_open for row in closed))
        self.assertEqual(closed[0].closed_by.startswith("merged"), True)
        # The hand-off and the admin question are not answered by a merge.
        self.assertEqual([row.kind for row in rows if row.is_open], ["admin", "handoff"])

    def test_a_new_head_answers_the_old_head_s_verdicts_and_nothing_else(self):
        rows = ow.head_rows(item="#170", head="22e68c5",
                            roles={"review": "Reasonix", "qa": "Antigravity"}, at=1000.0)
        closed = ow.settle(rows, ow.Fact(kind="head", item="#170", head="9f9f9f9"))
        self.assertEqual(sorted(row.kind for row in closed), ["verdict", "verdict"])
        self.assertTrue(all(row.head == "22e68c5" for row in closed))
        ci = next(row for row in rows if row.kind == "ci")
        self.assertTrue(ci.is_open)          # the old head's CI row is still its own debt

    def test_a_verdict_closes_only_its_own_debtor_and_head(self):
        rows = ow.head_rows(item="#170", head="22e68c5",
                            roles={"review": "Reasonix", "qa": "Antigravity"}, at=1000.0)
        closed = ow.settle(rows, ow.Fact(kind="verdict", item="#170", head="22e68c5",
                                         who="Reasonix", verdict="merge"))
        self.assertEqual([row.debtor for row in closed], ["Reasonix"])
        # A verdict on older code answers nothing.
        rows = ow.head_rows(item="#170", head="22e68c5", roles={"review": "Reasonix"}, at=1000.0)
        self.assertEqual(ow.settle(rows, ow.Fact(kind="verdict", item="#170", head="1111111",
                                                 who="Reasonix")), [])

    def test_a_reply_closes_the_hand_off_it_answers(self):
        rows = ow.handoff_rows(asked=["Reasonix", "DeepSeek"], asker="Claudebot", at=1000.0)
        closed = ow.settle(rows, ow.Fact(kind="reply", who="DeepSeek"))
        self.assertEqual([row.debtor for row in closed], ["DeepSeek"])
        self.assertEqual([row.debtor for row in rows if row.is_open], ["Reasonix"])

    def test_the_admin_answering_closes_the_admin_row(self):
        row = ow.admin_row(question="deploy?", at=1000.0)
        closed = ow.settle([row], ow.Fact(kind="reply", who="admin"))
        self.assertEqual(len(closed), 1)

    def test_no_fact_for_prose_so_saying_done_closes_nothing(self):
        """The round's core rule: a bot's word is not a fact, so it is not even expressible."""
        self.assertNotIn("prose", [fact.kind for fact in (ow.Fact(kind="reply"),)])
        with self.assertRaises(ow.OwedError):
            ow.Fact(kind="done")
        rows = self.rows()
        # The closest a model can come is a reply about itself: it answers only a hand-off.
        closed = ow.settle(rows, ow.Fact(kind="reply", who="Reasonix"))
        self.assertEqual([row.kind for row in closed], ["handoff"])


class TestTheStore(unittest.TestCase):

    def store(self, alias="DeepSeek", transport=None, root=None):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return ow.OwedStore(alias, state_dir=Path(tmp.name), transport=transport,
                            root=Path(tmp.name) if root is None else root,
                            clock=lambda: 1000.0)

    def test_a_row_survives_a_restart(self):
        store = self.store()
        row = ow.handoff_rows(asked=["Reasonix"], asker="Claudebot", at=1000.0)[0]
        store.save(row)
        again = ow.OwedStore("DeepSeek", state_dir=store.state_dir)
        self.assertEqual([r.id for r in again.open_rows()], [row.id])

    def test_one_file_per_row_so_two_writers_cannot_lose_each_other(self):
        store = self.store()
        for row in ow.head_rows(item="#170", head="22e68c5",
                                roles={"review": "Reasonix", "qa": "Antigravity"}, at=1000.0):
            store.save(row)
        files = sorted(path.name for path in store.directory.glob("*.json"))
        self.assertEqual(len(files), 3)

    def test_an_unreadable_row_is_reported_not_read_as_no_debt(self):
        store = self.store()
        store.save(ow.handoff_rows(asked=["Reasonix"], asker="Claudebot", at=1000.0)[0])
        (store.directory / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(len(store.open_rows()), 1)
        self.assertEqual(store.unreadable, ["broken.json"])
        # ... and the summary never claims the table is empty when a read failed.
        empty = self.store()
        empty.directory.mkdir(parents=True, exist_ok=True)
        (empty.directory / "broken.json").write_text("{", encoding="utf-8")
        self.assertIn("cannot read", empty.summary())

    def test_a_row_the_store_already_knows_keeps_what_it_learned(self):
        store = self.store()
        row = ow.handoff_rows(asked=["DeepSeek"], asker="Claudebot", at=1000.0)[0]
        row.observed = "no reply yet"
        store.save(row)
        fresh = ow.handoff_rows(asked=["DeepSeek"], asker="Claudebot", at=1200.0)[0]
        observed = store.observe([fresh])
        self.assertEqual(observed[0].observed, "no reply yet")   # not reset by re-observation
        self.assertEqual(observed[0].created_at, 1000.0)

    def test_open_rows_come_most_overdue_first(self):
        store = self.store()
        soon, later = ow.handoff_rows(asked=["Reasonix"], asker="C", at=1000.0,
                                     deadline=5000.0)[0], \
            ow.handoff_rows(asked=["Antigravity"], asker="C", at=1000.0, deadline=2000.0)[0]
        store.save(soon)
        store.save(later)
        self.assertEqual([row.debtor for row in store.open_rows(now=1000.0)],
                         ["Antigravity", "Reasonix"])

    def test_the_summary_lists_what_is_open(self):
        store = self.store()
        self.assertEqual(store.summary(now=1000.0), "owed: nothing")
        store.save(ow.handoff_rows(asked=["DeepSeek"], asker="Claudebot", at=1000.0)[0])
        self.assertIn("DeepSeek owes an answer", store.summary(now=1000.0))

    def test_a_row_the_bus_refuses_is_reported(self):
        class Refusing:
            ref = "refs/heads/main"

            def commit_and_push(self, message, paths=None):
                raise RuntimeError("push failed")

        store = self.store(transport=Refusing())
        with self.assertRaises(ow.OwedPushError):
            store.save(ow.handoff_rows(asked=["Reasonix"], asker="Claudebot", at=1000.0)[0])
        # ... and the local copy stands: the debt is not lost because the bus was down.
        self.assertEqual(len(store.open_rows()), 1)

    def test_the_bus_path_is_a_document_and_names_the_bot(self):
        pushed = []

        class Bus:
            ref = "refs/heads/main"

            def commit_and_push(self, message, paths=None):
                pushed.append((message, list(paths or [])))

        store = self.store(transport=Bus())
        store.save(ow.handoff_rows(asked=["Reasonix"], asker="Claudebot", at=1000.0)[0])
        self.assertEqual(len(pushed), 1)
        self.assertTrue(pushed[0][1][0].startswith(".agents/owed/deepseek/"))


class TestRoundFiveReplays(unittest.TestCase):
    """The two stalls #162 is drawn from, end to end on facts alone."""

    def store(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state, clock = timer_clock()
        return state, ow.OwedStore("Claudebot", state_dir=Path(tmp.name), clock=clock)

    def test_146_the_ci_wait_that_nobody_closed(self):
        """#146 sat unmerged: a head pushed, CI owed a result, and no row said so."""
        state, store = self.store()
        for row in ow.head_rows(item="#146", head="38f1977",
                                roles={"review": "Reasonix", "qa": "Antigravity"},
                                at=state["t"], deadline=state["t"] + 3600):
            store.save(row)
        state["t"] += 7200                                     # two hours of nobody looking
        rows = store.open_rows(now=state["t"])
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.deadline < state["t"] for row in rows))
        self.assertIn("overdue", rows[0].line(now=state["t"]))

        # CI concludes on that head: only the CI row is answered.
        closed = ow.settle(rows, ow.Fact(kind="ci", item="#146", head="38f1977",
                                         verdict="pass"))
        for row in closed:
            store.save(row)
        self.assertEqual(len(store.open_rows(now=state["t"])), 2)
        # ... and the facts that end it close what is left.
        merge = ow.merge_row(item="#146", head="38f1977", coordinator="Claudebot", ci_green=True,
                             verdicts={"review": "merge", "qa": "pass"}, at=state["t"])
        self.assertIsNotNone(merge)
        store.save(merge)
        closed = ow.settle(store.open_rows(now=state["t"]),
                           ow.Fact(kind="merge", item="#146", head="38f1977"))
        for row in closed:
            store.save(row)
        self.assertEqual(store.open_rows(now=state["t"]), [])

    def test_133_the_hand_off_that_missed_deepseek(self):
        """A request to two peers: DeepSeek was never named in the tracker, so it stalled."""
        state, store = self.store()
        asked = ow.handoff_rows(asked=["Reasonix", "DeepSeek"], asker="Claudebot",
                                item="#133", topic="tg-5122", at=state["t"],
                                deadline=state["t"] + 600)
        for row in asked:
            store.save(row)
        self.assertEqual([row.debtor for row in store.open_rows(now=state["t"])],
                         ["DeepSeek", "Reasonix"])
        state["t"] += 900
        # Reasonix answers; DeepSeek's debt stays open, and it is now overdue.
        closed = ow.settle(store.open_rows(now=state["t"]),
                           ow.Fact(kind="reply", item="#133", who="Reasonix"))
        for row in closed:
            store.save(row)
        open_now = store.open_rows(now=state["t"])
        self.assertEqual([row.debtor for row in open_now], ["DeepSeek"])
        self.assertIn("overdue", open_now[0].line(now=state["t"]))


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
