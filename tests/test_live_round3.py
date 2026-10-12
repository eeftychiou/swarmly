"""Findings of the build 9 live test where Codex was offline during a hand-off."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run


class TestGeneralCooldownIgnoresTopics(unittest.TestCase):

    def test_a_topic_turn_does_not_hold_off_general(self):
        config = make_config(limits={"min_seconds_between_agent_turns": 60})
        channel = make_channel(config)
        run(channel.handle_event(event("@HermesBot work here", author_id=ADMIN_ID,
                                       username="alice", is_bot=False, thread_id=7)))
        ok, why = channel.should_respond(event("@HermesBot please look", message_id=2))
        self.assertTrue(ok, why)

    def test_a_general_turn_still_does(self):
        config = make_config(limits={"min_seconds_between_agent_turns": 60})
        channel = make_channel(config)
        run(channel.handle_event(event("@HermesBot hi", author_id=ADMIN_ID, username="alice",
                                       is_bot=False)))
        ok, why = channel.should_respond(event("ok then", message_id=2))
        self.assertFalse(ok)
        self.assertIn("cooldown", why)


class TestRelayedHandoff(unittest.TestCase):
    """make_channel's bot is @HermesBot; its peers include @CodexBot and @AntigravityBot."""

    def check(self, text):
        return make_channel().should_respond(event(text, username="CodexBot"))

    def test_asking_a_peer_to_hand_this_bot_work_does_not_address_it(self):
        for text in ("@AntigravityBot please hand @HermesBot a task: run the suite on main",
                     "@AntigravityBot, ask @HermesBot to run the full suite",
                     "[Codex] @AntigravityBot could you delegate this to @HermesBot?"):
            ok, why = self.check(text)
            self.assertFalse(ok, text)
            self.assertIn("hand this bot", why)

    def test_a_direct_request_still_addresses_it(self):
        for text in ("@HermesBot please run the suite", "@AntigravityBot and @HermesBot: go",
                     "@AntigravityBot reviews.\n- @HermesBot runs pytest",
                     "@AntigravityBot thanks. @HermesBot can you QA #8?"):
            ok, why = self.check(text)
            self.assertTrue(ok, f"{text}: {why}")


class TestHandledIdsSurviveARestart(unittest.TestCase):

    def test_a_restarted_bot_still_knows_what_it_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(bus={"repo": tmp})
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            run(channel.handle_event(event("@HermesBot QA #8", message_id=327)))
            again = make_channel(config)
            chat = next(iter(again._tg_admitted))[0]
            self.assertTrue(again.took_telegram_copy({"chat": chat, "message_id": 327}))
            self.assertFalse(again.took_telegram_copy({"chat": chat, "message_id": 328}))


