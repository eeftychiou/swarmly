"""The same filters through both readers (#438): the panel's cards and ``/topics``' lines.

The filter function itself is pinned in ``tests/test_topic_filter.py``. What these pin is the
wiring: that ``/topics stage=review`` narrows the registry it prints, that a bad word is said
out loud, and that the page's query reaches the panel -- including through the HTTP request,
which is where a filter could otherwise be parsed and then dropped.
"""

from __future__ import annotations

import asyncio
import unittest

from swarmly.state import keys as K
from swarmly.telegram import topic_filter as tf
from swarmly.telegram import webui
from swarmly.telegram.registry_shadow import RegistryShadow
from tests.test_registry_shadow import HOME, Clock, channel_for

ISSUE_LINK = {"provider": "github", "kind": "issue", "ref": "https://github.com/o/r/issues/3",
              "role": "work", "title": "#3", "status": "open", "phase": "development",
              "added_by": "legacy"}
PR_LINK = {"provider": "github", "kind": "pr", "ref": "https://github.com/o/r/pull/9",
           "role": "work", "title": "#9", "status": "open", "phase": "review",
           "added_by": "legacy"}
DONE_LINK = {"provider": "github", "kind": "issue", "ref": "https://github.com/o/r/issues/1",
             "role": "work", "title": "#1", "status": "closed", "phase": "done",
             "added_by": "legacy"}


def record(thread, title, links=(), status="open"):
    return {"topic_id": thread, "chat_id": HOME, "title": title, "status": status,
            "opened_at": "2026-10-27T00:00:00Z", "last_activity": "2026-10-27T00:00:00Z",
            "links": list(links), "agents": {}}


class TestSlashTopicsFilters(unittest.TestCase):
    """``/topics stage=review`` prints the same set the panel's chips would."""

    def setUp(self):
        self.clock = Clock()
        self.shadow_channel = channel_for("Claudebot", self.clock)
        self.shadow = RegistryShadow(self.shadow_channel, clock=self.clock)

    def store(self, *records):
        from swarmly.state.store import MemoryLog, MemoryStore
        store = MemoryStore("Claudebot", MemoryLog(clock=self.clock), clock=self.clock)
        asyncio.run(store.start())
        asyncio.run(store.claim_lease(10 ** 6))
        for row in records:
            asyncio.run(store.write(K.topic(row["topic_id"]), row))
        asyncio.run(store.refresh())
        return store

    def test_a_stage_filter_narrows_the_list(self):
        store = self.store(record(11, "developing", [ISSUE_LINK]),
                           record(12, "reviewing", [PR_LINK]))
        text = self.shadow.describe(store, filters=tf.parse(["stage=review"]))
        self.assertIn("reviewing (12)", text)
        self.assertNotIn("developing (11)", text)
        self.assertIn("1 hidden by the filters", text)

    def test_a_type_filter_and_a_state_filter(self):
        store = self.store(record(11, "an issue", [ISSUE_LINK]),
                           record(12, "a pr", [PR_LINK]),
                           record(13, "gone", [DONE_LINK], status="closed"))
        prs = self.shadow.describe(store, filters=tf.parse(["type=pr"]))
        self.assertIn("a pr (12)", prs)
        self.assertNotIn("an issue (11)", prs)
        closed = self.shadow.describe(store, filters=tf.parse(["state=closed"]))
        self.assertIn("gone (13) closed", closed)
        self.assertNotIn("a pr (12)", closed)

    def test_the_default_view_hides_a_topic_whose_work_is_done(self):
        store = self.store(record(11, "finished", [DONE_LINK]),
                           record(12, "moving", [ISSUE_LINK]))
        text = self.shadow.describe(store)
        self.assertIn("moving (12)", text)
        self.assertNotIn("finished (11)", text)

    def test_an_unknown_filter_is_said_not_swallowed(self):
        store = self.store(record(11, "an issue", [ISSUE_LINK]))
        text = self.shadow.describe(store, filters=tf.parse(["stage=reveiw"]))
        self.assertIn("unknown filter: stage=reveiw", text)
        self.assertIn("an issue (11)", text, "an unknown word must not empty the list")

    def test_nothing_matching_says_so(self):
        store = self.store(record(11, "an issue", [ISSUE_LINK]))
        text = self.shadow.describe(store, filters=tf.parse(["state=closed"]))
        self.assertIn("no topic matches state=closed", text)

    def test_the_closed_count_points_at_the_filter_not_at_all(self):
        store = self.store(record(11, "an issue", [ISSUE_LINK]),
                           record(13, "gone", [DONE_LINK], status="closed"))
        text = self.shadow.describe(store)
        self.assertIn("+1 closed (state=closed lists them)", text)


