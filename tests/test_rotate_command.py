"""`/rotate@bot` (issue #433): the admin retires a topic's session, and the next turn is new.

Live check 6 of #423 found the command had no handler at all: `/rotate@Claude_obot` arrived as
an ordinary message, a model wrote a handover in prose, and the topic kept resuming the same
session id. These tests hold the command to the design (PROTOCOL §11.6): the old session writes
its handover, the retired id is never resumed, and the next turn runs in a **new** session.
"""

import asyncio
import dataclasses
import tempfile
import unittest

from swarmly.telegram.adapters import ReasonixAdapter
from swarmly.telegram.adapters.claude import ClaudeThreadAdapter
from swarmly.telegram.channel import ChannelEvent
from swarmly.telegram.config import AdapterSettings
from tests.test_claude_adapter import FakeProcess, ScriptedRunner, message, result_json
from tests.test_claude_topic_sessions import session_flag
from tests.test_telegram_channel import ADMIN_ID, GROUP, make_channel

THREAD = 42
CONTEXT = f"tg-{THREAD}"


def admin(text: str, thread=THREAD, author_id: int = ADMIN_ID) -> ChannelEvent:
    return ChannelEvent(text=text, chat_id=GROUP, author_id=author_id,
                        author_username="admin", author_is_bot=False,
                        message_id=500, thread_id=thread)


class RotateCommandTests(unittest.TestCase):
    def channel(self, runs: int = 2):
        """A channel on a real ``claude_thread`` adapter; `runs` CLI calls are scripted (the
        handover turn, then the next turn)."""
        claude = ClaudeThreadAdapter(
            "Hermes", ClaudeThreadAdapter.default_settings(),
            runner=ScriptedRunner(*(FakeProcess(result_json()) for _ in range(runs))))
        channel = make_channel(adapter=claude)
        channel.topics.remember(GROUP, THREAD, "a topic with a session", current=True)
        return channel, claude

    def test_a_rotate_yields_a_new_session_on_the_next_turn(self):
        """DISCRIMINATING: without the command the next turn resumes the old session id."""
        with tempfile.TemporaryDirectory() as tmp:
            channel, claude = self.channel(runs=3)     # first turn, handover, next
            claude.use_state_file(f"{tmp}/claude-sessions-hermes.json")
            self.assertEqual(channel.topics.context(GROUP, THREAD), CONTEXT)
            first = message("remember 42", context_id=CONTEXT)
            asyncio.run(claude.respond(first))
            old = claude.topic_session(CONTEXT)

            reply = asyncio.run(channel.handle_event(admin("/rotate@HermesBot")))
            self.assertIn("rotated on the admin's request", reply or "")
            self.assertIn("handover", reply or "")

            new = claude.topic_session(CONTEXT)
            self.assertNotEqual(old, new, "the retired session id must not come back")

            asyncio.run(claude.respond(dataclasses.replace(
                first, text="what are we testing?", fresh_text=None)))
            self.assertEqual(session_flag(claude._runner.calls[-1]), ("--session-id", new),
                             "the first turn after a rotate starts the new session")
            handover = claude._runner.calls[1][-1].lower()
            self.assertIn("handover", handover,
                          "the old session is asked for its handover")

    def test_a_rotate_by_a_peer_is_refused(self):
        channel, _ = self.channel(runs=0)
        peer = admin("/rotate@HermesBot", author_id=202)
        peer.author_is_bot, peer.author_username = True, "CodexBot"
        reply = asyncio.run(channel.handle_event(peer))
        self.assertIn("/rotate is the admin's", reply or "")

    def test_a_rotate_outside_a_topic_is_refused(self):
        channel, _ = self.channel(runs=0)
        reply = asyncio.run(channel.handle_event(admin("/rotate@HermesBot", thread=None)))
        self.assertIn("inside a topic", reply or "")

    def test_a_rotate_retires_a_reasonix_session_too(self):
        """The other adapters rotate the same way: the old id is never resumed (Reasonix has
        no ``topic_session``: the next turn starts ``fresh``, which is a new session)."""
        reasonix = ReasonixAdapter("DeepSeek", AdapterSettings(type="reasonix"),
                                   runner=ScriptedRunner(FakeProcess(b"handover text")))
        reasonix._remember(CONTEXT, "old-session", "rules")
        self.assertEqual(reasonix.topic_mode(CONTEXT), "session")
        self.assertTrue(asyncio.run(reasonix.rotate(
            CONTEXT, message("rotate", context_id=CONTEXT))))
        self.assertIsNone(reasonix.session_for(CONTEXT), "the retired session is dropped")
        self.assertEqual(reasonix.topic_mode(CONTEXT), "fresh",
                         "the next turn starts a new session, not the retired one")

    def test_a_second_rotate_before_the_next_turn_says_already_rotated(self):
        """Review (#436): Claude's rotate refuses a second handover, and the answer used to
        claim "no handover came" — false, and the admin re-running check 6 would hit it."""
        channel, claude = self.channel(runs=2)          # the first turn, then the handover
        with tempfile.TemporaryDirectory() as tmp:
            claude.use_state_file(f"{tmp}/claude-sessions-hermes.json")
            asyncio.run(claude.respond(message("remember 42", context_id=CONTEXT)))
            first = asyncio.run(channel.handle_event(admin("/rotate@HermesBot")))
            self.assertIn("rotated on the admin's request", first or "")
            calls_after_first = len(claude._runner.calls)
            again = asyncio.run(channel.handle_event(admin("/rotate@HermesBot")))
            self.assertEqual(len(claude._runner.calls), calls_after_first,
                             "no second handover turn is spent")
        self.assertIn("already rotated", again or "")
        self.assertNotIn("no handover came", again or "")

    def test_a_rotate_for_another_bot_gets_no_answer(self):
        channel, _ = self.channel(runs=0)
        self.assertIsNone(asyncio.run(channel.handle_event(admin("/rotate@SomeOtherBot"))))


if __name__ == "__main__":
    unittest.main()
