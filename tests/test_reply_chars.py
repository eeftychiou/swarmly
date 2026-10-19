"""
A reply-length budget that a topic can set (issue #31).

The 500-character convention is now a number: ``limits.reply_chars`` (500 by default) is what
the turn rules ask for and what a reply ticket is held to, and ``projects.<key>.reply_chars``
gives one project's topics a different one. Nothing configured means 500, as before.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import (DEFAULT_REPLY_CHARS, IncomingMessage, channel_rules,
                                            format_channel_prompt, turn_card)
from swarmly.telegram.config import ChannelLimits, ConfigError
from swarmly.telegram.replies import ReplyStore
from tests.test_telegram_channel import event, make_channel, make_config, run

GROUP = -1001234567890
TOPIC = 12
OTHER_TOPIC = 15


class RecordingEcho(EchoAdapter):
    """Echo, but it keeps every message it was handed, so a test can read the turn's rules."""

    def __init__(self, alias: str = "Hermes") -> None:
        super().__init__(alias)
        self.seen = []

    async def respond(self, message):
        self.seen.append(message)
        from swarmly.telegram.adapters.base import AdapterReply
        return AdapterReply(text="ok")


def project_config(**project) -> object:
    settings = {"repo": "example-org/webapp", "cwd": ".", "topics": [TOPIC]}
    settings.update(project)
    return make_config(projects={"webapp": settings})


def receiver(config):
    channel = make_channel(config)
    adapter = RecordingEcho()
    channel.adapter = adapter
    return channel, adapter


class TestConfig(unittest.TestCase):
    def test_nothing_configured_is_500(self):
        config = make_config()
        self.assertEqual(config.limits.reply_chars, DEFAULT_REPLY_CHARS)
        self.assertIsNone(config.projects.get("webapp"))

    def test_the_global_limit_is_configurable(self):
        config = make_config(limits={"reply_chars": 800})
        self.assertEqual(config.limits.reply_chars, 800)

    def test_a_project_can_set_its_own(self):
        config = project_config(reply_chars=1500)
        self.assertEqual(config.projects["webapp"].reply_chars, 1500)
        self.assertEqual(config.limits.reply_chars, 500)      # the global one is untouched

    def test_a_reply_budget_must_be_a_positive_integer(self):
        for value in (0, -5, "abc", True):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    make_config(limits={"reply_chars": value})
                with self.assertRaises(ConfigError):
                    project_config(reply_chars=value)

    def test_a_reply_budget_cannot_exceed_one_telegram_message(self):
        with self.assertRaises(ConfigError):
            make_config(limits={"reply_chars": 4097})
        with self.assertRaises(ConfigError):
            project_config(reply_chars=4097)

    def test_the_example_configs_document_the_keys(self):
        from pathlib import Path
        for path in (Path("config/telegram.example.yaml"),
                     Path("swarmly/telegram/templates/config.example.yaml")):
            with self.subTest(path=str(path)):
                text = path.read_text(encoding="utf-8")
                self.assertIn("limits:", text)
                self.assertIn("reply_chars:", text)      # the global budget
                self.assertIn("reply_chars: 1500", text)  # and the per-project override


class TestTurnRules(unittest.TestCase):
    def test_the_default_rules_say_500(self):
        self.assertIn("- Keep the response strictly under 500 characters.", channel_rules())
        self.assertIn("under 500 characters",
                      turn_card(IncomingMessage(text="", author_alias="")))

    def test_a_turn_carries_its_topics_budget(self):
        message = IncomingMessage(text="", author_alias="", context_id="tg-12",
                                  reply_chars=1500)
        self.assertIn("under 1500 characters", turn_card(message))
        self.assertIn("- Keep the response strictly under 1500 characters.",
                      channel_rules(reply_chars=1500))
        self.assertIn("- Keep the response strictly under 1500 characters.",
                      format_channel_prompt("Codex", "hi", reply_chars=1500))

    def test_a_topic_override_reaches_only_that_topic(self):
        # Two projects: one sets a budget, the other does not.
        config = make_config(projects={
            "plan": {"repo": "o/plan", "cwd": ".", "topics": [TOPIC], "reply_chars": 1500},
            "webapp": {"repo": "o/webapp", "cwd": ".", "topics": [OTHER_TOPIC]},
        })
        channel, adapter = receiver(config)
        # A project-bound topic runs its own adapter (one per project): hand every turn the
        # recording one, so the test sees what each turn was told.
        with patch.object(channel, "_adapter_for", lambda *a, **k: adapter):
            run(channel.handle_event(event("@HermesBot plan it", message_id=1, thread_id=TOPIC,
                                           author_id=202, username="CodexBot")))
            run(channel.handle_event(event("@HermesBot quick one", message_id=2,
                                           thread_id=OTHER_TOPIC, author_id=202,
                                           username="CodexBot")))

        # The bound topic gets the project's budget; a topic it does not cover gets the
        # default -- the override never leaks across topics (#31).
        self.assertEqual([message.reply_chars for message in adapter.seen], [1500, 500])

    def test_the_global_limit_reaches_an_unbound_topic(self):
        channel, adapter = receiver(make_config(limits={"reply_chars": 800}))
        run(channel.handle_event(event("@HermesBot hello", message_id=1, thread_id=OTHER_TOPIC,
                                       author_id=202, username="CodexBot")))
        self.assertEqual(adapter.seen[0].reply_chars, 800)


class TestTheSender(unittest.TestCase):
    def test_a_ticket_is_held_to_the_configured_budget(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            from pathlib import Path
            store = ReplyStore(Path(tmp) / "replies.sqlite3", reply_chars=700)
            row, _ = store.register(chat_id=GROUP, message_id=1, recipient="CodexBot",
                                    text="do the thing")
            ticket = row["id"]
            store.submit(ticket, text="x" * 640)                  # the @handle plus the text: 651
            with self.assertRaises(ValueError):
                store.submit(ticket, text="x" * 700)              # 711 with the @handle: over

    def test_the_default_budget_is_unchanged(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            from pathlib import Path
            store = ReplyStore(Path(tmp) / "replies.sqlite3")
            self.assertEqual(store.reply_chars, 500)
            row, _ = store.register(chat_id=GROUP, message_id=1, recipient="CodexBot",
                                    text="do the thing")
            ticket = row["id"]
            with self.assertRaises(ValueError) as raised:
                store.submit(ticket, text="y" * 500)               # 500 was already over it
            self.assertIn("under 500 characters", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
