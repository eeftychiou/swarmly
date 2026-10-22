"""Who gets woken, and with what news (round 5.5, #163): the decision, frozen as facts.

The round-5 replays this item is drawn from are here as facts rather than transcripts: the
standing `gh issue view 120` check whose output never changed, #146's CI turning green, and
DeepSeek owing a push on #133.
"""

import unittest

from swarmly.telegram import owed as ow
from swarmly.telegram import owed_wake as wk


def row(**fields):
    fields.setdefault("kind", "verdict")
    fields.setdefault("item", "#146")
    fields.setdefault("debtor", "Reasonix")
    fields.setdefault("owes", "a verdict on c166987")
    fields.setdefault("head", "c166987")
    fields.setdefault("topic", "tg-5122")
    return ow.Row(**fields)


class TestNoNewsNoTurn(unittest.TestCase):
    """The standing check that cost round 5 half a day: unchanged output, unchanged row."""

    def test_a_row_that_has_not_changed_wakes_nobody(self):
        self.assertEqual(wk.plan([row(observed="pending", woken="pending")]), [])

    def test_a_row_never_observed_wakes_nobody(self):
        self.assertEqual(wk.plan([row(observed="", woken="")]), [])

    def test_a_closed_row_never_wakes_anybody(self):
        self.assertEqual(wk.plan([row(observed="green", woken="pending", state=ow.CLOSED)]), [])


class TestWhatIsNotABot(unittest.TestCase):
    """A wake starts a turn, so it needs a chat handler behind the debtor (#163 review)."""

    def test_a_ci_row_is_observed_and_wakes_nobody(self):
        ci = ow.Row(kind="ci", item="#146", debtor=ow.CI, owes="a result on c166987",
                    head="c166987", observed="green", woken="pending", topic="tg-5122")
        self.assertEqual(wk.plan([ci]), [])

    def test_an_admin_row_wakes_nobody(self):
        question = ow.Row(kind="admin", item="#120", debtor=ow.ADMIN, owes="an answer",
                          observed="asked", woken="", topic="tg-5122")
        self.assertEqual(wk.plan([question]), [])

    def test_a_debtor_off_the_roster_wakes_nobody(self):
        stranger = row(debtor="Stranger", observed="green", woken="pending")
        self.assertEqual(wk.plan([stranger], bots={"Reasonix", "Codex"}), [])

    def test_a_debtor_on_the_roster_wakes(self):
        known = row(debtor="Codex", observed="green", woken="pending")
        wakes = wk.plan([known], bots={"Reasonix", "Codex"})
        self.assertEqual([wake.debtor for wake in wakes], ["Codex"])


class TestTheDelta(unittest.TestCase):

    def test_the_wake_names_the_row_and_the_change(self):
        wakes = wk.plan([row(observed="green", woken="pending")], now=1000.0)
        self.assertEqual(len(wakes), 1)
        wake = wakes[0]
        self.assertEqual(wake.debtor, "Reasonix")
        self.assertEqual(wake.row, "verdict:#146@c166987:Reasonix")
        self.assertIn("pending to green", wake.text)
        self.assertIn("#146", wake.text)
        self.assertIn("c166987", wake.text)

    def test_the_head_is_not_said_twice(self):
        """``owes`` already names the head; repeating it reads as a bug, not a fact."""
        text = wk.plan([row(observed="green", woken="pending")], now=1000.0)[0].text
        self.assertEqual(text, "a verdict on c166987 on #146: pending to green")

    def test_a_head_the_owes_does_not_name_is_added(self):
        merge = row(kind="merge", owes="a merge of #146", observed="waiting", woken="")
        merge.observed = "CI green"
        self.assertIn("at c166987", wk.plan([merge], now=1000.0)[0].text)

    def test_the_wake_carries_the_discussion_it_belongs_to(self):
        self.assertEqual(wk.plan([row(observed="green")])[0].topic, "tg-5122")


