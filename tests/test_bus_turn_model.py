"""A turn run from the bus keeps the /model and /reasoning choice of its topic."""

import unittest
from unittest import mock

from swarmly.telegram.poll import _bus_incoming
from tests.test_telegram_channel import make_channel, make_config


class TestBusTurnModel(unittest.TestCase):

    def setUp(self):
        self.channel = make_channel(make_config(adapter={"type": "claude"}))
        self.channel._models.update({"general": "sonnet", "topic-7": "opus"})
        self.channel._efforts.update({"topic-7": "high"})

    def test_a_bus_turn_runs_generals_choice(self):
        incoming = _bus_incoming("please review", "codex", self.channel.config, "tok",
                                 self.channel)
        self.assertEqual((incoming.model, incoming.effort), ("sonnet", None))

    def test_a_bus_turn_about_a_topic_runs_that_topics_choice(self):
        with mock.patch.object(self.channel, "topic_for_text", return_value=(-100, 7)), \
                mock.patch.object(self.channel.topics, "context", return_value="topic-7"):
            incoming = _bus_incoming("QA for #37", "codex", self.channel.config, "tok",
                                     self.channel)
        self.assertEqual((incoming.model, incoming.effort), ("opus", "high"))

    def test_no_channel_means_the_adapters_defaults(self):
        incoming = _bus_incoming("hi", "codex", self.channel.config, "tok")
        self.assertEqual((incoming.model, incoming.effort), (None, None))


if __name__ == "__main__":
    unittest.main()