class TestMovedHandoffIsMirrored(unittest.TestCase):

    def test_a_reply_moved_to_a_known_topic_is_mirrored(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = make_channel(make_config(bus={"repo": tmp}))
            channel.bot = object()
            posted = []

            async def fake_post(_channel, **kw):
                posted.append(kw)

            async def scenario():
                await channel._post_in_known_topic(
                    event("hi", username="DeepSeekBot"), "@CodexBot please run the full suite on main",
                    (-100, 307))
                await asyncio.gather(*list(channel._mirror_tasks))
            with mock.patch("swarmly.telegram.relay.post_and_notify", fake_post):
                run(scenario())
        self.assertEqual([p["to"] for p in posted], ["Codex"])
        self.assertEqual(posted[0]["extra"]["telegram"]["thread"], 307)


class TestTopicRootIsNotAReply(unittest.TestCase):
    """Telegram makes every top-level post in a forum topic a reply to the topic's opening
    service message, whose author is the bot that opened the topic."""

    def _message(self, reply_to, thread=307, text="/status@AntigravityBot"):
        from types import SimpleNamespace as NS
        return NS(text=text, caption=None, chat=NS(id=-100), message_id=380,
                  from_user=NS(id=202, username="CodexBot", is_bot=True),
                  reply_to_message=reply_to, is_topic_message=True,
                  message_thread_id=thread, forum_topic_created=None,
                  forum_topic_edited=None, photo=None, document=None, voice=None,
                  audio=None, video=None, video_note=None, sticker=None, animation=None)

    def _channel(self):
        from types import SimpleNamespace as NS
        channel = make_channel()
        channel.bot = NS(id=555)
        return channel, NS

    def test_the_topic_opening_message_is_not_a_reply_to_its_opener(self):
        channel, NS = self._channel()
        root = NS(message_id=307, from_user=NS(id=555), forum_topic_created=NS(name="x"))
        self.assertFalse(channel.event_from_aiogram(self._message(root)).replies_to_bot)
        bare_root = NS(message_id=307, from_user=NS(id=555))
        self.assertFalse(channel.event_from_aiogram(self._message(bare_root)).replies_to_bot)

    def test_a_real_reply_in_a_topic_still_counts(self):
        channel, NS = self._channel()
        mine = NS(message_id=350, from_user=NS(id=555), forum_topic_created=None)
        self.assertTrue(channel.event_from_aiogram(self._message(mine)).replies_to_bot)

    def test_a_command_for_another_bot_starts_no_turn(self):
        channel = make_channel()
        ok, why = channel.should_respond(event("/reasoning@AntigravityBot", thread_id=307,
                                               replies_to_bot=True))
        self.assertFalse(ok)
        self.assertIn("another bot", why)


class TestBuild12Findings(unittest.TestCase):

    def test_a_banner_is_not_a_command_argument(self):
        from swarmly.telegram.commands import command_args
        self.assertEqual(command_args("[DeepSeek] /model@example_codex_bot"), [])
        self.assertEqual(command_args("/reasoning@x high @other"), ["high"])
        channel = make_channel(make_config(adapter={"type": "claude"}))
        run(channel.handle_event(event("[Codex] /model@HermesBot", message_id=9)))
        self.assertIn("runs its CLI default model", channel._test_sender.last)

    def test_an_answer_to_the_asking_bot_is_not_mirrored(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = make_channel(make_config(bus={"repo": tmp}))
            channel.bot = object()
            posted = []

            async def fake_post(_channel, **kw):
                posted.append(kw["to"])

            async def scenario():
                channel._mirror_handoffs("@CodexBot Yes. #7 was closed.", -100, 7, 5,
                                         asker="CodexBot")
                channel._mirror_handoffs("@CodexBot Yes.\n- @AntigravityBot review #9",
                                         -100, 7, 6, asker="CodexBot")
                await asyncio.gather(*list(channel._mirror_tasks))
            with mock.patch("swarmly.telegram.relay.post_and_notify", fake_post):
                run(scenario())
        self.assertEqual(posted, ["Antigravity"])


class TestBuild12Report(unittest.TestCase):

    def test_a_dead_owners_poll_lock_is_taken_over(self):
        import json as _json
        import os as _os
        from swarmly.telegram.poll import acquire_poll_lock, poll_lock_path, release_poll_lock
        with tempfile.TemporaryDirectory() as tmp:
            path = poll_lock_path(Path(tmp), "codex")
            path.parent.mkdir(parents=True)
            path.write_text(_json.dumps({"acquired_at": 1000.0, "owner": "99999999"}))
            with mock.patch("swarmly.telegram.poll.pid_is_alive", return_value=False):
                self.assertTrue(acquire_poll_lock(Path(tmp), "codex", ttl_seconds=1800,
                                                  now=1010.0))
            self.assertEqual(_json.loads(path.read_text())["owner"], str(_os.getpid()))
            release_poll_lock(Path(tmp), "codex")

    def test_a_live_owners_poll_lock_still_refuses(self):
        import json as _json
        from swarmly.telegram.poll import acquire_poll_lock, poll_lock_path
        with tempfile.TemporaryDirectory() as tmp:
            path = poll_lock_path(Path(tmp), "codex")
            path.parent.mkdir(parents=True)
            path.write_text(_json.dumps({"acquired_at": 1000.0, "owner": "4242"}))
            with mock.patch("swarmly.telegram.poll.pid_is_alive", return_value=True), \
                    self.assertLogs("swarmly.telegram.poll", "WARNING"):
                self.assertFalse(acquire_poll_lock(Path(tmp), "codex", ttl_seconds=1800,
                                                   now=1010.0))

    def test_send_retries_a_network_error(self):
        from aiogram.exceptions import TelegramNetworkError
        from swarmly.telegram.cli import _send_with_retry
        channel = make_channel()
        calls = []

        async def flaky(text, thread_id=None):
            calls.append(thread_id)
            if len(calls) == 1:
                raise TelegramNetworkError(method=mock.Mock(), message="network name gone")
        channel.send = flaky
        with self.assertLogs("swarmly.telegram.cli", "WARNING"):
            run(_send_with_retry(channel, "hi", 307, first_wait=0))
        self.assertEqual(calls, [307, 307])


class TestShortHandoffsLeaveGeneral(unittest.TestCase):
    """The group export showed short hand-offs (under the length limit) staying in General."""

    def _channel(self, reply):
        from swarmly.telegram.adapters import EchoAdapter
        from swarmly.telegram.adapters.base import AdapterReply

        class Fixed(EchoAdapter):
            async def respond(self, message):
                return AdapterReply(text=reply)
        channel = make_channel(make_config(), adapter=Fixed("Hermes"))
        channel.bot = mock.Mock(id=555)
        channel.bot.create_forum_topic = mock.AsyncMock(
            return_value=mock.Mock(message_thread_id=900))
        return channel

    def test_a_short_handoff_moves_to_a_topic(self):
        channel = self._channel("@CodexBot please run the full suite on main.")
        run(channel.handle_event(event("@HermesBot get the suite run on main",
                                       author_id=ADMIN_ID, username="alice", is_bot=False)))
        channel.bot.create_forum_topic.assert_awaited_once()
        self.assertTrue(any("moved this to" in str(m) for m in channel._test_sender.sent))

    def test_a_short_answer_to_the_asking_bot_stays(self):
        channel = self._channel("@CodexBot yes, #7 is closed.")
        run(channel.handle_event(event("@HermesBot is #7 closed?", username="CodexBot")))
        channel.bot.create_forum_topic.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
