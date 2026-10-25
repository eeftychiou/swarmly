"""Regression tests for issue #85: a timeout must NAME its cause.

The defect these pin: on this host ``resolve_agentapi()`` succeeds (the binary
is present) but ``agentapi send-message`` exits 1 with
``{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}``. Every dispatch therefore
took the slow ``agy -p`` path and, when it exceeded ``timeout_seconds``, the
transport reported only ``thread execution timed out after 1500s`` — identical
to a context-overflow timeout. An operator reading that line cannot tell the two
apart, so the wrong remedy gets applied.

Each test below is one of two kinds, and says which:

* **discriminating** — fails on the pre-fix adapter (``394ed05``), which emitted
  only the cause-free string ``…timed out after 1500s``;
* **invariant** — passes on both revisions on purpose, because it guards a
  property that must hold either way (an absent ``agentapi`` staying unflagged,
  the reason staying bounded). These pin behaviour against future regressions
  rather than proving this diff.
"""

import asyncio
import subprocess
import unittest
from unittest.mock import patch

from swarmly.telegram.adapters.antigravity import (
    AntigravityThreadAdapter,
    describe_agentapi_failure,
)
from swarmly.telegram.adapters.base import IncomingMessage


class DescribeAgentapiFailureTests(unittest.TestCase):
    """The reason must come from agentapi's own payload, not just the exit code."""

    def test_extracts_error_field_from_json_on_stderr(self):
        result = subprocess.CompletedProcess(
            args=["agentapi", "send-message"],
            returncode=1,
            stdout="",
            stderr='{\n  "error": "ANTIGRAVITY_LS_ADDRESS is not set"\n}\n',
        )
        self.assertEqual(describe_agentapi_failure(result), "ANTIGRAVITY_LS_ADDRESS is not set")

    def test_extracts_error_field_from_json_on_stdout(self):
        result = subprocess.CompletedProcess(
            args=["agentapi", "send-message"],
            returncode=1,
            stdout='{"error": "no active session"}',
            stderr="",
        )
        self.assertEqual(describe_agentapi_failure(result), "no active session")

    def test_non_json_payload_falls_back_to_first_line(self):
        result = subprocess.CompletedProcess(
            args=["agentapi", "send-message"],
            returncode=1,
            stdout="",
            stderr="connection refused\nmore detail",
        )
        self.assertEqual(describe_agentapi_failure(result), "connection refused")

    def test_empty_payload_falls_back_to_exit_code(self):
        result = subprocess.CompletedProcess(
            args=["agentapi", "send-message"], returncode=7, stdout="", stderr=""
        )
        self.assertEqual(describe_agentapi_failure(result), "exit code 7")

    def test_oversized_reason_is_truncated_and_single_line(self):
        """The reason reaches a Telegram message, so it must be bounded.

        agentapi could echo a large body (or a path/token) back; an unbounded
        value would flood the message and an embedded newline would break the
        one-line envelope rule.
        """
        huge = "x" * 5000
        result = subprocess.CompletedProcess(
            args=["agentapi", "send-message"],
            returncode=1,
            stdout="",
            stderr=f'{{"error": "line one\\nline two {huge}"}}',
        )
        reason = describe_agentapi_failure(result)
        self.assertLessEqual(len(reason), 200)
        self.assertNotIn("\n", reason)
        self.assertIn("line one", reason)


