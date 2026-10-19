"""/model with no name lists the models the bot's CLI can switch to."""

import asyncio
import json
import unittest
from unittest import mock

from swarmly.telegram import model_list
from swarmly.telegram.model_list import (available_models, parse_agy_models,
                                         parse_codex_models)
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run

AGY_OUTPUT = b"Gemini 3.8 Flash (Medium)\nClaude Sonnet 5.5 (High)\n\nGPT-OSS 120B (Medium)\n"
CODEX_OUTPUT = (json.dumps({"id": 1, "result": {}}) + "\n" + json.dumps(
    {"id": 2, "result": {"data": [{"id": "gpt-5.5-codex"}, {"id": "gpt-5.5"}, {"x": 1}]}})
    + "\n").encode()


class FakeAgy:
    def __init__(self, stdout=AGY_OUTPUT, returncode=0):
        self._stdout, self.returncode = stdout, returncode

    async def communicate(self):
        return self._stdout, b""


class FakeStream:
    def __init__(self, data=b""):
        self.lines = data.splitlines(keepends=True)
        self.written = b""

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def write(self, data):
        self.written += data

    async def drain(self):
        return None

    def close(self):
        return None


class FakeCodex:
    def __init__(self, stdout=CODEX_OUTPUT):
        self.stdin, self.stdout, self.returncode = FakeStream(), FakeStream(stdout), None

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def spawner(process, calls=None):
    async def spawn(argv, env, stdin):
        if calls is not None:
            calls.append(list(argv))
        return process
    return spawn


class TestDiscovery(unittest.TestCase):

    def setUp(self):
        model_list._cache.clear()
        self.addCleanup(model_list._cache.clear)

    def test_agy_lists_one_engine_per_line(self):
        names = run(available_models("antigravity", spawn=spawner(FakeAgy())))
        self.assertEqual(names, ["Gemini 3.8 Flash (Medium)", "Claude Sonnet 5.5 (High)",
                                 "GPT-OSS 120B (Medium)"])

    def test_codex_lists_the_ids_of_its_model_list(self):
        process = FakeCodex()
        names = run(available_models("codex", spawn=spawner(process)))
        self.assertEqual(names, ["gpt-5.5-codex", "gpt-5.5"])
        self.assertIn(b'"model/list"', process.stdin.written)

    def test_claude_needs_no_cli(self):
        self.assertIn("opus", run(available_models("claude", spawn=None)))

    def test_reasonix_has_no_list(self):
        self.assertIsNone(run(available_models("reasonix")))

    def test_a_list_is_reused_and_a_failure_is_not_kept_long(self):
        calls = []
        clock = mock.Mock(return_value=0.0)
        run(available_models("antigravity", spawn=spawner(FakeAgy(), calls), clock=clock))
        run(available_models("antigravity", spawn=spawner(FakeAgy(), calls), clock=clock))
        self.assertEqual(len(calls), 1)
        model_list._cache.clear()
        failing = FakeAgy(returncode=1)
        self.assertIsNone(run(available_models("antigravity", spawn=spawner(failing, calls),
                                               clock=clock)))
        clock.return_value = model_list.FAILED_TTL_SECONDS + 1
        run(available_models("antigravity", spawn=spawner(FakeAgy(), calls), clock=clock))
        self.assertEqual(len(calls), 3)

    def test_a_missing_cli_is_no_list(self):
        async def missing(argv, env, stdin):
            raise FileNotFoundError(argv[0])
        self.assertIsNone(run(available_models("antigravity", spawn=missing)))

    def test_parsers_refuse_noise(self):
        self.assertEqual(parse_agy_models("Usage: agy models\nFlags:"), [])
        self.assertEqual(parse_codex_models("not json\n{\"id\": 3}"), [])


class TestModelCommandLists(unittest.TestCase):

    def setUp(self):
        model_list._cache.clear()
        self.addCleanup(model_list._cache.clear)

    def _say(self, channel, text):
        run(channel.handle_event(event(text, author_id=ADMIN_ID, username="alice",
                                       is_bot=False)))
        return channel._test_sender.last

    def test_antigravity_lists_what_agy_models_prints(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeAgy())):
            reply = self._say(channel, "/model@HermesBot")
        self.assertIn("Models it can switch to", reply)
        self.assertIn("Gemini 3.8 Flash (Medium)", reply)

    def test_claude_lists_its_aliases(self):
        channel = make_channel(make_config(adapter={"type": "claude"}))
        reply = self._say(channel, "/model@HermesBot")
        self.assertIn("• sonnet", reply)

    def test_reasonix_points_at_its_toml(self):
        channel = make_channel(make_config(adapter={"type": "reasonix"}))
        self.assertIn("reasonix.toml", self._say(channel, "/model@HermesBot"))

    def test_a_cli_that_lists_nothing_says_so(self):
        channel = make_channel(make_config(adapter={"type": "codex"}))
        with mock.patch.object(model_list, "_spawn", spawner(FakeCodex(stdout=b""))):
            reply = self._say(channel, "/model@HermesBot")
        self.assertIn("did not list its models", reply)

    def test_switching_does_not_ask_the_cli(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        calls = []
        with mock.patch.object(model_list, "_spawn", spawner(FakeAgy(), calls)):
            self._say(channel, "/model@HermesBot Gemini 3.8 Flash (Medium)")
        self.assertEqual(calls, [])

    def test_a_command_for_another_bot_asks_nothing(self):
        channel = make_channel(make_config(adapter={"type": "antigravity"}))
        calls = []
        with mock.patch.object(model_list, "_spawn", spawner(FakeAgy(), calls)):
            run(channel.handle_event(event("/model@CodexBot", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
