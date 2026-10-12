"""Fixes from the second live Telegram run: Markdown, silence, labelled commands, backlog,
and the bus fetch."""

from __future__ import annotations

import datetime
import types
import unittest
from pathlib import Path
from unittest import mock

from swarmly.telegram.adapters.base import is_silent_reply
from swarmly.telegram.commands import parse_command
from swarmly.telegram.wire import markdown_entities, markdown_html
from swarmly.transports.git import GitTransport
from tests.test_telegram_channel import make_channel, run


class TestMarkdown(unittest.TestCase):

    def test_entities(self):
        text, entities = markdown_entities("a **bold** and `x` ✓ **b**")
        self.assertEqual(text, "a bold and x ✓ b")
        self.assertEqual([(e["type"], e["offset"], e["length"]) for e in entities],
                         [("bold", 2, 4), ("code", 11, 1), ("bold", 15, 1)])

    def test_html(self):
        self.assertEqual(markdown_html("**a** &amp; `b`"), "<b>a</b> &amp; <code>b</code>")


class TestSilence(unittest.TestCase):

    def test_silent_replies(self):
        for text in ("NO_REPLY", "@alice NO_REPLY.", "(no reply needed: just an ack)",
                     "Staying silent, this was a confirmation."):
            self.assertTrue(is_silent_reply(text), text)
        for text in ("Done, PR #2 is up.", "Please don't stay silent on reviews; " + "x" * 200):
            self.assertFalse(is_silent_reply(text), text)


class TestLabelledCommand(unittest.TestCase):

    def test_label_before_command(self):
        self.assertEqual(parse_command("[Codex] /status"), ("status", None))
        self.assertEqual(parse_command("/ping@HermesBot"), ("ping", "HermesBot"))


class TestBacklog(unittest.TestCase):

    def test_old_messages_ignored_after_start(self):
        channel = make_channel()
        channel._started_wall = 10_000_000.0
        handled = []

        async def fake_handle(event):
            handled.append(event)
        channel.handle_event = fake_handle
        channel.event_from_aiogram = lambda message: message
        old = types.SimpleNamespace(message_id=1, date=datetime.datetime.fromtimestamp(
            10_000_000.0 - 3 * 3600, tz=datetime.timezone.utc))
        fresh = types.SimpleNamespace(message_id=2, date=datetime.datetime.fromtimestamp(
            10_000_000.0 - 60, tz=datetime.timezone.utc))
        run(channel._on_aiogram_message(old))
        run(channel._on_aiogram_message(fresh))
        self.assertEqual([m.message_id for m in handled], [2])


class TestGitRefresh(unittest.TestCase):

    def test_fetches_only_the_bus_branch_and_retries_a_lock_race(self):
        transport = GitTransport(Path("."), ref="origin/agents-bus")
        results = [types.SimpleNamespace(returncode=1, stderr="error: cannot lock ref"),
                   types.SimpleNamespace(returncode=0, stderr="")]
        with mock.patch("swarmly.transports.git.subprocess.run",
                        side_effect=results) as fake, \
                mock.patch("swarmly.transports.git.time.sleep") as slept:
            transport.refresh()
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(fake.call_args[0][0][-1],
                         "+refs/heads/agents-bus:refs/remotes/origin/agents-bus")
        slept.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestBuild5Fixes(unittest.TestCase):

    def test_command_replies_from_peers_start_no_turn(self):
        from swarmly.telegram.commands import is_command_reply
        from tests.test_telegram_channel import event
        self.assertTrue(is_command_reply("@HermesBot ROLE DeepSeek developer: writes code"))
        self.assertTrue(is_command_reply("[DeepSeek] STATUS DeepSeek task=x result=y"))
        self.assertFalse(is_command_reply("Status of the PR: green"))
        channel = make_channel()
        decision, reason = channel.should_respond(event(
            "STATUS Codex task=adapter:codex result=ok", replies_to_bot=True))
        self.assertFalse(decision)
        self.assertIn("command reply", reason)

    def test_a_cooldown_drop_is_announced_once(self):
        from tests.test_telegram_channel import event, make_config
        channel = make_channel(make_config(limits={"min_seconds_between_agent_turns": 60.0}))
        run(channel.handle_event(event("@HermesBot one", message_id=1)))
        self.assertIsNone(run(channel.handle_event(event("@HermesBot two", message_id=2))))
        run(channel.handle_event(event("@HermesBot three", message_id=3)))
        notices = [s for s in channel._test_sender.sent if "cooldown" in s["text"]]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["reply_to"], 2)
        self.assertTrue(notices[0]["text"].startswith("⚠️"))

    def test_pong_carries_wall_clock_time(self):
        channel = make_channel()
        year = int(channel._command_reply("ping").split("t=")[1][:4])
        self.assertGreaterEqual(year, 2025)

    def test_autolinks_lose_their_brackets(self):
        from swarmly.telegram.wire import strip_autolinks
        self.assertEqual(strip_autolinks("see <https://eur-lex.europa.eu/search> now"),
                         "see https://eur-lex.europa.eu/search now")
        self.assertEqual(strip_autolinks("a <b> tag"), "a <b> tag")
