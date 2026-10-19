"""The bare /model lists the models the bot's CLI can run (admin request, 2026-10-19)."""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from swarmly.telegram import model_list
from swarmly.telegram.model_list import (available_models, models_line, parse_agy_models,
                                         parse_codex_cache, parse_reasonix_doctor)
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run

AGY_OUT = ("Fetching available models...\n"
           "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
           "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
           "claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)\n")
CODEX_CACHE = json.dumps({"models": [
    {"slug": "gpt-5.6-luna", "visibility": "list", "priority": 9},
    {"slug": "gpt-reserve", "visibility": "hide", "priority": 4},
    {"slug": "gpt-6-luna", "visibility": "list", "priority": 4}]})
REASONIX_DOCTOR = json.dumps({"providers": [
    {"name": "deepseek", "models": ["deepseek-flash", "deepseek-v4-pro"]}]})


class _Done:
    def __init__(self, out, rc=0):
        self.stdout, self.returncode = out, rc


def runner_for(outputs):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _Done(outputs[argv[0]])
    runner.calls = calls
    return runner


class ParsingTests(unittest.TestCase):
    def test_agy_lines_keep_the_id_first(self):
        self.assertEqual(parse_agy_models(AGY_OUT)[:2],
                         ["gemini-3.8-flash-high (Gemini 3.8 Flash (High))",
                          "gemini-3.8-flash-medium (Gemini 3.8 Flash (Medium))"])

    def test_codex_hides_what_its_picker_hides_and_keeps_its_order(self):
        self.assertEqual(parse_codex_cache(CODEX_CACHE), ["gpt-6-luna", "gpt-5.6-luna"])

    def test_reasonix_lists_every_provider_model(self):
        self.assertEqual(parse_reasonix_doctor(REASONIX_DOCTOR),
                         ["deepseek-flash", "deepseek-v4-pro"])


class FetchTests(unittest.TestCase):
    def setUp(self):
        model_list._cache.clear()

    def test_each_cli_is_asked_its_own_way(self):
        runner = runner_for({"agy": AGY_OUT, "reasonix": REASONIX_DOCTOR})
        self.assertEqual(len(available_models("antigravity", runner=runner)), 3)
        self.assertEqual(available_models("reasonix", runner=runner)[0], "deepseek-flash")
        self.assertEqual(runner.calls, [["agy", "models"], ["reasonix", "doctor", "--json"]])
        with tempfile.TemporaryDirectory() as home:
            Path(home, "models_cache.json").write_text(CODEX_CACHE, encoding="utf-8")
            self.assertEqual(available_models("codex", codex_home=Path(home)),
                             ["gpt-6-luna", "gpt-5.6-luna"])
        self.assertIn("opus", available_models("claude"))

    def test_a_list_is_reused_until_it_expires(self):
        runner = runner_for({"agy": AGY_OUT})
        clock = [0.0]
        available_models("antigravity", runner=runner, clock=lambda: clock[0])
        available_models("antigravity", runner=runner, clock=lambda: clock[0])
        self.assertEqual(len(runner.calls), 1)
        clock[0] += model_list.CACHE_SECONDS + 1
        available_models("antigravity", runner=runner, clock=lambda: clock[0])
        self.assertEqual(len(runner.calls), 2)

    def test_a_cli_that_fails_gives_an_empty_list_and_a_pointer(self):
        def broken(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 20)
        self.assertEqual(available_models("antigravity", runner=broken), [])
        self.assertIn("agy models", models_line("antigravity", []))


class ModelCommandTests(unittest.TestCase):
    def setUp(self):
        model_list._cache.clear()

    def _say(self, channel, text, message_id=1):
        run(channel.handle_event(event(text, author_id=ADMIN_ID, username="alice",
                                       is_bot=False, message_id=message_id)))
        return channel._test_sender.last

    def test_the_bare_command_lists_the_models(self):
        model_list._cache["antigravity"] = (time.monotonic() + 600, parse_agy_models(AGY_OUT))
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        reply = self._say(channel, "/model@HermesBot")
        self.assertIn("its CLI default model", reply)
        self.assertIn("Available: gemini-3.8-flash-high (Gemini 3.8 Flash (High))", reply)

    def test_claude_lists_its_aliases(self):
        channel = make_channel(make_config(adapter={"type": "claude"}))
        reply = self._say(channel, "/model@HermesBot")
        self.assertIn("Available: best, opus, sonnet, haiku, opusplan or any claude-* name",
                      reply)

    def test_an_antigravity_engine_name_may_have_spaces(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        reply = self._say(channel, "/model@HermesBot Gemini 3.8 Flash (Medium)")
        self.assertIn("runs Gemini 3.8 Flash (Medium) here", reply)
        self.assertEqual(channel._models, {"general": "Gemini 3.8 Flash (Medium)"})

    def test_other_clis_still_take_one_word(self):
        channel = make_channel(make_config(adapter={"type": "claude"}))
        self.assertIn("takes one model name", self._say(channel, "/model@HermesBot opus sonnet"))
