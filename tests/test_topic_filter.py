"""The topic filters the panel and ``/topics`` share (#438): each dimension, and all of them.

One registry, three questions. What these pin: a link's type (an issue, a PR, a Drive/cloud/
local link, or none), its state (GitHub's, or the record's own), its stage (the phase), that
they combine, that the default view is "open topics with something still to do", and that the
words an admin types -- ``/topics stage=review`` -- are the words the page's URL carries.
"""

from __future__ import annotations

import unittest

from swarmly.telegram import topic_filter as tf

ISSUE = {"provider": "github", "kind": "issue", "ref": "https://github.com/o/r/issues/12",
         "role": "work", "status": "open", "phase": "development"}
PR = {"provider": "github", "kind": "pr", "ref": "https://github.com/o/r/pull/30",
      "role": "work", "status": "open", "phase": "review"}
DRIVE = {"provider": "gdrive", "kind": "doc", "ref": "drive:/x", "role": "work",
         "status": "open", "phase": "design"}
DONE = {"provider": "github", "kind": "issue", "ref": "https://github.com/o/r/issues/9",
        "role": "work", "status": "closed", "phase": "done"}


def topic(*links, status="open"):
    return {"topic_id": 1, "status": status, "links": list(links)}


class TestTheTypeOfALink(unittest.TestCase):
    def test_an_issue_a_pr_something_else_and_nothing(self):
        self.assertEqual(tf.item_type(ISSUE), "issue")
        self.assertEqual(tf.item_type(PR), "pr")
        self.assertEqual(tf.item_type(DRIVE), "other")
        self.assertEqual(tf.item_type(None), "none")
        # A GitHub PR that only says so in its URL is still a PR.
        self.assertEqual(tf.item_type({"provider": "github",
                                       "ref": "https://github.com/o/r/pull/7"}), "pr")

    def test_a_card_that_already_carries_its_type_is_believed(self):
        """The panel publishes the type it computed, so the page cannot read it differently."""
        self.assertEqual(tf.item_type({"type": "pr", "provider": "gdrive"}), "pr")


class TestTheStateOfALink(unittest.TestCase):
    def test_closed_covers_the_words_a_work_item_is_closed_in(self):
        self.assertEqual(tf.item_state(ISSUE), "open")
        for word in ("closed", "merged", "done"):
            self.assertEqual(tf.item_state({"status": word}), "closed", word)

    def test_an_unstated_state_is_open(self):
        self.assertEqual(tf.item_state({"phase": "review"}), "open")


class TestOneFilterAtATime(unittest.TestCase):
    def test_type_keeps_only_its_own(self):
        only_prs = tf.parse(["type=pr"])
        self.assertTrue(only_prs.topic_ok("open", [PR]))
        self.assertFalse(only_prs.topic_ok("open", [ISSUE]))

    def test_type_none_is_an_empty_topic(self):
        none = tf.parse(["type=none"])
        self.assertTrue(none.topic_ok("open", []))
        self.assertFalse(none.topic_ok("open", [ISSUE]))

    def test_state_filters_the_record_and_the_item(self):
        closed = tf.parse(["state=closed"])
        self.assertTrue(closed.topic_ok("closed", [DONE]))
        self.assertFalse(closed.topic_ok("open", [ISSUE]))
        open_only = tf.parse(["state=open"])
        self.assertTrue(open_only.topic_ok("open", [ISSUE]))
        self.assertFalse(open_only.topic_ok("closed", [DONE]))

    def test_stage_keeps_the_items_at_that_phase(self):
        review = tf.parse(["stage=review"])
        self.assertTrue(review.topic_ok("open", [PR]))
        self.assertFalse(review.topic_ok("open", [ISSUE]))
        # A topic is listed when *one* of its items is at that stage.
        self.assertTrue(review.topic_ok("open", [ISSUE, PR]))

    def test_the_unknown_words_are_reported_not_dropped(self):
        filters = tf.parse(["stage=reveiw", "colour=red"])
        self.assertEqual(filters.unknown, ("stage=reveiw", "colour=red"))
        self.assertEqual(filters.stages, ())


