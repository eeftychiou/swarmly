"""The frozen head: a claimed SHA checked against the PR, and a rewrite under review (#183).

The acceptance for this item is a simulator: a claimed SHA that is not the head, and a
force-push during a review, must both be flagged. The two round-5.5 live cases are the facts
here -- `621722f` reported while #173 was at `a7b0355`, and #173 rebuilt under review.
"""

import unittest

from swarmly.telegram import head_freeze as hf
from swarmly.telegram import owed as ow
from tests.test_telegram_channel import GROUP

#: #173's head when a bot claimed otherwise (round 5.5, 18:54).
CURRENT = "a7b0355c4e1d2f3a4b5c6d7e8f9012345678abcd"


def verdict_row(**fields):
    fields.setdefault("kind", "verdict")
    fields.setdefault("item", "#173")
    fields.setdefault("debtor", "Reasonix")
    fields.setdefault("head", "a7b0355")
    fields.setdefault("owes", "a review verdict on a7b0355")
    fields.setdefault("topic", "tg-5122")
    return ow.Row(**fields)


class TestAClaimedShaIsCheckedAgainstThePr(unittest.TestCase):

    def test_a_claimed_head_that_is_not_the_pr_head_is_flagged(self):
        """The live case: DeepSeek reported 621722f while #173 was still at a7b0355."""
        found = hf.claims("#173 is at 621722f, review it.", ["#173"])
        self.assertEqual([(claim.item, claim.head) for claim in found],
                         [("#173", "621722f")])
        bad = hf.mismatches(found, heads={"#173": CURRENT})
        self.assertEqual([(m.item, m.claimed) for m in bad], [("#173", "621722f")])
        line = hf.claim_notice(bad[0])
        self.assertIn("#173 is at a7b0355", line)
        self.assertIn("not 621722f", line)
        self.assertIn("stays open", line)

    def test_a_claim_the_facts_support_is_quiet(self):
        for said in ("#173 is at a7b0355", "#173 @ a7b0355", "pushed a7b0355 to #173",
                     "head a7b0355 on #173"):
            found = hf.claims(said, ["#173"])
            self.assertEqual(len(found), 1, said)
            self.assertEqual(hf.mismatches(found, heads={"#173": CURRENT}), [], said)

    def test_a_bare_sha_is_not_a_claim(self):
        """Commit ids are written for all sorts of reasons; the notice must not add noise."""
        self.assertEqual(hf.claims("rebased onto c166987 and pushed 5b90b50", ["#173"]), [])
        self.assertEqual(hf.claims("#173 conflicts with main", ["#173"]), [])
        # In the item's own clause, but not offered as its head: "the culprit was 621722f"
        # names a commit, not where the PR is.
        self.assertEqual(hf.claims("#173 is blocked: the culprit was 621722f", ["#173"]), [])
        self.assertEqual(hf.claims("rebased onto c166987 but #173 is untouched", ["#173"]), [])

    def test_a_sha_in_the_next_sentence_is_not_this_items(self):
        text = "#173 is ready. The commit 621722f fixed the ladder."
        self.assertEqual(hf.claims(text, ["#173"]), [])

    def test_an_item_no_round_state_knows_is_not_a_mismatch(self):
        found = hf.claims("#999 is at 621722f", ["#999"])
        self.assertEqual(hf.mismatches(found, heads={}), [])


class TestARewriteUnderReview(unittest.TestCase):

    def test_a_head_that_moved_under_an_open_verdict_is_flagged(self):
        """The #173 case: a verdict open on a7b0355 while the branch was rebuilt."""
        rows = [verdict_row(head="a7b0355")]
        found = hf.rewrites(rows, heads={"#173": "18f5f0e"})
        self.assertEqual([(r.item, r.head, r.now, r.debtor) for r in found],
                         [("#173", "a7b0355", "18f5f0e", "Reasonix")])
        line = hf.rewrite_notice(found[0])
        self.assertIn("head moved while a verdict was open", line)
        self.assertIn("nits go to follow-up issues", line)

    def test_the_same_head_is_not_a_rewrite(self):
        rows = [verdict_row(head="a7b0355")]
        self.assertEqual(hf.rewrites(rows, heads={"#173": CURRENT}), [])

    def test_a_closed_verdict_is_not_a_rewrite(self):
        rows = [verdict_row(head="a7b0355", state=ow.CLOSED)]
        self.assertEqual(hf.rewrites(rows, heads={"#173": "18f5f0e"}), [])

    def test_no_head_in_the_facts_is_not_a_rewrite(self):
        rows = [verdict_row(head="a7b0355")]
        self.assertEqual(hf.rewrites(rows, heads={}), [])


