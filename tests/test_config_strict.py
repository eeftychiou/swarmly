"""Review P1-7: the transport config loader parses booleans and numbers strictly and names
keys it does not read, instead of guessing or ignoring them without a word."""

import asyncio
import io
import unittest
from contextlib import redirect_stdout

from swarmly.telegram import cli
from swarmly.telegram.config import ConfigError, unknown_keys
from tests.test_telegram_channel import make_config


class TestBooleans(unittest.TestCase):

    def test_a_quoted_false_turns_the_adapter_off(self):
        self.assertFalse(make_config(adapter={"type": "echo", "enabled": "false"}).adapter.enabled)
        self.assertFalse(make_config(adapter={"type": "echo", "enabled": "off"}).adapter.enabled)
        self.assertTrue(make_config(adapter={"type": "echo", "enabled": "yes"}).adapter.enabled)

    def test_a_quoted_false_turns_the_bus_off(self):
        self.assertFalse(make_config(bus={"enabled": "false"}).bus.enabled)

    def test_anything_else_is_an_error_naming_the_key(self):
        for section, values in (("adapter", {"type": "echo", "enabled": "nope"}),
                                ("bus", {"enabled": 2})):
            with self.assertRaises(ConfigError) as caught:
                make_config(**{section: values})
            self.assertIn(f"{section}.enabled", str(caught.exception))


class TestNumbers(unittest.TestCase):

    def test_zero_is_not_silently_the_default(self):
        for key in ("timeout_seconds", "max_output_chars"):
            with self.assertRaises(ConfigError) as caught:
                make_config(adapter={"type": "echo", key: 0})
            self.assertIn(f"adapter.{key}", str(caught.exception))

    def test_numbers_and_defaults(self):
        adapter = make_config(adapter={"type": "echo", "timeout_seconds": "120",
                                       "reply_timeout_seconds": 0}).adapter
        self.assertEqual(adapter.timeout_seconds, 120)
        self.assertEqual(adapter.reply_timeout_seconds, 0)
        self.assertEqual(make_config().adapter.max_output_chars, 3500)

    def test_true_is_not_a_number(self):
        with self.assertRaises(ConfigError):
            make_config(adapter={"type": "echo", "timeout_seconds": True})
        with self.assertRaises(ConfigError) as caught:
            make_config(limits={"max_parallel_turns": True})
        self.assertIn("limits.max_parallel_turns", str(caught.exception))


class TestChatLink(unittest.TestCase):

    def test_a_copied_message_link_gives_the_group_id(self):
        for link in ("https://t.me/c/1234567890/1/2", "t.me/c/1234567890/55",
                     "https://t.me/c/1234567890"):
            config = make_config(agent={"home_channel_id": link})
            self.assertEqual(config.identity.home_channel_id, -1001234567890, link)

    def test_ids_still_work_and_other_links_are_errors(self):
        self.assertEqual(make_config(agent={"home_channel_id": "-1001234567890"})
                         .identity.home_channel_id, -1001234567890)
        with self.assertRaises(ConfigError):
            make_config(agent={"home_channel_id": "https://t.me/some_public_group"})


class TestUnknownKeys(unittest.TestCase):

    def test_misspelt_keys_are_named_with_a_suggestion(self):
        found = unknown_keys({"agent": {"admin_user": 1}, "adapter": {"timeout": 5},
                              "limits": {"max_parallel_turn": 2}, "buss": {},
                              "peers": {"Anyone": "AnyBot"}})
        self.assertEqual(found, [
            "buss: unknown key, ignored (did you mean bus?)",
            "agent.admin_user: unknown key, ignored (did you mean agent.admin_user_id?)",
            "adapter.timeout: unknown key, ignored (did you mean adapter.timeout_seconds?)",
            "limits.max_parallel_turn: unknown key, ignored "
            "(did you mean limits.max_parallel_turns?)",
        ])

    def test_the_loader_keeps_running_and_check_reports_them(self):
        with self.assertLogs("swarmly.telegram.config", level="WARNING"):
            config = make_config(adapter={"type": "echo", "timeot_seconds": 5})
        self.assertEqual(len(config.unknown_keys), 1)
        out = io.StringIO()
        with redirect_stdout(out):
            code = asyncio.run(cli._check(config))
        self.assertEqual(code, 1)
        self.assertIn("adapter.timeot_seconds: unknown key", out.getvalue())

    def test_handles_in_allowed_users_are_reported_and_ignored(self):
        with self.assertLogs("swarmly.telegram.config", level="WARNING"):
            config = make_config(agent={"allowed_users": ["@ana", 555]})
        self.assertEqual(len(config.unknown_keys), 1)
        self.assertIn("'ana' is a handle", config.unknown_keys[0])
        self.assertTrue(config.is_member(555, None, False))
        self.assertFalse(config.is_member(2, "ana", False))

    def test_a_complete_config_has_none(self):
        self.assertEqual(make_config().unknown_keys, [])


if __name__ == "__main__":
    unittest.main()
