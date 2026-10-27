"""/model with no name lists the models the bot's CLI can switch to."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmly.telegram import model_list
from swarmly.telegram.model_list import (available_models, parse_agy_models,
                                         parse_codex_cache, parse_reasonix_doctor)
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run

AGY_OUTPUT = (b"gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
              b"claude-sonnet-5.5-high\tClaude Sonnet 5.5 (High)\n\n")
REASONIX_OUTPUT = json.dumps({"providers": [
    {"name": "deepseek", "models": ["deepseek-flash", "deepseek-v4-pro"]},
    {"name": "other", "models": ["deepseek-flash"]}]}).encode()
CODEX_CACHE = json.dumps({"models": [
    {"slug": "gpt-5.6-terra", "priority": 2},
    {"slug": "gpt-6-luna", "priority": 1},
    {"slug": "internal", "priority": 0, "visibility": "hide"}]})


class FakeProcess:
    def __init__(self, stdout=AGY_OUTPUT, returncode=0):
        self._stdout, self.returncode = stdout, returncode

    async def communicate(self):
        return self._stdout, b""


def spawner(process, calls=None):
    async def spawn(argv, env):
        if calls is not None:
            calls.append(list(argv))
        return process
    return spawn


class TestDiscovery(unittest.TestCase):

    def setUp(self):
        model_list._cache.clear()
        self.addCleanup(model_list._cache.clear)

    def test_agy_lists_its_ids_with_their_engine_names(self):
        calls = []
        names = run(available_models("antigravity", spawn=spawner(FakeProcess(), calls)))
        self.assertEqual(names, ["gemini-3.8-flash-medium (Gemini 3.8 Flash (Medium))",
                                 "claude-sonnet-5.5-high (Claude Sonnet 5.5 (High))"])
        self.assertEqual(calls, [["agy", "models"]])

    def test_reasonix_lists_every_provider_model_once(self):
        calls = []
        names = run(available_models("reasonix",
                                     spawn=spawner(FakeProcess(REASONIX_OUTPUT), calls)))
        self.assertEqual(names, ["deepseek-flash", "deepseek-v4-pro"])
        self.assertEqual(calls, [["reasonix", "doctor", "--json"]])

    def test_codex_reads_its_own_cache_without_hidden_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "models_cache.json").write_text(CODEX_CACHE, encoding="utf-8")
            names = run(available_models("codex", codex_home=Path(tmp)))
        self.assertEqual(names, ["gpt-6-luna", "gpt-5.6-terra"])

    def test_claude_needs_no_cli(self):
        self.assertIn("opus", run(available_models("claude")))

    def test_a_list_is_reused_and_a_failure_is_retried_sooner(self):
        calls = []
        clock = mock.Mock(return_value=0.0)
        run(available_models("antigravity", spawn=spawner(FakeProcess(), calls), clock=clock))
        run(available_models("antigravity", spawn=spawner(FakeProcess(), calls), clock=clock))
        self.assertEqual(len(calls), 1)
        model_list._cache.clear()
        failing = FakeProcess(returncode=1)
        self.assertIsNone(run(available_models("antigravity", spawn=spawner(failing, calls),
                                               clock=clock)))
        clock.return_value = model_list.FAILED_TTL_SECONDS + 1
        run(available_models("antigravity", spawn=spawner(FakeProcess(), calls), clock=clock))
        self.assertEqual(len(calls), 3)

    def test_a_missing_cli_is_no_list(self):
        async def missing(argv, env):
            raise FileNotFoundError(argv[0])
        self.assertIsNone(run(available_models("antigravity", spawn=missing)))

    def test_parsers_refuse_noise(self):
        self.assertEqual(parse_agy_models("Usage: agy models\nFlags:"), [])
        self.assertEqual(parse_reasonix_doctor("{}"), [])
        self.assertEqual(parse_codex_cache("[]"), [])


class TestModelCommandLists(unittest.TestCase):

    def setUp(self):
        model_list._cache.clear()
        self.addCleanup(model_list._cache.clear)

    def _say(self, channel, text, message_id=1):
        run(channel.handle_event(event(text, author_id=ADMIN_ID, username="alice",
                                       is_bot=False, message_id=message_id)))
        return channel._test_sender.last

    def test_antigravity_lists_what_agy_models_prints(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess())):
            reply = self._say(channel, "/model@HermesBot")
        self.assertIn("Models it can switch to", reply)
        self.assertIn("gemini-3.8-flash-medium (Gemini 3.8 Flash (Medium))", reply)

    def test_claude_lists_its_aliases(self):
        channel = make_channel(make_config(adapter={"type": "claude"}))
        reply = self._say(channel, "/model@HermesBot")
        self.assertIn("• sonnet", reply)
        self.assertIn("any claude-* name", reply)

    def test_reasonix_lists_its_providers_models(self):
        channel = make_channel(make_config(adapter={"type": "reasonix"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess(REASONIX_OUTPUT))):
            self.assertIn("• deepseek-v4-pro", self._say(channel, "/model@HermesBot"))

    def test_codex_lists_its_cache(self):
        channel = make_channel(make_config(adapter={"type": "codex"}))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "models_cache.json").write_text(CODEX_CACHE, encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
                reply = self._say(channel, "/model@HermesBot")
        self.assertIn("• gpt-6-luna", reply)
        self.assertNotIn("internal", reply)

    def test_a_cli_that_lists_nothing_says_where_to_look(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess(returncode=1))):
            reply = self._say(channel, "/model@HermesBot")
        self.assertIn("could not be listed", reply)
        self.assertIn("agy models", reply)

    def test_antigravity_switches_by_exact_id(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        calls = []
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess(), calls)):
            reply = self._say(channel, "/model@HermesBot gemini-3.8-flash-medium")
            spaced = self._say(channel, "/model@HermesBot Gemini 3.8 Flash (Medium)",
                               message_id=2)
        self.assertIn("runs gemini-3.8-flash-medium here", reply)
        self.assertIn("one model name", spaced)        # free-form names are refused
        self.assertEqual(channel._models, {"general": "gemini-3.8-flash-medium"})
        self.assertEqual(calls, [])                    # switching never asks the CLI

    def test_other_clis_still_take_one_name(self):
        channel = make_channel(make_config(adapter={"type": "claude"}))
        self.assertIn("one model name", self._say(channel, "/model@HermesBot opus sonnet"))

    def test_a_command_for_another_bot_asks_nothing(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        calls = []
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess(), calls)):
            run(channel.handle_event(event("/model@CodexBot", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
        self.assertEqual(calls, [])


class TestTheBackgroundLookup(unittest.TestCase):
    """#393: a bot that was never sent /model still publishes what it can run.

    The dashboard greys a Model pick out when the card's list is empty, and a card was empty
    until someone asked the bot ``/model`` with no name, so every peer but the one with a
    built-in list showed a dead button. The list is now looked up in the background.
    """

    def setUp(self):
        model_list._cache.clear()
        self.addCleanup(model_list._cache.clear)

    def beat(self, channel):
        """One wake of the background loop, as it runs at start."""
        return run(channel._refresh_models())

    def test_antigravity_publishes_its_models_after_one_beat(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess())):
            self.beat(channel)
        self.assertIn("gemini-3.8-flash-medium (Gemini 3.8 Flash (Medium))",
                      channel.status_snapshot().models)

    def test_codex_publishes_its_models_after_one_beat(self):
        channel = make_channel(make_config(adapter={"type": "codex"}))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "models_cache.json").write_text(CODEX_CACHE, encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
                self.beat(channel)
        self.assertIn("gpt-6-luna", channel.status_snapshot().models)

    def test_reasonix_publishes_its_models_after_one_beat(self):
        channel = make_channel(make_config(adapter={"type": "reasonix"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess(REASONIX_OUTPUT))):
            self.beat(channel)
        self.assertIn("deepseek-v4-pro", channel.status_snapshot().models)

    def test_a_reasonix_bot_whose_doctor_fails_reads_its_own_settings_file(self):
        """The DeepSeek bots run this adapter; its settings file names the providers even
        when the CLI cannot list them."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "reasonix.toml").write_text(
                'default_model = "deepseek-flash"\n\n'
                '[[providers]]\nname = "deepseek-pro"\n', encoding="utf-8")
            channel = make_channel(make_config(adapter={"type": "reasonix", "cwd": tmp}))
            with mock.patch.object(model_list, "_spawn",
                                   spawner(FakeProcess(returncode=1))):
                names = self.beat(channel)
            self.assertEqual(list(channel.status_snapshot().models),
                             ["deepseek-flash", "deepseek-pro"])
        self.assertEqual(names, ["deepseek-flash", "deepseek-pro"])

    def test_a_lookup_that_fails_still_publishes_an_empty_list(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeProcess(returncode=1))):
            self.assertIsNone(self.beat(channel))
        self.assertEqual(channel.status_snapshot().models, ())    # nothing invented

    def test_the_loop_looks_up_once_before_it_sleeps(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        beats = []

        async def one_wake():
            beats.append(1)
            raise asyncio.CancelledError

        channel._refresh_models = one_wake
        with mock.patch.object(asyncio, "sleep", mock.AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                run(channel._models_loop())
        self.assertEqual(beats, [1])


if __name__ == "__main__":
    unittest.main()
