"""#425: a hand-off written in the admin's private chat.

Peers never see a private chat, so a hand-off left there was lost (live, 27 Oct: the admin said
"go ahead" on #260 in the private chat, Claudebot answered "@DeepSeek_dd_bot please take #424",
and DeepSeek never heard of it). The turn rules now say where it is, and the transport places a
hand-off written there in the work item's own linked topic -- or, when the item has no linked
topic, refuses it with a notice. A private chat never opens a topic of its own: the admin
decided that on 27 Oct, so private chats cannot create duplicate topics.
"""

from __future__ import annotations

import asyncio
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from swarmly.state import keys as K
from swarmly.telegram.adapters.base import IncomingMessage, channel_envelope, turn_card
from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, make_config, run
from tests.test_token_space_routing import Spy
from swarmly.telegram.handoff_split import handoff_lines
from tests.test_topic_open import Scripted, TopicBot, drain

REPO = "owner/swarmly"
TOPIC = 9424
ITEM = "https://github.com/owner/swarmly/issues/424"
#: A second linked item: the admin's own message names this one, the hand-off names #424.
TOPIC_260 = 9260
ITEM_260 = "https://github.com/owner/swarmly/issues/260"
HANDOFF = "@CodexBot please review the PR on #424"


def private_message(**kw):
    return IncomingMessage(text="go ahead", author_alias="alice", private_chat=True, **kw)


def a_link(ref, added_at="2026-10-27T10:00:00Z"):
    return {"provider": "github", "kind": "issue", "role": "work", "ref": ref,
            "status": "open", "phase": "development", "added_by": "claudebot",
            "added_at": added_at}


def a_store(records=None):
    """A reader shaped like the registry store: topic records by key."""
    if records is None:
        records = {K.topic(TOPIC): {"status": "open", "links": [a_link(ITEM)]},
                   # Linked *later* than #424: the newest link wins, so a lookup that reads
                   # the admin's message first would pick #260's topic and prove nothing.
                   K.topic(TOPIC_260): {"status": "open",
                                        "links": [a_link(ITEM_260, "2026-10-27T11:00:00Z")]}}

    class Store:
        ready = True

        def keys(self, prefix=""):
            return [key for key in records if key.startswith(prefix)]

        def read(self, key):
            return records.get(key)

    return Store()


def private_channel(adapter=None, *, linked=True, bus_repo=None):
    """A channel whose one topic is (or is not) linked to #424 in the registry."""
    channel = make_channel(
        make_config(projects={"swarmly": {"repo": REPO, "cwd": "."}},
                    state={"backend": "telegram", "links": "registry"},
                    **({"bus": {"repo": bus_repo}} if bus_repo else {})),
        adapter=adapter)
    channel._pulse_topic = lambda: None
    channel.state_link.store = a_store() if linked else a_store({})
    channel.bot = TopicBot()
    return channel


def dm(text="@HermesBot please file this"):
    return event(text, author_id=ADMIN_ID, username="alice", is_bot=False, chat_id=ADMIN_ID)


def turn(channel, text="@HermesBot please file this"):
    """Run one private-chat turn, with the bus mirror collected rather than left running."""
    posted = []

    async def fake_post(_channel, **kw):
        posted.append(kw)

    async def go():
        with mock.patch("swarmly.telegram.relay.post_and_notify", fake_post):
            await channel.handle_event(dm(text))
            await drain(channel)
            await asyncio.gather(*list(channel._mirror_tasks))

    run(go())
    return posted


class TheHandoffLinesTests(unittest.TestCase):
    """What travels to the topic: the hand-off, and not the rest of a private reply."""

    def test_the_line_that_carries_the_ask(self):
        body = ("Will do.\n@CodexBot please review the PR on #424\n"
                "The rest of this is for you alone.")
        self.assertEqual(handoff_lines(body), "@CodexBot please review the PR on #424")
        bullets = "Merge gate:\n- @CodexBot runs pytest\n- @AntigravityBot comments"
        self.assertEqual(handoff_lines(bullets),
                         "- @CodexBot runs pytest\n- @AntigravityBot comments")

    def test_a_handle_alone_carries_its_block(self):
        body = "@CodexBot:\nReview the PR on #424.\n\nAnything else is mine."
        self.assertEqual(handoff_lines(body), "@CodexBot:\nReview the PR on #424.")

    def test_a_handle_alone_above_a_blank_line_still_carries_its_ask(self):
        # The shape a model naturally writes, and the one #427 review found dropped: the
        # topic got a bare handle while the private chat said "handed that over".
        self.assertEqual(handoff_lines("@CodexBot:\n\nPlease review #424."),
                         "@CodexBot:\nPlease review #424.")
        self.assertEqual(
            handoff_lines("@CodexBot:\n\n\nPlease review #424.\n\nThat second part is yours."),
            "@CodexBot:\nPlease review #424.")

    def test_a_reply_that_names_nobody(self):
        # A handle in passing is not a hand-off: assigned_peers ignores it, and so does this.
        self.assertEqual(handoff_lines("Yes, #424 is ready. The team is @CodexBot."), "")
        self.assertEqual(handoff_lines("Yes, #424 is ready; I read it twice."), "")


