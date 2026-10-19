"""
Tests for the Telegram home channel.

No network and no aiogram requirement for the routing logic: the channel is exercised through
``ChannelEvent`` with an injected sender. The aiogram-specific paths are guarded with
``skipUnless``.

Run: python -m unittest tests.test_telegram_channel -v
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.gitenv import git_env
from swarmly.telegram.adapters import EchoAdapter, SubprocessAdapter, build_adapter
from swarmly.telegram.adapters.antigravity import AntigravityAdapter, AntigravityThreadAdapter
from swarmly.telegram.adapters.base import AdapterReply, IncomingMessage
from swarmly.telegram.adapters.codex import CodexAdapter, CodexThreadAdapter
from swarmly.telegram.adapters.hermes import HermesAdapter
from swarmly.telegram.channel import AgentChannel, ChannelEvent
from swarmly.telegram.config import AdapterSettings, AgentConfig, ChannelLimits, ConfigError
from swarmly.models import A2AMessage, MessageIntent, MessagePriority, RecipientInfo, SenderInfo

try:
    import aiogram  # noqa: F401
    HAS_AIOGRAM = True
except ImportError:
    HAS_AIOGRAM = False

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

GROUP = -1001234567890
ADMIN_ID = 1234567890

CONFIG_YAML = """
agent:
  name: HermesBot
  alias: Hermes
  bot_token: "123456:TEST"
  home_channel_id: -1001234567890
  admin_user_id: 1234567890
peers:
  Codex: CodexBot
  Antigravity: AntigravityBot
adapter:
  type: echo
limits:
  min_seconds_between_messages: 0
