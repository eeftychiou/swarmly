"""
Telegram robustness ported from ductor: forum topics, the typing indicator, /stop,
incoming files, and ending the CLI when a turn is cancelled or times out.

A fake bot records what would reach Telegram; no network and no real process.
"""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from swarmly.telegram.adapters import SubprocessAdapter
from swarmly.telegram.channel import ATTACHMENTS_DIRNAME, attachments_from_aiogram
from swarmly.telegram.config import AdapterSettings, ConfigError
from tests.test_telegram_channel import (ADMIN_ID, GROUP, _GatedAdapter, event, make_channel,
                                         make_config, run)


class FakeBot:
    id = 555

    def __init__(self):
        self.sent = []
        self.actions = []
        self.reactions = []
        self.downloads = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=100 + len(self.sent))

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs)

    async def set_message_reaction(self, **kwargs):
        self.reactions.append(kwargs)

    async def download(self, file_id, destination):
        self.downloads.append(file_id)
        Path(destination).write_bytes(b"data for " + file_id.encode())


def bot_channel(config=None, adapter=None):
    channel = make_channel(config)
    channel._sender = None                         # go through the bot, as in production
    channel.bot = FakeBot()
    if adapter is not None:
        channel.adapter = adapter
    return channel


def human(text, **kw):
    return event(text, author_id=ADMIN_ID, username="alice", is_bot=False, **kw)


async def _settle(rounds=10):
    for _ in range(rounds):
        await asyncio.sleep(0)


class TestForumTopics(unittest.TestCase):
    def test_the_reply_goes_to_the_topic_the_message_came_from(self):
        channel = bot_channel()
        run(channel.handle_event(human("@HermesBot hello", thread_id=7)))
        self.assertEqual(channel.bot.sent[-1]["message_thread_id"], 7)

    def test_a_message_outside_topics_is_answered_without_a_thread(self):
        channel = bot_channel()
        run(channel.handle_event(human("@HermesBot hello")))
        self.assertIsNone(channel.bot.sent[-1]["message_thread_id"])

    def test_aiogram_messages_carry_the_topic_only_when_they_are_in_one(self):
        channel = make_channel()
        base = dict(text="@HermesBot hi", caption=None, chat=types.SimpleNamespace(id=GROUP),
                    from_user=types.SimpleNamespace(id=1, username="alice", is_bot=False),
                    message_id=3, reply_to_message=None, message_thread_id=7)
        in_topic = channel.event_from_aiogram(types.SimpleNamespace(is_topic_message=True, **base))
        plain = channel.event_from_aiogram(types.SimpleNamespace(is_topic_message=False, **base))
        self.assertEqual(in_topic.thread_id, 7)
        self.assertIsNone(plain.thread_id)


class TestAttachments(unittest.TestCase):
    def test_the_largest_photo_and_any_document_are_described(self):
        message = types.SimpleNamespace(
            photo=[types.SimpleNamespace(file_id="small", file_size=10),
                   types.SimpleNamespace(file_id="large", file_size=900)],
            document=types.SimpleNamespace(file_id="doc", file_name="log.txt",
                                           mime_type="text/plain", file_size=50),
            voice=None, audio=None, video=None)
        found = attachments_from_aiogram(message)
        self.assertEqual([item["file_id"] for item in found], ["large", "doc"])
        self.assertEqual(found[1]["name"], "log.txt")

    def test_files_are_downloaded_into_the_agents_folder_and_named_in_the_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = bot_channel(make_config(adapter={"type": "echo", "cwd": tmp}))
            files = [{"file_id": "doc", "name": "../../build log.txt", "mime": "text/plain",
                      "size": 4096}]
            run(channel.handle_event(human("@HermesBot what failed?", message_id=9,
                                           attachments=files)))
            # Resolved like the channel resolves it: Windows temp dirs can be 8.3 short names.
            folder = Path(tmp).resolve() / ATTACHMENTS_DIRNAME
            saved = list(folder.glob("9-*"))
            self.assertEqual(len(saved), 1, "saved inside the folder, path tricks removed")
            self.assertEqual(saved[0].read_bytes(), b"data for doc")
            self.assertEqual((folder / ".gitignore").read_text(), "*\n")
            reply = channel.bot.sent[-1]["text"]
            self.assertIn(str(saved[0]), reply)
            self.assertIn("text/plain, 4 KB", reply)

    def test_files_over_the_limit_are_listed_but_not_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = bot_channel(make_config(adapter={"type": "echo", "cwd": tmp},
                                              limits={"max_attachment_mb": 1}))
            files = [{"file_id": "big", "name": "dump.bin", "size": 5 * 1024 * 1024}]
            run(channel.handle_event(human("@HermesBot look", attachments=files)))
            self.assertEqual(channel.bot.downloads, [])
            self.assertIn("not downloaded: dump.bin is 5.0 MB", channel.bot.sent[-1]["text"])

    def test_an_unaddressed_file_is_not_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = bot_channel(make_config(adapter={"type": "echo", "cwd": tmp}))
            run(channel.handle_event(event("just a photo", attachments=[
                {"file_id": "p", "name": "photo.jpg", "size": 10}])))
            self.assertEqual(channel.bot.downloads, [])

    def test_the_size_limit_is_validated(self):
        for bad in (0, -1, "lots"):
            with self.assertRaises(ConfigError, msg=bad):
                make_config(limits={"max_attachment_mb": bad})


