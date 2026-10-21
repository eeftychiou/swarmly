"""`/cli@bot` on the channel: the admin's native command, answered with no model turn (#116)."""

import asyncio
import sys
import unittest
from unittest import mock

from swarmly.telegram import native as nv
from swarmly.telegram.adapters import EchoAdapter
from tests.test_telegram_channel import (ADMIN_ID, event, make_channel, make_config, run)


class CountingAdapter(EchoAdapter):
    """An adapter that records every prompt it is given — a passthrough must give it none."""

    def __init__(self, alias="Hermes"):
        super().__init__(alias)
        self.turns = []

    async def respond(self, message):
        self.turns.append(message.text)
        return await super().respond(message)


def channel(**overrides):
    """A claude-typed bot whose binary exists (the test's own interpreter), plus its adapter."""
    adapter = CountingAdapter()
    config = make_config(adapter={"type": "claude", "command": f"{sys.executable} {{prompt}}"},
                         **overrides)
    made = make_channel(config, adapter=adapter)
    made.bot = mock.Mock(id=555)                    # a bot with an id, and an editor
    made.bot.edit_message_text = mock.AsyncMock()
    recorder = made._test_sender

    async def send(payload, reply_to_message_id=None):
        """A Telegram send: it answers with a message, which is what an edit needs."""
        recorder.sent.append({"text": payload, "reply_to": reply_to_message_id})
        return mock.Mock(message_id=4000 + len(recorder.sent))

    made._sender = send
    return made, adapter


