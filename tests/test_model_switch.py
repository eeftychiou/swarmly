"""adapter.model and the runtime /model command: which model a bot's CLI runs, per topic."""

import json
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.adapters.claude import ClaudeAdapter, ClaudeThreadAdapter
from swarmly.telegram.adapters.codex import CodexAdapter
from swarmly.telegram.config import AdapterSettings, ConfigError, model_problem
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run


def _argv(settings: AdapterSettings, model=None):
    return shlex.split(settings.command_template(model=model), posix=(os.name != "nt"))


class FakeProcess:
    def __init__(self, stdout=b'{"result": "ok", "session_id": "s"}'):
        self._stdout, self.returncode = stdout, 0

    async def communicate(self):
        return self._stdout, b""


def _recording(adapter_cls, settings):
    calls = []

    async def runner(argv, cwd, env, timeout):
        calls.append(list(argv))
        return FakeProcess()

    return adapter_cls("Claude", settings, runner=runner), calls


class TestAdapterModel(unittest.TestCase):

    def test_claude_model_goes_before_the_prompt(self):
        argv = _argv(AdapterSettings(type="claude", model="opus"))
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertLess(argv.index("--model"), argv.index("--"))

    def test_codex_model_flag(self):
        argv = _argv(AdapterSettings(type="codex", model="gpt-5-codex"))
        self.assertEqual(argv[:3], ["codex", "exec", "--skip-git-repo-check"])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5-codex")

    def test_no_model_leaves_the_cli_default(self):
        self.assertNotIn("--model", _argv(AdapterSettings(type="claude_thread")))
        self.assertNotIn("-m", _argv(AdapterSettings(type="codex")))

    def test_runtime_model_overrides_the_yaml(self):
        argv = _argv(AdapterSettings(type="claude", model="sonnet"), model="opus")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv.count("--model"), 1)

    def test_explicit_command_wins(self):
        settings = AdapterSettings(type="claude", model="opus",
                                   command="claude -p --model haiku -- {prompt}")
        self.assertEqual(settings.command_template(model="sonnet"),
                         "claude -p --model haiku -- {prompt}")
        self.assertFalse(settings.model_switchable)

    def test_model_name_is_validated(self):
        for bad in ("opus --dangerously-skip-permissions", "--yolo", "a;b", "x y"):
            with self.assertRaises(ConfigError, msg=bad):
                AdapterSettings(type="claude", model=bad)
        self.assertIsNone(AdapterSettings(type="claude", model="  ").model)

    def test_gh_is_not_in_the_standard_tools(self):
        # gh acts with the machine owner's GitHub token: not allowed without asking.
        argv = _argv(AdapterSettings(type="claude", permissions="standard"))
        tools = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertFalse([tool for tool in tools if "gh" in tool.split("(")[-1].split(":")[0]])

    def test_a_runtime_model_that_is_not_one_name_is_dropped(self):
        settings = AdapterSettings(type="codex", permissions="strict", model="o3")
        argv = _argv(settings, model="gpt-5 --dangerously-bypass-approvals-and-sandbox")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "o3")

    def test_model_problem_stays_within_the_cli(self):
        for ok in ("opus", "sonnet[1m]", "claude-opus-4-1", "haiku"):
            self.assertIsNone(model_problem("claude_thread", ok), ok)
        for ok in ("gpt-5-codex", "o3", "o4-mini", "codex-mini-latest"):
            self.assertIsNone(model_problem("codex", ok), ok)
        self.assertIsNotNone(model_problem("claude", "gpt-5"))
        self.assertIsNotNone(model_problem("codex", "opus"))
        self.assertIsNotNone(model_problem("codex", "gemini-2.5-pro"))
        self.assertIsNotNone(model_problem("hermes", "anything"))


