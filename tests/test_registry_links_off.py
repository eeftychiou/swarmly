"""With ``state.links: registry`` a mention is not a link (#457, design §6 phase 5).

What went wrong live: text refs were still learned under the flag, so `/topics` listed links
nobody made with a `LINK:` line -- `#42` cut out of a truncated title, `#13221` cut out of a
topic id. These pin both halves: the flag switches the learning and the moves off (and drops
the guesses an earlier build wrote), and the two shapes that were never refs are not learned in
either mode.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from swarmly.state import keys as K
from swarmly.telegram.registry_shadow import RegistryShadow
from swarmly.telegram.topic_open import issue_refs
from tests.test_registry_shadow import HOME, Clock, channel_for, seen
from tests.test_telegram_channel import make_channel, make_config


class TestWhatIsNotARef(unittest.TestCase):
    def test_a_number_a_truncated_title_cut_off_is_not_a_ref(self):
        """`(#42…` is the start of #421: the live phantom #42 was this."""
        self.assertEqual(issue_refs("CI green for #421 (#42\u2026"), ["#421"])
        self.assertEqual(issue_refs("#421 (#42..."), ["#421"])
        self.assertEqual(issue_refs("a title cut here (#12\u2026)"), [])
        # Prose is not a truncation: a real ref followed by three dots stays a ref, and the
        # bracketed three-dot form is the same cut as the one above (#457 review).
        self.assertEqual(issue_refs("see #123..."), ["#123"])
        self.assertEqual(issue_refs("(#42..."), [])

    def test_a_topic_id_is_not_an_issue_number(self):
        """`#13221` was a forum thread id, learned as a ref."""
        text = "opened in #13221 and #132 also lives there"
        self.assertEqual(issue_refs(text, topic_ids=(13221,)), ["#132"])
        self.assertEqual(issue_refs(text), ["#13221", "#132"], "without the list it is a ref")

    def test_the_refs_it_does_learn_are_unchanged(self):
        self.assertEqual(issue_refs("fixes #12, PR 34 and /pull/56"), ["#12", "#34", "#56"])
        self.assertEqual(issue_refs("#12 twice, #12 again"), ["#12"])


class TestLearningUnderTheFlag(unittest.TestCase):
    def channel(self, links="registry"):
        channel = make_channel(make_config())
        channel.state_link.links_mode = links
        return channel

    def test_registry_mode_learns_nothing_from_message_text(self):
        channel = self.channel("registry")
        channel._learn_topic_refs(channel.config.identity.home_channel_id, 28784,
                                  "we should look at #123 and #456")
        self.assertEqual(channel._ref_topics, {})
        self.assertIsNone(channel._topic_for_refs("about #123"))

    def test_legacy_mode_is_unchanged(self):
        channel = self.channel("legacy")
        channel._learn_topic_refs(channel.config.identity.home_channel_id, 28784,
                                  "we should look at #123")
        self.assertIn("#123", channel._ref_topics)
        self.assertEqual(channel._topic_for_refs("about #123"),
                         (channel.config.identity.home_channel_id, 28784))

    def test_the_admins_own_pin_still_works(self):
        """/topic #N is a person saying where the item lives, not text being guessed at."""
        channel = self.channel("registry")
        channel._learn_topic_refs(channel.config.identity.home_channel_id, 28784,
                                  "#123", pin=True)
        self.assertIn("#123", channel._ref_topics)

    def test_a_topic_id_in_the_text_is_not_learned(self):
        channel = self.channel("legacy")
        channel.topics.remember(channel.config.identity.home_channel_id, 13221, "elsewhere")
        channel._learn_topic_refs(channel.config.identity.home_channel_id, 28784,
                                  "see #13221 and #77")
        self.assertEqual(sorted(channel._ref_topics), ["#77"])


class FakeLink:
    """A state link whose registry says `#12` lives in topic 55."""

    links_mode = "registry"

    def __init__(self, topic=(HOME, 55)):
        self.topic = topic

    def topic_for_refs(self, refs, repo=None):
        return self.topic if "#12" in list(refs or ()) else None