"""


class FakeSender:
    """Collects what the channel would post to Telegram."""

    def __init__(self) -> None:
        self.sent = []

    async def __call__(self, payload: str, reply_to_message_id=None):
        self.sent.append({"text": payload, "reply_to": reply_to_message_id})
        return payload

    @property
    def last(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""


def make_config(**overrides) -> AgentConfig:
    data = {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
        "peers": {"Codex": "CodexBot", "Antigravity": "AntigravityBot"},
        "adapter": {"type": "echo"},
        "limits": {"min_seconds_between_messages": 0, "min_seconds_between_agent_turns": 0,
                   "selftest_seconds": 0},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return AgentConfig.from_dict(data)


def make_channel(config: AgentConfig = None, adapter=None, **kwargs):
    sender = FakeSender()
    clock_state = {"t": 1000.0}
    slept = []

    def clock():
        return clock_state["t"]

    async def sleeper(seconds):
        slept.append(seconds)
        clock_state["t"] += seconds

    channel = AgentChannel(config or make_config(), adapter=adapter or EchoAdapter("Hermes"),
                           sender=sender, clock=kwargs.pop("clock", clock),
                           sleeper=kwargs.pop("sleeper", sleeper))
    channel._test_sender = sender
    channel._test_slept = slept
    channel._test_clock = clock_state
    return channel


def event(text: str, author_id=202, username="CodexBot", is_bot=True, chat_id=GROUP,
          replies_to_bot=False, message_id=1, thread_id=None, attachments=None,
          raw=None) -> ChannelEvent:
    return ChannelEvent(text=text, chat_id=chat_id, author_id=author_id, author_username=username,
                        author_is_bot=is_bot, replies_to_bot=replies_to_bot, message_id=message_id,
                        thread_id=thread_id, attachments=list(attachments or []), raw=raw)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def _token(line: str, name: str) -> int:
    """Parse an integer ``name=<int>`` token out of a single-line reply.

    Substring checks are prefix-satisfiable (``"busy_dropped=1" in "busy_dropped=111"``), so the
    value is read out and compared instead. A missing token fails as an assertion with the reply
    quoted — the case these tests exist to catch, so it must not surface as an AttributeError.
    """
    match = re.search(r"\b%s=(\d+)\b" % re.escape(name), line)
    if match is None:
        raise AssertionError(f"no `{name}=<int>` token in reply: {line!r}")
    return int(match.group(1))


# --------------------------------------------------------------------------------------
# 1. Configuration
# --------------------------------------------------------------------------------------
@unittest.skipUnless(HAS_YAML, "PyYAML not installed")
class TestConfig(unittest.TestCase):

    def test_config_yaml_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(CONFIG_YAML, encoding="utf-8")
            config = AgentConfig.load(path)

        self.assertEqual(config.identity.alias, "Hermes")
        self.assertEqual(config.identity.name, "HermesBot")
        self.assertEqual(config.identity.username, "@HermesBot")
        self.assertEqual(config.identity.home_channel_id, GROUP)
        self.assertEqual(config.identity.admin_user_id, ADMIN_ID)
        self.assertEqual(list(config.peers), ["Codex", "Antigravity"])
        self.assertEqual(config.adapter.type, "echo")
        self.assertEqual(config.validate(), [])

    def test_token_can_come_from_the_environment(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(CONFIG_YAML.replace('bot_token: "123456:TEST"',
                                                "bot_token_env: UNIT_TEST_TOKEN"), encoding="utf-8")
            os.environ["UNIT_TEST_TOKEN"] = "999:FROM_ENV"
            try:
                config = AgentConfig.load(path)
            finally:
                del os.environ["UNIT_TEST_TOKEN"]

        self.assertEqual(config.identity.bot_token, "999:FROM_ENV")

    def test_validation_reports_every_missing_field(self):
        config = AgentConfig.from_dict({"agent": {"alias": "Hermes", "name": "HermesBot"}})
        problems = config.validate()
        self.assertTrue(any("home_channel_id" in p for p in problems))
        self.assertTrue(any("admin_user_id" in p for p in problems))
        self.assertTrue(any("bot token" in p for p in problems))

    def test_self_alias_in_peers_is_rejected(self):
        config = make_config(peers={"Hermes": "HermesBot"})
        self.assertTrue(any("own alias" in p for p in config.validate()))

    def test_admin_check_is_by_numeric_id(self):
        config = make_config()
        self.assertTrue(config.is_admin(ADMIN_ID))
        self.assertTrue(config.is_admin(str(ADMIN_ID)))
        self.assertFalse(config.is_admin(999))

    def test_peer_resolution_by_username_and_id(self):
        config = make_config(peers={"Codex": "CodexBot", "Antigravity": "987654321"})
        self.assertEqual(config.peer_alias_for("CodexBot"), "Codex")
        self.assertEqual(config.peer_alias_for("@codexbot"), "Codex")
        self.assertEqual(config.peer_alias_for(None, 987654321), "Antigravity")
        self.assertIsNone(config.peer_alias_for("StrangerBot"))


# --------------------------------------------------------------------------------------
# 2. Adapters for the three runtimes
# --------------------------------------------------------------------------------------
class TestAdapterCommands(unittest.TestCase):

    def test_codex_default_command_is_the_verified_one(self):
        settings = CodexAdapter.default_settings(cwd="/repo")
        self.assertEqual(settings.type, "codex")
        self.assertEqual(settings.command, "codex exec --sandbox workspace-write {prompt}")
        adapter = CodexAdapter("Codex", settings)
        self.assertEqual(adapter.build_argv("hello"), ["codex", "exec", "--sandbox", "workspace-write", "hello"])

    def test_codex_thread_queues_into_the_configured_session(self):
        settings = CodexThreadAdapter.default_settings(
            cwd="/repo", thread="0190f000-0000-7000-8000-000000000001"
        )
        adapter = CodexThreadAdapter("Codex", settings)
        argv = adapter.build_argv("hello")
        self.assertEqual(
            argv,
            [
                "codex",
                "queue",
                "--thread",
                "0190f000-0000-7000-8000-000000000001",
                "--message",
                "hello",
            ],
        )

    def test_codex_thread_requires_a_session(self):
        with self.assertRaises(ConfigError):
            CodexThreadAdapter("Codex", CodexThreadAdapter.default_settings())

    def test_codex_thread_allows_explicit_username_delivery_during_loop_guard(self):
        adapter = CodexThreadAdapter("Codex", CodexThreadAdapter.default_settings(thread="current"))
        self.assertTrue(adapter.accepts_explicit_username_delivery())
        self.assertTrue(adapter.accepts_exact_username_during_loop_guard())
        self.assertFalse(CodexAdapter("Codex", CodexAdapter.default_settings())
                         .accepts_exact_username_during_loop_guard())
        self.assertTrue(AntigravityAdapter("Antigravity", AntigravityAdapter.default_settings())
                        .accepts_explicit_username_delivery())
        adapter_agy_thread = AntigravityThreadAdapter("Antigravity", AntigravityThreadAdapter.default_settings(conversation="current"))
        self.assertTrue(adapter_agy_thread.accepts_explicit_username_delivery())
        self.assertTrue(adapter_agy_thread.accepts_exact_username_during_loop_guard())

    def test_codex_thread_returns_a_delivery_receipt_marker(self):
        import subprocess

        settings = CodexThreadAdapter.default_settings(cwd="/repo", thread="current")
        adapter = CodexThreadAdapter("Codex", settings)
        completed = subprocess.CompletedProcess(
            args=adapter.build_argv("ignored"), returncode=0, stdout="queued\n", stderr=""
        )
        with patch("swarmly.telegram.adapters.codex.subprocess.run", return_value=completed) as run_queue:
            reply = asyncio.run(adapter.respond(IncomingMessage("hello", "Hermes")))

        self.assertIsNotNone(reply)
        self.assertTrue(reply.ok)
        self.assertTrue(reply.meta["queued"])
        self.assertEqual(reply.meta["conversation"], "current")
        run_queue.assert_called_once()
        self.assertEqual(run_queue.call_args.args[0][0:5], [
            "codex", "queue", "--thread", "current", "--message"
        ])
        self.assertIn("hello", run_queue.call_args.args[0][-1])

    def test_codex_thread_reports_a_missing_cli_actionably(self):
        settings = CodexThreadAdapter.default_settings(cwd="/repo", thread="current")
        adapter = CodexThreadAdapter("Codex", settings)
        with patch("swarmly.telegram.adapters.codex.subprocess.run",
                   side_effect=FileNotFoundError(2, "The system cannot find the file specified")):
            reply = asyncio.run(adapter.respond(IncomingMessage("hello", "Hermes")))

        self.assertIsNotNone(reply)
        self.assertFalse(reply.ok)
        self.assertIn("the codex CLI is not on this daemon's PATH", reply.error)
        self.assertIn("run_codex_telegram", reply.error)

    def test_antigravity_thread_runs_without_echoing_to_telegram(self):
        import subprocess

        settings = AntigravityThreadAdapter.default_settings(cwd="/repo", conversation="current")
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        completed = subprocess.CompletedProcess(
            args=["agentapi", "send-message"], returncode=0, stdout="done\n", stderr=""
        )
        # No endpoint discovery: on Windows it runs tasklist and PowerShell through the same
        # subprocess.run this test counts.
        with patch.object(adapter, "_get_summary_dbs", return_value=[]), \
             patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value="/bin/agentapi"), \
             patch("swarmly.telegram.endpoint.resolve_endpoint", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", return_value=completed) as run_thread:
            reply = asyncio.run(adapter.respond(IncomingMessage("hello", "Hermes")))

        self.assertIsNone(reply)
        run_thread.assert_called_once()
        argv = run_thread.call_args.args[0]
        self.assertEqual(argv[0:2], ["/bin/agentapi", "send-message"])
        self.assertEqual(argv[3], "current")
        self.assertIn("hello", argv[4])

        # Test fallback when agentapi is not available
        completed_agy = subprocess.CompletedProcess(
            args=["agy", "-p"], returncode=0, stdout="done\n", stderr=""
        )
        with patch.object(adapter, "_get_summary_dbs", return_value=[]), \
             patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", return_value=completed_agy) as run_fallback:
            reply = asyncio.run(adapter.respond(IncomingMessage("hello fallback", "Hermes")))

        self.assertIsNone(reply)
        run_fallback.assert_called_once()
        argv_fb = run_fallback.call_args.args[0]
        self.assertEqual(argv_fb[0:2], ["agy", "-p"])
        self.assertIn("--dangerously-skip-permissions", argv_fb)
        self.assertIn("--conversation", argv_fb)
        self.assertIn("current", argv_fb)
        self.assertIn("hello fallback", argv_fb[2])


    def test_hermes_default_command_is_oneshot_mode(self):
        settings = HermesAdapter.default_settings(cwd="/repo", profile="coding")
        self.assertEqual(settings.command, "hermes -z {prompt}")
        self.assertEqual(settings.extra_env, {"HERMES_PROFILE": "coding"})
        adapter = HermesAdapter("Hermes", settings)
        self.assertEqual(adapter.build_argv("hello"), ["hermes", "-z", "hello"])

    def test_antigravity_default_command_is_print_mode(self):
        settings = AntigravityAdapter.default_settings(cwd="/repo")
        self.assertEqual(settings.command, "agy -p {prompt} --print-timeout 20m --dangerously-skip-permissions")
        adapter = AntigravityAdapter("Antigravity", settings)
        self.assertEqual(adapter.build_argv("hello"), ["agy", "-p", "hello", "--print-timeout", "20m", "--dangerously-skip-permissions"])

    def test_antigravity_with_conversation_appends_flag(self):
        settings = AntigravityAdapter.default_settings(cwd="/repo", conversation="12345678-1234-5678-1234-567812345678")
        adapter = AntigravityAdapter("Antigravity", settings)
        self.assertEqual(
            adapter.build_argv("hello"),
            ["agy", "-p", "hello", "--print-timeout", "20m", "--dangerously-skip-permissions", "--conversation", "12345678-1234-5678-1234-567812345678"]
        )

    def test_antigravity_admin_switch_command(self):
        import asyncio
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = AntigravityAdapter.default_settings(cwd=tmpdir)
            adapter = AntigravityAdapter("Antigravity", settings)
            msg = IncomingMessage(
                text="switch context to 12345678-1234-5678-1234-567812345678",
                author_alias="admin",
                is_admin=True
            )
            reply = asyncio.run(adapter.respond(msg))
            self.assertTrue(reply.ok)
            self.assertIn("Switched active context to", reply.text)
            self.assertEqual(
                (Path(tmpdir) / ".active_conversation").read_text(encoding="utf-8"),
                "12345678-1234-5678-1234-567812345678"
            )

    def test_antigravity_inherits_conversation_model(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = AntigravityAdapter.default_settings(cwd=tmpdir, conversation="12345678-1234-5678-1234-567812345678")
            adapter = AntigravityAdapter("Antigravity", settings)
            adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash"
            argv = adapter.build_argv("hello")
            self.assertIn("--conversation", argv)
            self.assertIn("12345678-1234-5678-1234-567812345678", argv)
            self.assertIn("--model", argv)
            self.assertIn("gemini-3.8-flash", argv)

    def test_prompt_is_never_shell_interpreted(self):
        adapter = CodexAdapter("Codex", CodexAdapter.default_settings())
        argv = adapter.build_argv("rm -rf / ; echo pwned")
        self.assertEqual(argv[-1], "rm -rf / ; echo pwned")
        self.assertEqual(len(argv), 5)              # one argv element, no extra tokens

    def test_quoted_program_path_reaches_the_runner_unquoted(self):
        settings = AdapterSettings(type="custom", command='"/opt/tools/my agent" --prompt {prompt}')
        adapter = SubprocessAdapter("Custom", settings)
        argv = adapter.build_argv("hi")
        self.assertEqual(argv[0], "/opt/tools/my agent")
        self.assertEqual(argv[-1], "hi")

    def test_template_without_placeholder_appends_the_prompt(self):
        adapter = SubprocessAdapter("X", AdapterSettings(type="custom", command="mytool --json"))
        self.assertEqual(adapter.build_argv("ask"), ["mytool", "--json", "ask"])

    def test_unknown_adapter_type_is_a_config_error(self):
        config = make_config(adapter={"type": "wat"})
        with self.assertRaises(ConfigError):
            build_adapter(config)

    def test_build_adapter_returns_the_right_class_for_each_type(self):
        for kind, expected in (("codex", CodexAdapter),
                               ("codex_thread", CodexThreadAdapter),
                               ("hermes", HermesAdapter),
                               ("antigravity", AntigravityAdapter),
                               ("antigravity_thread", AntigravityThreadAdapter),
                               ("echo", EchoAdapter)):
            adapter_config = {"type": kind}
            if kind in ("codex_thread", "antigravity_thread"):
                adapter_config["conversation"] = "current"
            config = make_config(adapter=adapter_config)
            self.assertIsInstance(build_adapter(config), expected)

    def test_subprocess_empty_output_diagnosability(self):
        import asyncio

        class FakeProc:
            def __init__(self, stdout: bytes, stderr: bytes, code: int = 0):
                self._stdout = stdout
                self._stderr = stderr
                self.returncode = code

            async def communicate(self):
                return self._stdout, self._stderr

        async def fake_runner_empty(argv, cwd, env, timeout):
            return FakeProc(b"", b"", 0)

        async def fake_runner_stderr(argv, cwd, env, timeout):
            return FakeProc(b"", b"soft-denial occurred\n", 0)

        adapter = SubprocessAdapter("Antigravity", AdapterSettings(type="custom", command="tool {prompt}"), runner=fake_runner_empty)
        reply = asyncio.run(adapter.respond(IncomingMessage("hi", "Hermes")))
        self.assertFalse(reply.ok)
        self.assertIn("empty stdout and stderr", reply.error)
        self.assertIn("returncode: 0", reply.error)

        adapter2 = SubprocessAdapter("Antigravity", AdapterSettings(type="custom", command="tool {prompt}"), runner=fake_runner_stderr)
        reply2 = asyncio.run(adapter2.respond(IncomingMessage("hi", "Hermes")))
        self.assertFalse(reply2.ok)
        self.assertIn("empty stdout; stderr: soft-denial occurred", reply2.error)

    def test_environment_carries_the_alias(self):
        adapter = HermesAdapter("Hermes", HermesAdapter.default_settings(profile="coding"))
        env = adapter.environment({"PATH": "/usr/bin"})
        self.assertEqual(env["A2A_AGENT_ALIAS"], "Hermes")
        self.assertEqual(env["HERMES_PROFILE"], "coding")


class TestCheckPreflight(unittest.TestCase):
    """``check`` resolves the adapter's CLI, so a broken PATH fails before the daemon starts."""

    def _codex_thread_config(self) -> AgentConfig:
        return make_config(adapter={"type": "codex_thread", "conversation": "current"})

    def test_check_reports_a_missing_cli(self):
        from swarmly.telegram.cli import _check

        output = io.StringIO()
        with patch("swarmly.telegram.cli.shutil.which", return_value=None), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(self._codex_thread_config()))

        self.assertEqual(code, 1)
        self.assertIn("'codex' not found on PATH", output.getvalue())
        self.assertIn("run_codex_telegram", output.getvalue())

    def test_check_passes_when_the_cli_resolves(self):
        from swarmly.telegram.cli import _check

        output = io.StringIO()
        with patch("swarmly.telegram.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(self._codex_thread_config()))

        self.assertEqual(code, 0)
        self.assertIn("OK", output.getvalue())

    def test_check_accepts_an_absolute_program_path(self):
        from swarmly.telegram.cli import _check

        config = make_config(
            adapter={"type": "codex", "command": '"/opt/codex/bin/codex" exec {prompt}'}
        )
        output = io.StringIO()
        with patch("swarmly.telegram.cli.os.path.exists", return_value=True), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(config))

        self.assertEqual(code, 0)

    def test_check_resolves_a_relative_program_against_the_adapter_cwd(self):
        from swarmly.telegram.cli import _check

        config = make_config(
            adapter={"type": "codex", "command": "bin/codex exec {prompt}", "cwd": "/repo"}
        )
        output = io.StringIO()
        with patch("swarmly.telegram.cli.os.path.exists", return_value=True), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(config))

        self.assertEqual(code, 0)

    def test_check_launcher_hint_is_codex_specific(self):
        from swarmly.telegram.cli import _check

        output = io.StringIO()
        with patch("swarmly.telegram.cli.shutil.which", return_value=None), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(make_config(adapter={"type": "hermes"})))

        self.assertEqual(code, 1)
        self.assertIn("'hermes' not found on PATH", output.getvalue())
        self.assertNotIn("run_codex_telegram", output.getvalue())

    def test_check_reports_a_missing_program_path_with_path_wording(self):
        from swarmly.telegram.cli import _check

        config = make_config(adapter={"type": "codex", "command": '"/nope/codex" exec {prompt}'})
        output = io.StringIO()
        with patch("swarmly.telegram.cli.os.path.exists", return_value=False), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(config))

        self.assertEqual(code, 1)
        self.assertIn("'/nope/codex' does not exist", output.getvalue())

    def test_check_skips_in_process_and_custom_adapters(self):
        from swarmly.telegram.cli import _check

        for adapter in ({"type": "echo"}, {"type": "custom", "command": "tool {prompt}"}):
            output = io.StringIO()
            with patch("swarmly.telegram.cli.shutil.which", return_value=None), \
                    contextlib.redirect_stdout(output):
                code = asyncio.run(_check(make_config(adapter=adapter)))
            self.assertEqual(code, 0, f"{adapter['type']}: {output.getvalue()}")

    def test_check_preflights_the_recommended_antigravity_thread_adapter(self):
        """DISCRIMINATING: `check` must verify agentapi for antigravity_thread.

        `agy-install.md` marks `antigravity_thread` as the recommended type, but
        it was absent from `_CLI_ADAPTER_TYPES` AND its branch set no
        `command_line`, so the preflight block could not inspect it. A host with
        no `agentapi` on PATH still reported "OK — config is usable", and the
        operator only discovered the problem when the transport silently could
        not queue a single message.
        """
        from swarmly.telegram.cli import _check

        config = make_config(adapter={"type": "antigravity_thread"})
        output = io.StringIO()
        with patch("swarmly.telegram.cli.shutil.which", return_value=None), \
                patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                      return_value=None), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(config))

        self.assertEqual(
            code, 1,
            "check passed on a host with no agentapi and the recommended adapter "
            f"type selected: {output.getvalue()}")
        self.assertIn("'agentapi' not found on PATH", output.getvalue())
        self.assertIn("the antigravity_thread adapter cannot", output.getvalue())

    def test_check_preflights_a_resolved_agentapi_path_for_antigravity_thread(self):
        """DISCRIMINATING: a resolved-but-unreachable agentapi must fail `check`.

        The branch prints whatever `resolve_agentapi()` returned, so `check` has
        value only if it then verifies that program. Setting no `command_line`
        there makes the resolved value decorative: the healthy-host test passes
        either way, because a skipped preflight appends no problem.
        """
        from swarmly.telegram.cli import _check

        config = make_config(adapter={"type": "antigravity_thread"})
        output = io.StringIO()
        with patch("swarmly.telegram.cli.os.path.exists", return_value=False), \
                patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                      return_value=["/nope/agentapi", "agentapi"]), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(config))

        self.assertEqual(code, 1, output.getvalue())
        self.assertIn("'/nope/agentapi' does not exist", output.getvalue())

    def test_check_accepts_a_resolvable_agentapi_for_antigravity_thread(self):
        """The preflight must not false-positive on a healthy host."""
        from swarmly.telegram.cli import _check

        config = make_config(adapter={"type": "antigravity_thread"})
        output = io.StringIO()
        with patch("swarmly.telegram.cli.shutil.which", return_value="/bin/agentapi"), \
                patch("swarmly.telegram.cli.os.path.exists", return_value=True), \
                patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                      return_value=["/bin/agentapi", "agentapi"]), \
                contextlib.redirect_stdout(output):
            code = asyncio.run(_check(config))

        self.assertEqual(code, 0, output.getvalue())

    def test_cli_adapter_types_cover_the_command_template_registry(self):
        from swarmly.telegram.cli import _CLI_ADAPTER_TYPES
        from swarmly.telegram.config import DEFAULT_COMMANDS

        template_types = {kind for kind, template in DEFAULT_COMMANDS.items() if template}
        self.assertTrue(
            template_types <= set(_CLI_ADAPTER_TYPES),
            f"unpreflighted CLI types: {sorted(template_types - set(_CLI_ADAPTER_TYPES))}",
        )
        self.assertIn("codex_thread", _CLI_ADAPTER_TYPES)
        # antigravity_thread has no DEFAULT_COMMANDS template (it resolves
        # agentapi itself), so the registry check above cannot see it. Pin it
        # explicitly -- that blind spot is how the recommended type stayed
        # unpreflighted.
        self.assertIn("antigravity_thread", _CLI_ADAPTER_TYPES)