class TestTurnModel(unittest.TestCase):

    def test_message_model_reaches_the_argv(self):
        adapter, calls = _recording(ClaudeAdapter, AdapterSettings(type="claude", model="sonnet"))
        run(adapter.respond(IncomingMessage(text="hi", author_alias="admin", model="opus")))
        run(adapter.respond(IncomingMessage(text="hi", author_alias="admin")))
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "opus")
        self.assertEqual(calls[1][calls[1].index("--model") + 1], "sonnet")

    def test_resumed_claude_session_keeps_going_on_the_new_model(self):
        adapter, calls = _recording(ClaudeThreadAdapter, AdapterSettings(type="claude_thread"))
        run(adapter.respond(IncomingMessage(text="hi", author_alias="admin")))
        run(adapter.respond(IncomingMessage(text="hi", author_alias="admin", model="haiku")))
        self.assertIn("--resume", calls[1])
        self.assertEqual(calls[1][calls[1].index("--model") + 1], "haiku")

    def test_resumed_codex_session_gets_the_model(self):
        calls = []

        async def runner(argv, cwd, env, timeout):
            calls.append(list(argv))
            return FakeProcess(b'{"type":"thread.started","thread_id":"t1"}\n'
                               b'{"type":"item.completed","item":{"type":"agent_message",'
                               b'"text":"ok"}}\n')

        adapter = CodexAdapter("Codex", AdapterSettings(type="codex"), runner=runner)
        adapter._threads["topic-1"] = "t1"
        run(adapter.respond(IncomingMessage(text="hi", author_alias="admin",
                                            context_id="topic-1", model="o3")))
        argv = calls[0]
        self.assertEqual(argv[argv.index("-m") + 1], "o3")
        self.assertLess(argv.index("-m"), argv.index("resume"))


def _claude_channel(**adapter):
    config = make_config(adapter={"type": "claude", **adapter})
    return make_channel(config), config


