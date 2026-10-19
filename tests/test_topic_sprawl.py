"""Round 2 export: one PR was discussed in five topics. Work on an item stays in the topic
already about it, across restarts, and a hand-off is answered once."""

from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from swarmly.telegram.channel import REF_TOPIC_SECONDS
from swarmly.telegram.poll import _echo_reply, bus_poll_once
from tests.test_bus_live_fixes import channel_for
from tests.test_handoff_mirror import mirror
from tests.test_telegram_channel import GROUP, event, make_channel, make_config, run
from tests.test_topic_open import Scripted, TopicBot, ask, drain


class TestTheTopicMapSurvivesARestart(unittest.TestCase):

    def test_a_restarted_bot_still_knows_the_topic_about_a_pr(self):
        with TemporaryDirectory() as tmp:
            config = make_config()
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            run(channel.handle_event(event("QA on PR 37 starts here", thread_id=77)))
            again = make_channel(config)
            self.assertEqual(again.topic_for_text("re-review #37"), (GROUP, 77))
            self.assertIsNone(again.topic_for_text("#38"))

    def test_an_item_nobody_mentioned_for_a_week_is_let_go(self):
        channel = make_channel()
        channel._learn_topic_refs(GROUP, 77, "#37")
        channel._ref_seen["#37"] = time.time() - REF_TOPIC_SECONDS - 1
        self.assertIsNone(channel.topic_for_text("#37"))
        channel._learn_topic_refs(GROUP, 90, "#37")
        self.assertEqual(channel.topic_for_text("#37"), (GROUP, 90))

    def test_a_closed_topic_is_forgotten(self):
        channel = make_channel()
        channel._learn_topic_refs(GROUP, 77, "#37 and #34")
        channel._forget_topic_refs(GROUP, 77)
        self.assertIsNone(channel.topic_for_text("#37 #34"))


class TestNewTopicChecksKnownTopics(unittest.TestCase):

    def test_new_topic_for_a_known_pr_continues_there(self):
        adapter = Scripted("NEW_TOPIC: PR #37 re-review\n@CodexBot please re-review #37.")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()
        run(channel.handle_event(event("QA on #37 failed", thread_id=77, message_id=3)))
        run(channel.handle_event(ask("@HermesBot get #37 reviewed again", message_id=4)))
        self.assertEqual(channel.bot.created, [])
        sent = channel._test_sender.sent
        self.assertTrue(any("please re-review #37" in s["text"] for s in sent), sent)
        self.assertTrue(any(s["text"].startswith("↪ continued in") for s in sent), sent)

    def test_own_brief_for_a_known_pr_runs_in_that_topic(self):
        adapter = Scripted("NEW_TOPIC: QA #37\nRe-run the suite on #37.", "done")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()

        async def scenario():
            await channel.handle_event(event("#37 is up", thread_id=77, message_id=3))
            await channel.handle_event(ask("@HermesBot QA #37", message_id=4))
            await drain(channel)
        run(scenario())
        self.assertEqual(channel.bot.created, [])
        self.assertEqual(len(adapter.seen), 2)

    def test_new_topic_for_a_new_item_still_opens_one(self):
        adapter = Scripted("NEW_TOPIC: research\n@CodexBot compare the two APIs.")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()
        run(channel.handle_event(event("#37 is up", thread_id=77, message_id=3)))
        run(channel.handle_event(ask("@HermesBot look into the APIs", message_id=4)))
        self.assertEqual(len(channel.bot.created), 1)


class TestMovedTopicsAreNamedAfterTheItem(unittest.TestCase):

    def test_a_moved_topic_title_leads_with_the_pr(self):
        channel = make_channel(adapter=Scripted("@CodexBot " + "QA notes " * 60))
        channel.bot = TopicBot()
        run(channel.handle_event(ask("@HermesBot Copy, QA ready for PR 34")))
        self.assertEqual(channel.bot.created, [(GROUP, "Hermes: #34 Copy, QA ready for PR 34")])


class TestABusEchoOpensNoTopic(unittest.TestCase):

    def test_a_handoff_answering_a_bus_echo_stays_put(self):
        channel = make_channel(adapter=Scripted("@AntigravityBot " + "next step " * 60))
        channel.bot = TopicBot()
        run(channel.handle_event(event("[Codex] [gitbus ← hermes] @HermesBot QA ready, over to you")))
        self.assertEqual(channel.bot.created, [])

    def test_it_still_goes_to_the_topic_about_its_item(self):
        channel = make_channel(adapter=Scripted("@CodexBot " + "next step " * 60))
        channel.bot = TopicBot()
        run(channel.handle_event(event("#37 is up", thread_id=77, message_id=3)))
        run(channel.handle_event(event("[Codex] [gitbus ← hermes] @HermesBot QA on #37 passed",
                                       message_id=4)))
        self.assertEqual(channel.bot.created, [])
        sent = channel._test_sender.sent
        self.assertTrue(any(s["text"].startswith("↪ moved this to") for s in sent), sent)


class TestBusRepliesGoToTheirTopic(unittest.TestCase):

    def test_an_echo_with_no_topic_goes_to_the_topic_about_its_pr(self):
        channel = make_channel()
        channel.bot = object()
        channel._learn_topic_refs(GROUP, 77, "#37")
        channel._sender = None
        channel.send = mock.AsyncMock()
        reply = mock.Mock(text="QA on #37 passed")
        outcome = mock.Mock(replies_posted=0)
        asyncio.run(_echo_reply(channel, "codex", reply, outcome, about="@HermesBot QA #37"))
        kwargs = channel.send.await_args.kwargs
        self.assertEqual((kwargs.get("chat_id"), kwargs.get("thread_id")), (GROUP, 77))

    def test_an_echo_about_nothing_known_stays_in_general(self):
        channel = make_channel()
        channel.bot = object()
        channel._sender = None
        channel.send = mock.AsyncMock()
        outcome = mock.Mock(replies_posted=0)
        asyncio.run(_echo_reply(channel, "codex", mock.Mock(text="done"), outcome))
        self.assertIsNone(channel.send.await_args.kwargs.get("thread_id"))


class TestAHandoffIsAnsweredOnce(unittest.TestCase):

    def test_a_mirror_without_a_topic_token_is_matched_by_sender(self):
        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [mirror("m1", {"message_id": 55})],
                                  sender=lambda _text, _reply_to: None)
            run(channel.handle_event(event("@HermesBot please QA #8", message_id=55)))
            outcome = asyncio.run(bus_poll_once(channel))
        self.assertEqual(outcome.delivered, [])
        self.assertIn("m1", outcome.machine_events)

    def test_the_same_id_from_another_sender_still_runs(self):
        channel = make_channel()
        run(channel.handle_event(event("@HermesBot please QA #8", message_id=55,
                                       username="AntigravityBot")))
        self.assertFalse(channel.took_telegram_copy({"message_id": 55}, sender="codex"))
        self.assertTrue(channel.took_telegram_copy({"message_id": 55}, sender="Antigravity"))
        self.assertFalse(channel.took_telegram_copy({"message_id": 56}, sender="Antigravity"))

    def test_the_sender_ledger_survives_a_restart(self):
        with TemporaryDirectory() as tmp:
            config = make_config()
            config.source_path = Path(tmp) / "config.yaml"
            channel = make_channel(config)
            run(channel.handle_event(event("@HermesBot please QA #8", message_id=55)))
            again = make_channel(config)
            self.assertTrue(again.took_telegram_copy({"message_id": 55}, sender="codex"))


if __name__ == "__main__":
    unittest.main()