class TestSubprocessAdapter(unittest.TestCase):
    """The subprocess boundary is faked: no process is ever spawned."""

    class FakeProcess:
        def __init__(self, stdout=b"", stderr=b"", returncode=0):
            self._stdout, self._stderr, self.returncode = stdout, stderr, returncode

        async def communicate(self):
            return self._stdout, self._stderr

    def _adapter(self, result=None, raises=None, record=None):
        async def runner(argv, cwd, env, timeout):
            if record is not None:
                record.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
            if raises is not None:
                raise raises
            return result

        return SubprocessAdapter("Hermes", HermesAdapter.default_settings(), runner=runner)

    def test_successful_run_returns_trimmed_stdout(self):
        adapter = self._adapter(self.FakeProcess(stdout=b"  answer here  \n"))
        reply = run(adapter.respond(IncomingMessage(text="do it", author_alias="admin")))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "  answer here  \n")

    def test_nonzero_exit_is_reported_as_a_failure(self):
        adapter = self._adapter(self.FakeProcess(stderr=b"boom: model unavailable", returncode=1))
        reply = run(adapter.respond(IncomingMessage(text="do it", author_alias="admin")))
        self.assertFalse(reply.ok)
        self.assertIn("model unavailable", reply.error)

    def test_empty_output_is_a_failure_not_a_silent_success(self):
        adapter = self._adapter(self.FakeProcess(stdout=b"   \n"))
        reply = run(adapter.respond(IncomingMessage(text="do it", author_alias="admin")))
        self.assertFalse(reply.ok)
        self.assertIn("no output", reply.error)

    def test_timeout_is_reported(self):
        adapter = self._adapter(raises=asyncio.TimeoutError())
        adapter._run = lambda argv: _raise(asyncio.TimeoutError())
        reply = run(adapter.respond(IncomingMessage(text="do it", author_alias="admin")))
        self.assertFalse(reply.ok)
        self.assertIn("timed out", reply.error)

    def test_missing_binary_is_reported_clearly(self):
        adapter = self._adapter(raises=FileNotFoundError("codex not found"))
        reply = run(adapter.respond(IncomingMessage(text="do it", author_alias="admin")))
        self.assertFalse(reply.ok)
        self.assertIn("could not start", reply.error)

    def test_long_output_is_truncated_with_a_marker(self):
        adapter = self._adapter(self.FakeProcess(stdout=b"x" * 5000))
        adapter.settings.max_output_chars = 100
        reply = run(adapter.respond(IncomingMessage(text="do it", author_alias="admin")))
        self.assertTrue(reply.text.startswith("x" * 100))
        self.assertIn("truncated", reply.text)

    def test_prompt_carries_the_author_alias(self):
        record = []

        async def runner(argv, cwd, env, timeout):
            record.append({"argv": list(argv)})
            return self.FakeProcess(stdout=b"ok")

        adapter = HermesAdapter("Hermes", HermesAdapter.default_settings(), runner=runner)
        run(adapter.respond(IncomingMessage(text="status?", author_alias="admin")))
        self.assertIn("from admin", record[0]["argv"][-1])

    def test_codex_also_frames_the_prompt_with_the_author(self):
        record = []

        async def runner(argv, cwd, env, timeout):
            record.append({"argv": list(argv)})
            return self.FakeProcess(stdout=b"ok")

        adapter = CodexAdapter("Codex", CodexAdapter.default_settings(), runner=runner)
        run(adapter.respond(IncomingMessage(text="diff please", author_alias="Antigravity")))
        self.assertIn("from Antigravity", record[0]["argv"][-1])

    def test_disabled_adapter_stays_silent(self):
        adapter = self._adapter(self.FakeProcess(stdout=b"ok"))
        adapter.settings.enabled = False
        self.assertIsNone(run(adapter.respond(IncomingMessage(text="hi", author_alias="admin"))))


async def _raise(exc):
    raise exc


# --------------------------------------------------------------------------------------
# 3. Channel routing
# --------------------------------------------------------------------------------------
class TestChannelRouting(unittest.TestCase):
    def test_terminal_peer_ack_reachability_and_filtering(self):
        """
        Reachability proof that terminal bot closures never reach the arming site (:1001).
        Because should_respond correctly classifies and filters them, and because the only
        bot events that can bypass the filter are human admins (not bots), this proves
        item (1) of issue 14 is already guarded and the arming site is unreachable.
        """
        from swarmly.telegram.channel import ChannelEvent
        channel = make_channel()

        # 1. bot-authored pure sign-off, plain -> (False, ...)
        ev = event("Acknowledged — standing down", username="CodexBot", is_bot=True)
        decision, reason = channel.should_respond(ev)
        self.assertFalse(decision)
        self.assertIn("terminal acknowledgment", reason)

        # 2. bot-authored pure sign-off, @mention-prefixed -> (False, ...)
        ev = event("@HermesBot closing here", username="CodexBot", is_bot=True)
        decision, reason = channel.should_respond(ev)
        self.assertFalse(decision)

        # 3. question-free NON-sign-off -> routes
        ev = event("@HermesBot I found the issue.", username="CodexBot", is_bot=True)
        decision, reason = channel.should_respond(ev)
        self.assertTrue(decision)

        # 4. mixed closure + real action -> routes
        ev = event("@HermesBot Acknowledged — standing down. Please verify this.", username="CodexBot", is_bot=True)
        decision, reason = channel.should_respond(ev)
        self.assertTrue(decision)

        # 5. human-authored terminal closure -> routes/registers
        ev = event("@HermesBot Acknowledged — standing down", username="HumanUser", is_bot=False,
                   author_id=ADMIN_ID)
        decision, reason = channel.should_respond(ev)
        self.assertTrue(decision)

        # 6. bus-shaped event (author_id=None, author_is_bot=True) sign-off -> (False, ...)
        ev = ChannelEvent(text="Acknowledged — standing down", chat_id=GROUP, author_id=None, author_username="CodexBot", author_is_bot=True, message_id=2)
        decision, reason = channel.should_respond(ev)
        self.assertFalse(decision)


    def test_mention_is_answered_and_preserved_in_the_prompt(self):
        channel = make_channel()
        text = run(channel.handle_event(event("@HermesBot which tests fail on main?")))
        self.assertIn("which tests fail on main?", text)
        self.assertIn("@HermesBot", text)

    def test_alias_mention_also_works(self):
        channel = make_channel()
        self.assertIsNotNone(run(channel.handle_event(event("Hermes, status please"))))

    def test_unaddressed_bot_chatter_is_ignored(self):
        channel = make_channel()
        self.assertIsNone(run(channel.handle_event(event("just thinking out loud"))))
        self.assertEqual(channel.stats.ignored, 1)

    def test_admin_mention_is_answered(self):
        channel = make_channel()
        text = run(channel.handle_event(event("@HermesBot status", author_id=ADMIN_ID, username="alice_admin", is_bot=False)))
        self.assertIn("received: @HermesBot status", text)

    def test_unmentioned_admin_message_is_ignored_by_default(self):
        channel = make_channel()
        reply = run(channel.handle_event(event("status", author_id=ADMIN_ID, username="alice_admin", is_bot=False)))
        self.assertIsNone(reply)
        self.assertEqual(channel.stats.ignored, 1)

    def test_message_mentioning_other_peer_is_ignored(self):
        channel = make_channel()
        reply = run(channel.handle_event(event("@CodexBot status", author_id=ADMIN_ID, username="alice_admin", is_bot=False)))
        self.assertIsNone(reply)
        self.assertEqual(channel.stats.ignored, 1)

    def test_reply_to_this_bot_is_answered(self):
        channel = make_channel()
        self.assertIsNotNone(run(channel.handle_event(
            event("and then?", replies_to_bot=True))))

    def test_reply_to_this_bot_keeps_the_telegram_thread(self):
        channel = make_channel()
        message_id = 42

        run(channel.handle_event(event(
            "and then?", replies_to_bot=True, message_id=message_id)))

        self.assertEqual(channel._test_sender.sent[-1]["reply_to"], message_id)

    def test_messages_from_other_chats_are_ignored(self):
        channel = make_channel()
        self.assertIsNone(run(channel.handle_event(event("@HermesBot hello", chat_id=-999))))
        self.assertEqual(channel.stats.ignored, 1)

    def test_admin_dm_is_allowed_outside_the_home_channel(self):
        channel = make_channel()
        self.assertIsNotNone(run(channel.handle_event(
            event("private note", chat_id=ADMIN_ID, author_id=ADMIN_ID, username="alice_admin", is_bot=False))))

    def test_the_admin_in_another_group_is_not_a_dm(self):
        """Live test: a bot whose home channel was another group answered the admin's plain
        "Test" in a new group, unmentioned, because every non-home chat counted as a DM."""
        channel = make_channel()
        for text in ("Test", "@HermesBot hello"):
            with self.subTest(text=text):
                self.assertIsNone(run(channel.handle_event(event(
                    text, chat_id=-1009876543210, author_id=ADMIN_ID,
                    username="alice_admin", is_bot=False))))
        self.assertEqual(channel.should_respond(event(
            "Test", chat_id=-1009876543210, author_id=ADMIN_ID, username="alice_admin",
            is_bot=False)), (False, "not addressed to this agent"))

    def test_unknown_bot_addressing_us_is_ignored_by_default(self):
        channel = make_channel()
        reply = run(channel.handle_event(event("@HermesBot hello", username="stranger_bot", author_id=777)))
        self.assertIsNone(reply)
        self.assertEqual(channel.stats.handled, 0)

    def test_only_members_of_the_swarm_start_turns(self):
        channel = make_channel(make_config(agent={"maintainer_username": "@Alice",
                                                  "allowed_users": ["@Ana", 555]}))

        def answered(username, author_id, is_bot=False):
            return channel.should_respond(event("@HermesBot hi", username=username,
                                                author_id=author_id, is_bot=is_bot))[0]
        self.assertTrue(answered("anyone", ADMIN_ID))          # the admin, by id
        # Review P1-8: a handle grants nothing; it can change owner.
        self.assertFalse(answered("alice", 1))                 # the maintainer handle
        self.assertFalse(answered("ana", 2))                   # an @handle in allowed_users
        self.assertTrue(answered(None, 555))                   # allowed by id
        self.assertTrue(answered("CodexBot", 202, is_bot=True))   # a peer bot
        self.assertFalse(answered("mallory", 3))
        self.assertFalse(answered("rogue_bot", 4, is_bot=True))
        self.assertIn("limits.strangers", channel.should_respond(
            event("@HermesBot hi", username="mallory", author_id=3, is_bot=False))[1])

    def test_a_stranger_is_warned_about_once(self):
        channel = make_channel()
        with self.assertLogs("swarmly.telegram", level="WARNING") as logs:
            for message_id in (1, 2):
                run(channel.handle_event(event("@HermesBot hi", username="mallory",
                                               author_id=3, is_bot=False,
                                               message_id=message_id)))
        self.assertEqual(sum("ignores @mallory" in line for line in logs.output), 1)

    def test_a_stranger_does_not_reset_the_peer_turn_cap(self):
        channel = make_channel()
        key = (GROUP, 7)
        channel._peer_turns[key] = 3
        channel._paused_topics.add(key)
        run(channel.handle_event(event("anyone there?", username="mallory", author_id=3,
                                       is_bot=False, thread_id=7)))
        self.assertEqual(channel._peer_turns.get(key), 3)
        self.assertIn(key, channel._paused_topics)

    def test_a_frozen_bot_ignores_a_strangers_status(self):
        channel = make_channel()
        channel._frozen = True
        run(channel.handle_event(event("/status@HermesBot", username="mallory", author_id=3,
                                       is_bot=False)))
        self.assertEqual(channel._test_sender.sent, [])

    def test_permissions_that_read_as_off_are_refused(self):
        for value in (False, 0, None):
            with self.subTest(value=value):
                if value is None:
                    config = make_config(adapter={"type": "claude", "permissions": None})
                    self.assertEqual(config.adapter.permissions, "full")
                    continue
                with self.assertRaises(ConfigError):
                    make_config(adapter={"type": "claude", "permissions": value})

    def test_a_placeholder_admin_id_is_a_problem(self):
        config = make_config(agent={"admin_user_id": 0})
        self.assertTrue(any("admin_user_id" in p for p in config.validate()))

    def test_limits_strangers_is_validated(self):
        with self.assertRaises(ConfigError):
            make_config(limits={"strangers": "maybe"})

    def test_unknown_bot_addressing_us_is_answered_when_strangers_are(self):
        channel = make_channel(make_config(limits={"strangers": "answer"}))
        reply = run(channel.handle_event(event("@HermesBot hello", username="stranger_bot", author_id=777)))
        self.assertIsNotNone(reply)
        self.assertEqual(channel.stats.handled, 1)

    def test_peer_bot_mentioning_by_username_is_answered(self):
        channel = make_channel()
        reply = run(channel.handle_event(event("[Codex] @HermesBot: Can you review PR #158?", username="CodexBot", is_bot=True)))
        self.assertIsNotNone(reply)
        self.assertEqual(channel.stats.handled, 1)

    def test_peer_bot_mentioning_other_bot_is_ignored(self):
        channel = make_channel()
        reply = run(channel.handle_event(event("[Codex] @AntigravityBot: Can you review PR #158?", username="CodexBot", is_bot=True)))
        self.assertIsNone(reply)
        self.assertEqual(channel.stats.ignored, 1)

    def test_casual_mention_of_alias_in_prose_is_ignored(self):
        channel = make_channel()
        # In prose without @ or leading address, Hermes should NOT treat this as a mention
        reply = run(channel.handle_event(event("[Codex] @AntigravityBot: I discussed with Hermes and we agreed.", username="CodexBot", is_bot=True)))
        self.assertIsNone(reply)
        self.assertEqual(channel.stats.ignored, 1)

    def test_respond_without_mention_switch(self):
        # a human in the group can be answered without being addressed; bots still must
        # address this agent (that restriction is what keeps agent-to-agent chatter bounded)
        config = make_config(limits={"respond_without_mention": True, "min_seconds_between_messages": 0},
                             agent={"allowed_users": [777]})
        channel = make_channel(config)
        reply = run(channel.handle_event(event("no mention here", author_id=777, username="alice_admin",
                                               is_bot=False)))
        self.assertIsNotNone(reply)

    def test_respond_without_mention_does_not_apply_to_other_bots(self):
        config = make_config(limits={"respond_without_mention": True, "min_seconds_between_messages": 0})
        channel = make_channel(config)
        self.assertIsNone(run(channel.handle_event(event("no mention, from a peer bot"))))


