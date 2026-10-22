"""Who gets woken, and with what news (round 5.5, #163): the decision, frozen as facts.

The round-5 replays this item is drawn from are here as facts rather than transcripts: the
standing `gh issue view 120` check whose output never changed, #146's CI turning green, and
DeepSeek owing a push on #133.
"""

import unittest

from swarmly.telegram import owed as ow
from swarmly.telegram import owed_wake as wk


def row(**fields):
    fields.setdefault("kind", "ci")
    fields.setdefault("item", "#146")
    fields.setdefault("debtor", ow.CI)
    fields.setdefault("owes", "a result on c166987")
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


class TestTheDelta(unittest.TestCase):

    def test_the_wake_names_the_row_and_the_change(self):
        wakes = wk.plan([row(observed="green", woken="pending")], now=1000.0)
        self.assertEqual(len(wakes), 1)
        wake = wakes[0]
        self.assertEqual(wake.debtor, ow.CI)
        self.assertEqual(wake.row, "ci:#146@c166987:CI")
        self.assertIn("pending to green", wake.text)
        self.assertIn("#146", wake.text)
        self.assertIn("c166987", wake.text)

    def test_the_wake_carries_the_discussion_it_belongs_to(self):
        self.assertEqual(wk.plan([row(observed="green")])[0].topic, "tg-5122")


class TestAWakeNeedsNews(unittest.TestCase):

    def test_the_same_news_twice_is_not_a_wake(self):
        first = row(observed="green", woken="pending")
        wakes = wk.plan([first])
        self.assertEqual(len(wakes), 1)
        # The transport saves what mark_woken returns; the next evaluation is quiet.
        saved = wk.mark_woken([first], wakes)[0]
        self.assertEqual(saved.woken, "green")
        self.assertEqual(saved.wakes, 1)
        self.assertEqual(wk.plan([saved]), [])

    def test_a_third_repeat_goes_to_the_admin_once(self):
        """A debt nobody acts on is the admin's to see, not a bot's to be nagged about."""
        stuck = row(observed="green", woken="green", wakes=wk.MAX_WAKES)
        wakes = wk.plan([stuck])
        self.assertEqual(len(wakes), 1)
        self.assertTrue(wakes[0].escalated)
        self.assertEqual(wakes[0].debtor, "admin")
        self.assertIn("has not acted", wakes[0].text)
        saved = wk.mark_woken([stuck], wakes)[0]
        self.assertTrue(saved.escalated)
        self.assertEqual(wk.plan([saved]), [])          # said once, not every beat

    def test_a_new_head_is_a_new_debt_and_wakes_again(self):
        """The head is in the row's id, so new code is a new row with no woken value."""
        old_head = ow.Row(kind="verdict", item="#170", debtor="Reasonix", head="aaaaaaa",
                          owes="a review verdict on aaaaaaa", observed="no verdict yet",
                          woken="no verdict yet", wakes=1, topic="tg-5122")
        self.assertEqual(wk.plan([old_head]), [])        # that head's news is spent
        new_head = ow.Row(kind="verdict", item="#170", debtor="Reasonix", head="bbbbbbb",
                          owes="a review verdict on bbbbbbb", observed="no verdict yet",
                          topic="tg-5122")
        wakes = wk.plan([old_head, new_head])
        self.assertEqual([(wake.debtor, wake.row) for wake in wakes],
                         [("Reasonix", "verdict:#170@bbbbbbb:Reasonix")])


class TestTheRoundFiveReplays(unittest.TestCase):

    def test_146_ci_turning_green_wakes_the_coordinator(self):
        merge = ow.Row(kind="merge", item="#146", debtor="Claudebot", head="38f1977",
                       owes="a merge of #146", observed="waiting", woken="waiting",
                       topic="tg-5122")
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