class TestReadyMeansMergeable(unittest.TestCase):

    def test_a_conflicting_pr_called_ready_is_flagged(self):
        blockers = hf.ready_blockers({"head": CURRENT}, mergeable="CONFLICTING", item_key="173")
        self.assertEqual(len(blockers), 1)
        self.assertIn("conflicts with main", blockers[0])
        self.assertIn("not ready", hf.ready_notice(blockers))

    def test_a_mergeable_pr_has_no_blocker_from_the_facts(self):
        self.assertEqual(hf.ready_blockers({"head": CURRENT}, mergeable="MERGEABLE",
                                           item_key="173"), [])

    def test_a_head_older_than_the_verdicts_is_a_blocker(self):
        blockers = hf.ready_blockers({"head": "a7b0355"}, mergeable="MERGEABLE",
                                     heads={"#173": "18f5f0e"}, item_key="173")
        self.assertEqual(len(blockers), 1)
        self.assertIn("not the head", blockers[0])


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()


class TestTheTransportSaysItOnce(unittest.TestCase):
    """The wiring: one notice per claim and per rewrite, in the discussion the debt lives in."""

    def channel(self, items):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from swarmly.telegram import owed as owed_mod
        from tests.test_telegram_channel import make_channel, make_config

        bus = TemporaryDirectory()
        self.addCleanup(bus.cleanup)
        channel = make_channel(make_config(fallback={"gitbus_root": bus.name,
                                                    "gitbus_ref": ""}))
        channel.bot = object()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = owed_mod.OwedStore("Hermes", state_dir=Path(directory.name),
                                   clock=lambda: 1000.0)
        channel.owed_store = lambda: store
        channel._test_table = (directory, store)
        channel.topics.remember(GROUP, 5122, "round 5.5")
        channel._test_topic = channel.topics.context(GROUP, 5122)
        channel._head_cache = (channel._clock(), {
            f"#{key}": {"head": str(item.get("head") or ""),
                        "mergeable": str(item.get("mergeable") or "")}
            for key, item in items.items()})
        notices = []
        channel._post_notice = lambda text, chat, thread: notices.append((text, chat, thread))
        channel._test_notices = notices
        channel.round_store = lambda: _Rounds(items)
        return channel

    def test_a_claimed_head_that_is_not_the_pr_head_is_said_once(self):
        channel = self.channel({"173": {"head": "a7b0355"}})
        channel.owed_store().save_all([verdict_row(head="a7b0355", topic=channel._test_topic)])
        said = channel._check_head_claims("#173 is at 621722f, review it.",
                                         9600, 5122)
        self.assertEqual(len(said), 1)
        self.assertEqual(len(channel._test_notices), 1)
        text, chat, thread = channel._test_notices[0]
        self.assertIn("not 621722f", text)
        self.assertEqual((chat, thread), (9600, 5122))          # said where the claim was made
        self.assertEqual(channel._check_head_claims("#173 is at 621722f", 9600, 5122), [])
        self.assertEqual(len(channel._test_notices), 1)         # once, not every message

    def test_a_rewrite_under_review_is_flagged_by_the_sweep(self):
        """The review's finding: `settle` closes exactly the rows the check reads.

        So the check runs **inside the sweep, before the closing facts** -- driving it after the
        sweep found nothing at all (#183 review).
        """
        channel = self.channel({"173": {"pr": 173, "head": "18f5f0e"}})
        store = channel.owed_store()
        store.save_all([verdict_row(head="a7b0355", topic=channel._test_topic)])
        channel.owed_sweep(now=1000.0)
        self.assertEqual(len(channel._test_notices), 1)
        text, chat, thread = channel._test_notices[0]
        self.assertIn("head moved while a verdict was open", text)
        self.assertEqual((chat, thread), (GROUP, 5122))        # the row's own discussion
        # And the row the sweep then closes is closed by the fact, not by the notice.
        closed = [row for row in store.load() if row.id == "verdict:#173@a7b0355:Reasonix"]
        self.assertFalse(closed[0].is_open)
        channel.owed_sweep(now=1060.0)                          # said once, not every beat
        self.assertEqual(len(channel._test_notices), 1)

    def test_an_item_called_ready_is_checked_against_the_facts(self):
        """#183's third rule, wired: "ready" is a claim like a SHA is."""
        channel = self.channel({"173": {"head": "18f5f0e"}})
        said = channel._check_head_claims("#173 is ready to merge", 9600, 5122)
        self.assertEqual(said, [])                              # nothing the facts know blocks it
        self.assertEqual(channel._test_notices, [])
        # A ready claim about a head the verdicts were not given on is blocked.
        channel._head_cache = (channel._clock(), {"#173": {"head": "18f5f0e", "mergeable": ""}})
        channel.owed_store().save_all([verdict_row(head="a7b0355")])
        channel._round_facts = lambda: {"#173": {"head": "18f5f0e", "mergeable": "CONFLICTING"}}
        said = channel._check_head_claims("#173 is ready", 9600, 5122)
        self.assertEqual(len(said), 1)
        self.assertIn("conflicts with main", channel._test_notices[-1][0])


class _Rounds:
    """A round store with items only: the head is the fact these tests turn on."""

    def __init__(self, items):
        self.items = items

    def rounds(self):
        return ["repo#167"]

    def load(self, _key):
        from swarmly.telegram.rounds import RoundState
        return RoundState(round="repo#167", status="ok", items=self.items, source="ref")
