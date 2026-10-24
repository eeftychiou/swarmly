"""One Claude session per topic for Claudebot (topic registry, phase 7): claude_thread."""

import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.adapters.claude import ClaudeThreadAdapter
from tests.test_channel_rules import topic_message
from tests.test_claude_adapter import FakeProcess, ScriptedRunner, result_json


def session_flag(argv):
    for flag in ("--session-id", "--resume"):
        if flag in argv:
            return flag, argv[argv.index(flag) + 1]
    return None, None


class ClaudeTopicSessionTests(unittest.TestCase):
    def adapter(self, runs, state_dir=None):
        runner = ScriptedRunner(*(FakeProcess(result_json()) for _ in range(runs)))
        claude = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(),
                                     runner=runner)
        if state_dir is not None:
            claude.use_state_file(Path(state_dir) / "claude-sessions-claude.json")
        return claude, runner

    def test_a_topic_keeps_one_session_and_starts_it_fresh(self):
        claude, runner = self.adapter(2)
        message = topic_message()
        self.assertEqual(claude.topic_mode(message.context_id), "session")
        first = dataclasses.replace(message, fresh_text="TASK and PROGRESS\n\nhello")
        asyncio.run(claude.respond(first))
        asyncio.run(claude.respond(dataclasses.replace(message, fresh_text="IGNORED")))
        (flag1, id1), (flag2, id2) = (session_flag(a) for a in runner.calls)
        self.assertEqual((flag1, flag2), ("--session-id", "--resume"))
        self.assertEqual(id1, id2, "the same session for every turn in the topic")
        self.assertIn("TASK and PROGRESS", runner.calls[0][-1], "a new session gets the task")
        self.assertNotIn("IGNORED", runner.calls[1][-1], "a resumed one gets the message")

    def test_a_replaced_session_is_really_new_and_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude, runner = self.adapter(2, tmp)
            message = topic_message()
            asyncio.run(claude.respond(message))
            old = claude.topic_session(message.context_id)
            self.assertTrue(claude.forget_session(message.context_id))   # a drift reset
            new = claude.topic_session(message.context_id)
            self.assertNotEqual(old, new)
            asyncio.run(claude.respond(message))
            self.assertEqual(session_flag(runner.calls[1]), ("--session-id", new))
            again, _ = self.adapter(0, tmp)                              # the transport restarts
            self.assertEqual(again.topic_session(message.context_id), new)
            self.assertIn(new, again._known, "a session it used is resumed after a restart")

    def test_the_registry_records_the_current_session(self):
        from swarmly.telegram.registry_shadow import session_of
        claude, _ = self.adapter(0)
        before = claude.topic_session("tg-7")
        claude.forget_session("tg-7")       # even an unrecorded session is replaced (review)
        self.assertNotEqual(claude.topic_session("tg-7"), before)
        self.assertEqual(session_of(claude, "tg-7"), claude.topic_session("tg-7"))


if __name__ == "__main__":
    unittest.main()
