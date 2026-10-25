"""`/cli@bot` on the channel: the admin's native command, answered with no model turn (#116)."""

import asyncio
import sys
import threading
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


class FakeProcess:
    """A one-shot CLI process: it answers, or hangs until something ends it."""

    def __init__(self, stdout="ok\n", stderr="", returncode=0, *, hang=None):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.hang = hang                     # an Event a hanging process waits on
        self.terminated = False

    def communicate(self, timeout=None):
        if self.hang is not None and not self.terminated:
            self.hang.wait(10)
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode if self.terminated else None

    def terminate(self):
        self.terminated = True
        if self.hang is not None:
            self.hang.set()

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def fake_proc(**kwargs):
    """A ``subprocess.Popen`` stand-in the tests can also inspect."""
    return mock.Mock(return_value=FakeProcess(**kwargs))


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

    def test_a_longer_command_is_not_cli(self):
        """`/clinic` is not `/cli nic`: a passthrough must not answer a different command."""
        made, _ = channel()
        with mock.patch("swarmly.telegram.native.subprocess.Popen") as spawned:
            run(made.handle_event(event("/clinic@HermesBot", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertEqual(spawned.call_args_list, [])
        # It is not a passthrough: whatever else happens, no CLI command is run for it.
        self.assertFalse([row["text"] for row in made._test_sender.sent
                          if "running one-shot" in row["text"]])


class TestARun(unittest.TestCase):
    """The admin's read-only command: it runs, its output streams, and no turn happens."""

    def test_a_one_shot_command_is_run_and_its_output_edited_in(self):
        made, adapter = channel()
        with mock.patch("swarmly.telegram.native.subprocess.Popen",
                        fake_proc(stdout="claude 2.0.1\n")):
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertEqual(adapter.turns, [])                     # no model turn, no tokens
        self.assertIn("running one-shot, no model turn", made._test_sender.last)
        edited = made.bot.edit_message_text.await_args.kwargs
        self.assertIn("/cli --version: ok", edited["text"])
        self.assertIn("claude 2.0.1", edited["text"])

    def test_the_argv_is_the_cli_and_the_command(self):
        made, _ = channel()
        with mock.patch("swarmly.telegram.native.subprocess.Popen",
                        fake_proc()) as runner:
            run(made.handle_event(event("/cli@HermesBot mcp list", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        argv = runner.call_args.args[0]
        self.assertEqual(argv, [sys.executable, "mcp", "list"])
        self.assertFalse(runner.call_args.kwargs["shell"])

    def test_a_failure_keeps_its_exit_code(self):
        made, _ = channel()
        with mock.patch("swarmly.telegram.native.subprocess.Popen",
                        fake_proc(stdout="", stderr="nope", returncode=2)):
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertIn("failed (exit 2)", made.bot.edit_message_text.await_args.kwargs["text"])

    def test_a_refusal_is_explained_and_runs_nothing(self):
        made, adapter = channel()
        with mock.patch("swarmly.telegram.native.subprocess.Popen") as runner:
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
        with mock.patch("swarmly.telegram.native.subprocess.Popen") as runner:
            run(made.handle_event(event("/cli@HermesBot /clear", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        self.assertEqual(runner.call_args_list, [])
        self.assertIn("/cli!", made._test_sender.last)

    def test_a_turn_that_is_running_refuses_the_passthrough(self):
        made, _ = channel()
        lock = made._lock_for(event("@HermesBot hi", thread_id=7))
        with mock.patch("swarmly.telegram.native.subprocess.Popen") as runner:
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
        with mock.patch("swarmly.telegram.native.subprocess.Popen",
                        fake_proc(stdout="\x1b[31mred\x1b[0m ok\n")):
            run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                        username="alice", is_bot=False)))
        text = made.bot.edit_message_text.await_args.kwargs["text"]
        self.assertNotIn("\x1b", text)
        self.assertIn("red ok", text)

    def test_a_message_that_cannot_be_edited_is_posted_instead(self):
        made, _ = channel()
        made.bot.edit_message_text = mock.AsyncMock(side_effect=RuntimeError("too old"))
        with mock.patch("swarmly.telegram.native.subprocess.Popen",
                        fake_proc(stdout="v1\n")):
            with self.assertLogs("swarmly.telegram", "WARNING"):
                run(made.handle_event(event("/cli@HermesBot --version", author_id=ADMIN_ID,
                                            username="alice", is_bot=False)))
        self.assertIn("v1", made._test_sender.last)


class TestStopAndTheLock(unittest.TestCase):
    """#121 fix-first: `/stop` reaches the process, and the topic's lock covers the whole run."""

    def test_stop_kills_a_one_shot_passthrough(self):
        made, _ = channel()
        started = []

        def spawn(argv, **options):
            process = FakeProcess(stdout="working\n", hang=threading.Event())
            started.append(process)
            return process

        async def scenario():
            with mock.patch("swarmly.telegram.native.subprocess.Popen", spawn):
                run_in_background = asyncio.ensure_future(made.handle_event(
                    event("/cli@HermesBot --version", author_id=ADMIN_ID, username="alice",
                          is_bot=False, message_id=1)))
                for _ in range(200):
                    if started and started[0].terminated is False and made._native_runners:
                        break
                    await asyncio.sleep(0.01)
                stop = await made.handle_event(event("/stop@HermesBot", author_id=ADMIN_ID,
                                                     username="alice", is_bot=False,
                                                     message_id=2))
                await run_in_background
                return stop

        asyncio.run(scenario())
        self.assertTrue(started and started[0].terminated)
        self.assertIn("stopped on request", made.bot.edit_message_text.await_args.kwargs["text"])

    def test_the_topic_lock_is_held_for_the_whole_run(self):
        """A turn must not start mid-passthrough: both would drive the same CLI session."""
        made, _ = channel()
        release = threading.Event()

        def spawn(argv, **options):
            process = FakeProcess(stdout="slow\n", hang=release)
            return process

        async def scenario():
            with mock.patch("swarmly.telegram.native.subprocess.Popen", spawn):
                task = asyncio.ensure_future(made.handle_event(
                    event("/cli@HermesBot --version", author_id=ADMIN_ID, username="alice",
                          is_bot=False, message_id=1)))
                for _ in range(200):
                    if made._native_runners:
                        break
                    await asyncio.sleep(0.01)
                held = made._lock_for(event("x")).locked()
                release.set()
                await task
                return held

        self.assertTrue(asyncio.run(scenario()))
        self.assertFalse(made._lock_for(event("x")).locked())     # and released afterwards


class TestASlashThatNeverFinishes(unittest.TestCase):
    """#121 review: a session that hits its deadline is a timeout, not an answer."""

    def session(self, *, expired):
        class Session(FakeProcess):
            pass

        made_sessions = []

        class FakeTerminal:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.expired = expired
                self.sent = []
                made_sessions.append(self)

            def open(self):
                return self

            def send(self, line):
                self.sent.append(line)

            def read_until_quiet(self, *, deadline=None):
                return "half an answer"

            def close(self):
                self.closed = True

            text = lambda self: "half an answer"            # noqa: E731 - a stand-in

        return FakeTerminal, made_sessions

    def test_a_deadline_is_reported_as_killed_not_ok(self):
        made, sessions = self.session(expired=True)
        bot, _ = channel()
        with mock.patch("swarmly.telegram.native.InteractiveSession", made):
            run(bot.handle_event(event("/cli@HermesBot /cost", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        self.assertEqual(sessions[0].sent, ["/cost"])
        text = bot.bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("killed after", text)
        self.assertNotIn(": ok", text)
        self.assertIn("half an answer", text)          # a cut-off screen is still shown

    def test_a_session_that_finishes_is_still_ok(self):
        made, sessions = self.session(expired=False)
        bot, _ = channel()
        with mock.patch("swarmly.telegram.native.InteractiveSession", made):
            run(bot.handle_event(event("/cli@HermesBot /cost", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        self.assertIn(": ok", bot.bot.edit_message_text.await_args.kwargs["text"])


class TestStopDuringASlashRead(unittest.TestCase):
    """A `/stop` while a slash command is reading: the run ends as stopped, not as ok."""

    def test_stop_during_the_read_reports_stopped(self):
        bot, _ = channel()
        release = threading.Event()
        sessions = []

        class Terminal:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.sent = []
                self.closed = False
                sessions.append(self)

            def open(self):
                return self

            def send(self, line):
                self.sent.append(line)

            def read_until_quiet(self, *, deadline=None):
                release.wait(10)                    # the command is still on screen
                return "/cost: $0.02 so far"

            def close(self):
                self.closed = True
                release.set()

            def text(self):
                return "/cost: $0.02 so far"

        async def scenario():
            with mock.patch("swarmly.telegram.native.InteractiveSession", Terminal):
                running = asyncio.ensure_future(bot.handle_event(
                    event("/cli@HermesBot /cost", author_id=ADMIN_ID, username="alice",
                          is_bot=False, message_id=1)))
                for _ in range(200):
                    if sessions and sessions[0].sent:
                        break
                    await asyncio.sleep(0.01)
                await bot.handle_event(event("/stop@HermesBot", author_id=ADMIN_ID,
                                             username="alice", is_bot=False, message_id=2))
                await running

        asyncio.run(scenario())
        self.assertTrue(sessions[0].closed)
        text = bot.bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("stopped on request", text)
        self.assertNotIn(": ok", text)


class TestFrozenAndStop(unittest.TestCase):

    def test_a_frozen_bot_still_answers_a_passthrough(self):
        made, _ = channel()
        made._frozen = True
        with mock.patch("swarmly.telegram.native.subprocess.Popen",
                        fake_proc(stdout="v2\n")):
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
        with mock.patch("swarmly.telegram.native.subprocess.Popen") as runner:
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