class TestHandoffAddressing(unittest.TestCase):
    """Issue #27: every @handle in a bot's hand-off counts as addressed, and a mention that is
    still dropped is reported to the sender with the handle and the reason."""

    LIST_ONE_LINE = ("[Claude] Round 2: @CodexBot review #24, @AntigravityBot QA #24, "
                     "@HermesBot develop #27")
    LIST_LINES = ("[Claude] Round 2 hand-off:\n- @CodexBot review #24\n"
                  "- @AntigravityBot QA #24\n- @HermesBot develop #27")

    def test_a_list_of_handles_on_one_line_addresses_every_bot_in_it(self):
        channel = make_channel()
        decision, reason = channel.should_respond(event(self.LIST_ONE_LINE))
        self.assertTrue(decision, reason)

    def test_a_list_of_handles_over_several_lines_addresses_every_bot_in_it(self):
        channel = make_channel()
        decision, reason = channel.should_respond(event(self.LIST_LINES))
        self.assertTrue(decision, reason)

    def test_each_bot_named_in_a_list_runs_its_own_turn(self):
        channel = make_channel()
        reply = run(channel.handle_event(event(self.LIST_LINES, message_id=7)))
        self.assertIsNotNone(reply)
        self.assertEqual(channel.stats.handled, 1)

    def test_a_dropped_mention_tells_the_sender_the_handle_and_the_reason(self):
        channel = make_channel()
        reply = run(channel.handle_event(
            event("[Codex] @CodexBot please hand @HermesBot the review", message_id=9)))
        self.assertIsNone(reply)                       # still not this bot's work
        notices = [entry["text"] for entry in channel._test_sender.sent]
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("@HermesBot", notices[0])        # which handle was dropped
        self.assertIn("hand this bot the work", notices[0])   # and why
        self.assertTrue(notices[0].startswith("⚠️"), notices[0])

    def test_a_machine_notice_is_dropped_without_a_notice_back(self):
        channel = make_channel()
        run(channel.handle_event(event("⚠️ [Codex] paused this topic for @HermesBot", message_id=3)))
        self.assertEqual(channel._test_sender.sent, [])   # no turn, and no notice ping-pong

    def test_a_terminal_sign_off_naming_this_bot_gets_no_notice(self):
        channel = make_channel()
        run(channel.handle_event(event("@HermesBot closing here", message_id=4)))
        self.assertEqual(channel._test_sender.sent, [])

    def test_one_drop_notice_per_window(self):
        channel = make_channel()
        text = "[Codex] @CodexBot please hand @HermesBot the review"
        run(channel.handle_event(event(text, message_id=11)))
        run(channel.handle_event(event(text, message_id=12)))
        self.assertEqual(len(channel._test_sender.sent), 1)

    def test_each_sender_gets_its_own_drop_notice(self):
        """#27 review: the window dedupes per sender, so a second peer's dropped hand-off is
        reported on its own message instead of being swallowed by the first peer's notice."""
        channel = make_channel()
        run(channel.handle_event(event(
            "[Codex] @CodexBot please hand @HermesBot the review",
            username="CodexBot", author_id=202, message_id=31)))
        run(channel.handle_event(event(
            "[Antigravity] @AntigravityBot please hand @HermesBot the QA",
            username="AntigravityBot", author_id=303, message_id=32)))
        notices = [e for e in channel._test_sender.sent if e["text"].startswith("⚠️")]
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual([n["reply_to"] for n in notices], [31, 32])
        for notice in notices:
            self.assertIn("@HermesBot", notice["text"])        # the handle that was dropped
            self.assertIn("hand this bot the work", notice["text"])

    def test_a_cooldown_drop_notice_names_the_dropped_handle(self):
        """#27 review: a mention lost to the bot-to-bot cooldown still says which handle went
        unanswered, so the sender's human does not read the silence as work taken."""
        channel = make_channel(make_config(limits={"min_seconds_between_agent_turns": 60.0,
                                                   "min_seconds_between_messages": 0}))
        run(channel.handle_event(event("@HermesBot run the suite", message_id=20)))
        self.assertIsNone(run(channel.handle_event(event("@HermesBot run it again",
                                                          message_id=21))))
        notices = [e for e in channel._test_sender.sent if e["text"].startswith("⚠️")]
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("@HermesBot", notices[0]["text"])        # which handle was dropped
        self.assertIn("cooldown", notices[0]["text"])          # and why
        self.assertEqual(notices[0]["reply_to"], 21)

    def test_each_sender_gets_its_own_cooldown_notice(self):
        """#27 review: the cooldown window is per sender, so a second peer dropped inside it
        hears about its own message instead of being swallowed by the first peer's notice."""
        channel = make_channel(make_config(limits={"min_seconds_between_agent_turns": 60.0,
                                                   "min_seconds_between_messages": 0}))
        run(channel.handle_event(event("@HermesBot run the suite", message_id=40)))
        run(channel.handle_event(event("@HermesBot run it again", message_id=41)))
        run(channel.handle_event(event("@HermesBot and the QA pass", username="AntigravityBot",
                                       author_id=303, message_id=42)))
        notices = [e for e in channel._test_sender.sent if e["text"].startswith("⚠️")]
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual([n["reply_to"] for n in notices], [41, 42])
        self.assertIn("CodexBot", notices[0]["text"])
        self.assertIn("AntigravityBot", notices[1]["text"])


class TestAppendedHandleRouting(unittest.TestCase):
    """Issue #28: the handle a transport appends for delivery must not pick the recipient."""

    def test_a_trailing_appended_handle_does_not_route_to_a_bot_named_in_prose(self):
        channel = make_channel()
        # A peer report that mentions this bot in passing, so its transport appended the
        # handle: the body's addressee is Antigravity, not Hermes.
        text = ("[Antigravity] @AntigravityBot QA passed on #44. Hermes wrote the fix. "
                "@HermesBot")
        decision, reason = channel.should_respond(event(text))
        self.assertFalse(decision, reason)

    def test_an_appended_handle_still_routes_when_the_body_addresses_nobody(self):
        channel = make_channel()
        decision, reason = channel.should_respond(
            event("[Codex] shipped, needs a look when you can. @HermesBot"))
        self.assertTrue(decision, reason)

    def test_a_real_handle_in_the_body_still_routes(self):
        channel = make_channel()
        decision, reason = channel.should_respond(
            event("[Codex] @HermesBot take #30 please; @CodexBot reviews it. @HermesBot"))
        self.assertTrue(decision, reason)