class TimeoutCauseTests(unittest.TestCase):
    """``respond`` must surface the degraded cause on the timeout path."""

    CONV = "12345678-1234-5678-1234-567812345678"

    def _adapter(self):
        settings = AntigravityThreadAdapter.default_settings(cwd=".", conversation=self.CONV)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.resolve_conversation = lambda: (self.CONV, "Test")
        adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash-high"
        adapter.sync_conversation_to_cli = lambda cid: None
        return adapter

    def _timeout_error_for_resolved_agentapi(self) -> str:
        """The host shape: binary resolves, send-message exits 1, print run times out."""
        adapter = self._adapter()
        failed = subprocess.CompletedProcess(
            args=["agentapi", "send-message"],
            returncode=1,
            stdout="",
            stderr='{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}',
        )

        def fake_run(argv, **kwargs):
            if "send-message" in argv:
                return failed
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value="/bin/agentapi"), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", side_effect=fake_run):
            reply = asyncio.run(adapter.respond(IncomingMessage(text="ping", author_alias="Hermes")))
        return reply.error

    def _timeout_error_for_absent_agentapi(self) -> str:
        """The supported headless shape: no agentapi at all, print run times out."""
        adapter = self._adapter()
        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value=None), \
             patch(
                 "swarmly.telegram.adapters.antigravity.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1500),
             ):
            reply = asyncio.run(adapter.respond(IncomingMessage(text="ping", author_alias="Hermes")))
        return reply.error

    def test_timeout_names_the_slow_path_and_the_endpoint_reason(self):
        """The real host shape: binary resolves, send-message exits 1, print run times out."""
        adapter = self._adapter()
        agentapi_failed = subprocess.CompletedProcess(
            args=["agentapi", "send-message"],
            returncode=1,
            stdout="",
            stderr='{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}',
        )

        def fake_run(argv, **kwargs):
            if "send-message" in argv:
                return agentapi_failed
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value="/bin/agentapi"), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", side_effect=fake_run):
            reply = asyncio.run(adapter.respond(IncomingMessage(text="ping", author_alias="Hermes")))

        self.assertIsNotNone(reply)
        self.assertFalse(reply.ok)
        self.assertIn("slow path", reply.error)
        self.assertIn("ANTIGRAVITY_LS_ADDRESS is not set", reply.error)
        # The whole point: it must NOT read like a bare timeout.
        self.assertNotEqual(
            reply.error,
            "Antigravity thread execution timed out after 1500s",
        )

    def test_timeout_with_agentapi_absent_is_generic_not_slow_path(self):
        """An absent agentapi is a SUPPORTED headless config, not a degradation.

        Pins the false-alarm guard: the class docstring says an absent agentapi
        "gracefully falls back to agy -p", so a timeout there must NOT be reported
        as a degraded slow path on every single message. Asserting the absence of
        the degradation log makes this behavioural rather than a tautology on the
        invented word "generic".
        """
        adapter = self._adapter()

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value=None), \
             patch(
                 "swarmly.telegram.adapters.antigravity.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1500),
             ), \
             self.assertNoLogs("swarmly.telegram.antigravity", level="WARNING"):
            reply = asyncio.run(adapter.respond(IncomingMessage(text="ping", author_alias="Hermes")))

        self.assertIsNotNone(reply)
        self.assertFalse(reply.ok)
        self.assertIn("generic", reply.error)
        self.assertIn("1500", reply.error)
        self.assertNotIn("slow path", reply.error)

    def test_the_two_timeout_causes_are_distinguishable(self):
        """The whole point of the change: identical budget, DIFFERENT strings.

        Pre-fix both cases produced the same cause-free line, which is what made
        the original outage undiagnosable. This asserts they now differ and that
        each carries its own actionable cause.
        """
        slow = self._timeout_error_for_resolved_agentapi()
        generic = self._timeout_error_for_absent_agentapi()

        self.assertNotEqual(slow, generic)
        self.assertIn("slow path", slow)
        self.assertNotIn("slow path", generic)
        self.assertIn("1500", slow)
        self.assertIn("1500", generic)

    def test_agentapi_failure_reason_reaches_the_warning_log(self):
        """The degraded reason must be logged in EXTRACTED form, not as a raw blob.

        Both properties below were MEASURED against the pre-fix tree before being
        asserted (``scratch/probe_log_shape.py``): pre-fix the logged message is
        the multi-line raw JSON payload, post-fix it is one extracted line.

        An earlier draft of this test merely asserted the substring
        ``ANTIGRAVITY_LS_ADDRESS is not set``, which PASSED on the pre-fix code by
        accident — the raw payload already contained it. A second draft asserted
        the absence of ``'{"error"'``, which also passed on both, because the real
        payload has whitespace after the brace. Neither was evidence.
        """
        adapter = self._adapter()
        agentapi_failed = subprocess.CompletedProcess(
            args=["agentapi", "send-message"],
            returncode=1,
            stdout="",
            stderr='{\n  "error": "ANTIGRAVITY_LS_ADDRESS is not set"\n}',
        )
        ok = subprocess.CompletedProcess(args=["agy", "-p"], returncode=0, stdout="done", stderr="")

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value="/bin/agentapi"), \
             patch(
                 "swarmly.telegram.adapters.antigravity.subprocess.run",
                 side_effect=lambda argv, **kw: agentapi_failed if "send-message" in argv else ok,
             ), \
             self.assertLogs("swarmly.telegram.antigravity", level="WARNING") as captured:
            asyncio.run(adapter.respond(IncomingMessage(text="ping", author_alias="Hermes")))

        warnings = [r.getMessage() for r in captured.records if "agentapi queue failed" in r.getMessage()]
        self.assertEqual(len(warnings), 1, "expected exactly one agentapi fallback warning")
        msg = warnings[0]
        self.assertIn("ANTIGRAVITY_LS_ADDRESS is not set", msg)
        self.assertIn("exit code 1", msg)
        # Discriminating properties (false on the pre-fix tree):
        self.assertNotIn("\n", msg, "reason must be collapsed to one line")
        self.assertNotIn("{", msg, "the raw JSON payload must not be logged")


if __name__ == "__main__":
    unittest.main()
