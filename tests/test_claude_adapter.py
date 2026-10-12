"""
Tests for the Claude Code adapters (``claude`` and ``claude_thread``) and for the
in-chat ``switch context to`` command with a mention prefix (issue #130).

No process is spawned: an injected runner returns canned stdout/stderr/returncode.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

from swarmly.telegram.adapters import ClaudeAdapter, ClaudeThreadAdapter, build_adapter
from swarmly.telegram.adapters.antigravity import (
    ACTIVE_CONVERSATION_FILENAME,
    AntigravityThreadAdapter,
    match_context_switch,
)
from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.config import AgentConfig


def result_json(text: str = "pong", *, is_error: bool = False, subtype: str = "success",
                session: str = "s-1") -> bytes:
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error,
                       "result": text, "session_id": session, "total_cost_usd": 0.01,
                       "num_turns": 1}).encode()


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._out, self._err, self.returncode = stdout, stderr, returncode

    async def communicate(self):
        return self._out, self._err


class ScriptedRunner:
    """Returns the queued results in order and records every argv."""

    def __init__(self, *results: FakeProcess):
        self.results = list(results)
        self.calls: List[List[str]] = []

    async def __call__(self, argv: Sequence[str], cwd: Optional[str], env: Dict[str, str],
                       timeout: float) -> Any:
        self.calls.append(list(argv))
        return self.results.pop(0)


def message(text: str = "@claude_bot ping", **kw) -> IncomingMessage:
    kw.setdefault("author_alias", "Codex")
    return IncomingMessage(text=text, **kw)


class TestClaudeAdapter(unittest.TestCase):
    def test_default_command_is_print_mode_json_with_end_of_options(self):
        runner = ScriptedRunner(FakeProcess(result_json()))
        adapter = ClaudeAdapter("Claude", ClaudeAdapter.default_settings(), runner=runner)
        asyncio.run(adapter.respond(message("--help me")))
        argv = runner.calls[0]
        self.assertEqual(argv[:6], ["claude", "-p", "--output-format", "json",
                                    "--permission-mode", "acceptEdits"])
        self.assertEqual(argv[-2], "--")
        self.assertIn("--help me", argv[-1])       # after "--": a prompt, never a flag

    def test_reply_is_the_result_field_not_the_json(self):
        runner = ScriptedRunner(FakeProcess(result_json("@CodexBot: done, PR #7 merged")))
        adapter = ClaudeAdapter("Claude", ClaudeAdapter.default_settings(), runner=runner)
        reply = asyncio.run(adapter.respond(message()))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "@CodexBot: done, PR #7 merged")
        self.assertEqual(reply.meta["session_id"], "s-1")

    def test_long_json_is_parsed_before_trimming(self):
        """The JSON envelope is far longer than max_output_chars; trimming first
        would cut it mid-object and post raw JSON to the channel."""
        padding = {"usage": {"x" * 50: "y" * 5000}}
        raw = json.loads(result_json("short answer"))
        raw.update(padding)
        runner = ScriptedRunner(FakeProcess(json.dumps(raw).encode()))
        adapter = ClaudeAdapter("Claude", ClaudeAdapter.default_settings(), runner=runner)
        reply = asyncio.run(adapter.respond(message()))
        self.assertEqual(reply.text, "short answer")

    def test_is_error_result_is_a_failure(self):
        runner = ScriptedRunner(FakeProcess(result_json("Credit balance is too low",
                                                        is_error=True)))
        adapter = ClaudeAdapter("Claude", ClaudeAdapter.default_settings(), runner=runner)
        reply = asyncio.run(adapter.respond(message()))
        self.assertFalse(reply.ok)
        self.assertIn("Credit balance is too low", reply.error)

    def test_max_turns_subtype_is_a_failure(self):
        runner = ScriptedRunner(FakeProcess(result_json("", subtype="error_max_turns")))
        adapter = ClaudeAdapter("Claude", ClaudeAdapter.default_settings(), runner=runner)
        reply = asyncio.run(adapter.respond(message()))
        self.assertFalse(reply.ok)
        self.assertIn("error_max_turns", reply.error)

    def test_plain_text_output_still_works_with_a_custom_command(self):
        settings = ClaudeAdapter.default_settings()
        settings.command = "claude -p -- {prompt}"
        runner = ScriptedRunner(FakeProcess(b"plain reply\n"))
        reply = asyncio.run(ClaudeAdapter("Claude", settings, runner=runner).respond(message()))
        self.assertEqual(reply.text.strip(), "plain reply")

    def test_built_from_config(self):
        cfg = AgentConfig.from_dict({
            "agent": {"name": "ClaudeBot", "alias": "Claude", "bot_token": "1:x",
                      "home_channel_id": -100, "admin_user_id": 1},
            "adapter": {"type": "claude", "cwd": "/tmp"},
        })
        adapter = build_adapter(cfg)
        self.assertIsInstance(adapter, ClaudeAdapter)
        self.assertEqual(adapter.build_argv("hi")[0], "claude")
        self.assertEqual(adapter.environment({})["GIT_AUTHOR_NAME"], "Claude")


class TestClaudeThreadAdapter(unittest.TestCase):
    def test_resumes_the_configured_session(self):
        runner = ScriptedRunner(FakeProcess(result_json(session="abc")))
        settings = ClaudeThreadAdapter.default_settings(cwd="/work", session="abc")
        reply = asyncio.run(ClaudeThreadAdapter("Claude", settings, runner=runner)
                            .respond(message()))
        argv = runner.calls[0]
        at = argv.index("--resume")
        self.assertEqual(argv[at + 1], "abc")
        self.assertLess(at, argv.index("--"))
        self.assertEqual(reply.meta["conversation"], "abc")

    def test_derived_session_is_stable_and_created_once(self):
        # A session this process has not seen is created first (no failed resume, no
        # warning in the log on a new work folder's first turn), then resumed.
        runner = ScriptedRunner(FakeProcess(result_json("first")),
                                FakeProcess(result_json("second")))
        settings = ClaudeThreadAdapter.default_settings(cwd="/work")
        adapter = ClaudeThreadAdapter("Claude", settings, runner=runner)
        self.assertEqual(adapter.session_id,
                         ClaudeThreadAdapter.derived_session_id("claude", "/work"))
        first = asyncio.run(adapter.respond(message()))
        second = asyncio.run(adapter.respond(message()))
        self.assertEqual((first.text, second.text), ("first", "second"))
        flags = [("--session-id" in c, "--resume" in c) for c in runner.calls]
        self.assertEqual(flags, [(True, False), (False, True)])
        self.assertTrue(all(adapter.session_id in c for c in runner.calls))

    def test_a_new_topic_creates_its_session_without_a_failed_resume(self):
        runner = ScriptedRunner(FakeProcess(result_json("first")),
                                FakeProcess(result_json("second")))
        adapter = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(cwd="/w"),
                                      runner=runner)
        topic = message(context_id="tg-7")
        first = asyncio.run(adapter.respond(topic))
        second = asyncio.run(adapter.respond(topic))
        self.assertEqual((first.text, second.text), ("first", "second"))
        flags = [("--session-id" in c, "--resume" in c) for c in runner.calls]
        self.assertEqual(flags, [(True, False), (False, True)])

    def test_a_topic_session_that_exists_already_is_resumed(self):
        in_use = FakeProcess(stderr=b"Error: Session ID x is already in use.\n", returncode=1)
        runner = ScriptedRunner(in_use, FakeProcess(result_json("back")))
        adapter = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(cwd="/w"),
                                      runner=runner)
        reply = asyncio.run(adapter.respond(message(context_id="tg-7")))
        self.assertEqual(reply.text, "back")
        flags = [("--session-id" in c, "--resume" in c) for c in runner.calls]
        self.assertEqual(flags, [(True, False), (False, True)])

    def test_a_missing_named_session_is_not_invented(self):
        missing = FakeProcess(stderr=b"No conversation found with session ID: my-name\n",
                              returncode=1)
        runner = ScriptedRunner(missing)
        settings = ClaudeThreadAdapter.default_settings(cwd="/work", session="my-name")
        reply = asyncio.run(ClaudeThreadAdapter("Claude", settings, runner=runner)
                            .respond(message()))
        self.assertFalse(reply.ok)
        self.assertIn("my-name", reply.error)
        self.assertEqual(len(runner.calls), 1)

    def test_turns_are_serialised(self):
        active = 0
        peak = 0

        class SlowProcess(FakeProcess):
            async def communicate(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
                return result_json(), b""

        async def runner(argv, cwd, env, timeout):
            return SlowProcess()

        adapter = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(
            cwd="/work", session="abc"), runner=runner)

        async def both():
            await asyncio.gather(adapter.respond(message()), adapter.respond(message()))

        asyncio.run(both())
        self.assertEqual(peak, 1)

    def test_explicitly_addressed_messages_always_reach_the_session(self):
        adapter = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(
            session="abc"), runner=ScriptedRunner())
        self.assertTrue(adapter.accepts_explicit_username_delivery())
        self.assertFalse(ClaudeAdapter("Claude", ClaudeAdapter.default_settings(),
                                       runner=ScriptedRunner())
                         .accepts_explicit_username_delivery())


class TestContextSwitchWithMention(unittest.TestCase):
    """Issue #130: addressed messages carry the bot mention, so the command never matched."""

    def test_prefixes_are_skipped(self):
        cases = {
            "switch context to Foo": "Foo",
            "@agy_bot switch context to \"My Thread\"": "My Thread",
            "Antigravity: switch conversation to X": "X",
            "@a_bot @b_bot set conversation to Y": "Y",
        }
        for text, target in cases.items():
            self.assertEqual(match_context_switch(text, "Antigravity"), target, text)

    def test_the_command_must_still_lead_the_message(self):
        for text in ("hello switch context to Z", "@agy_bot please switch context to Z",
                     "@agy_bot", ""):
            self.assertIsNone(match_context_switch(text, "Antigravity"), text)

    def test_adapter_executes_the_prefixed_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = AntigravityThreadAdapter.default_settings(cwd=tmp, conversation="auto")
            adapter = AntigravityThreadAdapter("Antigravity", settings)
            with patch.object(adapter, "_lookup_conversation",
                              return_value=("new-session-uuid", "New Session")):
                reply = asyncio.run(adapter.respond(IncomingMessage(
                    text="@example_agy_bot switch context to new-session-uuid",
                    author_alias="Admin", is_admin=True)))
            self.assertTrue(reply.ok, reply.error)
            self.assertEqual((Path(tmp) / ACTIVE_CONVERSATION_FILENAME)
                             .read_text(encoding="utf-8"), "new-session-uuid")


if __name__ == "__main__":
    unittest.main()
