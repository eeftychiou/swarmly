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
