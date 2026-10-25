"""The codex adapter keeps one Codex session per discussion (forum topic)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.adapters.codex import CodexAdapter, parse_codex_events
from tests.test_claude_adapter import FakeProcess, ScriptedRunner, message

TID = "0190f000-0000-7000-8000-000000000002"


def events(text="ok", thread=TID, tokens=1) -> bytes:
    lines = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"},
             {"type": "item.completed",
              "item": {"id": "item_0", "type": "agent_message", "text": text}},
             {"type": "turn.completed", "usage": {"input_tokens": tokens}}]
    return "\n".join(json.dumps(line) for line in lines).encode()


def adapter(runner, command=None):
    settings = CodexAdapter.default_settings(cwd="/w")
    if command:
        settings.command = command
    return CodexAdapter("Codex", settings, runner=runner)


class CodexSessionTests(unittest.TestCase):
    def test_general_runs_fresh_as_before(self):
        runner = ScriptedRunner(FakeProcess(b"plain answer"))
        reply = asyncio.run(adapter(runner).respond(message()))
        self.assertEqual(reply.text, "plain answer")
        self.assertNotIn("--json", runner.calls[0])
        self.assertNotIn("resume", runner.calls[0])

    def test_a_topic_keeps_its_session(self):
        runner = ScriptedRunner(FakeProcess(events("ok")), FakeProcess(events("HERON")))
        codex = adapter(runner)
        topic = message(context_id="tg-7")
        first = asyncio.run(codex.respond(topic))
        second = asyncio.run(codex.respond(topic))
        self.assertEqual((first.text, second.text), ("ok", "HERON"))
        new, again = runner.calls
        self.assertNotIn("resume", new)
        self.assertEqual(new[-2], "--json")                     # just before the prompt
        at = again.index("resume")
        self.assertEqual(again[at + 1:at + 3], [TID, "--json"])
        self.assertLess(again.index("--sandbox"), at)           # exec options before resume
        self.assertEqual(second.meta["conversation"], TID)
        self.assertEqual(second.meta["context_id"], "tg-7")

    def test_topics_do_not_share_a_session(self):
        runner = ScriptedRunner(FakeProcess(events("a", "t-a")), FakeProcess(events("b", "t-b")),
                                FakeProcess(events("a2", "t-a")))
        codex = adapter(runner)
        for context in ("tg-1", "tg-2", "tg-1"):
            asyncio.run(codex.respond(message(context_id=context)))
        third = runner.calls[2]
        self.assertEqual(third[third.index("resume") + 1], "t-a")

    def test_a_lost_session_starts_a_new_one(self):
        gone = FakeProcess(stderr=b"Error: thread/resume: thread/resume failed: no rollout "
                                  b"found for thread id x (code -32600)\n", returncode=1)
        runner = ScriptedRunner(FakeProcess(events("ok", "old")), gone,
                                FakeProcess(events("fresh", "new")))
        codex = adapter(runner)
        topic = message(context_id="tg-7")
        asyncio.run(codex.respond(topic))
        reply = asyncio.run(codex.respond(topic))
        self.assertEqual(reply.text, "fresh")
        self.assertNotIn("resume", runner.calls[2])
        self.assertEqual(codex.thread_for("tg-7"), "new")

    HANDOVER = ("1. Goal and scope: issue #16, docs and charts; done = PR merged.\n"
                "2. Decisions: never commit flight data. 7. Next: open the PR.")

    def test_a_session_past_the_token_limit_hands_over_to_a_new_one(self):
        runner = ScriptedRunner(FakeProcess(events("ok", "old", tokens=900)),
                                FakeProcess(events("big", "old", tokens=1200)),
                                FakeProcess(events(self.HANDOVER, "old", tokens=1300)),
                                FakeProcess(events("fresh", "new", tokens=50)))
        codex = adapter(runner)
        codex.settings.session_token_limit = 1000
        topic = message("delta", context_id="tg-7", fresh_text="task and progress")
        asyncio.run(codex.respond(topic))
        second = asyncio.run(codex.respond(topic))
        self.assertTrue(second.meta.get("rotated"))
        self.assertEqual(second.text, "big")                     # the reply, not the handover
        self.assertIsNone(codex.thread_for("tg-7"))
        handover_call = runner.calls[2]
        self.assertEqual(handover_call[handover_call.index("resume") + 1], "old")
        self.assertIn("Session handover", handover_call[-1])
        reply = asyncio.run(codex.respond(topic))
        self.assertEqual(reply.text, "fresh")
        first_new = runner.calls[3]
        self.assertNotIn("resume", first_new)
        self.assertIn("taking over this topic", first_new[-1])
        self.assertIn("never commit flight data", first_new[-1])
        self.assertIn("task and progress", first_new[-1])
        self.assertLess(first_new[-1].index("never commit"), first_new[-1].index("task and"))
        self.assertNotIn("delta", first_new[-1])
        self.assertEqual(codex.thread_for("tg-7"), "new")
        # Started from it: the next turn resumes the new session, with no handover again.
        runner.results.append(FakeProcess(events("again", "new", tokens=60)))
        asyncio.run(codex.respond(topic))
        self.assertNotIn("taking over", runner.calls[4][-1])

    def test_a_failed_handover_tells_the_new_session_to_rebuild(self):
        runner = ScriptedRunner(FakeProcess(events("big", "old", tokens=1200)),
                                FakeProcess(stderr=b"boom", returncode=1),
                                FakeProcess(events("fresh", "new", tokens=50)))
        codex = adapter(runner)
        codex.settings.session_token_limit = 1000
        topic = message("delta", context_id="tg-7", fresh_text="task and progress")
        asyncio.run(codex.respond(topic))
        asyncio.run(codex.respond(topic))
        self.assertIn("could not write a handover", runner.calls[2][-1])

    def test_a_pending_handover_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "codex-sessions-codex.json"
            runner = ScriptedRunner(FakeProcess(events("big", "old", tokens=1200)),
                                    FakeProcess(events(self.HANDOVER, "old", tokens=1300)))
            codex = adapter(runner)
            codex.use_state_file(path)
            codex.settings.session_token_limit = 1000
            asyncio.run(codex.respond(message("x", context_id="tg-7")))
            self.assertEqual(len(list((path.parent / "handover").glob("*.md"))), 1)
            later_runner = ScriptedRunner(FakeProcess(events("fresh", "new", tokens=5)))
            later = adapter(later_runner)
            later.use_state_file(path)
            asyncio.run(later.respond(message("y", context_id="tg-7")))
            self.assertIn("never commit flight data", later_runner.calls[0][-1])

    def test_a_zero_token_limit_never_retires(self):
        runner = ScriptedRunner(FakeProcess(events("ok", tokens=10**9)),
                                FakeProcess(events("again", tokens=10**9)))
        codex = adapter(runner)
        codex.settings.session_token_limit = 0
        topic = message(context_id="tg-7")
        asyncio.run(codex.respond(topic))
        asyncio.run(codex.respond(topic))
        self.assertIn("resume", runner.calls[1])

    def test_sessions_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "codex-sessions-codex.json"
            first = adapter(ScriptedRunner(FakeProcess(events("ok"))))
            first.use_state_file(path)
            asyncio.run(first.respond(message(context_id="tg-7")))
            runner = ScriptedRunner(FakeProcess(events("again")))
            later = adapter(runner)
            later.use_state_file(path)
            asyncio.run(later.respond(message(context_id="tg-7")))
            self.assertIn(TID, runner.calls[0])

    def test_a_sessions_file_from_before_the_rules_hash_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-sessions-codex.json"
            path.write_text(json.dumps({"tg-7": TID}), encoding="utf-8")
            codex = adapter(ScriptedRunner())
            codex.use_state_file(path)
            self.assertEqual(codex.thread_for("tg-7"), TID)

    def test_a_resume_error_anywhere_in_stderr_starts_a_new_session(self):
        for err in (b"Error: thread/resume: thread/resume failed: no rollout found for thread"
                    b" id x (code -32600)\nWARN codex_core::exec: shutting down the app server\n",
                    b"Error: rollout for x could not be read: expected value at line 2\n"):
            runner = ScriptedRunner(FakeProcess(events("ok", "old")),
                                    FakeProcess(stderr=err, returncode=1),
                                    FakeProcess(events("fresh", "new")))
            codex = adapter(runner)
            topic = message(context_id="tg-7")
            asyncio.run(codex.respond(topic))
            reply = asyncio.run(codex.respond(topic))
            self.assertEqual(reply.text, "fresh")
            self.assertEqual(codex.thread_for("tg-7"), "new")

    def test_a_quoted_resume_error_in_the_output_keeps_the_session(self):
        quoted = (b'{"type":"thread.started","thread_id":"old"}\n'
                  b'{"type":"item.completed","item":{"type":"agent_message","text":'
                  b'"grep found: no rollout found for thread id"}}\n'
                  b'{"type":"turn.failed","error":{"message":"usage limit reached"}}\n')
        runner = ScriptedRunner(FakeProcess(events("ok", "old")),
                                FakeProcess(quoted, returncode=1))
        codex = adapter(runner)
        topic = message(context_id="tg-7")
        asyncio.run(codex.respond(topic))
        reply = asyncio.run(codex.respond(topic))
        self.assertFalse(reply.ok)
        self.assertIn("usage limit", reply.error)
        self.assertEqual(len(runner.calls), 2)                  # no second, fresh run
        self.assertEqual(codex.thread_for("tg-7"), "old")

    def test_resume_last_comes_after_the_exec_options(self):
        command = CodexAdapter.default_settings(model="o5", resume=True).command
        self.assertEqual(command, "codex exec --sandbox workspace-write -m o5 resume --last "
                                  "{prompt}")

    def test_an_unreadable_sessions_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-sessions-codex.json"
            path.write_text("{not json", encoding="utf-8")
            codex = adapter(ScriptedRunner())
            codex.use_state_file(path)
            self.assertIsNone(codex.thread_for("tg-7"))

    def test_a_custom_command_is_left_alone(self):
        runner = ScriptedRunner(FakeProcess(b"wrapped"))
        reply = asyncio.run(adapter(runner, "my-wrapper {prompt}")
                            .respond(message(context_id="tg-7")))
        self.assertEqual(reply.text, "wrapped")
        self.assertNotIn("--json", runner.calls[0])

    def test_a_failed_turn_says_why(self):
        api = json.dumps({"type": "error", "status": 400, "error": {
            "type": "invalid_request_error", "message": "The model is not supported."}})
        out = "\n".join(json.dumps(e) for e in (
            {"type": "thread.started", "thread_id": TID},
            {"type": "item.completed", "item": {"type": "error", "message": "a warning"}},
            {"type": "error", "message": api},
            {"type": "turn.failed", "error": {"message": api}})).encode()
        runner = ScriptedRunner(FakeProcess(out, b"Reading additional input from stdin...\n", 1))
        reply = asyncio.run(adapter(runner).respond(message(context_id="tg-7")))
        self.assertFalse(reply.ok)
        self.assertIn("The model is not supported.", reply.error)
        self.assertIsNone(adapter(ScriptedRunner()).thread_for("tg-7"))

    def test_the_last_agent_message_is_the_reply(self):
        out = "\n".join(json.dumps(e) for e in (
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}}))
        self.assertEqual(parse_codex_events("noise\n" + out), ("t", "final", None))


if __name__ == "__main__":
    unittest.main()