# --------------------------------------------------------------------------------------
# 4. Loop guard and pacing (Telegram requires both for bot-to-bot mode)
# --------------------------------------------------------------------------------------
class TestLoopGuard(unittest.TestCase):

    def test_addressed_messages_are_never_cut_off(self):
        config = make_config(limits={"min_seconds_between_messages": 0})
        channel = make_channel(config)

        for i in range(10):
            reply = run(channel.handle_event(event(f"@HermesBot turn {i}", message_id=i)))
            self.assertIsNotNone(reply, f"turn {i} must always be answered when addressed")

    def test_codex_exact_username_routes(self):
        from unittest.mock import AsyncMock

        config = make_config(
            agent={"name": "example_codex_bot", "alias": "Codex"},
            adapter={"type": "codex_thread", "conversation": "current"},
            limits={"min_seconds_between_messages": 0},
        )
        adapter = CodexThreadAdapter("Codex", config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(text="queued"))
        channel = make_channel(config, adapter=adapter)

        reply1 = run(channel.handle_event(event("Codex: first", username="AntigravityBot", message_id=1)))
        self.assertIsNotNone(reply1)
        reply2 = run(channel.handle_event(event("Codex: second", username="AntigravityBot", message_id=2)))
        self.assertIsNotNone(reply2)

        reply3 = run(channel.handle_event(event(
            "[Antigravity] @example_codex_bot: please process this",
            username="AntigravityBot", message_id=4,
        )))
        self.assertIsNotNone(reply3)
        self.assertEqual(adapter.respond.await_count, 3)

    def test_codex_exact_username_routes_receipts_that_contain_ack_words(self):
        from unittest.mock import AsyncMock

        config = make_config(
            agent={"name": "example_codex_bot", "alias": "Codex"},
            adapter={"type": "codex_thread", "conversation": "current"},
        )
        adapter = CodexThreadAdapter("Codex", config.adapter)
        adapter.respond = AsyncMock(return_value=None)
        channel = make_channel(config, adapter=adapter)

        message = (
            "[Antigravity] @example_codex_bot @example_hermes_bot: Strategic RFC committed at "
            "docs/research/proposals/strategic_roadmap_rfc.md. Hermes feedback received on "
            "Issue #157. @example_codex_bot please review and post your assessment."
        )
        self.assertIsNone(run(channel.handle_event(
            event(message, username="AntigravityBot", message_id=5)
        )))
        adapter.respond.assert_awaited_once()

    def test_codex_thread_does_not_open_ticket_for_terminal_closure(self):
        from unittest.mock import AsyncMock

        config = make_config(
            agent={"name": "example_codex_bot", "alias": "Codex"},
            adapter={"type": "codex_thread", "conversation": "current"},
            limits={"min_seconds_between_messages": 0},
        )
        adapter = CodexThreadAdapter("Codex", config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(text="must not run"))
        with TemporaryDirectory() as tmp:
            config.source_path = Path(tmp) / "agent.local.yaml"
            channel = make_channel(config, adapter=adapter)
            closure = "@example_codex_bot: closing here; idle until maintainer sign-off."
            self.assertIsNone(run(channel.handle_event(event(
                closure, username="AntigravityBot", message_id=6))))
            adapter.respond.assert_not_awaited()
            self.assertEqual(channel.replies.summary(), {})

    def test_codex_thread_queue_success_posts_delivery_receipt(self):
        settings = CodexThreadAdapter.default_settings(cwd="/repo", thread="current")
        adapter = CodexThreadAdapter("Codex", settings)
        completed = subprocess.CompletedProcess(
            args=adapter.build_argv("ignored"), returncode=0, stdout="queued\n", stderr=""
        )
        config = make_config(
            agent={"name": "example_codex_bot", "alias": "Codex"},
            adapter={"type": "codex_thread", "conversation": "current"},
            limits={"min_seconds_between_messages": 0},
            peers={"Hermes": "example_hermes_bot"},
        )
        channel = make_channel(config, adapter=adapter)

        with patch("swarmly.telegram.adapters.codex.subprocess.run", return_value=completed):
            reply = run(channel.handle_event(event(
                "@example_codex_bot Please comment on #17",
                username="example_hermes_bot",
                message_id=17,
            )))

        self.assertTrue(
            reply.startswith("[A2A v1.1] RECEIPT to=Hermes intent=QUEUED"), reply)
        self.assertIn("✅ [Codex] queued in active task", reply)
        self.assertEqual(channel._test_sender.last, reply)

    def test_sends_are_paced(self):
        config = make_config(limits={"min_seconds_between_messages": 3.0})
        channel = make_channel(config)
        run(channel.send("first"))
        run(channel.send("second"))
        self.assertTrue(channel._test_slept, "the second send must wait")
        self.assertAlmostEqual(channel._test_slept[0], 3.0, places=2)

    def test_send_can_return_accepted_message_id_without_changing_payload_api(self):
        channel = make_channel()

        async def sender(payload, reply_to_message_id=None):
            return type("AcceptedMessage", (), {"message_id": 42})()

        channel._sender = sender
        self.assertEqual(run(channel.send("hello")), "hello")
        self.assertEqual(run(channel.send("hello", return_message_id=True)), 42)

    def test_peer_bot_status_notices_are_ignored(self):
        channel = make_channel()
        for notice in ("⚠️ No reply: timeout", "↪ Redirected current run", "❌ Error occurred", "🟢 Bot online"):
            self.assertIsNone(run(channel.handle_event(event(notice, replies_to_bot=True, is_bot=True))))
            self.assertEqual(channel.stats.handled, 0)

    def test_peer_bot_terminal_acknowledgments_are_ignored(self):
        channel = make_channel()
        terminal_acks = [
            "[Codex] @Hermes: Acknowledged. I'll remain idle pending maintainer sign-off on #157/#148.",
            "@HermesBot: Acknowledged, standing down.",
            "@HermesBot: Aligned on both sides — closing here; Hermes idle until maintainer sign-off.",
            "@HermesBot: Nothing further needed from my side. Standing down.",
            "@HermesBot: Roger that, no further action needed.",
            "@HermesBot: Acknowledged; please stand down.",
            "@HermesBot: Acknowledged; kindly remain idle.",
            "@HermesBot: Closing here; please stay idle pending sign-off.",
            "@HermesBot: Acknowledged; please stand down and take no further action.",
            "@HermesBot: Acknowledged; please stand down and stop.",
            "@HermesBot: Acknowledged; please stand down also stop.",
            "@HermesBot: Acknowledged; please stand down, also take no further action.",
            "@HermesBot: Acknowledged; please stand down, but no action required.",
            "@HermesBot: Acknowledged; please stand down; no action required.",
            "@HermesBot: Closing here; no action required, please stay idle.",
            "@HermesBot: Acknowledged; please stand down; no further action required.",
            "@HermesBot: Closing here; no further action required, please stay idle.",
            "@HermesBot: Acknowledged; please stand down; there is no further action required.",
            "@HermesBot: Acknowledged; please stand down; no additional action required.",
            "@HermesBot: Acknowledged; please stand down; no other action required.",
            "@HermesBot: Acknowledged; please stand down; without action required.",
        ]
        for ack_msg in terminal_acks:
            reply = run(channel.handle_event(event(ack_msg, is_bot=True)))
            self.assertIsNone(reply, f"Terminal acknowledgment '{ack_msg}' should be ignored")
        self.assertEqual(channel.stats.handled, 0)
        self.assertEqual(channel.stats.ignored, len(terminal_acks))

    def test_peer_bot_actionable_request_is_answered(self):
        channel = make_channel()
        msg = "@HermesBot: Can you please verify PR #148?"
        reply = run(channel.handle_event(event(msg, is_bot=True)))
        self.assertIsNotNone(reply, "Actionable peer bot request with question must be answered")

    def test_mixed_status_and_imperative_request_is_not_suppressed(self):
        channel = make_channel()
        messages = (
            "@HermesBot: Acknowledged; please see issue #14.",
            "@HermesBot: Closing here; update the PR description.",
            "@HermesBot: Received; need you to verify the migration.",
            "@HermesBot: Acknowledged; please stand down and update PR #44.",
            "@HermesBot: Closing here; kindly remain idle, but please see issue #14.",
            "@HermesBot: Acknowledged; please stay idle until done, then verify the migration.",
            "@HermesBot: Acknowledged; please stand down and stop, but update PR #44.",
            "@HermesBot: Acknowledged; please stand down and take no further action, but please verify the migration.",
            "@HermesBot: Acknowledged; please stand down and stop, but need you to verify the migration.",
            "@HermesBot: Acknowledged; please stand down and stop, but you should update PR #44.",
            "@HermesBot: Acknowledged; please stand down and stop, but must review issue #14.",
            "@HermesBot: Acknowledged; please stand down and stop, but want you to verify the migration.",
            "@HermesBot: Acknowledged; please stand down and stop, but action required: update PR #44.",
            "@HermesBot: Acknowledged; please stand down and stop; update PR #44.",
            "@HermesBot: Please update PR #44; acknowledged; please stand down.",
            "@HermesBot: Need you to review issue #14; acknowledged; kindly remain idle.",
            "@HermesBot: Action required: update PR #44; closing here; please stand down.",
            "@HermesBot: Acknowledged; please stand down, but update PR #44 and stop.",
            "@HermesBot: Acknowledged; please stand down, then please see issue #14 and take no further action.",
        )
        for message_id, message in enumerate(messages, start=20):
            self.assertIsNotNone(
                run(channel.handle_event(event(message, message_id=message_id))),
                message,
            )

    def test_agent_turn_cooldown(self):
        config = make_config(limits={"min_seconds_between_agent_turns": 10.0, "min_seconds_between_messages": 0})
        channel = make_channel(config)
        # Turn 1
        reply1 = run(channel.handle_event(event("@HermesBot hello 1", message_id=1)))
        self.assertIsNotNone(reply1)
        # Turn 2 immediately after (same clock time) should be rejected by cooldown
        reply2 = run(channel.handle_event(event("@HermesBot hello 2", message_id=2)))
        self.assertIsNone(reply2)
        # Advance clock by 11 seconds
        channel._test_clock["t"] += 11.0
        reply3 = run(channel.handle_event(event("@HermesBot hello 3", message_id=3)))
        self.assertIsNotNone(reply3)

    def test_universal_rate_limit_1_message_per_minute(self):
        # The 60s bot-to-bot cooldown and the send pacing defaults (#145: 3s apart, 18/min)
        config = AgentConfig.from_dict({
            "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                      "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
            "peers": {"Codex": "CodexBot"},
            "adapter": {"type": "echo"},
        })
        self.assertEqual(config.limits.min_seconds_between_messages, 3.0)
        self.assertEqual(config.limits.min_seconds_between_agent_turns, 60.0)
        self.assertEqual(config.limits.max_messages_per_minute, 18)

        channel = make_channel(config)
        # Turn 1
        reply1 = run(channel.handle_event(event("@HermesBot msg 1", message_id=1)))
        self.assertIsNotNone(reply1)
        # Immediate turn 2 should be rejected by 60s cooldown
        channel._test_clock["t"] += 30.0  # only 30s elapsed
        reply2 = run(channel.handle_event(event("@HermesBot msg 2", message_id=2)))
        self.assertIsNone(reply2)
        # After 60s total, turn 3 is accepted
        channel._test_clock["t"] += 31.0  # 61s total elapsed
        reply3 = run(channel.handle_event(event("@HermesBot msg 3", message_id=3)))
        self.assertIsNotNone(reply3)


# --------------------------------------------------------------------------------------
# 5. Outbound formatting
# --------------------------------------------------------------------------------------
class TestOutboundFormatting(unittest.TestCase):

    def test_html_is_escaped(self):
        channel = make_channel()
        payload = run(channel.send("<b>bold</b> & <script>alert(1)</script>"))
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", payload)
        self.assertNotIn("<script>", payload)

    def test_long_messages_are_clamped_to_telegrams_limit(self):
        channel = make_channel(make_config(limits={"max_reply_parts": 1}))
        payload = run(channel.send("x" * 9000))
        self.assertLessEqual(len(payload), 4096)
        self.assertIn("truncated", payload)

    def test_stats_are_counted(self):
        channel = make_channel()
        run(channel.handle_event(event("@HermesBot hello")))
        self.assertEqual(channel.stats.handled, 1)
        self.assertGreaterEqual(channel.stats.sent, 1)


# --------------------------------------------------------------------------------------
# 6. aiogram-specific paths
# --------------------------------------------------------------------------------------
@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed")
class TestAiogramWiring(unittest.TestCase):

    class FakeUser:
        def __init__(self, id, username=None, is_bot=True):
            self.id, self.username, self.is_bot = id, username, is_bot

    class FakeChat:
        def __init__(self, id):
            self.id = id

    class FakeMessage:
        def __init__(self, text, user, chat_id=GROUP, message_id=1, reply_user=None):
            self.text = text
            self.from_user = user
            self.chat = TestAiogramWiring.FakeChat(chat_id)
            self.message_id = message_id
            self.reply_to_message = (TestAiogramWiring.FakeMessage("prev", reply_user, chat_id)
                                     if reply_user else None)

    def test_aiogram_message_becomes_a_channel_event(self):
        channel = make_channel()

        class FakeBot:
            id = 555

        channel.bot = FakeBot()
        message = self.FakeMessage("@HermesBot status", self.FakeUser(202, "CodexBot"),
                                   reply_user=self.FakeUser(555, "HermesBot"))
        ev = channel.event_from_aiogram(message)
        self.assertEqual(ev.text, "@HermesBot status")
        self.assertEqual(ev.chat_id, GROUP)
        self.assertEqual(ev.author_username, "CodexBot")
        self.assertTrue(ev.replies_to_bot)
        self.assertTrue(ev.author_is_bot)

    def test_own_message_is_not_answered(self):
        channel = make_channel()

        class FakeBot:
            id = 555

        channel.bot = FakeBot()
        ev = self.FakeMessage("I am the bot", self.FakeUser(555, "HermesBot"))
        self.assertIsNone(run(channel.handle_event(channel.event_from_aiogram(ev))))


# --------------------------------------------------------------------------------------
# 7. The `discover` command — how an operator finds the group id
# --------------------------------------------------------------------------------------
class TestDiscoverCommand(unittest.TestCase):

    class FakeResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _run_discover(self, payload, config=None):
        import contextlib
        import io
        import urllib.request

        from swarmly.telegram.cli import _discover

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda url, timeout=None: self.FakeResponse(payload)
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = asyncio.run(_discover(config or make_config()))
        finally:
            urllib.request.urlopen = original
        return code, buffer.getvalue()

    def test_group_ids_are_listed_with_the_one_to_copy(self):
        payload = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": GROUP, "type": "supergroup", "title": "A2A agents"},
                                          "from": {"username": "hermes_bot"}}},
            {"update_id": 2, "message": {"chat": {"id": ADMIN_ID, "type": "private",
                                                  "username": "alice_admin"}}},
        ]}
        code, out = self._run_discover(payload)
        self.assertEqual(code, 0)
        self.assertIn(str(GROUP), out)
        self.assertIn("A2A agents", out)
        self.assertIn("home_channel_id", out)

    def test_no_chats_yet_explains_the_next_step(self):
        code, out = self._run_discover({"ok": True, "result": []})
        self.assertEqual(code, 1)
        self.assertIn("Add the bot to the group", out)

    def test_missing_token_is_reported_before_any_request(self):
        config = make_config(agent={"bot_token": ""})
        import contextlib
        import io
        from swarmly.telegram.cli import _discover
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = asyncio.run(_discover(config))
        self.assertEqual(code, 2)
        self.assertIn("no bot token", err.getvalue())

    def test_shipped_template_parses_and_reports_what_is_missing(self):
        """The template is what the operator copies: it must stay valid YAML and loadable."""
        template = Path(__file__).resolve().parent.parent / "config" / "telegram.template.yaml"
        if not template.exists() or not HAS_YAML:
            self.skipTest("template or PyYAML unavailable")
        config = AgentConfig.load(template)
        problems = config.validate()
        # an unfilled template must fail loudly rather than silently start a broken agent
        self.assertTrue(any("home_channel_id" in p for p in problems), problems)
        self.assertTrue(any("admin_user_id" in p for p in problems), problems)
        self.assertTrue(any("<FILL IN>" in config.identity.alias or "bot token" in p for p in problems + [config.identity.alias]))

    def test_telegram_error_is_surfaced_not_swallowed(self):
        code, _ = self._run_discover({"ok": False, "description": "Unauthorized"})
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------------------
# 8. Commit authorship — an agent commits as itself, not as the human
# --------------------------------------------------------------------------------------