class TestTheExplicitLinkStillMoves(unittest.TestCase):
    """The flag turns off the *guess*; the item's explicit link still decides (#457 review).

    Phase 5 lands before phase 6, so `state.links: registry` with `state.routing: legacy`
    is a real configuration -- and there the learned map is empty, so every reader that
    looked only at it lost the link the admin had made.
    """

    def channel(self, links="registry"):
        config = make_config()
        config.limits.work_in_topics = True
        channel = make_channel(config)
        channel.state_link = FakeLink()
        channel.state_link.links_mode = links
        return channel

    def test_the_text_helper_finds_the_explicit_link(self):
        channel = self.channel("registry")
        self.assertEqual(channel.topic_for_text("please look at #12"),
                         (HOME, 55))

    def test_legacy_mode_ignores_the_explicit_link_and_guesses(self):
        channel = self.channel("legacy")
        channel._learn_topic_refs(HOME, 55, "#12")
        self.assertEqual(channel.topic_for_text("please look at #12"), (HOME, 55))

    def test_the_explicit_link_moves_a_reply_and_the_guess_does_not(self):
        channel = self.channel("registry")
        moved = channel._topic_elsewhere(self.reply_in(28784, "answer here"), "about #12")
        self.assertEqual(moved, (HOME, 55))
        nothing = channel._topic_elsewhere(self.reply_in(28784, "answer here"), "about #999")
        self.assertIsNone(nothing, "a number nobody linked moves nothing")

    def test_a_brief_goes_to_the_topic_its_item_is_linked_in(self):
        """The decision `_open_topic` takes, driven: not a string match on the source (#457)."""
        channel = self.channel("registry")
        self.assertEqual(channel._topic_for_brief("re-review PR #12"), (HOME, 55))
        self.assertIsNone(channel._topic_for_brief("a brief about #999"))

    def test_a_brief_falls_back_to_the_learned_topic_when_no_link_exists(self):
        """The `else` of `_topic_for_brief`: with no explicit link, `legacy` mode still moves.

        Without this, replacing the body with only `_linked_topic(about)` left all the other
        tests green (#462 review).
        """
        channel = self.channel("legacy")            # FakeLink answers nothing in this mode
        self.assertFalse(channel._transport_only_on())
        channel._learn_topic_refs(HOME, 55, "#12")
        self.assertEqual(channel._topic_for_brief("#12"), (HOME, 55))
        self.assertIsNone(channel._topic_for_brief("#999"), "nothing learned, nothing moves")

    def test_a_general_reply_goes_where_its_item_lives(self):
        """The decision the General move takes, message and answer read together."""
        channel = self.channel("registry")
        event = self.reply_in(None, "handing the review over")
        self.assertEqual(channel._topic_for_general_work(event, "reviewed #12"), (HOME, 55))
        self.assertIsNone(channel._topic_for_general_work(event, "reviewed #999"))

    def test_transport_only_moves_a_brief_on_a_link_never_on_a_guess(self):
        """With General and pulse transport-only only a link moves a brief (admin, 25 Oct)."""
        linked = self.channel("registry")
        linked.config.limits.general_turns = "never"
        self.assertEqual(linked._topic_for_brief("#12"), (HOME, 55))

        guessed = self.channel("legacy")
        guessed.config.limits.general_turns = "never"
        guessed._learn_topic_refs(HOME, 55, "#12")     # a learned ref, the legacy way
        self.assertIn("#12", guessed._ref_topics)
        self.assertIsNone(guessed._topic_for_brief("#12"),
                          "a guess does not move a brief: it stays where it was asked")

    def reply_in(self, thread, text):
        from swarmly.telegram.channel import ChannelEvent
        return ChannelEvent(text=text, chat_id=HOME, thread_id=thread,
                            author_id=999, author_username="deepseek", author_is_bot=True)


class TestTheRegistrysLinks(unittest.TestCase):
    """`RegistryShadow.sync`: what it writes is the explicit link, never a guess."""

    def setUp(self):
        self.clock = Clock()
        self.channel = channel_for("Claudebot", self.clock)

    def store(self):
        from swarmly.state.store import MemoryLog, MemoryStore
        store = MemoryStore("Claudebot", MemoryLog(clock=self.clock), clock=self.clock)
        asyncio.run(store.start())
        asyncio.run(store.claim_lease(10 ** 6))
        return store

    def sync(self, store, links_mode):
        self.channel.state_link = SimpleNamespace(links_mode=links_mode)
        channel = self.channel
        channel._ref_topics = {"#42": (HOME, 10134), "#13221": (HOME, 10134)}
        seen(channel, 10134, "Stability round")
        asyncio.run(RegistryShadow(channel, clock=self.clock).sync(store))
        return (store.read(K.topic(10134)) or {}).get("links") or []

    def test_registry_mode_writes_no_learned_link(self):
        links = self.sync(self.store(), "registry")
        self.assertEqual([link for link in links if link.get("added_by") == "legacy"], [])

    def test_registry_mode_drops_the_guesses_an_earlier_build_wrote(self):
        store = self.store()
        record = {"topic_id": 10134, "chat_id": HOME, "title": "Stability round",
                  "status": "open", "opened_by": "admin",
                  "opened_at": "2026-10-27T00:00:00Z",
                  "last_activity": "2026-10-27T00:00:00Z",
                  "links": [
                      # the phantom: learned from text, which the flag forbids
                      {"id": "L1", "provider": "github", "kind": "issue",
                       "ref": "https://github.com/o/r/issues/42", "role": "reference",
                       "title": "#42", "added_by": "legacy",
                       "added_at": "2026-10-27T00:00:00Z"},
                      # the admin's own LINK: line: this one must survive
                      {"id": "L2", "provider": "github", "kind": "issue",
                       "ref": "https://github.com/o/r/issues/7", "role": "work",
                       "title": "#7", "phase": "development", "status": "open",
                       "added_by": "admin", "added_at": "2026-10-27T00:00:00Z"},
                  ], "agents": {}}
        asyncio.run(store.write(K.topic(10134), record))
        links = self.sync(store, "registry")
        self.assertEqual([link.get("title") for link in links], ["#7"])
        self.assertEqual([link.get("added_by") for link in links], ["admin"])

    def test_legacy_mode_still_learns(self):
        links = self.sync(self.store(), "legacy")
        titles = sorted(link.get("title") for link in links)
        self.assertEqual(titles, ["#13221", "#42"])
        self.assertTrue(all(link.get("added_by") == "legacy" for link in links))


if __name__ == "__main__":
    unittest.main()