class TheTurnRulesTests(unittest.TestCase):
    """The turn rules no longer send a private turn to General."""

    def test_a_private_turn_says_where_it_is_and_where_hand_offs_go(self):
        card = turn_card(private_message())
        self.assertIn("private chat", card)
        self.assertIn("never a peer", card)
        self.assertIn("@username and the task", card)
        self.assertIn("linked topic", card)
        self.assertNotIn("General", card)

    def test_the_envelope_marks_it_as_private(self):
        line = channel_envelope(private_message())
        self.assertIn("in the admin's private chat: no peer sees this", line)
        self.assertNotIn("General", line)

    def test_group_turns_keep_their_own_wording(self):
        general = turn_card(IncomingMessage(text="hi", author_alias="alice"))
        self.assertIn("reply here, in General (setup only", general)
        self.assertNotIn("private chat", general)
        topic = turn_card(IncomingMessage(text="hi", author_alias="alice", context_id="tg-7"))
        self.assertIn("reply here, in this topic", topic)


class AHandoffGoesToTheLinkedTopicTests(unittest.TestCase):
    """With a topic linked to the item, the hand-off is placed there and mirrored on the bus."""

    def test_it_is_posted_in_the_items_topic_and_the_reply_stays_in_the_chat(self):
        channel = private_channel(adapter=Scripted(HANDOFF + "\nAll done otherwise."))
        spy = Spy(channel)
        turn(channel)
        where = [(chat, thread, text) for chat, thread, text in spy.posts]
        self.assertEqual([(chat, thread) for chat, thread, _ in where],
                         [(ADMIN_ID, None), (GROUP, TOPIC), (ADMIN_ID, None)], where)
        self.assertIn(HANDOFF, where[1][2])
        self.assertNotIn("All done otherwise", where[1][2])      # the hand-off, not the chat
        self.assertIn("handed that over", where[2][2])
        self.assertEqual(channel.bot.created, [])                # never opens a topic

    def test_the_hand_off_is_mirrored_on_the_bus_from_that_topic(self):
        with TemporaryDirectory() as tmp:
            channel = private_channel(adapter=Scripted(HANDOFF), bus_repo=tmp)
            posted = turn(channel)
        self.assertEqual([(p["to"], p["intent"]) for p in posted], [("Codex", "TASK_HANDOFF")])
        self.assertTrue(posted[0]["text"].startswith(HANDOFF))
        # The bus copy is the topic's, so the peer that missed the Telegram post finds it
        # there -- and never the private chat it was written in.
        from swarmly.telegram.redact import Redactor
        self.assertEqual(posted[0]["extra"]["telegram"],
                         Redactor.from_config(channel.config).topic_ref(
                             GROUP, TOPIC, channel.router))


class TheHandOffChoosesItsOwnItemTests(unittest.TestCase):
    """#427 review: the topic follows the item the hand-off names, not the admin's message."""

    def test_the_hand_offs_own_item_decides_the_topic(self):
        # Both items have a linked topic. The admin's message names #260, the hand-off #424.
        channel = private_channel(adapter=Scripted(HANDOFF + "\nThanks."))
        spy = Spy(channel)
        turn(channel, text="@HermesBot go ahead on #260")
        self.assertEqual([(chat, thread) for chat, thread, _ in spy.posts],
                         [(ADMIN_ID, None), (GROUP, TOPIC), (ADMIN_ID, None)], spy.posts)
        self.assertIn(HANDOFF, spy.posts[1][2])

    def test_the_admins_text_answers_only_when_the_hand_off_names_no_item(self):
        channel = private_channel(adapter=Scripted("@CodexBot please take that over."))
        spy = Spy(channel)
        turn(channel, text="@HermesBot go ahead on #424")
        self.assertEqual([(chat, thread) for chat, thread, _ in spy.posts],
                         [(ADMIN_ID, None), (GROUP, TOPIC), (ADMIN_ID, None)], spy.posts)
        self.assertIn("take that over", spy.posts[1][2])


class AHandoffWithNoLinkedTopicIsRefusedTests(unittest.TestCase):
    """Without a linked topic it is not delivered, and the bot is told where to take it."""

    def test_it_is_not_posted_and_the_bot_is_told(self):
        with TemporaryDirectory() as tmp:
            channel = private_channel(adapter=Scripted(HANDOFF), linked=False, bus_repo=tmp)
            spy = Spy(channel)
            posted = turn(channel)
        self.assertEqual([(chat, thread) for chat, thread, _ in spy.posts],
                         [(ADMIN_ID, None), (ADMIN_ID, None)])
        self.assertIn("@CodexBot", spy.posts[1][2])
        self.assertIn("linked to the item", spy.posts[1][2])
        self.assertEqual(posted, [])
        self.assertEqual(channel.bot.created, [])

    def test_a_reply_that_hands_nothing_over_is_left_alone(self):
        channel = private_channel(adapter=Scripted("Yes, #424 is ready."), linked=False)
        spy = Spy(channel)
        turn(channel)
        self.assertEqual([(chat, thread) for chat, thread, _ in spy.posts], [(ADMIN_ID, None)])
        self.assertIn("Yes, #424 is ready.", spy.posts[0][2])

    def test_no_hand_off_line_fails_closed(self):
        # "@CodexBot(please ...)" counts as an assignment for assigned_peers but opens no
        # hand-off line: nothing is placed, and the whole private reply is never posted into
        # a topic a peer can read (#427 review, nit 3).
        channel = private_channel(adapter=Scripted("@CodexBot(please check #424)"))
        spy = Spy(channel)
        turn(channel)
        self.assertEqual([(chat, thread) for chat, thread, _ in spy.posts], [(ADMIN_ID, None)])
        self.assertIn("@CodexBot(please check #424)", spy.posts[0][2])


if __name__ == "__main__":
    unittest.main()