class TestThePanelFilters(unittest.TestCase):
    """``registry_panel`` and ``topic_cards``: the panel's side of the same rule."""

    def cards(self, *records, filters=None):
        return webui.topic_cards(now=1_800_000_000.0, topics=list(records), filters=filters)

    def test_the_default_view_matches_the_command(self):
        cards = self.cards(record(11, "moving", [ISSUE_LINK]),
                           record(12, "finished", [DONE_LINK]))
        self.assertEqual([card["topic"] for card in cards], ["moving"])

    def test_a_card_says_what_type_each_link_is(self):
        cards = self.cards(record(11, "mixed", [ISSUE_LINK, PR_LINK]))
        self.assertEqual([link["type"] for link in cards[0]["links"]], ["issue", "pr"])

    def test_the_filters_view_names_the_choices_and_what_is_selected(self):
        chosen = tf.parse(["type=pr", "stage=review"])
        view = webui.filters_view(chosen)
        self.assertEqual(view["selected"], {"type": "pr", "stage": "review"})
        self.assertEqual(view["choices"]["type"], list(tf.TYPES))
        self.assertTrue(view["asked"])


class TestThePageQuery(unittest.TestCase):
    """The URL is the filter: a chip writes it, and the server reads it back (#438)."""

    def provider(self, seen):
        def provide(filters=None):
            seen.append(filters)
            return {"bots": [], "refreshed": "now",
                    "registry": {"ready": True, "topics": [],
                                 "hidden": 0, "filters": webui.filters_view(filters)}}
        return provide

    def start(self, provider):
        server = webui.DashboardServer(provider, port=0)
        return server, server.start()

    def get(self, url, path):
        from urllib.request import urlopen
        with urlopen(url + path) as answer:
            return answer.status, answer.read().decode("utf-8")

    def test_the_request_carries_the_pages_filters_to_the_provider(self):
        import json
        seen = []
        server, url = self.start(self.provider(seen))
        try:
            status, body = self.get(url, webui.STATE_PATH + "?type=pr&stage=review")
            self.assertEqual(status, 200)
            self.assertEqual(seen[-1].types, ("pr",))
            self.assertEqual(seen[-1].stages, ("review",))
            self.assertEqual(json.loads(body)["filters"]["selected"],
                             {"type": "pr", "stage": "review"})
            self.get(url, webui.STATE_PATH)
            self.assertFalse(seen[-1].asked, "a bare request is the default view")
        finally:
            server.stop()

    def test_a_provider_that_takes_no_filters_still_answers(self):
        import json
        server = webui.DashboardServer(lambda: {"bots": []}, port=0)
        url = server.start()
        try:
            status, body = self.get(url, webui.STATE_PATH + "?type=pr")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["filters"]["selected"], {"type": "pr"})
        finally:
            server.stop()

    def test_the_page_keeps_the_filters_in_its_own_url(self):
        """The chips write the query, and the poll carries it: no server round trip to set one."""
        page = webui.page_html(title="Swarm", every=3.0)
        self.assertIn("function filterQuery()", page)
        self.assertIn("function filterSet(key, value)", page)
        self.assertIn('fetch(STATE + filterQuery()', page)
        self.assertIn('id="topic-filters"', page)
        self.assertIn("filterBar(state.registry || {})", page)


if __name__ == "__main__":
    unittest.main()