class TestTheyCombine(unittest.TestCase):
    def test_a_pr_in_review(self):
        both = tf.parse(["type=pr", "stage=review"])
        self.assertTrue(both.topic_ok("open", [PR]))
        self.assertFalse(both.topic_ok("open", [ISSUE]))
        # An issue in review does not satisfy "a PR in review".
        issue_review = dict(ISSUE, phase="review")
        self.assertTrue(both.topic_ok("open", [PR, issue_review]))
        self.assertFalse(tf.parse(["type=pr", "stage=qa"]).topic_ok("open", [PR]))

    def test_type_state_and_stage_together(self):
        narrow = tf.parse(["type=issue", "state=closed", "stage=done"])
        self.assertTrue(narrow.topic_ok("closed", [DONE]))
        self.assertFalse(narrow.topic_ok("open", [DONE]))


class TestTheDefaultView(unittest.TestCase):
    def test_open_topics_with_something_left_to_do(self):
        self.assertTrue(tf.DEFAULT.topic_ok("open", [ISSUE]))
        self.assertFalse(tf.DEFAULT.topic_ok("open", [DONE]),
                         "a topic whose every item is done is finished, not news")
        self.assertFalse(tf.DEFAULT.topic_ok("closed", [ISSUE]))

    def test_a_topic_with_no_items_stays(self):
        """Nothing says it is done, and a topic the admin just opened must not vanish."""
        self.assertTrue(tf.DEFAULT.topic_ok("open", []))
        self.assertFalse(tf.parse(["type=issue"]).topic_ok("open", []))

    def test_asking_for_done_finds_it(self):
        """The default view hides done work; asking for it must not then report nothing."""
        asked = tf.parse(["stage=done"])
        self.assertTrue(asked.topic_ok("open", [DONE]))

    def test_all_lists_the_closed_records_too(self):
        everything = tf.parse(["all"])
        self.assertEqual(everything.topic_states, ("open", "closed"))
        self.assertTrue(everything.topic_ok("closed", [DONE]))


class TestTheWords(unittest.TestCase):
    def test_typed_arguments_and_a_url_carry_the_same_filter(self):
        from_url = tf.from_query({"type": "issue,pr", "stage": "review", "all": "1"})
        typed = tf.parse(["type=issue,pr", "stage=review", "all"])
        self.assertEqual(from_url, typed)
        self.assertEqual(sorted(from_url.as_query().items()),
                         [("all", "1"), ("stage", "review"), ("type", "issue,pr")])

    def test_all_is_one_word_whatever_its_case(self):
        """`/topics ALL` has always meant all (#438 review): it is never "unknown filter"."""
        for word in ("all", "ALL", "All", "show-all"):
            filters = tf.parse([word])
            self.assertTrue(filters.show_closed, word)
            self.assertEqual(filters.unknown, (), word)

    def test_the_words_come_back_out_as_they_went_in(self):
        self.assertEqual(tf.parse(["type=pr", "stage=qa"]).words(),
                         ["type=pr", "stage=qa"])
        self.assertEqual(tf.parse(["state=closed"]).words(), ["state=closed"])

    def test_a_state_beats_all_in_both_directions(self):
        self.assertEqual(tf.parse(["state=closed"]).topic_states, ("closed",))
        self.assertEqual(tf.parse(["all"]).topic_states, ("open", "closed"))


class TestSelecting(unittest.TestCase):
    def test_records_and_cards_are_filtered_by_the_same_rule(self):
        records = [topic(ISSUE), topic(PR), topic()]
        self.assertEqual(len(tf.select_topics(records)), 3)
        self.assertEqual([r for r in tf.select_topics(records, tf.parse(["type=pr"]))],
                         [records[1]])
        cards = [{"status": "open", "links": [ISSUE]}, {"status": "open", "links": [PR]}]
        self.assertEqual(tf.select_cards(cards, tf.parse(["type=pr"])), [cards[1]])

    def test_a_closed_record_is_left_out_unless_asked_for(self):
        records = [topic(DONE, status="closed")]
        self.assertEqual(tf.select_topics(records), [])
        self.assertEqual(tf.select_topics(records, tf.parse(["state=closed"])), records)


if __name__ == "__main__":
    unittest.main()