class TestGitIdentity(unittest.TestCase):
    def test_default_identity_is_the_alias(self):
        name, email = make_config().identity.git_identity
        self.assertEqual(name, "Hermes")
        self.assertEqual(email, "hermes@a2a.local")

    def test_identity_can_be_overridden_in_the_config_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "agent:\n  name: HermesBot\n  alias: Hermes\n  bot_token: '1:2'\n"
                "  home_channel_id: -100\n  admin_user_id: 1\n"
                "  git_name: Hermes Agent\n"
                "  git_email: 1234567+hermesbot@users.noreply.github.com\n"
                "adapter:\n  type: echo\n")
            config = AgentConfig.load(path)
        self.assertEqual(config.identity.git_identity,
                         ("Hermes Agent", "1234567+hermesbot@users.noreply.github.com"))

    def test_runtime_environment_authors_commits_as_the_agent(self):
        config = make_config(agent={"git_email": "hermes@users.noreply.github.com"},
                             adapter={"type": "codex"})
        adapter = build_adapter(config)
        self.assertIsInstance(adapter, SubprocessAdapter)
        env = adapter.environment(base={})
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Hermes")
        self.assertEqual(env["GIT_COMMITTER_NAME"], "Hermes")
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], "hermes@users.noreply.github.com")
        self.assertEqual(env["GIT_COMMITTER_EMAIL"], "hermes@users.noreply.github.com")
        self.assertEqual(env["A2A_AGENT_ALIAS"], "Hermes")

    def test_a_custom_command_adapter_carries_the_identity_too(self):
        config = make_config(adapter={"type": "custom", "command": "mytool {prompt}"})
        adapter = build_adapter(config)
        self.assertIsInstance(adapter, SubprocessAdapter)
        self.assertEqual(adapter.environment(base={})["GIT_AUTHOR_NAME"], "Hermes")

    def test_every_adapter_type_carries_the_identity(self):
        for kind in ("echo", "hermes", "codex", "antigravity"):
            adapter = build_adapter(make_config(adapter={"type": kind}))
            self.assertEqual(adapter.git_identity, ("Hermes", "hermes@a2a.local"), kind)


# --------------------------------------------------------------------------------------
# 9. v1.1 command wiring parity — unaddressed commands must stay ignored
# --------------------------------------------------------------------------------------
class TestCommandStrictIgnoreParity(unittest.TestCase):

    def test_unaddressed_commands_are_ignored(self):
        channel = make_channel()
        self.assertIsNone(run(channel.handle_event(event("/ping"))))
        self.assertIsNone(run(channel.handle_event(event("/ping@CodexBot", message_id=2))))
        self.assertEqual(channel.stats.handled, 0)
        self.assertEqual(channel.stats.ignored, 2)

    def test_a_command_addressed_to_this_bot_is_answered_by_the_transport(self):
        channel = make_channel()
        reply = run(channel.handle_event(event("/ping@HermesBot")))
        self.assertTrue(reply.startswith("PONG Hermes"))
        self.assertEqual(channel.stats.handled, 1)


# --------------------------------------------------------------------------------------
# 10. issue #6 item 3 — the mention-entity path must never exceed max_message_chars
# --------------------------------------------------------------------------------------
class TestEntityPathClamp(unittest.TestCase):

    @staticmethod
    def _covered(rendered, entity):
        blob = rendered.encode("utf-16-le")
        return blob[entity["offset"] * 2:(entity["offset"] + entity["length"]) * 2].decode("utf-16-le")

    def test_long_entity_message_is_clamped_and_keeps_its_addressing(self):
        from swarmly.telegram.wire import utf16_length
        channel = make_channel()
        limit = channel.config.limits.max_message_chars
        rendered, entities = channel.addressed_payload("Codex, " + "x" * (limit + 2000))
        self.assertLessEqual(utf16_length(rendered), limit)
        self.assertIn("… [message truncated]", rendered)
        self.assertIn("@CodexBot", [self._covered(rendered, e) for e in entities])

    def test_clamped_entities_never_cover_characters_that_were_cut(self):
        from swarmly.telegram.wire import utf16_length
        channel = make_channel()
        limit = channel.config.limits.max_message_chars
        rendered, entities = channel.addressed_payload("Codex, Antigravity, " + "y" * (limit + 2000))
        width = utf16_length(rendered)
        self.assertLessEqual(width, limit)
        for entity in entities:
            self.assertLessEqual(entity["offset"] + entity["length"], width)
            if entity["type"] != "bold":                  # the bold [alias] banner
                self.assertTrue(self._covered(rendered, entity).startswith("@"))

    def test_clamp_is_utf16_safe_with_non_bmp_characters(self):
        from swarmly.telegram.wire import utf16_length
        channel = make_channel()
        limit = channel.config.limits.max_message_chars
        rendered, entities = channel.addressed_payload("Codex, " + "\U0001F642" * limit)
        self.assertLessEqual(utf16_length(rendered), limit)

    def test_short_messages_are_untouched_by_the_clamp(self):
        channel = make_channel()
        rendered, entities = channel.addressed_payload("Codex, status?")
        self.assertNotIn("truncated", rendered)
        self.assertIn("@CodexBot", [self._covered(rendered, e) for e in entities])


# --------------------------------------------------------------------------------------
# Upgrade visibility: the online notice carries the running revision
# --------------------------------------------------------------------------------------
class TestOnlineNoticeRevision(unittest.TestCase):
    """A restart must announce WHICH revision is running; upgrades become visible in-channel."""

    def test_notice_contains_revision(self):
        channel = make_channel()
        notice = channel.online_notice()
        self.assertTrue(notice.startswith("[A2A v1.1] HELLO to=* intent=ONLINE ref=Hermes rev="),
                        notice)
        self.assertIn("🟢 Hermes online ", notice)
        self.assertRegex(notice, r"\(adapter: echo, rev: ([0-9a-f]{7,40}|unknown)\)$")

    def test_local_revision_matches_checkout(self):
        from swarmly.telegram.channel import _local_revision
        root = Path(__file__).resolve().parents[1]
        rev = _local_revision()
        if (root / ".git").exists():
            expected = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, env=git_env()).stdout.strip()
            self.assertEqual(rev, expected)
        else:
            self.assertEqual(rev, "unknown")

    def test_local_revision_full_is_the_whole_sha(self):
        """The receipt needs the FULL sha: gate 3 compares it as a prefix of the
        target, so a short form would compare a prefix against a prefix."""
        from swarmly.telegram.channel import _local_revision_full
        root = Path(__file__).resolve().parents[1]
        rev = _local_revision_full()
        if (root / ".git").exists():
            expected = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, env=git_env()).stdout.strip()
            self.assertEqual(rev, expected)
            self.assertEqual(len(rev), 40, rev)
        else:
            self.assertEqual(rev, "unknown")

    def test_start_emits_the_machine_receipt_before_the_human_notice(self):
        """Issue #70: AgentChannel.start() itself logs the gate-3 receipt.

        The aiogram boundary is stubbed — this asserts what start() logs at that
        moment, not aiogram. The ORDER is asserted rather than assumed, because
        the choice is deliberate: the receipt is a direct logger.info while the
        human notice goes through send(), which can raise under flood control. A
        receipt that only appeared when the network cooperated would fail-closed
        a healthy switch, so the receipt comes first."""
        from swarmly.telegram.channel import _local_revision_full
        order = []

        class Recorder(logging.Handler):
            def emit(self, record):
                if "A2A_ONLINE_NOTICE" in record.getMessage():
                    order.append(("receipt", record.getMessage()))

        async def noop(*args, **kwargs):
            return None

        class FakeBot:
            def __init__(self, token=None, default=None):
                self.token = token

            async def get_me(self):
                return types.SimpleNamespace(username="HermesBot")

            async def send_message(self, **kwargs):
                return types.SimpleNamespace(message_id=1)

            async def close(self):
                return None

        dispatcher = types.SimpleNamespace(
            message=types.SimpleNamespace(register=lambda *a, **k: None),
            start_polling=noop, stop_polling=noop)

        fake_client = types.ModuleType("aiogram.client")
        fake_default = types.ModuleType("aiogram.client.default")
        fake_default.DefaultBotProperties = lambda **kwargs: types.SimpleNamespace(**kwargs)
        fake_client.default = fake_default
        fake_exceptions = types.ModuleType("aiogram.exceptions")
        fake_exceptions.TelegramRetryAfter = type("TelegramRetryAfter", (Exception,), {})
        fake_aiogram = types.ModuleType("aiogram")
        fake_aiogram.Bot = FakeBot
        fake_aiogram.Dispatcher = lambda: dispatcher
        fake_aiogram.client = fake_client
        fake_aiogram.exceptions = fake_exceptions

        channel = make_channel()

        async def sender(payload, reply_to_message_id=None):
            order.append(("send", payload))
            return payload

        channel._sender = sender
        logger = logging.getLogger("swarmly.telegram")
        handler = Recorder()
        previous_level = logger.level

        async def scenario():
            await channel.start()
            for task in (channel._reply_task, channel._ack_task, channel._bus_task):
                if task is not None:
                    task.cancel()

        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            with patch.dict(sys.modules, {
                "aiogram": fake_aiogram,
                "aiogram.client": fake_client,
                "aiogram.client.default": fake_default,
                "aiogram.exceptions": fake_exceptions,
            }):
                run(scenario())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        receipts = [text for kind, text in order if kind == "receipt"]
        self.assertEqual(len(receipts), 1,
                         f"exactly one machine receipt expected; order={order}")
        self.assertEqual(
            receipts[0],
            f"A2A_ONLINE_NOTICE rev={_local_revision_full()} "
            f"alias=Hermes chat={GROUP}", receipts[0])
        if (Path(__file__).resolve().parents[1] / ".git").exists():
            self.assertRegex(receipts[0], r"^A2A_ONLINE_NOTICE rev=[0-9a-f]{40} ")
        kinds = [kind for kind, _ in order]
        self.assertIn("send", kinds, "the human notice must still be sent")
        self.assertLess(kinds.index("receipt"), kinds.index("send"),
                        "the receipt must be logged before the notice send")


# --------------------------------------------------------------------------------------
# Gitbus fallback: poll the durable plane when Telegram delivery fails
# --------------------------------------------------------------------------------------
def bus_message(message_id, sender, recipient, text, intent="QUERY_REQUEST"):
    return A2AMessage(
        protocol="a2a/v1.0", message_id=message_id, timestamp_utc="2026-09-19T00:00:00+00:00",
        sender=SenderInfo(agent_id=sender, model_family="custom", framework="custom"),
        recipient=RecipientInfo(agent_id=recipient),
        intent=MessageIntent(intent), priority=MessagePriority.ROUTINE, signature="",
        payload={"text": text})