class TestAWakeNeedsNews(unittest.TestCase):

    def test_the_same_news_twice_is_not_a_wake(self):
        first = row(observed="green", woken="pending")
        wakes = wk.plan([first], now=1000.0)
        self.assertEqual(len(wakes), 1)
        # The transport saves what mark_woken returns; the next evaluation is quiet.
        saved = wk.mark_woken([first], wakes, now=1000.0)[0]
        self.assertEqual(saved.woken, "green")
        self.assertEqual(saved.wakes, 1)
        self.assertEqual(saved.woken_at, 1000.0)
        self.assertEqual(wk.plan([saved], now=1060.0), [])

    def test_an_ignored_wake_is_nudged_once_and_then_goes_to_the_admin(self):
        """The walk the transport actually makes -- no hand-seeded ``wakes`` (#163 review)."""
        stuck = row(observed="green", woken="pending")
        first = wk.plan([stuck], now=1000.0)
        self.assertEqual([wake.escalated for wake in first], [False])
        stuck = wk.mark_woken([stuck], first, now=1000.0)[0]
        self.assertEqual(stuck.wakes, 1)
        # Still inside the grace: the turn this woke may well be running.
        self.assertEqual(wk.plan([stuck], now=1060.0), [])
        # Grace spent and nothing changed: one nudge, naming the debt and saying so.
        nudge = wk.plan([stuck], now=1000.0 + wk.WAKE_GRACE + 1)
        self.assertEqual([wake.escalated for wake in nudge], [False])
        self.assertEqual(nudge[0].debtor, "Reasonix")
        self.assertIn("nothing has changed since the last wake", nudge[0].text)
        stuck = wk.mark_woken([stuck], nudge, now=1000.0 + wk.WAKE_GRACE + 1)[0]
        self.assertEqual(stuck.wakes, wk.MAX_WAKES)
        # The nudge gets its own grace, and then the admin is told -- once.
        quiet = 1000.0 + 2 * wk.WAKE_GRACE + 2
        self.assertEqual(wk.plan([stuck], now=quiet - 60), [])
        told = wk.plan([stuck], now=quiet, admin_topic="tg-admin")
        self.assertEqual([wake.debtor for wake in told], ["admin"])
        self.assertTrue(told[0].escalated)
        self.assertIn("has not acted", told[0].text)
        stuck = wk.mark_woken([stuck], told, now=quiet)[0]
        self.assertTrue(stuck.escalated)
        self.assertEqual(wk.plan([stuck], now=quiet + 3600), [])   # said once, not every beat

    def test_an_escalation_goes_to_the_admin_not_to_the_rows_topic(self):
        stuck = row(observed="green", woken="green", wakes=wk.MAX_WAKES, topic="tg-5122")
        told = wk.plan([stuck], now=5000.0, admin_topic="tg-home")
        self.assertEqual(told[0].topic, "tg-home")

    def test_news_after_an_escalation_starts_the_count_again(self):
        stalled = row(observed="green", woken="green", wakes=wk.MAX_WAKES, escalated=True)
        stalled.observed = "red"                      # the fact moved again
        wakes = wk.plan([stalled], now=9000.0)
        self.assertEqual([wake.escalated for wake in wakes], [False])
        saved = wk.mark_woken([stalled], wakes, now=9000.0)[0]
        self.assertEqual(saved.wakes, 1)
        self.assertFalse(saved.escalated)

    def test_a_new_head_is_a_new_debt_and_wakes_again(self):
        """The head is in the row's id, so new code is a new row with no woken value."""
        old_head = row(debtor="Reasonix", head="aaaaaaa", owes="a review verdict on aaaaaaa",
                       observed="no verdict yet", woken="no verdict yet", wakes=1,
                       woken_at=1000.0)
        self.assertEqual(wk.plan([old_head], now=1000.0), [])   # that head's news is spent
        new_head = row(debtor="Reasonix", head="bbbbbbb", owes="a review verdict on bbbbbbb",
                       observed="no verdict yet", woken="")
        wakes = wk.plan([old_head, new_head], now=1000.0)
        self.assertEqual([(wake.debtor, wake.row) for wake in wakes],
                         [("Reasonix", "verdict:#146@bbbbbbb:Reasonix")])


class TestTheRoundFiveReplays(unittest.TestCase):

    def test_146_ci_turning_green_wakes_the_coordinator(self):
        merge = row(kind="merge", debtor="Claudebot", owes="a merge of #146",
                    observed="waiting", woken="waiting")
        merge.observed = "CI green, review merge, QA pass"
        wakes = wk.plan([merge])
        self.assertEqual([wake.debtor for wake in wakes], ["Claudebot"])
        self.assertIn("CI green", wakes[0].text)

    def test_133_a_push_nobody_made_is_a_contradiction_not_a_closed_row(self):
        """DeepSeek owed a push on #133 and claimed one; the head never moved."""
        claims = [("DeepSeek", "#133", "pushed the fix", "aaaaaaa")]
        found = wk.contradictions(claims, heads={"#133": "38f1977"})
        self.assertEqual(len(found), 1)
        self.assertIn("the head is 38f1977, not aaaaaaa", found[0].fact)
        line = wk.notice(found[0])
        self.assertIn("DeepSeek said pushed the fix on #133", line)
        self.assertIn("stays open", line)

    def test_a_claim_the_facts_support_is_not_flagged(self):
        self.assertEqual(wk.contradictions([("DeepSeek", "#133", "pushed", "38f1977")],
                                          heads={"#133": "38f1977"}), [])

    def test_a_short_sha_the_full_head_agrees_with_is_not_a_contradiction(self):
        """Bots say seven characters, ``gh`` answers forty (#163 review, QA)."""
        found = wk.contradictions([("DeepSeek", "#133", "pushed", "38f1977")],
                                  heads={"#133": "38f1977abcdef1234567890"})
        self.assertEqual(found, [])
        # ... and the other way round, when the facts hold the short form.
        self.assertEqual(wk.contradictions([("DeepSeek", "#133", "pushed",
                                             "38f1977abcdef1234567890")],
                                          heads={"#133": "38f1977"}), [])

    def test_a_different_head_is_still_a_contradiction(self):
        found = wk.contradictions([("DeepSeek", "#133", "pushed", "9e9e9e9")],
                                  heads={"#133": "38f1977abcdef1234567890"})
        self.assertEqual(len(found), 1)
        self.assertIn("not 9e9e9e9", found[0].fact)

    def test_one_turn_per_real_change_and_none_for_nothing_changed(self):
        """The acceptance, as facts: three rows, one changed, so one wake."""
        unchanged = row(observed="pending", woken="pending")
        changed = row(item="#146", observed="green", woken="pending")
        never = row(item="#120", observed="", woken="")
        wakes = wk.plan([unchanged, changed, never])
        self.assertEqual(len(wakes), 1)
        self.assertIn("#146", wakes[0].text)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