class TestTypingAndStop(unittest.TestCase):
    def hold(self, channel, adapter, during):
        """Start a turn, run ``during`` while it is held open, then release it."""

        async def scenario():
            turn = asyncio.create_task(channel.handle_event(
                human("@HermesBot long job", message_id=1, thread_id=4)))
            await adapter.entered.wait()
            await _settle()
            extra = await during(channel)
            adapter.release.set()
            return await turn, extra

        return run(scenario())

    def test_typing_shows_in_the_topic_while_a_turn_runs(self):
        adapter = _GatedAdapter("Hermes")
        channel = bot_channel(adapter=adapter)

        async def during(ch):
            return list(ch.bot.actions)

        _, actions = self.hold(channel, adapter, during)
        self.assertEqual(actions[0], {"chat_id": GROUP, "action": "typing",
                                      "message_thread_id": 4})

    def test_typing_can_be_turned_off(self):
        adapter = _GatedAdapter("Hermes")
        channel = bot_channel(make_config(limits={"typing_indicator": False}), adapter)

        async def during(ch):
            return list(ch.bot.actions)

        _, actions = self.hold(channel, adapter, during)
        self.assertEqual(actions, [])

    def test_a_persons_message_gets_an_eyes_reaction_when_its_turn_starts(self):
        adapter = _GatedAdapter("Hermes")
        channel = bot_channel(adapter=adapter)

        async def during(ch):
            return list(ch.bot.reactions)

        _, reactions = self.hold(channel, adapter, during)
        self.assertEqual(len(reactions), 1)
        self.assertEqual((reactions[0]["chat_id"], reactions[0]["message_id"]), (GROUP, 1))
        self.assertEqual(reactions[0]["reaction"][0].emoji, "👀")
        quiet = bot_channel(make_config(limits={"ack_reaction": False}), _GatedAdapter("Hermes"))
        _, reactions = self.hold(quiet, quiet.adapter, during)
        self.assertEqual(reactions, [])

    def test_a_passing_provider_error_is_retried_before_failing(self):
        from swarmly.telegram.adapters import AdapterReply, EchoAdapter

        class Flaky(EchoAdapter):
            def __init__(self, errors):
                super().__init__("Hermes")
                self.errors, self.calls = list(errors), 0

            async def respond(self, message):
                self.calls += 1
                if self.errors:
                    return AdapterReply(ok=False, error=self.errors.pop(0))
                return await super().respond(message)

        capacity = "Codex failed: Selected model is at capacity. Please try a different model."
        flaky = Flaky([capacity])
        channel = bot_channel(adapter=flaky)
        channel._transient_retry_delays = (0, 0)
        run(channel.handle_event(human("@HermesBot job", message_id=1)))
        self.assertEqual(flaky.calls, 2)
        self.assertFalse(any("❌" in sent["text"] for sent in channel.bot.sent))

        stuck = Flaky([capacity] * 5)                      # gives up after the retries
        channel = bot_channel(adapter=stuck)
        channel._transient_retry_delays = (0, 0)
        run(channel.handle_event(human("@HermesBot job", message_id=1)))
        self.assertEqual(stuck.calls, 3)

        quota = Flaky(["429 usage limit reached"])         # not retried: it lasts hours
        channel = bot_channel(adapter=quota)
        channel._transient_retry_delays = (0, 0)
        run(channel.handle_event(human("@HermesBot job", message_id=1)))
        self.assertEqual(quota.calls, 1)

    def test_pause_lets_the_running_turn_finish_then_holds_until_resume(self):
        adapter = _GatedAdapter("Hermes")
        channel = bot_channel(adapter=adapter)

        async def during(ch):
            await ch.handle_event(human("/pause", message_id=2))
            await ch.handle_event(human("@HermesBot new work", message_id=3))
            return [sent["text"] for sent in ch.bot.sent]

        _, early = self.hold(channel, adapter, during)
        run(_settle())
        texts = [sent["text"] for sent in channel.bot.sent]
        self.assertIn("pausing", early[-1])
        self.assertEqual(adapter.calls, 1, "no new turn while pausing")
        self.assertTrue(any("paused. /resume" in t for t in texts), texts)
        self.assertTrue(channel._frozen)
        run(channel.handle_event(human("@HermesBot still there?", message_id=4)))
        self.assertEqual(adapter.calls, 1, "held while paused")
        run(channel.handle_event(human("/resume", message_id=5)))
        self.assertFalse(channel._frozen)
        adapter.release.set()
        run(channel.handle_event(human("@HermesBot go on", message_id=6)))
        self.assertEqual(adapter.calls, 2)

    def test_only_the_admin_pauses_and_a_bare_pause_from_others_is_silent(self):
        channel = bot_channel()
        run(channel.handle_event(event("/pause", author_id=999, username="bob",
                                       is_bot=False)))
        self.assertFalse(channel._frozen)
        self.assertEqual(channel.bot.sent, [])
        run(channel.handle_event(human("/pause@OtherBot")))
        self.assertFalse(channel._frozen)
        run(channel.handle_event(human("/pause@HermesBot")))
        self.assertTrue(channel._frozen)

    def test_admin_stop_cancels_the_turn_and_clears_the_queue(self):
        adapter = _GatedAdapter("Hermes")
        channel = bot_channel(adapter=adapter)
        queued = []

        async def during(ch):
            queued.append(asyncio.create_task(ch.handle_event(
                human("@HermesBot and another thing", message_id=2))))
            await _settle()
            await ch.handle_event(human("/stop@HermesBot", message_id=3))
            return await queued[0]

        turn_result, queued_result = self.hold(channel, adapter, during)
        self.assertIsNone(turn_result, "a stopped turn posts no reply")
        self.assertIsNone(queued_result, "a message queued before /stop is dropped")
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(channel.stats.stopped, 1)
        texts = [sent["text"] for sent in channel.bot.sent]
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("stopped the running turn and cleared 1 queued message(s)", texts[0])

    def test_a_message_after_stop_still_runs(self):
        adapter = _GatedAdapter("Hermes")
        channel = bot_channel(adapter=adapter)

        async def during(ch):
            await ch.handle_event(human("/stop@HermesBot", message_id=3))

        self.hold(channel, adapter, during)
        run(channel.handle_event(human("@HermesBot next job", message_id=4)))
        self.assertEqual(adapter.calls, 2)
        self.assertIn("next job", channel.bot.sent[-1]["text"])

    def test_only_the_admin_can_stop_a_turn(self):
        adapter = _GatedAdapter("Hermes")
        channel = bot_channel(make_config(agent={"allowed_users": [77]}), adapter=adapter)

        async def during(ch):
            await ch.handle_event(event("/stop@HermesBot", author_id=77, username="someone",
                                        is_bot=False, message_id=5))
            await ch.handle_event(event("/stop@HermesBot", message_id=6))        # peer bot
            return ch._current_turn is not None and not ch._current_turn.done()

        result, still_running = self.hold(channel, adapter, during)
        self.assertTrue(still_running)
        self.assertIsNotNone(result, "the turn finished and replied")
        texts = [sent["text"] for sent in channel.bot.sent]
        self.assertTrue(any("only the admin can stop" in text for text in texts), texts)
        self.assertEqual(channel.stats.stopped, 0)

    def test_stop_with_nothing_running_says_so(self):
        channel = bot_channel()
        run(channel.handle_event(human("/stop@HermesBot")))
        self.assertIn("nothing was running", channel.bot.sent[-1]["text"])

    def test_stop_for_another_bot_is_ignored(self):
        channel = bot_channel()
        run(channel.handle_event(human("/stop@CodexBot")))
        self.assertEqual(channel.bot.sent, [])

    def test_a_bare_stop_in_the_admins_private_chat_is_for_this_bot(self):
        """Like every other command there: a private chat holds one bot, so a bare /stop can
        only mean it (it used to be ignored there)."""
        channel = bot_channel()
        run(channel.handle_event(human("/stop", chat_id=ADMIN_ID)))
        self.assertIn("nothing was running", channel.bot.sent[-1]["text"])