def fake_result(stdout="ok\n", stderr="", returncode=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


class TestTheAdminGate(unittest.TestCase):

    def test_a_peer_who_is_not_the_admin_is_refused(self):
        made, adapter = channel()
        run(made.handle_event(event("/cli@HermesBot --version", author_id=202,
                                    username="CodexBot")))
        self.assertIn("admin's", made._test_sender.last)
        self.assertEqual(adapter.turns, [])

    def test_a_command_for_another_bot_is_not_answered(self):
        made, _ = channel()
        run(made.handle_event(event("/cli@CodexBot --version")))
        self.assertEqual(made._test_sender.sent, [])


class TestARun(unittest.TestCase):
    """The admin's read-only command: it runs, its output streams, and no turn happens."""

    def test_a_one_shot_command_is_run_and_its_output_edited_in(self):
        made, adapter = channel()
        with mock.patch("swarmly.telegram.native.subprocess.run",
                        return_value=fake_result("claude 2.0.1\n")):
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertEqual(adapter.turns, [])                     # no model turn, no tokens
        self.assertIn("running one-shot, no model turn", made._test_sender.last)
        edited = made.bot.edit_message_text.await_args.kwargs
        self.assertIn("/cli --version: ok", edited["text"])
        self.assertIn("claude 2.0.1", edited["text"])

    def test_the_argv_is_the_cli_and_the_command(self):
        made, _ = channel()
        with mock.patch("swarmly.telegram.native.subprocess.run",
                        return_value=fake_result()) as runner:
            run(made.handle_event(event("/cli@HermesBot mcp list", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        argv = runner.call_args.args[0]
        self.assertEqual(argv, [sys.executable, "mcp", "list"])
        self.assertFalse(runner.call_args.kwargs["shell"])

    def test_a_failure_keeps_its_exit_code(self):
        made, _ = channel()
        with mock.patch("swarmly.telegram.native.subprocess.run",
                        return_value=fake_result(stdout="", stderr="nope", returncode=2)):
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertIn("failed (exit 2)", made.bot.edit_message_text.await_args.kwargs["text"])

    def test_a_refusal_is_explained_and_runs_nothing(self):
        made, adapter = channel()
        with mock.patch("swarmly.telegram.native.subprocess.run") as runner:
            run(made.handle_event(event("/cli@HermesBot /compact", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertEqual(runner.call_args_list, [])
        self.assertIn("costs tokens", made._test_sender.last)
        self.assertEqual(adapter.turns, [])

    def test_an_unknown_command_is_refused(self):
        made, _ = channel()
        run(made.handle_event(event("/cli@HermesBot /frobnicate", author_id=ADMIN_ID,
                                    username="alice", is_bot=False)))
        self.assertIn("not on the allowlist", made._test_sender.last)

    def test_a_command_that_changes_something_asks_first(self):
        made, _ = channel()
        with mock.patch("swarmly.telegram.native.subprocess.run") as runner:
            run(made.handle_event(event("/cli@HermesBot /clear", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertEqual(runner.call_args_list, [])
        self.assertIn("/cli!", made._test_sender.last)

    def test_a_turn_that_is_running_refuses_the_passthrough(self):
        made, _ = channel()
        lock = made._lock_for(event("@HermesBot hi", thread_id=7))
        with mock.patch("swarmly.telegram.native.subprocess.run") as runner:
            run(_hold(lock, made))
        self.assertEqual(runner.call_args_list, [])
        self.assertIn("a turn is running in this topic", made._test_sender.last)


async def _hold(lock, made):
    async with lock:
        await made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                      username="alice", is_bot=False, thread_id=7))


class TestOutputHandling(unittest.TestCase):

    def test_the_output_is_redacted_and_stripped_before_it_is_posted(self):
        made, _ = channel()
        with mock.patch("swarmly.telegram.native.subprocess.run",
                        return_value=fake_result("\x1b[31mred\x1b[0m ok\n")):
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        text = made.bot.edit_message_text.await_args.kwargs["text"]
        self.assertNotIn("\x1b", text)
        self.assertIn("red ok", text)

    def test_a_message_that_cannot_be_edited_is_posted_instead(self):
        made, _ = channel()
        made.bot.edit_message_text = mock.AsyncMock(side_effect=RuntimeError("too old"))
        with mock.patch("swarmly.telegram.native.subprocess.run",
                        return_value=fake_result("v1\n")):
            with self.assertLogs("swarmly.telegram", "WARNING"):
                run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                            username="alice", is_bot=False)))
        self.assertIn("v1", made._test_sender.last)


class TestFrozenAndStop(unittest.TestCase):

    def test_a_frozen_bot_still_answers_a_passthrough(self):
        made, _ = channel()
        made._frozen = True
        with mock.patch("swarmly.telegram.native.subprocess.run",
                        return_value=fake_result("v2\n")):
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertIn("v2", made.bot.edit_message_text.await_args.kwargs["text"])

    def test_stop_kills_the_run_in_flight(self):
        made, _ = channel()
        runner = mock.Mock()
        runner.stop.return_value = True
        made._native_runners[(made.config.identity.home_channel_id, None)] = runner
        run(made.handle_event(event("/stop@HermesBot", author_id=ADMIN_ID, username="alice",
                                    is_bot=False)))
        runner.stop.assert_called_once()
        self.assertIn("killed the /cli run", made._test_sender.last)

    def test_stop_with_nothing_running_says_so(self):
        made, _ = channel()
        run(made.handle_event(event("/stop@HermesBot", author_id=ADMIN_ID, username="alice",
                                    is_bot=False)))
        self.assertIn("nothing was running", made._test_sender.last)


class TestNoCli(unittest.TestCase):
    """An in-process bot (echo) has nothing to pass a command to."""

    def test_an_adapter_without_a_cli_refuses(self):
        adapter = CountingAdapter()
        made = make_channel(make_config(), adapter=adapter)
        with mock.patch("swarmly.telegram.native.subprocess.run") as runner:
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertEqual(runner.call_args_list, [])
        self.assertIn("no CLI", made._test_sender.last)


class TestParseRequests(unittest.TestCase):

    def test_the_bang_and_the_target_are_read_from_the_command_word(self):
        self.assertEqual(nv.parse_request("/cli@HermesBot --version"), (False, "--version"))
        self.assertEqual(nv.parse_request("/cli!@HermesBot /clear"), (True, "/clear"))
        self.assertIsNone(nv.parse_request("/cleanup"))


if __name__ == "__main__":
    unittest.main()
