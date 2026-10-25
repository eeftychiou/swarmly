"""The runtime /reasoning command (alias /effort): a bot's reasoning effort, per topic,
set by the admin without touching adapter.effort or the CLI's own defaults."""

import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.adapters.claude import ClaudeAdapter
from swarmly.telegram.commands import is_command_reply
from swarmly.telegram.config import AdapterSettings
from tests.test_model_switch import _recording
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run


def _channel(**adapter):
    config = make_config(adapter={"type": "claude", **adapter})
    return make_channel(config), config


def _say(channel, text, **kwargs):
    kwargs.setdefault("author_id", ADMIN_ID)
    kwargs.setdefault("username", "alice")
    kwargs.setdefault("is_bot", False)
    run(channel.handle_event(event(text, **kwargs)))
    return channel._test_sender.last


class TestReasoningCommand(unittest.TestCase):

    def test_shows_the_cli_default_when_nothing_is_set(self):
        channel, config = _channel()
        reply = _say(channel, "/reasoning@HermesBot")
        self.assertIn("CLI default", reply)
        self.assertIn("low|medium|high|xhigh|max", reply)
        self.assertIsNone(config.adapter.effort)

    def test_admin_sets_and_resets(self):
        channel, config = _channel()
        self.assertIn("reasons at high", _say(channel, "/reasoning@HermesBot high", message_id=2))
        self.assertEqual(channel._efforts, {"general": "high"})
        self.assertIn("effort=high", _say(channel, "/status@HermesBot", message_id=3))
        self.assertIn("CLI default", _say(channel, "/reasoning@HermesBot default", message_id=4))
        self.assertEqual(channel._efforts, {})
        self.assertIsNone(config.adapter.effort)    # the configured default is untouched

    def test_effort_is_an_alias(self):
        channel, _ = _channel()
        self.assertIn("reasons at low", _say(channel, "/effort@HermesBot LOW"))
        self.assertEqual(channel._efforts, {"general": "low"})

    def test_only_the_admin_sets_it(self):
        channel, _ = _channel()
        reply = _say(channel, "/reasoning@HermesBot max", author_id=202, username="CodexBot",
                     is_bot=True)
        self.assertIn("only the admin", reply)
        self.assertEqual(channel._efforts, {})

    def test_a_level_the_cli_lacks_is_refused(self):
        channel, _ = _channel(type="codex")
        self.assertIn("kept", _say(channel, "/reasoning@HermesBot max"))
        self.assertIn("kept", _say(channel, "/reasoning@HermesBot huge", message_id=2))
        self.assertEqual(channel._efforts, {})

    def test_an_explicit_command_cannot_be_changed(self):
        channel, _ = _channel(command="my-cli {prompt}")
        self.assertIn("cannot set reasoning effort", _say(channel, "/reasoning@HermesBot high"))

    def test_topics_are_separate_and_the_turn_carries_the_effort(self):
        channel, _ = _channel()
        seen = []

        async def capture(message):
            seen.append(message.effort)
            return None

        channel.adapter.respond = capture
        _say(channel, "/reasoning@HermesBot medium", thread_id=7)
        self.assertNotIn("general", channel._efforts)
        run(channel.handle_event(event("@HermesBot go", author_id=ADMIN_ID, username="alice",
                                       is_bot=False, thread_id=7, message_id=5)))
        run(channel.handle_event(event("@HermesBot go", author_id=ADMIN_ID, username="alice",
                                       is_bot=False, message_id=6)))
        self.assertEqual(seen, ["medium", None])

    def test_the_choice_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(adapter={"type": "claude"}, bus={"repo": tmp})
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            _say(channel, "/reasoning@HermesBot xhigh")
            self.assertEqual(make_channel(config)._efforts, {"general": "xhigh"})

    def test_a_tampered_efforts_file_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(adapter={"type": "claude"}, bus={"repo": tmp})
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            _say(channel, "/reasoning@HermesBot low")
            channel._efforts_path.write_text(json.dumps(
                {"general": "high --dangerously-skip-permissions", "topic-1": "medium"}))
            with self.assertLogs("swarmly.telegram", level="WARNING"):
                again = make_channel(config)
            self.assertEqual(again._efforts, {"topic-1": "medium"})

    def test_peers_take_the_answers_as_machine_traffic(self):
        for text in ("Hermes reasons at high here from the next turn.",
                     "[Hermes] reasons at its CLI default effort here (the CLI's default). x",
                     "Hermes goes back to its CLI default effort here from the next turn.",
                     "Hermes: only the admin can set my reasoning effort.",
                     "Hermes runs opus here from the next turn; the conversation carries on.",
                     "Hermes kept high: codex has no max."):
            self.assertTrue(is_command_reply(text), text)
        self.assertFalse(is_command_reply("Hermes runs the tests here and they pass."))


class TestTurnEffort(unittest.TestCase):

    def test_turn_effort_reaches_the_argv_for_that_turn_only(self):
        adapter, calls = _recording(ClaudeAdapter, AdapterSettings(type="claude"))
        run(adapter.respond(IncomingMessage(text="hi", author_alias="admin", effort="high")))
        run(adapter.respond(IncomingMessage(text="hi", author_alias="admin")))
        self.assertEqual(calls[0][calls[0].index("--effort") + 1], "high")
        self.assertNotIn("--effort", calls[1])

    def test_a_bad_runtime_effort_falls_back_to_the_config(self):
        settings = AdapterSettings(type="codex", effort="low")
        argv = shlex.split(settings.command_template(effort="max --yolo"),
                           posix=(os.name != "nt"))
        self.assertIn("model_reasoning_effort=low", argv)
        self.assertNotIn("--yolo", argv)

    def test_antigravity_model_switches_at_runtime(self):
        settings = AdapterSettings(type="antigravity")
        self.assertTrue(settings.model_switchable)
        self.assertIn("--model gemini-3.8-flash-high",
                      settings.command_template(model="gemini-3.8-flash-high"))
        self.assertIn("--effort high", settings.command_template(effort="high"))


if __name__ == "__main__":
    unittest.main()
