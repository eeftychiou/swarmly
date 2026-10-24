"""A built-in command answer names bots, but never calls them: no @handle is appended.

Live: /state answered "codex: agrees at p…" and "antigravity: agrees at p…", one line per bot,
and the send appended every peer's @handle, so each one was called to a turn over a status
line. The answer goes out as it is, and a peer reads it as machine traffic.
"""

import types
import unittest

from swarmly.telegram.commands import is_command_reply
from tests.test_telegram_channel import event, make_channel, run

STATE_ANSWER = ("State store (telegram, shadow): ready\n"
                "position 4160, 12 key(s), hash 3fa2\n"
                "lease: Hermes (term 3)\n"
                "snapshot: message 812\n"
                "Codex: agrees at p4096 (3fa2)\n"
                "Antigravity: agrees at p4096 (3fa2)")


class RecordingBot:
    def __init__(self):
        self.accepted = []

    async def send_message(self, **kwargs):
        self.accepted.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.accepted))


def bot_channel():
    channel = make_channel()
    channel._sender = None
    channel.bot = RecordingBot()
    return channel


class TestCommandAnswersAddressNobody(unittest.TestCase):

    def test_the_state_answer_would_address_the_peers_it_names(self):
        """The cause: a line that opens with a peer's alias addresses that peer."""
        rendered, entities = make_channel().addressed_payload(STATE_ANSWER)
        self.assertIn("@CodexBot", rendered)
        self.assertTrue(entities)

    def test_a_command_answer_goes_out_without_handles(self):
        channel = bot_channel()

        async def answer(ev):
            return STATE_ANSWER
        channel._transport_command = answer
        run(channel.handle_event(event("/state@HermesBot")))
        self.assertEqual(len(channel.bot.accepted), 1)
        sent = channel.bot.accepted[0]
        self.assertNotIn("@CodexBot", sent["text"])
        self.assertNotIn("@AntigravityBot", sent["text"])
        self.assertIsNone(sent["entities"])
        self.assertIn("Codex: agrees", sent["text"])

    def test_a_model_reply_still_addresses_the_peer_it_names(self):
        channel = bot_channel()
        run(channel.send("Codex: please take the review"))
        self.assertIn("@CodexBot", channel.bot.accepted[0]["text"])

    def test_a_split_answer_adds_no_handle_to_any_part(self):
        channel = bot_channel()
        limit = channel.config.limits.max_message_chars
        run(channel.send("Codex: agrees\n" * (limit // 8), address_peers=False))
        self.assertGreater(len(channel.bot.accepted), 1)
        for sent in channel.bot.accepted:
            self.assertNotIn("@CodexBot", sent["text"])
            self.assertIsNone(sent["entities"])


class TestRegistryAnswersAreMachineTraffic(unittest.TestCase):

    def test_state_storetest_and_topics_answers_are_command_replies(self):
        for text in ("[Claudebot] State store (telegram, shadow): ready",
                     "[Claudebot] State store: off (state.backend is not set to telegram).",
                     "[Claudebot] Store test 7: PASS, 5/5 bots saw every line",
                     "[Claudebot] Topic registry (shadow): 4 topic(s)",
                     "State store: not started."):
            self.assertTrue(is_command_reply(text), text)

    def test_a_sentence_about_the_store_is_not_a_command_reply(self):
        for text in ("[Codex] The state store looks healthy to me",
                     "[Codex] State stores are hard",
                     "[Codex] Topic registryless mode"):
            self.assertFalse(is_command_reply(text), text)


if __name__ == "__main__":
    unittest.main()
