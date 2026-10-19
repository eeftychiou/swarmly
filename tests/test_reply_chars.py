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

    def test_an_explicit_500_follows_the_cap_too(self):
        """Review of #31: the clamp is for the default; a written-down 500 is an operated
        number and an error when it cannot be delivered."""
        with self.assertRaises(ConfigError):
            make_config(limits={"max_message_chars": 400, "reply_chars": 500})

    def test_a_lowered_message_cap_does_not_break_the_default(self):
        """Review of #31: max_message_chars below the *default* 500 must still load, clamped."""
        config = make_config(limits={"max_message_chars": 400})
        self.assertEqual(config.limits.reply_chars, 400)

    def test_an_explicit_budget_above_the_message_cap_is_refused(self):
        with self.assertRaises(ConfigError):
            make_config(limits={"max_message_chars": 400, "reply_chars": 800})

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


class TestTheChannelHandsItOver(unittest.TestCase):
    def test_handle_event_passes_the_projects_budget_to_the_ticket(self):
        """Review of #31: removing the argument at channel.py:2295 would silently bring back
        "rules say 1500, the store rejects at 500", and every other test would still pass."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        class Store:
            def __init__(self):
                self.kwargs = None

            def register(self, **kwargs):
                self.kwargs = kwargs
                return ({"id": "t1", "recipient": kwargs.get("recipient"),
                         "reply_chars": kwargs.get("reply_chars"), "state": "pending"}, True)

        with TemporaryDirectory() as tmp:
            config = project_config(reply_chars=1500)
            config.source_path = Path(tmp) / "config.yaml"
            config.source_path.write_text("agent: {}")
            channel, _ = receiver(config)
            channel.replies = Store()
            run(channel.handle_event(event("@HermesBot plan it", message_id=1, thread_id=TOPIC,
                                           author_id=202, username="CodexBot")))
            self.assertEqual(channel.replies.kwargs["reply_chars"], 1500)

            channel.replies = Store()
            run(channel.handle_event(event("@HermesBot general", message_id=2, thread_id=None,
                                           author_id=202, username="CodexBot")))
            self.assertEqual(channel.replies.kwargs["reply_chars"], 500)   # the global one


class TestTheBus(unittest.TestCase):
    def test_a_bus_turn_is_held_too(self):
        """Review of #31: `_bus_incoming` left reply_chars unset, so a gitbus turn fell back
        to 500 even when the channel was configured for something else."""
        from swarmly.telegram.poll import _bus_incoming
        config = make_config(limits={"reply_chars": 800})
        incoming = _bus_incoming("do the thing", "Codex", config, "token")
        self.assertEqual(incoming.reply_chars, 800)
        self.assertIn("under 800 characters", turn_card(incoming))


class TestTheSender(unittest.TestCase):
    def store(self, tmp, **kwargs):
        from pathlib import Path
        return ReplyStore(Path(tmp) / "replies.sqlite3", **kwargs)

    def ticket(self, store, message_id=1, **kwargs):
        row, _ = store.register(chat_id=GROUP, message_id=message_id, recipient="CodexBot",
                                text="do the thing", **kwargs)
        return row["id"]

    def assert_refused(self, store, ticket, text, limit):
        """Refused *for the budget*, and says which budget (a second submit raises anyway)."""
        with self.assertRaises(ValueError) as raised:
            store.submit(ticket, text=text)
        self.assertIn(f"under {limit} characters", str(raised.exception))
        self.assertNotIn("already has a different response", str(raised.exception))

    def test_a_ticket_is_held_to_the_configured_budget(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            store = self.store(tmp, reply_chars=700)
            ticket = self.ticket(store)
            store.submit(ticket, text="x" * 640)                  # the @handle plus the text: 651
            self.assertEqual(store.get(ticket)["state"], "ready")
            # 711 with the @handle: over a 700 budget, and it says so.
            self.assert_refused(store, ticket, "x" * 700, 700)

    def test_the_default_budget_is_unchanged(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            self.assertEqual(store.reply_chars, 500)
            self.assert_refused(store, self.ticket(store), "y" * 500, 500)

    def test_a_ticket_carries_its_topics_budget(self):
        """Review of #31: a ticket raised in a topic whose project allows 1500 must accept a
        1500-character reply, though the channel's own budget is 500."""
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            ticket = self.ticket(store, reply_chars=1500)
            self.assertEqual(store.budget_for(store.get(ticket)), 1500)
            store.submit(ticket, text="z" * 1200)                 # over 500, well under 1500
            self.assertEqual(store.get(ticket)["state"], "ready")

    def test_an_old_ticket_without_a_budget_falls_back_to_the_store(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            ticket = self.ticket(store)                           # registered without one
            self.assertIsNone(store.get(ticket)["reply_chars"])
            self.assertEqual(store.budget_for(store.get(ticket)), 500)

    def test_a_legacy_store_gets_the_column_and_keeps_its_tickets(self):
        """Review of #31: a database written before this change must migrate in place, and its
        tickets fall back to the store's budget."""
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            store = self.store(tmp, reply_chars=700)
            ticket = self.ticket(store)
            # Make it look like a database from before the column existed (through the
            # store's own connection, so no handle outlives the change on Windows).
            with store._db() as db:
                db.execute("ALTER TABLE replies DROP COLUMN reply_chars")
            reopened = self.store(tmp, reply_chars=700)
            row = reopened.get(ticket)
            self.assertIsNone(row["reply_chars"])
            self.assertEqual(reopened.budget_for(row), 700)
            reopened.submit(ticket, text="x" * 100)
            self.assertEqual(reopened.get(ticket)["state"], "ready")

    def test_the_ticket_instructions_state_the_budget(self):
        """Review of #31: the CLI hint hardcoded 500, so a turn told 1500 was contradicted."""
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from swarmly.telegram.replies import reply_instructions
        with TemporaryDirectory() as tmp:
            config = project_config(reply_chars=1500)
            config.source_path = Path(tmp) / "config.yaml"
            config.source_path.write_text("agent: {}\n")
            store = self.store(tmp)
            ticket = self.ticket(store, reply_chars=1500)
            instructions = reply_instructions(config, store.get(ticket))
            self.assertIn("under 1500 characters", instructions)
            plain_ticket = self.ticket(store, message_id=2)       # no topic: the global budget
            self.assertIn("under 500 characters",
                          reply_instructions(config, store.get(plain_ticket)))


if __name__ == "__main__":
    unittest.main()
