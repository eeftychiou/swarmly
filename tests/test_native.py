"""`/cli@bot`: native CLI commands from Telegram, with no model turn (issue #116).

What these pin: the argv each allowlisted command actually runs, that nothing a passthrough
does can reach the model, that unknown and model-calling commands are refused rather than
guessed, that credentials never pass through, and that the output is cleaned and redacted.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram import native as nv

#: A fake CLI: it answers one line per command it is given, on a terminal or on a pipe alike.
FAKE_CLI = '''\
import sys

print("fake-cli 1.0")
mode = sys.argv[1] if len(sys.argv) > 1 else "exec"
if mode == "one-shot":
    print("one-shot output")
    sys.exit(0)
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(3)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    print("\\x1b[32m" + line + "\\x1b[0m: colour output")
    if line.startswith("/"):
        break
'''


class TestTheAllowlist(unittest.TestCase):

    def test_a_read_only_one_shot_command_runs_its_own_argv(self):
        for runtime, tokens in (("claude", ["--version"]),
                                ("claude", ["mcp", "list"]),
                                ("codex", ["login", "status"]),
                                ("antigravity", ["models"]),
                                ("reasonix", ["--version"])):
            with self.subTest(runtime=runtime, tokens=tokens):
                plan = nv.plan(runtime, " ".join(tokens))
                self.assertEqual(plan.kind, nv.RUN)
                self.assertEqual(plan.mode, nv.EXEC)
                self.assertEqual(plan.argv, (nv.BINARY[runtime], *tokens))

    def test_a_read_only_slash_command_runs_in_the_session(self):
        plan = nv.plan("claude", "/cost", session="abc")
        self.assertEqual(plan.kind, nv.RUN)
        self.assertEqual(plan.mode, nv.SLASH)
        self.assertEqual(plan.line, "/cost")
        self.assertEqual(plan.argv, ("claude", "--resume", "abc"))

    def test_a_session_that_does_not_exist_resumes_nothing(self):
        self.assertEqual(nv.plan("claude", "/cost").argv, ("claude",))
        self.assertEqual(nv.plan("codex", "/status", session="s1").argv,
                         ("codex", "resume", "s1"))

    def test_a_command_that_changes_something_needs_the_bang(self):
        asked = nv.plan("claude", "/clear")
        self.assertEqual(asked.kind, nv.CONFIRM)
        self.assertIn("/cli!", asked.describe())
        confirmed = nv.plan("claude", "/clear", confirmed=True)
        self.assertEqual(confirmed.kind, nv.RUN)
        self.assertEqual(confirmed.line, "/clear")

    def test_commands_that_would_call_the_model_are_refused(self):
        for runtime, command in (("claude", "/compact"), ("claude", "/review"),
                                 ("codex", "/init"), ("reasonix", "/compact")):
            with self.subTest(runtime=runtime, command=command):
                plan = nv.plan(runtime, command, confirmed=True)
                self.assertEqual(plan.kind, nv.REFUSE)
                self.assertIn("costs tokens", plan.reason)

    def test_credentials_never_pass_through(self):
        for runtime, command in (("claude", "/login"), ("claude", "/logout"),
                                 ("codex", "login"), ("claude", "mcp add token"),
                                 ("reasonix", "--api-key")):
            with self.subTest(runtime=runtime, command=command):
                plan = nv.plan(runtime, command, confirmed=True)
                self.assertEqual(plan.kind, nv.REFUSE)
                self.assertIn("credentials", plan.reason)

    def test_an_unknown_command_is_refused_not_guessed(self):
        for command in ("/frobnicate", "rm -rf /", "--dangerously-skip-permissions",
                        "/cost --all", "/model opus", ""):
            with self.subTest(command=command):
                plan = nv.plan("claude", command)
                self.assertEqual(plan.kind, nv.REFUSE, plan)
                self.assertTrue(plan.reason)

    def test_a_bot_with_no_cli_has_no_passthrough(self):
        plan = nv.plan("echo", "--version")
        self.assertEqual(plan.kind, nv.REFUSE)
        self.assertIn("no CLI", plan.reason)

    def test_the_binary_comes_from_the_configured_command(self):
        self.assertEqual(nv.binary_for("claude", command="claude --flag {prompt}"), "claude")
        self.assertEqual(nv.binary_for("claude_thread"), "claude")
        self.assertIsNone(nv.binary_for("custom"))

    def test_no_plan_carries_a_prompt(self):
        """The whole point: a passthrough never hands text to the model."""
        for runtime in ("claude", "codex", "antigravity", "reasonix"):
            for command in nv.commands_for(runtime):
                plan = nv.plan(runtime, command, confirmed=True)
                self.assertNotIn("-p", plan.argv)
                self.assertNotIn("--print", plan.argv)
                self.assertNotIn("{prompt}", " ".join(plan.argv))

    def test_a_request_is_read_from_the_command_word(self):
        self.assertEqual(nv.parse_request("/cli /cost"), (False, "/cost"))
        self.assertEqual(nv.parse_request("/cli! /clear"), (True, "/clear"))
        self.assertEqual(nv.parse_request("/cli@claudebot mcp list"), (False, "mcp list"))
        self.assertEqual(nv.parse_request("/CLI!@Bot models"), (True, "models"))
        self.assertIsNone(nv.parse_request("/cleanup"))
        self.assertEqual(nv.target_of("/cli@claudebot /cost"), "claudebot")
        self.assertIsNone(nv.target_of("/cli /cost"))


class TestClean(unittest.TestCase):

    def test_terminal_escapes_and_controls_are_stripped(self):
        text, truncated = nv.clean("\x1b[31mred\x1b[0m \x1b]0;title\x07plain\x00")
        self.assertEqual(text, "red plain")
        self.assertFalse(truncated)

    def test_carriage_returns_become_newlines(self):
        self.assertEqual(nv.clean("a\r\nb\rc")[0], "a\nb\nc")

    def test_the_output_is_capped(self):
        text, truncated = nv.clean("x" * 50, max_chars=10)
        self.assertTrue(truncated)
        self.assertLess(len(text), 40)
        self.assertIn("truncated", text)


class FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class TestTheExecRunner(unittest.TestCase):

    def runner(self, **kwargs):
        calls = []

        def run(argv, **options):
            calls.append((argv, options))
            return FakeResult(stdout="ok\n", returncode=0)

        return nv.Runner(runner=run, **kwargs), calls

    def test_a_read_only_command_runs_once_and_returns_its_output(self):
        runner, calls = self.runner(cwd="work")
        outcome = runner.run(nv.plan("claude", "--version"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.text, "ok")
        self.assertEqual(calls[0][0], ["claude", "--version"])
        self.assertEqual(calls[0][1]["cwd"], "work")
        self.assertFalse(calls[0][1]["shell"])

    def test_a_refused_plan_is_never_executed(self):
        runner, calls = self.runner()
        outcome = runner.run(nv.plan("claude", "/compact", confirmed=True))
        self.assertFalse(outcome.ok)
        self.assertEqual(calls, [])
        self.assertIn("refused", outcome.text)

    def test_a_failure_keeps_its_exit_code_and_stderr(self):
        calls = []

        def run(argv, **options):
            calls.append(argv)
            return FakeResult(stdout="", stderr="nope", returncode=2)

        outcome = nv.Runner(runner=run).run(nv.plan("codex", "login status"))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.exit_code, 2)
        self.assertEqual(outcome.text, "nope")

    def test_a_timeout_is_reported_not_raised(self):
        def run(argv, **options):
            raise subprocess.TimeoutExpired(argv, 1)

        outcome = nv.Runner(runner=run, timeout=1).run(nv.plan("claude", "--version"))
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.ok)
        self.assertIn("killed", outcome.line(nv.plan("claude", "--version"), 1.0))

    def test_the_runtime_never_inherits_a_secret(self):
        runner, calls = self.runner()
        old = os.environ.get("BOT_TOKEN")
        os.environ["BOT_TOKEN"] = "123:secret"
        try:
            runner.run(nv.plan("claude", "--version"))
        finally:
            if old is None:
                os.environ.pop("BOT_TOKEN", None)
            else:
                os.environ["BOT_TOKEN"] = old
        env = calls[0][1]["env"]
        self.assertNotIn("BOT_TOKEN", env)
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")

    def test_the_output_is_redacted_last(self):
        runner = nv.Runner(runner=lambda argv, **kw: FakeResult(stdout="user @alice here"),
                            redact=lambda text: text.replace("@alice", "@maintainer"))
        outcome = runner.run(nv.plan("claude", "--version"))
        self.assertEqual(outcome.text, "user @maintainer here")


class FakeSession:
    """An interactive session that records what was typed into it."""

    def __init__(self, reply="screen text", *, argv=(), **_kwargs):
        self.argv = tuple(argv)
        self.reply = reply
        self.sent = []
        self.closed = False
        self.opened = False

    def open(self):
        self.opened = True
        return self

    def send(self, line):
        self.sent.append(line)

    def read_until_quiet(self, *, deadline=None):
        return self.reply

    def close(self):
        self.closed = True


class TestTheInteractiveRunner(unittest.TestCase):

    def test_a_slash_command_is_typed_and_never_a_prompt(self):
        made = []

        def factory(argv, **kwargs):
            session = FakeSession(argv=argv)
            made.append(session)
            return session

        runner = nv.Runner(session_factory=factory)
        outcome = runner.run(nv.plan("claude", "/cost", session="abc"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.text, "screen text")
        self.assertEqual(made[0].argv, ("claude", "--resume", "abc"))
        self.assertEqual(made[0].sent, ["/cost"])
        self.assertTrue(made[0].closed)

    def test_the_session_is_closed_even_when_it_fails(self):
        made = []

        class Broken(FakeSession):
            def send(self, line):
                raise nv.SessionError("no terminal")

        def factory(argv, **kwargs):
            session = Broken(argv=argv)
            made.append(session)
            return session

        outcome = nv.Runner(session_factory=factory).run(nv.plan("claude", "/cost"))
        self.assertFalse(outcome.ok)
        self.assertIn("no terminal", outcome.text)
        self.assertTrue(made[0].closed)


class TestARealSession(unittest.TestCase):
    """One run against a real process, on whichever terminal mechanism this host has."""

    def script(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "fake_cli.py"
        path.write_text(FAKE_CLI, encoding="utf-8")
        return str(path)

    def test_a_command_comes_back_from_a_real_session(self):
        if sys.platform == "win32":
            self.skipTest("a pipe session is exercised on POSIX; the pty path is POSIX-only")
        script = self.script()
        session = nv.InteractiveSession([sys.executable, script], deadline=15.0,
                                        quiet_seconds=0.4)
        try:
            session.open()
            session.send("/cost")
            text = session.read_until_quiet()
        finally:
            session.close()
        self.assertIn("fake-cli 1.0", text)
        self.assertIn("/cost", text)
        cleaned, _ = nv.clean(text)
        self.assertIn("/cost", cleaned)
        self.assertNotIn("\x1b", cleaned)

    def test_an_exec_run_against_a_real_process(self):
        script = self.script()
        runner = nv.Runner(runner=None)
        native = nv.Native(nv.RUN, mode=nv.EXEC,
                           argv=(sys.executable, script, "one-shot"))
        outcome = runner.run(native)
        self.assertTrue(outcome.ok, outcome.text)
        self.assertIn("one-shot output", outcome.text)

    def test_a_failing_process_is_a_failure_not_an_exception(self):
        script = self.script()
        native = nv.Native(nv.RUN, mode=nv.EXEC, argv=(sys.executable, script, "fail"))
        outcome = nv.Runner().run(native)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.exit_code, 3)
        self.assertIn("boom", outcome.text)


class TestAvailability(unittest.TestCase):

    def test_a_missing_binary_is_reported(self):
        self.assertIn("not installed", nv.available("echo", command="no-such-binary-xyz --x"))
        self.assertEqual(nv.available("echo"), "this bot has no CLI to pass a command to")


if __name__ == "__main__":
    unittest.main()
