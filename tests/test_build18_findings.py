"""Build 18 live findings: send --topic to a gone topic, pauses across a restart."""

import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from swarmly.telegram.channel import TopicGone
from tests.test_telegram_channel import event, make_channel, make_config, run


class DeadTopicBot:
    def __init__(self):
        self.accepted = []

    async def send_message(self, **kw):
        from aiogram.exceptions import TelegramBadRequest
        await asyncio.sleep(0)
        if kw.get("message_thread_id") == 307:
            raise TelegramBadRequest(method=None,
                                     message="Bad Request: message thread not found")
        self.accepted.append(kw)
        return types.SimpleNamespace(message_id=len(self.accepted))


class TestSendToAGoneTopic(unittest.TestCase):

    def test_a_cli_send_fails_and_posts_nothing(self):
        channel = make_channel()
        channel._sender = None
        channel.bot = DeadTopicBot()
        channel.reroute_gone_topics = False
        with self.assertRaises(TopicGone):
            run(channel.send("probe", thread_id=307))
        self.assertEqual(channel.bot.accepted, [])

    def test_a_reply_still_lands_in_general(self):
        channel = make_channel()
        channel._sender = None
        channel.bot = DeadTopicBot()
        run(channel.send("the answer", thread_id=307))
        self.assertEqual(len(channel.bot.accepted), 1)
        self.assertIsNone(channel.bot.accepted[0].get("message_thread_id"))
        self.assertIn("meant for topic #307", channel.bot.accepted[0]["text"])


class TestPausesSurviveARestart(unittest.TestCase):

    def test_a_restart_keeps_the_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(bus={"repo": tmp}, limits={"max_peer_turns_per_topic": 1})
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            bot = dict(username="CodexBot", author_id=202, is_bot=True)
            run(channel.handle_event(event("@HermesBot go", thread_id=9, message_id=1, **bot)))
            run(channel.handle_event(event("@HermesBot again", thread_id=9, message_id=2,
                                           **bot)))
            again = make_channel(config)
            self.assertIn(next(iter(again._paused_topics))[1], (9,))
            run(again.handle_event(event("@HermesBot more", thread_id=9, message_id=3, **bot)))
            texts = [m["text"] for m in again._test_sender.sent]
            self.assertTrue(any("still paused here" in t for t in texts), texts)


if __name__ == "__main__":
    unittest.main()


class TestOneStillPausedNoticePerTopic(unittest.TestCase):

    def test_a_peers_reminder_counts_for_this_bot(self):
        channel = make_channel(make_config(limits={"max_peer_turns_per_topic": 1}))
        key = (event("x").chat_id, 9)
        channel._paused_topics.add(key)
        channel._peer_turns[key] = 1
        run(channel.handle_event(event("[Codex] ⚠️ still paused here: a person needs to post",
                                       thread_id=9, message_id=5)))
        run(channel.handle_event(event("@HermesBot go", thread_id=9, message_id=6)))
        texts = [m["text"] for m in channel._test_sender.sent]
        self.assertFalse(any("still paused here" in t for t in texts), texts)

    def test_the_default_cap_allows_a_round_of_four_bots(self):
        self.assertEqual(make_config().limits.max_peer_turns_per_topic, 8)


class TestARegistryReplyIsNeverMovedToGeneral(unittest.TestCase):
    """Live, 25 Oct: a reply in a just-deleted topic went to General, named a peer there and
    drew its General cooldown notice. With registry routing a reply stays in its topic."""

    def test_the_post_is_dropped(self):
        """#472: a bot on the registry drops it; there is no legacy side left to reroute to."""
        channel = make_channel()
        channel._sender = None
        channel.bot = DeadTopicBot()
        channel.state_link.backend = "telegram"
        run(channel.send("@reasonix_dd_bot thanks", thread_id=307))
        self.assertEqual(channel.bot.accepted, [])

    def test_general_turns_never_drops_it_too(self):
        channel = make_channel(make_config(limits={"general_turns": "never"}))
        channel._sender = None
        channel.bot = DeadTopicBot()
        run(channel.send("the answer", thread_id=307))
        self.assertEqual(channel.bot.accepted, [])


class TestWorkForAGoneTopicGetsANewOne(unittest.TestCase):
    """Live, 25 Oct: NEW_TOPIC found a topic "already about the same item" that had just
    been deleted; the assignment went to General. Now the work gets a topic of its own."""

    def test_a_new_topic_is_opened(self):
        from swarmly.telegram.channel import ChannelEvent
        channel = make_channel()
        channel._sender = None
        bot = DeadTopicBot()
        opened = []

        async def create_forum_topic(**kw):
            opened.append(kw)
            return types.SimpleNamespace(message_thread_id=900, name=kw.get("name"))
        bot.create_forum_topic = create_forum_topic
        channel.bot = bot
        channel.state_link.routing_mode = "registry"
        channel._topic_for_refs = lambda text: (channel.config.identity.home_channel_id, 307)
        ev = ChannelEvent(text="go", chat_id=channel.config.identity.home_channel_id,
                          author_id=1, author_username="admin", message_id=5, thread_id=55)
        run(channel._open_topic(ev, "#268 SNAPSHOT heal", "@CodexBot please take #268"))
        self.assertEqual(len(opened), 1)
        texts = [a.get("text", "") for a in bot.accepted]
        self.assertFalse(any("continued in" in t for t in texts), texts)
        self.assertFalse(any(a.get("message_thread_id") is None and "take #268" in
                             a.get("text", "") for a in bot.accepted), texts)
