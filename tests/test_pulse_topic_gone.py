"""The admin deleted 📡 Swarm pulse (live, 26 Oct): the bots forget it, the coordinator opens a
new one, nothing of the store goes to General, and the card follows the pulse topic."""

import asyncio
import unittest
from types import SimpleNamespace

from swarmly.telegram.channel import PULSE_TOPIC_NAME
from swarmly.telegram.state_link import ChannelIO
from tests.test_telegram_channel import GROUP, make_channel, make_config


class PulseTopicGoneTests(unittest.TestCase):
    def test_a_deleted_pulse_topic_is_forgotten(self):
        channel = make_channel()
        channel.topics.remember(GROUP, 3418, PULSE_TOPIC_NAME)
        self.assertEqual(channel._pulse_topic(), 3418)
        self.assertTrue(channel.pulse_topic_gone(GROUP, 3418))
        self.assertIsNone(channel._pulse_topic())
        self.assertFalse(channel.pulse_topic_gone(GROUP, 9400))     # not the pulse

    def test_the_store_never_posts_in_general(self):
        channel = make_channel()
        io = ChannelIO(channel)

        async def no_pulse():
            return None
        channel._ensure_pulse_topic = no_pulse
        channel.bot = SimpleNamespace()
        with self.assertRaises(RuntimeError):
            asyncio.run(io.post_line("STATE"))

    def test_a_thread_not_found_on_a_store_post_forgets_the_topic(self):
        channel = make_channel()
        channel.topics.remember(GROUP, 3418, PULSE_TOPIC_NAME)

        async def pulse():
            return 3418

        async def refuse(**kwargs):
            raise RuntimeError("Telegram server says - Bad Request: message thread not found")
        channel._ensure_pulse_topic = pulse
        channel.bot = SimpleNamespace(send_message=refuse)
        with self.assertRaises(RuntimeError):
            asyncio.run(ChannelIO(channel).post_line("STATE"))
        self.assertIsNone(channel._pulse_topic())

    def test_the_card_can_follow_the_pulse_topic(self):
        config = make_config(control={"topic_id": "pulse"})
        self.assertTrue(config.control.follow_pulse)
        self.assertIsNone(config.control.topic_id)
        channel = make_channel(config)
        channel.bot = SimpleNamespace()
        channel.topics.remember(GROUP, 500, PULSE_TOPIC_NAME)
        self.assertEqual(channel.control_center().thread_id, 500)
        channel.topics.forget(GROUP, 500)
        channel.topics.remember(GROUP, 600, PULSE_TOPIC_NAME)
        self.assertEqual(channel.control_center().thread_id, 600)   # rebuilt for the new topic


if __name__ == "__main__":
    unittest.main()