@patch("swarmly.telegram.poll._needs_baseline", new=lambda *_args: False)   # later cycles
class TestGitbusFallback(unittest.TestCase):
    """The durable-plane fallback: messages arrive even when Telegram delivery fails."""

    def _channel(self, tmp, messages, poll_minutes=10, pull=None):
        config = make_config(fallback={"gitbus_poll_minutes": poll_minutes, "gitbus_root": tmp})
        channel = make_channel(config)
        calls = {"pull": 0, "read": 0}

        def puller(root):
            calls["pull"] += 1
            if pull is not None:
                pull()

        def reader(root):
            calls["read"] += 1
            return [(m, True, "ok") for m in messages]

        channel._gitbus_pull = puller
        channel._gitbus_read = reader
        return channel, calls

    def test_disabled_poller_never_reads(self):
        with TemporaryDirectory() as tmp:
            channel, calls = self._channel(tmp, [bus_message("m1", "codex", "hermes", "hi")],
                                           poll_minutes=0)
            self.assertEqual(asyncio.run(channel.check_gitbus()), [])
            self.assertEqual(calls["read"], 0)

    def test_delivers_only_addressed_unseen_messages(self):
        with TemporaryDirectory() as tmp:
            messages = [
                bus_message("m1", "codex", "hermes", "first"),
                bus_message("m2", "codex", "codex", "not mine"),
                bus_message("m3", "codex", "*", "broadcast"),
                bus_message("m4", "hermes", "hermes", "own echo"),
            ]
            channel, calls = self._channel(tmp, messages)
            delivered = asyncio.run(channel.check_gitbus())
            self.assertEqual(delivered, ["m1"])
            texts = [row["text"] for row in channel._test_sender.sent]
            self.assertTrue(any("received: first" in t for t in texts))
            self.assertFalse(any("not mine" in t or "broadcast" in t for t in texts))
            # Ledger persisted; a second pass delivers nothing new.
            self.assertEqual(asyncio.run(channel.check_gitbus()), [])
            seen_path = Path(tmp) / ".agents" / "state" / "seen-hermes.json"
            self.assertTrue(seen_path.is_file())
            self.assertIn("m1", seen_path.read_text(encoding="utf-8"))

    def test_pull_failure_is_tolerated(self):
        with TemporaryDirectory() as tmp:
            def boom():
                raise RuntimeError("no remote")
            channel, _calls = self._channel(tmp, [bus_message("m1", "codex", "hermes", "still here")],
                                            pull=boom)
            self.assertEqual(asyncio.run(channel.check_gitbus()), ["m1"])

    def test_telegram_send_failure_does_not_lose_delivery(self):
        with TemporaryDirectory() as tmp:
            channel, _calls = self._channel(tmp, [bus_message("m1", "codex", "hermes", "orphan")])

            async def failing_send(payload, reply_to_message_id=None):
                raise RuntimeError("telegram down")

            channel._sender = failing_send
            delivered = asyncio.run(channel.check_gitbus())
            self.assertEqual(delivered, ["m1"])
            seen_path = Path(tmp) / ".agents" / "state" / "seen-hermes.json"
            self.assertTrue(seen_path.is_file())
            self.assertIn("m1", seen_path.read_text(encoding="utf-8"))

    def test_ledger_persists_every_id(self):
        with TemporaryDirectory() as tmp:
            channel, _calls = self._channel(tmp, [])
            ids = {f"msg-{i:04d}" for i in range(600)}
            channel._save_gitbus_seen(Path(tmp), ids)
            ledger = Path(tmp) / ".agents" / "state" / "seen-hermes.json"
            self.assertEqual(len(json.loads(ledger.read_text(encoding="utf-8"))["seen"]), 600)

    def test_empty_payload_is_skipped_without_bookkeeping(self):
        with TemporaryDirectory() as tmp:
            hollow = bus_message("m1", "codex", "hermes", "")
            hollow.payload = {}
            channel, _calls = self._channel(tmp, [hollow])
            self.assertEqual(asyncio.run(channel.check_gitbus()), [])
            self.assertFalse((Path(tmp) / ".agents" / "state" / "seen-hermes.json").exists())

    def test_corrupt_ledger_is_quarantined_not_stormed(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp) / ".agents" / "state"
            state.mkdir(parents=True)
            (state / "seen-hermes.json").write_text("{corrupt", encoding="utf-8")
            channel, _calls = self._channel(
                tmp, [bus_message("m1", "codex", "hermes", "post-corruption")])
            self.assertEqual(asyncio.run(channel.check_gitbus()), ["m1"])
            self.assertEqual(len(list(state.glob("seen-hermes.json.corrupt-*"))), 1)

    def test_adapter_failure_defers_the_message_to_a_later_poll(self):
        with TemporaryDirectory() as tmp:
            channel, _calls = self._channel(
                tmp, [bus_message("m1", "codex", "hermes", "retry me")])

            async def boom(message):
                raise RuntimeError("adapter exploded")

            channel.adapter.respond = boom
            self.assertEqual(asyncio.run(channel.check_gitbus()), [])
            seen_path = Path(tmp) / ".agents" / "state" / "seen-hermes.json"
            self.assertFalse(seen_path.exists() and "m1" in seen_path.read_text(encoding="utf-8"))

    def test_default_reader_parses_real_frontmatter_round_trip(self):
        from swarmly.telegram.channel import _default_gitbus_read
        with TemporaryDirectory() as tmp:
            inbox = Path(tmp) / ".agents" / "inbox"
            inbox.mkdir(parents=True)
            message = bus_message("msg-20260919T000000Z-codex", "codex", "hermes", "round trip")
            (inbox / "a.md").write_text(message.to_yaml_frontmatter(), encoding="utf-8")
            items = _default_gitbus_read(Path(tmp))
            self.assertEqual(len(items), 1)
            parsed = items[0][0]
            self.assertEqual(parsed.message_id, "msg-20260919T000000Z-codex")
            self.assertEqual(parsed.sender.agent_id, "codex")
            self.assertEqual(parsed.recipient.agent_id, "hermes")
            self.assertEqual(parsed.payload.get("text"), "round trip")

    def test_ledger_prunes_oldest_beyond_cap(self):
        from swarmly.telegram.channel import _GITBUS_SEEN_MAX
        with TemporaryDirectory() as tmp:
            channel, _calls = self._channel(tmp, [])
            ids = {f"msg-{i:06d}": None for i in range(_GITBUS_SEEN_MAX + 100)}
            channel._save_gitbus_seen(Path(tmp), ids)
            ledger = json.loads(
                (Path(tmp) / ".agents" / "state" / "seen-hermes.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["seen"]), _GITBUS_SEEN_MAX)
            self.assertIn(f"msg-{_GITBUS_SEEN_MAX + 99:06d}", ledger["seen"])   # newest retained
            self.assertNotIn("msg-000000", ledger["seen"])                      # oldest pruned

    def test_ledger_reload_keeps_newest_and_prunes(self):
        from swarmly.telegram.channel import _GITBUS_SEEN_MAX
        with TemporaryDirectory() as tmp:
            writer, _calls = self._channel(tmp, [])
            ids = {f"msg-{i:06d}": None for i in range(_GITBUS_SEEN_MAX + 100)}
            writer._save_gitbus_seen(Path(tmp), ids)
            fresh, _calls2 = self._channel(tmp, [])
            seen = fresh._load_gitbus_seen(Path(tmp))
            self.assertEqual(len(seen), _GITBUS_SEEN_MAX)
            self.assertIn(f"msg-{_GITBUS_SEEN_MAX + 99:06d}", seen)
            self.assertNotIn("msg-000000", seen)

    def test_non_dict_root_ledger_is_quarantined(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp) / ".agents" / "state"
            state.mkdir(parents=True)
            # Write a valid JSON array instead of a dict object
            (state / "seen-hermes.json").write_text(json.dumps(["msg-1", "msg-2"]), encoding="utf-8")
            channel, _calls = self._channel(tmp, [])
            seen = channel._load_gitbus_seen(Path(tmp))
            self.assertEqual(seen, {})
            corrupt = list(state.glob("seen-hermes.json.corrupt-*"))
            self.assertEqual(len(corrupt), 1)

    def test_save_gitbus_seen_negative_cap_guarded(self):
        with TemporaryDirectory() as tmp:
            channel, _calls = self._channel(tmp, [])
            ids = {"msg-1": None, "msg-2": None}
            with patch("swarmly.telegram.channel._GITBUS_SEEN_MAX", -5):
                channel._save_gitbus_seen(Path(tmp), ids)
            ledger = json.loads(
                (Path(tmp) / ".agents" / "state" / "seen-hermes.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["seen"]), 1)
            self.assertEqual(ledger["seen"], ["msg-2"])


# --------------------------------------------------------------------------------------
# 11. issue #9 item 3 — an addressed message dropped mid-turn must be visible
# --------------------------------------------------------------------------------------
class _GatedAdapter(EchoAdapter):
    """Echo that holds its turn open until the test releases it.

    The drop only happens while ``_turn_lock`` is held — i.e. exactly while a turn is still
    running — so the defect cannot be reproduced without real contention.
    """

    def __init__(self, alias):
        super().__init__(alias)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def respond(self, message):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return AdapterReply(text=f"[{self.alias}] received: {message.text}")


def gated_channel(config=None):
    # The drop/notice tests need the old semantics; queueing is the default now.
    channel = make_channel(config if config is not None
                           else make_config(limits={"busy_behavior": "drop"}))
    adapter = _GatedAdapter("Hermes")
    channel.adapter = adapter
    return channel, adapter


def drive_busy_drops(channel, adapter, drops, after=None):
    """Hold a turn open, feed ``drops`` at it, then release; returns (results, after(...)).

    Fresh gate events per call: a stale ``entered`` would resolve immediately and let the
    next event through before the turn task had even started.
    """
    adapter.entered = asyncio.Event()
    adapter.release = asyncio.Event()
    results = []

    async def scenario():
        turn = asyncio.create_task(channel.handle_event(
            event("@HermesBot: hold the turn open", message_id=1)))
        await adapter.entered.wait()
        for offset, text in enumerate(drops):
            results.append(await channel.handle_event(
                event(text, message_id=10 + offset, username="CodexBot")))
        extra = await after(channel) if after is not None else None
        adapter.release.set()
        await turn
        return extra

    return results, run(scenario())


class TestBusyDropVisibility(unittest.TestCase):
    """Measured on this host: 9 of 15 addressed messages were dropped in 74 minutes, with no
    counter and no notice — a 60% loss that reads as a healthy transport."""

    ADDRESSED = ("@HermesBot: Can you please verify PR #148?",
                 "@HermesBot: and issue #14 as well",
                 "@HermesBot: also check the migration")

    def _drive(self, channel, adapter, drops, after=None):
        return drive_busy_drops(channel, adapter, drops, after)

    def test_drop_mode_counts_the_message_dropped_mid_turn(self):
        channel, adapter = gated_channel()
        results, _ = self._drive(channel, adapter, self.ADDRESSED[:1])

        self.assertEqual(results, [None], "delivery semantics are unchanged: still dropped")
        self.assertEqual(channel.stats.busy_dropped, 1)
        self.assertEqual(channel.stats.ignored, 1, "the generic counter still moves")
        self.assertEqual(channel.stats.handled, 1, "the in-flight turn is the only handled one")
        self.assertEqual(adapter.calls, 1, "the dropped message never reaches the adapter")

    def test_drop_mode_does_not_log_the_notice(self):
        channel, adapter = gated_channel()
        with self.assertLogs("swarmly.telegram", level="WARNING") as captured:
            self._drive(channel, adapter, self.ADDRESSED[:1])
        self.assertEqual(len([r for r in captured.records if "busy:" in r.getMessage()]), 0,
                         [r.getMessage() for r in captured.records])

    def test_notice_mode_logs_exactly_one_line_per_window(self):
        channel, adapter = gated_channel(make_config(limits={"busy_behavior": "notice"}))

        with self.assertLogs("swarmly.telegram", level="WARNING") as captured:
            self._drive(channel, adapter, self.ADDRESSED)      # t=1000: first drop of the
            self._drive(channel, adapter, self.ADDRESSED)      # window -> notice, rest silent
            channel._test_clock["t"] += 900.0                  # exactly one window later
            self._drive(channel, adapter, self.ADDRESSED[:1])  # t=1900: one more notice

        busy = [r.getMessage() for r in captured.records if "busy:" in r.getMessage()]
        self.assertEqual(len(busy), 2, busy)                  # bounded: never one per drop
        self.assertIn("1 addressed msg(s)", busy[0])          # the drop that opened the window
        # 2 suppressed by the open window + 3 from the second drive + the 1 that opened the next
        self.assertIn("6 addressed msg(s)", busy[1])
        self.assertEqual(channel.stats.busy_dropped, 7)
        self.assertFalse(any("ignoring concurrent event" in r.getMessage()
                             for r in captured.records), captured.records)

    def test_notice_names_the_sender_and_the_reason_on_one_line(self):
        channel, adapter = gated_channel(make_config(limits={"busy_behavior": "notice"}))
        with self.assertLogs("swarmly.telegram", level="WARNING") as captured:
            self._drive(channel, adapter, self.ADDRESSED[:1])

        records = [r for r in captured.records if "busy:" in r.getMessage()]
        self.assertEqual(len(records), 1, [r.getMessage() for r in records])
        line = records[0].getMessage()
        self.assertIn("@CodexBot", line)              # who was dropped
        self.assertIn("turn in progress", line)       # why
        self.assertIn("Hermes", line)                 # whose transport dropped it
        self.assertTrue(line.startswith("⚠️"), line)   # marker-led machine class
        self.assertNotIn("\n", line)                  # one line, not a paragraph
        self.assertLess(len(line), 200)

    def test_notice_mode_still_drops_the_message(self):
        channel, adapter = gated_channel(make_config(limits={"busy_behavior": "notice"}))
        with self.assertLogs("swarmly.telegram", level="WARNING"):
            results, _ = self._drive(channel, adapter, self.ADDRESSED[:1])
        self.assertEqual(results, [None])
        self.assertEqual(channel.stats.ignored, 1)
        self.assertEqual(adapter.calls, 1)

    def test_diag_reports_the_busy_drop_counter(self):
        channel, adapter = gated_channel()
        _, diag = self._drive(
            channel, adapter, self.ADDRESSED[:1],
            after=lambda ch: ch.handle_event(event("/diag@HermesBot", message_id=99)))

        self.assertIn("DIAG alias=Hermes", diag)
        self.assertIn("busy_dropped=1", diag)
        self.assertEqual(len(diag.splitlines()), 1)

    def test_unknown_busy_behavior_is_rejected_when_the_config_loads(self):
        with self.assertRaises(ConfigError) as caught:
            make_config(limits={"busy_behavior": "sometimes"})
        self.assertIn("busy_behavior", str(caught.exception))
        self.assertIn("sometimes", str(caught.exception))
        # the rejection is at load time, not deferred to validate()
        with self.assertRaises(ConfigError):
            ChannelLimits(busy_behavior="notice_please")

    def test_busy_behavior_defaults_to_queue_with_a_15_minute_notice_window(self):
        limits = ChannelLimits()
        self.assertEqual(limits.busy_behavior, "queue")
        self.assertEqual(limits.busy_queue_max, 12)
        self.assertEqual(limits.busy_notice_window_seconds, 900.0)
        self.assertEqual(make_config().limits.busy_behavior, "queue")

    def test_status_reports_the_busy_drop_counter(self):
        """#93(1): /diag carried it; /status is where a loss anomaly is first noticed."""
        channel, adapter = gated_channel()
        _, status = self._drive(
            channel, adapter, self.ADDRESSED,
            after=lambda ch: ch.handle_event(event("/status@HermesBot", message_id=98)))

        self.assertIn("STATUS Hermes", status)
        self.assertEqual(_token(status, "busy_dropped"), channel.stats.busy_dropped,
                         "the token must reflect the counter, not a constant")
        self.assertEqual(_token(status, "busy_dropped"), 3,
                         "three addressed messages were dropped, not a hardcoded 1")
        self.assertIn("machine=", status, "the existing /status tokens must survive")
        self.assertEqual(len(status.splitlines()), 1)
        # Not a guarantee — /status is unbounded in general (`stats.last_error` carries `str(exc)`,
        # and `replies.summary()` grows with its row count). It holds here because this fixture has
        # `replies is None` and no error set, so the bound catches only a token explosion.
        self.assertLess(len(status), 200)

    def test_diag_reports_the_drop_as_its_own_token_inside_seen(self):
        """A busy drop is inside `seen` (it bumps `ignored`, which `seen` sums) *and* is its own
        `busy_dropped=` token. Conflating the two is what let a vacuous `seen=` assertion stand.

        The snapshot is taken when `after` is CALLED — `handle_event` returns an un-awaited
        coroutine that `drive_busy_drops` awaits afterwards. That is deliberate: at call time the
        drops are already counted (ignored=3) but `handled` is still 0, because the held turn
        counts it only on release (channel.py:1170) and /diag has not run yet — /diag bumps
        `handled` itself when it is served.

        Three drops, not one: with a single drop the counter is genuinely 1, so a token hardcoded
        to the literal ``busy_dropped=1`` would satisfy the cross-check and the test could not fail.
        """
        channel, adapter = gated_channel()
        at_call = {}

        def after(ch):
            observed = ch.handle_event(event("/diag@HermesBot", message_id=97))
            at_call["handled"] = ch.stats.handled
            at_call["ignored"] = ch.stats.ignored
            return observed

        _, diag = self._drive(channel, adapter, self.ADDRESSED, after=after)

        # seen=3: the three drops, inside the sum via `ignored`. Pinned by value, not by prefix.
        self.assertEqual(_token(diag, "seen"), 3,
                         "the drops are inside `seen`, not excluded from it")
        self.assertEqual(_token(diag, "busy_dropped"), channel.stats.busy_dropped,
                         "the token must reflect the counter, not a constant")
        self.assertEqual(_token(diag, "busy_dropped"), 3)
        self.assertEqual(at_call["handled"], 0, "the held turn has not been handled yet")
        self.assertEqual(at_call["ignored"], 3, "the drops are the ignored messages")
        # The claimed subset relation: nothing tests it if it is only true by line adjacency.
        self.assertGreaterEqual(channel.stats.ignored, channel.stats.busy_dropped)

        # Post-release: the held turn has now counted its `handled`, so a second /diag sees both
        # messages. This is what makes the `+ self.stats.handled` term load-bearing: an
        # `ignored`-only `seen` would read 3 here, not 5, and would pass the first check above.
        self.assertEqual(channel.stats.handled, 2,
                         "the held turn and the first /diag are now handled")
        second = run(channel.handle_event(event("/diag@HermesBot", message_id=96)))
        self.assertEqual(_token(second, "seen"), 5, "two handled messages plus three drops")


class TestBusyNoticeWindowValidation(unittest.TestCase):
    """#93(2): the notice window is a config-time contract, not a drop-time surprise.

    Before this, `limits.busy_notice_window_seconds: abc` loaded without a word and only failed
    on the inbound path at drop time — *after* `stats.ignored`, `stats.busy_dropped` and
    `_busy_drops_since_notice` had already been bumped. The drop was counted, the handler raised
    instead of returning, and the notice never emitted: the one moment the feature exists for was
    the moment it threw.
    """

    ADDRESSED = ("@HermesBot: Can you please verify PR #148?",)

    def _drive(self, channel, adapter, drops, after=None):
        return drive_busy_drops(channel, adapter, drops, after)

    def test_a_non_numeric_window_is_rejected_at_load_naming_key_and_section(self):
        for bad in ("abc", None, [], {}, "12s"):
            with self.assertRaises(ConfigError) as caught:
                make_config(limits={"busy_behavior": "notice",
                                    "busy_notice_window_seconds": bad})
            message = str(caught.exception)
            self.assertIn("busy_notice_window_seconds", message, bad)
            self.assertIn("limits", message, bad)

    def test_the_rejection_is_a_ConfigError_with_a_usable_message(self):
        """The old comprehension let a bare `ValueError: could not convert string to float`
        escape from three frames deep, naming neither the key nor the section."""
        with self.assertRaises(ConfigError) as caught:
            make_config(limits={"busy_notice_window_seconds": "abc"})
        self.assertIsInstance(caught.exception, ConfigError)
        self.assertIn("busy_notice_window_seconds", str(caught.exception))

    def test_a_window_that_is_not_positive_is_rejected(self):
        """window <= 0 inverts the bound: `now - last >= window` is true on every drop, so N
        drops fire N notices — the one-notice-per-drop flood the feature exists to prevent."""
        for bad in (0, 0.0, -1, -900.0):
            with self.assertRaises(ConfigError) as caught:
                make_config(limits={"busy_behavior": "notice",
                                    "busy_notice_window_seconds": bad})
            self.assertIn("busy_notice_window_seconds", str(caught.exception))

    def test_a_non_finite_window_is_rejected(self):
        """inf and nan both make the comparison false forever — notice mode silently dead."""
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.assertRaises(ConfigError) as caught:
                ChannelLimits(busy_behavior="notice", busy_notice_window_seconds=bad)
            self.assertIn("busy_notice_window_seconds", str(caught.exception))

    def test_a_numeric_string_is_tolerated_and_normalised_to_a_float(self):
        config = make_config(limits={"busy_behavior": "notice",
                                     "busy_notice_window_seconds": "300"})
        window = config.limits.busy_notice_window_seconds
        self.assertIsInstance(window, float, "normalised, so drop time cannot re-fail")
        self.assertEqual(window, 300.0)

    def test_a_valid_window_still_loads(self):
        self.assertEqual(ChannelLimits(busy_notice_window_seconds=1).busy_notice_window_seconds, 1)
        self.assertEqual(ChannelLimits(busy_notice_window_seconds=0.5).busy_notice_window_seconds,
                         0.5)

    def test_when_the_window_is_invalid_the_counters_are_untouched(self):
        """The acceptance pin: a config that refuses to start cannot also have half-recorded a
        drop. The rejection lands in `from_dict`, before any `handle_event` can run — so there is
        no channel to have counted anything."""
        with self.assertRaises(ConfigError):
            make_config(limits={"busy_behavior": "notice",
                                "busy_notice_window_seconds": "abc"})

        # And the inbound path cannot reintroduce the half-record: hand it an unusable window
        # anyway (a pre-validation host, or a hand-built object) and the drop must still be
        # fully recorded and the handler must still return — never counted-then-raised.
        channel, adapter = gated_channel(make_config(limits={"busy_behavior": "notice"}))
        channel.config.limits.busy_notice_window_seconds = "abc"
        with self.assertLogs("swarmly.telegram", level="WARNING"):
            results, _ = self._drive(channel, adapter, self.ADDRESSED)

        self.assertEqual(results, [None], "the drop path must not raise")
        self.assertEqual(channel.stats.busy_dropped, 1, "fully counted, not half-recorded")
        self.assertEqual(channel.stats.ignored, 1)

    def test_an_unusable_window_degrades_to_the_default_and_still_notifies(self):
        for bad in (float("nan"), float("inf"), 0, -1, "abc"):
            channel, adapter = gated_channel(make_config(limits={"busy_behavior": "notice"}))
            channel.config.limits.busy_notice_window_seconds = bad
            with self.assertLogs("swarmly.telegram", level="WARNING") as captured:
                self._drive(channel, adapter, self.ADDRESSED)

            busy = [r.getMessage() for r in captured.records if "busy:" in r.getMessage()]
            self.assertEqual(len(busy), 1,
                             f"{bad!r}: degraded to the 900s default, first drop notifies")
            self.assertIn("1 addressed msg(s)", busy[0])
            self.assertEqual(channel.stats.busy_dropped, 1)



class _SilentAdapter(EchoAdapter):
    """An agent that finished its turn with nothing to say."""

    async def respond(self, message):
        return AdapterReply(ok=False, error=f"{self.alias} produced no reply",
                            meta={"empty": True})


class TestNothingToSay(unittest.TestCase):
    """Roman 2: a closing "DONE ... @you" from a bot drew an empty turn, and the bot posted
    "produced no reply" into the exchange."""

    def test_an_empty_reply_to_a_bot_ends_quietly(self):
        channel = make_channel(adapter=_SilentAdapter("Hermes"))
        run(channel.handle_event(event("DONE, looks right. @HermesBot")))
        self.assertFalse([row for row in channel._test_sender.sent if "❌" in row["text"]])
        self.assertEqual(channel.stats.errors, 0)

    def test_an_empty_reply_to_a_person_is_still_reported(self):
        channel = make_channel(adapter=_SilentAdapter("Hermes"))
        run(channel.handle_event(event("@HermesBot hi", author_id=ADMIN_ID, username="alice",
                                       is_bot=False)))
        self.assertIn("produced no reply", channel._test_sender.sent[-1]["text"])

    def test_codex_marks_an_empty_run(self):
        adapter = CodexAdapter("Codex", CodexAdapter.default_settings(cwd="/repo"))
        adapter._thread.set("")
        reply = adapter.parse_output('{"type":"thread.started","thread_id":"t1"}\n', 1.0)
        self.assertFalse(reply.ok)
        self.assertTrue(reply.meta.get("empty"))

    def test_a_failed_run_on_a_bots_message_is_still_reported(self):
        class Failing(EchoAdapter):
            async def respond(self, message):
                return AdapterReply(ok=False, error="Hermes failed: exit code 1")
        channel = make_channel(adapter=Failing("Hermes"))
        run(channel.handle_event(event("over to you @HermesBot")))
        self.assertTrue(any("❌ Hermes failed" in row["text"]
                            for row in channel._test_sender.sent))

    def test_an_anonymous_admins_empty_run_is_still_reported(self):
        # An anonymous admin can't be told from any other member by id, so it only gets a
        # turn where strangers are answered; there its empty run is not silenced.
        channel = make_channel(make_config(limits={"strangers": "answer"}),
                               adapter=_SilentAdapter("Hermes"))
        anonymous = event("@HermesBot hi", author_id=1087968824, username="GroupAnonymousBot")
        anonymous.raw = types.SimpleNamespace(sender_chat=types.SimpleNamespace(id=GROUP))
        run(channel.handle_event(anonymous))
        self.assertTrue(any("produced no reply" in row["text"]
                            for row in channel._test_sender.sent))

    def test_an_anonymous_admin_is_told_once_why_nothing_happens(self):
        channel = make_channel(adapter=_SilentAdapter("Hermes"))

        def anonymous(text, message_id):
            item = event(text, author_id=1087968824, username="GroupAnonymousBot",
                         message_id=message_id)
            item.raw = types.SimpleNamespace(sender_chat=types.SimpleNamespace(id=GROUP))
            return item

        async def scenario():
            await channel.handle_event(anonymous("hello all", 1))          # not addressed
            await channel.handle_event(anonymous("@HermesBot start", 2))
            await channel.handle_event(anonymous("@HermesBot again", 3))
        run(scenario())
        notices = [row for row in channel._test_sender.sent if "anonymously" in row["text"]]
        self.assertEqual(len(notices), 1)
        self.assertIn("Remain anonymous", notices[0]["text"])

    def test_an_empty_quota_failure_on_a_bots_message_still_counts(self):
        class Limited(EchoAdapter):
            async def respond(self, message):
                return AdapterReply(ok=False, meta={"empty": True}, error=(
                    "Hermes produced no output (empty stdout; stderr: 429 rate limit)"))
        channel = make_channel(adapter=Limited("Hermes"))

        async def scenario():
            for n in range(3):
                await channel.handle_event(event(f"round {n} @HermesBot", message_id=10 + n))
        run(scenario())
        self.assertTrue(channel._frozen)


if __name__ == "__main__":
    unittest.main()


# Issue #106: ``AgentConfig.from_dict`` merges the developer's real ``.env`` into the
# process-global ``os.environ``; the guard keeps this module's tests from leaking it.
from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)
