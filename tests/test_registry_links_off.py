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
from tests.test_telegram_channel import make_channel, make_config, run
from tests.test_topic_open import ask


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


class NothingIsLearnedFromText(unittest.TestCase):
    """#472, design §6 phase 9c: the learned map is gone. A number in a message links nothing,
    and only an explicit link -- ``LINK:``/``/link``, or the admin's ``/topic`` -- does."""

    def channel(self):
        channel = make_channel(make_config())
        channel.state_link = FakeLink()
        return channel

    def test_a_mention_in_text_moves_nothing(self):
        channel = self.channel()
        self.assertIsNone(channel.topic_for_text("we should look at #123 and #456"))
        self.assertIsNone(channel._topic_for_brief("a brief about #123"))

    def test_the_admins_topic_command_still_pins_the_item(self):
        """The one thing the learned map carried that had to be replaced: the pin."""
        channel = make_channel(make_config())
        channel.topics.remember(HOME, 28784, "a round")
        pins = []
        channel.state_link.note_pin = lambda chat, thread, refs, who: pins.append(
            (int(chat), int(thread), tuple(refs)))
        run(channel.handle_event(ask("/topic #123", thread_id=28784, message_id=3)))
        self.assertEqual(pins, [(HOME, 28784, ("#123",))])

    def test_a_topic_id_in_the_text_is_not_a_ref(self):
        self.assertEqual(issue_refs("see #13221 and #77", topic_ids=(13221,)), ["#77"])


class FakeLink:
    """A state link whose registry says `#12` lives in topic 55."""

    def __init__(self, topic=(HOME, 55)):
        self.topic = topic

    def topic_for_refs(self, refs, repo=None):
        return self.topic if "#12" in list(refs or ()) else None


class TestTheExplicitLinkMoves(unittest.TestCase):
    """The item's explicit link decides, and nothing else (#472)."""

    def channel(self):
        config = make_config()
        config.limits.work_in_topics = True
        channel = make_channel(config)
        channel.state_link = FakeLink()
        return channel

    def test_the_text_helper_finds_the_explicit_link(self):
        self.assertEqual(self.channel().topic_for_text("please look at #12"), (HOME, 55))
        self.assertIsNone(self.channel().topic_for_text("please look at #999"))

    def test_the_link_moves_a_reply_and_a_guess_does_not(self):
        channel = self.channel()
        moved = channel._topic_elsewhere(self.reply_in(28784, "answer here"), "about #12")
        self.assertEqual(moved, (HOME, 55))
        nothing = channel._topic_elsewhere(self.reply_in(28784, "answer here"), "about #999")
        self.assertIsNone(nothing, "a number nobody linked moves nothing")

    def test_a_brief_goes_to_the_topic_its_item_is_linked_in(self):
        """The decision `_open_topic` takes, driven: not a string match on the source (#457)."""
        channel = self.channel()
        self.assertEqual(channel._topic_for_brief("re-review PR #12"), (HOME, 55))
        self.assertIsNone(channel._topic_for_brief("a brief about #999"))

    def test_a_general_reply_goes_where_its_item_lives(self):
        """The decision the General move takes, message and answer read together."""
        channel = self.channel()
        event = self.reply_in(None, "handing the review over")
        self.assertEqual(channel._topic_for_general_work(event, "reviewed #12"), (HOME, 55))
        self.assertIsNone(channel._topic_for_general_work(event, "reviewed #999"))

    def test_a_general_handoff_moves_by_the_item_it_names(self):
        """The other half of `_topic_for_general_work`: the ref is in the hand-off, not the
        answer (a body-only version left every test green, #462 nit)."""
        channel = self.channel()
        handoff = self.reply_in(None, "handing #12 over, please take it")
        self.assertEqual(channel._topic_for_general_work(handoff, "taking it"), (HOME, 55))
        unknown = self.reply_in(None, "handing #999 over, please take it")
        self.assertIsNone(channel._topic_for_general_work(unknown, "taking it"),
                          "a hand-off naming an item nobody linked stays in General")

    def test_transport_only_still_moves_a_brief_on_a_link(self):
        """With General and pulse transport-only, a link still moves a brief (admin, 25 Oct)."""
        channel = self.channel()
        channel.config.limits.general_turns = "never"
        self.assertEqual(channel._topic_for_brief("#12"), (HOME, 55))

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

    def sync(self, store):
        self.channel.state_link = SimpleNamespace()
        seen(self.channel, 10134, "Stability round")
        asyncio.run(RegistryShadow(self.channel, clock=self.clock).sync(store))
        return (store.read(K.topic(10134)) or {}).get("links") or []

    def test_a_sync_writes_no_guessed_link(self):
        """Nothing is learned, so a sync writes no ``added_by: legacy`` link at all (#472)."""
        links = self.sync(self.store())
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
        links = self.sync(store)
        self.assertEqual([link.get("title") for link in links], ["#7"])
        self.assertEqual([link.get("added_by") for link in links], ["admin"])

if __name__ == "__main__":
    unittest.main()