class TestModelCommand(unittest.TestCase):

    def _say(self, channel, text, **kwargs):
        kwargs.setdefault("author_id", ADMIN_ID)
        kwargs.setdefault("username", "alice")
        kwargs.setdefault("is_bot", False)
        run(channel.handle_event(event(text, **kwargs)))
        return channel._test_sender.last

    def test_admin_switches_and_resets(self):
        channel, _ = _claude_channel(model="sonnet")
        self.assertIn("sonnet", self._say(channel, "/model@HermesBot"))
        self.assertIn("runs opus", self._say(channel, "/model@HermesBot opus", message_id=2))
        self.assertEqual(channel._models, {"general": "opus"})
        self.assertIn("model=opus", self._say(channel, "/status@HermesBot", message_id=3))
        self.assertIn("goes back to sonnet",
                      self._say(channel, "/model@HermesBot default", message_id=4))
        self.assertEqual(channel._models, {})

    def test_only_the_admin_switches(self):
        channel, _ = _claude_channel()
        reply = self._say(channel, "/model@HermesBot opus", author_id=202, username="CodexBot",
                          is_bot=True)
        self.assertIn("only the admin", reply)
        self.assertEqual(channel._models, {})

    def test_a_model_of_another_cli_is_refused(self):
        channel, _ = _claude_channel()
        reply = self._say(channel, "/model@HermesBot gpt-5-codex")
        self.assertIn("not a Claude model", reply)
        self.assertEqual(channel._models, {})

    def test_the_yaml_model_is_always_accepted(self):
        channel, _ = _claude_channel(model="my-proxy-model")
        self._say(channel, "/model@HermesBot opus")
        self.assertIn("runs my-proxy-model",
                      self._say(channel, "/model@HermesBot my-proxy-model", message_id=2))

    def test_command_for_another_bot_is_not_answered(self):
        channel, _ = _claude_channel()
        run(channel.handle_event(event("/model@CodexBot opus", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        self.assertEqual(channel._test_sender.sent, [])

    def test_explicit_command_cannot_switch(self):
        channel, _ = _claude_channel(command="claude -p --model haiku -- {prompt}")
        self.assertIn("cannot switch", self._say(channel, "/model@HermesBot opus"))

    def test_topics_are_separate_and_the_turn_carries_the_model(self):
        channel, _ = _claude_channel()
        seen = []

        async def capture(message):
            seen.append(message.model)
            return None

        channel.adapter.respond = capture
        self._say(channel, "/model@HermesBot haiku", thread_id=7)
        self.assertEqual(list(channel._models.values()), ["haiku"])
        self.assertNotIn("general", channel._models)
        run(channel.handle_event(event("@HermesBot go", author_id=ADMIN_ID, username="alice",
                                       is_bot=False, thread_id=7, message_id=5)))
        run(channel.handle_event(event("@HermesBot go", author_id=ADMIN_ID, username="alice",
                                       is_bot=False, message_id=6)))
        self.assertEqual(seen, ["haiku", None])

    def test_choice_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(adapter={"type": "claude"}, bus={"repo": tmp})
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            self._say(channel, "/model@HermesBot opus")
            again = make_channel(config)
            self.assertEqual(again._models, {"general": "opus"})

    def test_a_tampered_models_file_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(adapter={"type": "claude"}, bus={"repo": tmp})
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            self._say(channel, "/model@HermesBot opus")
            path = channel._models_path
            path.write_text(json.dumps({"general": "opus --dangerously-skip-permissions",
                                        "topic-1": "gpt-5", "topic-2": "sonnet"}))
            with self.assertLogs("swarmly.telegram", level="WARNING"):
                again = make_channel(config)
            self.assertEqual(again._models, {"topic-2": "sonnet"})


class TestModelCommandFollowUps(unittest.TestCase):
    """#158: a bare /model in the admin's private chat, and /model naming several bots."""

    def test_bare_command_in_the_admin_dm_is_not_a_turn(self):
        channel, _ = _claude_channel()
        turns = []

        async def capture(message):
            turns.append(message)
            return None

        channel.adapter.respond = capture
        run(channel.handle_event(event("/model opus", author_id=ADMIN_ID, username="alice",
                                       is_bot=False, chat_id=ADMIN_ID)))
        self.assertEqual(turns, [])
        self.assertIn("runs opus", channel._test_sender.last)
        self.assertEqual(channel._models, {"general": "opus"})

    def _turns(self, channel):
        turns = []

        async def capture(message):
            turns.append(message.text)
            return None

        channel.adapter.respond = capture
        return turns

    def test_bare_command_in_the_home_group_is_ignored(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen):
                channel, _ = _claude_channel()
                channel._frozen = frozen
                turns = self._turns(channel)
                run(channel.handle_event(event("/model opus", author_id=ADMIN_ID,
                                               username="alice", is_bot=False)))
                self.assertEqual(channel._models, {})
                self.assertFalse([t for t in channel._test_sender.sent if "runs opus" in str(t)])
                self.assertFalse([t for t in turns if "/model" in t])

    def test_a_strangers_dm_switches_nothing(self):
        channel, _ = _claude_channel()
        turns = self._turns(channel)
        run(channel.handle_event(event("/model opus", author_id=303, username="mallory",
                                       is_bot=False, chat_id=303)))
        self.assertEqual(channel._test_sender.sent, [])
        self.assertEqual(turns, [])
        self.assertEqual(channel._models, {})

    def test_the_admins_bare_command_in_another_group_is_not_a_dm(self):
        channel, _ = _claude_channel()
        self._turns(channel)
        run(channel.handle_event(event("/model opus", author_id=ADMIN_ID, username="alice",
                                       is_bot=False, chat_id=-100999)))
        self.assertEqual(channel._models, {})
        self.assertFalse([t for t in channel._test_sender.sent if "runs opus" in str(t)])

    def test_other_bots_named_are_not_arguments(self):
        channel, _ = _claude_channel()
        run(channel.handle_event(event("/model opus @HermesBot @CodexBot", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        self.assertIn("runs opus", channel._test_sender.last)


class TestCodexSessionIds(unittest.TestCase):
    """#158 M11: session ids from the state file go on the command line after `resume`."""

    def test_a_tampered_sessions_file_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-sessions.json"
            path.write_text(json.dumps({
                "a": "--dangerously-bypass-approvals-and-sandbox",
                "b": {"thread": "x --yolo", "rules": "r"},
                "c": {"thread": 7},
                "d": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
                "e": {"thread": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f9", "rules": "abc"}}))
            adapter = CodexAdapter("Codex", AdapterSettings(type="codex"))
            with self.assertLogs("swarmly.telegram.adapters.codex", level="WARNING"):
                adapter.use_state_file(path)
        self.assertEqual(adapter._threads, {"d": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
                                            "e": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f9"})
        self.assertEqual(adapter._rules_held, {"e": "abc"})

    def test_an_odd_id_from_codex_is_not_kept(self):
        adapter = CodexAdapter("Codex", AdapterSettings(type="codex"))
        with self.assertLogs("swarmly.telegram.adapters.codex", level="WARNING"):
            adapter._remember("t", "-c x=1")
        self.assertEqual(adapter._threads, {})

    def test_ids_are_at_most_128_characters(self):
        adapter = CodexAdapter("Codex", AdapterSettings(type="codex"))
        adapter._remember("t", "a" * 128)
        self.assertEqual(adapter._threads, {"t": "a" * 128})
        with self.assertLogs("swarmly.telegram.adapters.codex", level="WARNING"):
            adapter._remember("u", "a" * 129)
        self.assertNotIn("u", adapter._threads)


class TestAntigravityModelNames(unittest.TestCase):
    """Issue #10 (brainstorm 2.10): the names the docs show are mixed case with spaces.

    docs/telegram/SETUP-ANTIGRAVITY.md documents ``--model 'Gemini 3.1 Pro (High)'``, which
    the lowercase-only validator refused. The names ``agy models`` lists are still one
    argv token each, and anything shell-dangerous stays refused.
    """

    DOCUMENTED = ("Gemini 3.1 Pro (High)", "Gemini 3.8 Flash High",
                  "gemini-3.8-flash-high", "Claude Sonnet 4.5 (Thinking)", "o3")

    DANGEROUS = ("", " ", "Gemini 3.1 Pro (High) ", " Gemini", "--model", "-c", "agy -p",
                 "Gemini; rm -rf /", "Gemini && id", "Gemini | tee /tmp/x", "$(id)", "`id`",
                 "Gemini" + chr(10) + "3.1", "Gemini" + chr(9) + "3.1",
                 "Gemini" + chr(0), "Gemini 3.1 'Pro'",
                 'Gemini 3.1 "Pro"', "Gemini 3.1 $(whoami)", "Gemini > out", "Gemini*",
                 "Gemini?", "Gemini 3.1 Pro (High))", "((High)")

    def test_the_documented_names_validate(self):
        for name in self.DOCUMENTED:
            self.assertIsNone(model_problem("antigravity", name), name)
            settings = AdapterSettings(type="antigravity", model=name)
            self.assertEqual(settings.model, name)

    def test_dangerous_values_stay_refused(self):
        for name in self.DANGEROUS:
            self.assertIsNotNone(model_problem("antigravity", name), repr(name))
            if name.strip() == name and name:
                with self.assertRaises(ConfigError, msg=repr(name)):
                    AdapterSettings(type="antigravity", model=name)

    def test_the_other_adapters_keep_the_strict_form(self):
        """The Antigravity form is Antigravity's: a Claude or Codex model still gets a no."""
        for name in ("Gemini 3.1 Pro (High)", "gemini-3.8-flash-high", "sonnet (1m)"):
            self.assertIsNotNone(model_problem("claude", name), name)
            self.assertIsNotNone(model_problem("codex", name), name)
        # At config load only the generic "one model name" check runs; a space is refused.
        with self.assertRaises(ConfigError):
            AdapterSettings(type="claude", model="Gemini 3.1 Pro (High)")

    def test_a_documented_name_reaches_agy_as_one_token(self):
        from swarmly.telegram.adapters.antigravity import AntigravityAdapter
        settings = AdapterSettings(type="antigravity", model="Gemini 3.1 Pro (High)")
        adapter = AntigravityAdapter("Antigravity", settings)
        argv = adapter.build_argv("hello")
        model = argv[argv.index("--model") + 1]
        self.assertEqual(model.strip("'\""), "Gemini 3.1 Pro (High)",
                         f"an engine name must be one argv token, got {argv}")

    def test_the_docs_and_the_validator_agree(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "telegram"
               / "SETUP-ANTIGRAVITY.md").read_text(encoding="utf-8")
        documented = re.findall(r"--model '([^']+)'", doc)
        self.assertTrue(documented, "the Antigravity setup doc shows no --model example")
        for name in documented:
            self.assertIsNone(model_problem("antigravity", name), name)
            self.assertIsNone(AdapterSettings(type="antigravity", model=name).model and None)


if __name__ == "__main__":
    unittest.main()