class HangingProcess:
    def __init__(self):
        self.returncode = None
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(3600)

    def kill(self):
        self.killed = True
        self.returncode = -9


class TestProcessIsEnded(unittest.TestCase):
    def adapter(self, process, timeout=900):
        async def runner(argv, cwd, env, timeout):
            return process

        settings = AdapterSettings(type="custom", command="tool {prompt}",
                                   timeout_seconds=timeout)
        return SubprocessAdapter("Hermes", settings, runner=runner)

    def test_a_cancelled_turn_kills_the_cli(self):
        process = HangingProcess()
        adapter = self.adapter(process)

        async def scenario():
            from swarmly.telegram.adapters.base import IncomingMessage
            task = asyncio.create_task(adapter.respond(IncomingMessage(text="go", author_alias="Codex")))
            await _settle()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        run(scenario())
        self.assertTrue(process.killed)

    def test_a_timed_out_turn_kills_the_cli(self):
        process = HangingProcess()
        adapter = self.adapter(process, timeout=0)
        from swarmly.telegram.adapters.base import IncomingMessage
        reply = run(adapter.respond(IncomingMessage(text="go", author_alias="Codex")))
        self.assertFalse(reply.ok)
        self.assertIn("timed out", reply.error)
        self.assertTrue(process.killed)


from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)
