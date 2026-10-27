"""A built-in command answer names bots, but never calls them: no @handle is appended.

Live: /state answered "codex: agrees at p…" and "antigravity: agrees at p…", one line per bot,
and the send appended every peer's @handle, so each one was called to a turn over a status
line. The answer goes out as it is, and a peer reads it as machine traffic.
"""

import tempfile
import types
import unittest

from swarmly.telegram.commands import is_command_reply
from tests.test_membership import config_at, joined
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, run

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

    def test_a_frozen_bot_answers_without_handles_too(self):
        channel = bot_channel()
        channel._frozen = True

        async def answer(ev):
            return STATE_ANSWER
        channel._transport_command = answer
        run(channel.handle_event(event("/state@HermesBot", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        self.assertEqual(len(channel.bot.accepted), 1)
        self.assertNotIn("@CodexBot", channel.bot.accepted[0]["text"])
        self.assertIsNone(channel.bot.accepted[0]["entities"])

    def test_the_swarm_answer_names_the_coordinator_without_calling_it(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder, peers={"Codex": "codex_x_bot"})),
                             role="developer")
            channel.membership.state.coordinator = "Codex"
            status = run(channel.membership.status_text())
            self.assertIn("@codex_x_bot", channel.addressed_payload(status)[0])  # the cause
            channel._sender = None
            channel.bot = RecordingBot()
            run(channel.handle_event(event("/swarm@HermesBot", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            sent = [m for m in channel.bot.accepted if "SWARM" in m["text"]]
            self.assertEqual(len(sent), 1)
            self.assertIn("coordinator: Codex", sent[0]["text"])
            self.assertNotIn("@codex_x_bot", sent[0]["text"])
            self.assertIsNone(sent[0]["entities"])

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

    def test_state_storetest_topics_and_swarm_answers_are_command_replies(self):
        for text in ("[Claudebot] State store (telegram, shadow): ready\nposition 4160",
                     "[Claudebot] State store (telegram, shadow): not ready -- last error: x",
                     "[Claudebot] State store: off (state.backend is not set to telegram).",
                     "State store: not started.",
                     "[Claudebot] Store test 1792873083-claudebot-3fa2: PASS, 5/5",
                     "[Claudebot] Store test 1792873083-claudebot-3fa2 started: every bot ...",
                     "[Claudebot] Topic registry (shadow):\n- 🧪 Swarm test: ...",
                     "[Claudebot] Topic registry: the state store is not running (x).",
                     "[Claudebot] Topic registry: empty so far (the lease holder fills it).",
                     # #435: the "(shadow)" label goes once no registry flag is in shadow, and
                     # a closed record is a count line, not a topic.
                     "[Claudebot] Topic registry:\n• Stability round (10134)",
                     "[Claudebot] Topic registry:\n• a gone one (8) closed"
                     "\n+1 closed (listed above)",
                     "[Claudebot] Topic registry: no open topics.\n+2 closed (listed above)",
                     # #438: nothing matched the filters asked for -- an answer, not a
                     # hand-off, so a peer does not move it into a topic.
                     "[Claudebot] Topic registry: no topic matches stage=review.",
                     "[Claudebot] Topic registry: no topic matches stage=review.\n"
                     "unknown filter: stage=reveiw",
                     "[Claudebot] SWARM bus: unknown\ncoordinator: Codex"):
            self.assertTrue(is_command_reply(text), text)

    def test_a_peer_writing_about_the_store_is_still_a_hand_off(self):
        """Live work on the store and registry opens with these words, and a peer that
        swallowed it as a command answer would drop the hand-off without a notice."""
        for text in ("[Codex] State store is wedged at p4096; Hermes, please re-snapshot it",
                     "[Codex] State store: I pushed the heal fix, Hermes please review",
                     "[Codex] Store test failed on my side, can you rerun it?",
                     "[Codex] Topic registry: I pushed the phase 4 fix in #250",
                     "[Codex] The state store looks healthy to me",
                     "[Codex] State stores are hard"):
            self.assertFalse(is_command_reply(text), text)


if __name__ == "__main__":
    unittest.main()
