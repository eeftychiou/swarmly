"""Where a post goes is decided in transport space, never by the words of a post.

Admin, 25 Oct: "agents should not have a right to move posts into topics. This is a routing
issue handled at the transport space in code and not token space." And before that: "a code
block of posting token space posts in General, pulse". With General and Swarm pulse
transport-only (``limits.general_turns: never``, or registry routing):

- text from token space -- a model's reply or brief, a peer's answer from the bus, an agent's
  own post from its shell -- is refused in General and Swarm pulse, whatever route it took;
- a reply is never moved to another topic by the items it names, and nothing is learned
  about items from the chatter in Swarm pulse;
- NEW_TOPIC opens the topic it asks for, not an older one the brief's numbers point at.
"""

from __future__ import annotations

import contextlib
import io
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from swarmly.telegram.adapters.base import AdapterReply
from tests.test_telegram_channel import (ADMIN_ID, GROUP, event, make_channel, make_config,
                                         run)
from tests.test_topic_open import Scripted, TopicBot, drain

PULSE = 3418


def channel_for(general_turns="never", adapter=None, **limits):
    channel = make_channel(make_config(limits={"general_turns": general_turns, **limits}),
                           adapter=adapter)
    channel._pulse_topic = lambda: PULSE
    return channel


def admin(text, thread_id=None):
    return event(text, author_id=ADMIN_ID, username="admin", is_bot=False,
                 thread_id=thread_id)


class Spy:
    """Records what ``send`` was asked to post, and where."""

    def __init__(self, channel):
        self.posts = []
        real = channel.send

        async def send(text, reply_to_message_id=None, **kw):
            self.posts.append((kw.get("chat_id"), kw.get("thread_id"), text))
            return await real(text, reply_to_message_id, **kw)
        channel.send = send


class TheSendPathRefusesTokenTextTests(unittest.TestCase):

    def test_general_and_the_pulse_refuse_it(self):
        channel = channel_for()
        for thread in (None, PULSE):
            with self.subTest(thread=thread):
                self.assertIsNone(run(channel.send("a reply", chat_id=GROUP, thread_id=thread,
                                                   token=True)))
        self.assertIsNone(run(channel.send("a reply", token=True)))       # General by default
        self.assertEqual(channel._test_sender.sent, [])

    def test_a_work_topic_takes_it(self):
        channel = channel_for()
        run(channel.send("a reply", chat_id=GROUP, thread_id=9400, token=True))
        self.assertEqual(len(channel._test_sender.sent), 1)

    def test_the_transports_own_lines_still_go_to_general(self):
        channel = channel_for()
        run(channel.send("↪ opened topic «x»", chat_id=GROUP, notice=True))
        self.assertEqual(len(channel._test_sender.sent), 1)

    def test_with_general_open_it_is_posted_as_before(self):
        channel = channel_for(general_turns="allow")
        run(channel.send("a reply", chat_id=GROUP, token=True))
        self.assertEqual(len(channel._test_sender.sent), 1)

    def test_registry_routing_turns_it_on(self):
        channel = channel_for(general_turns="allow")
        channel.state_link.routing_mode = "registry"
        self.assertEqual(channel.token_barred(GROUP, None), "General")
        self.assertEqual(channel.token_barred(GROUP, PULSE), "Swarm pulse")
        self.assertIsNone(channel.token_barred(GROUP, 9400))
        self.assertIsNone(channel.token_barred(ADMIN_ID, None))           # a DM


class RepliesAreNotMovedByWhatTheyNameTests(unittest.TestCase):

    def reply_in_a_topic(self, channel, body="Progress on #300: tests pass."):
        return channel._topic_elsewhere(event("@HermesBot status?", thread_id=9400), body)

    def test_transport_only_keeps_a_reply_where_its_turn_ran(self):
        channel = channel_for(work_in_topics=True)
        channel._learn_topic_refs(GROUP, 9500, "PR #300 lives here")
        self.assertIsNone(self.reply_in_a_topic(channel))

    def test_today_a_reply_still_moves_but_never_into_the_pulse(self):
        channel = channel_for(general_turns="allow", work_in_topics=True)
        channel._learn_topic_refs(GROUP, 9500, "PR #300 lives here")
        self.assertEqual(self.reply_in_a_topic(channel), (GROUP, 9500))
        # Live, 13:57: refs learned in the pulse moved a reply there.
        channel._ref_topics["#301"] = (GROUP, PULSE)
        channel._ref_seen["#301"] = time.time()
        self.assertIsNone(self.reply_in_a_topic(channel, "Progress on #301."))

    def test_nothing_is_learned_in_the_pulse(self):
        channel = channel_for(general_turns="allow")
        channel._learn_topic_refs(GROUP, PULSE, "Codex is busy on #300")
        self.assertNotIn("#300", channel._ref_topics)


class NewTopicOpensTheTopicItAsksForTests(unittest.TestCase):

    def scenario(self, general_turns):
        adapter = Scripted("NEW_TOPIC: Round on #300\nPlease handle #300.")
        channel = channel_for(general_turns, adapter=adapter)
        channel.bot = TopicBot()
        channel._learn_topic_refs(GROUP, 9500, "PR #300 lives here")

        async def go():
            await channel.handle_event(admin("@HermesBot start the round", thread_id=9400))
            await drain(channel)
        run(go())
        return channel

    def test_transport_only_opens_it(self):
        channel = self.scenario("never")
        self.assertEqual(channel.bot.created, [(GROUP, "Hermes: Round on #300")])
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertFalse(any("continued in" in t for t in texts), texts)

    def test_today_the_older_topic_takes_it(self):
        channel = self.scenario("allow")
        self.assertEqual(channel.bot.created, [])
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any("continued in" in t for t in texts), texts)

    def test_an_explicit_link_still_takes_it(self):
        adapter = Scripted("NEW_TOPIC: Round on #300\nPlease handle #300.")
        channel = channel_for(adapter=adapter)
        channel.bot = TopicBot()
        channel.state_link.links_mode = "registry"
        channel.state_link.topic_for_refs = lambda refs: (GROUP, 9500) if "#300" in refs \
            else None
        run(channel.handle_event(admin("@HermesBot start the round", thread_id=9400)))
        self.assertEqual(channel.bot.created, [])
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any("continued in" in t for t in texts), texts)


class ReportBackToGeneralTests(unittest.TestCase):

    def test_the_summary_stays_in_the_topic_and_general_gets_a_link(self):
        adapter = Scripted("Compared them.\nREPORT_BACK: API B is better.")
        channel = channel_for(adapter=adapter)
        channel.topics.remember(GROUP, 41, "Hermes: research")
        channel._topic_origins[(GROUP, 41)] = (GROUP, None)
        spy = Spy(channel)
        run(channel.handle_event(admin("@HermesBot which API?", thread_id=41)))
        in_general = [text for chat, thread, text in spy.posts
                      if chat == GROUP and thread is None]
        self.assertEqual(len(in_general), 1, spy.posts)
        self.assertIn("«Hermes: research» reported back", in_general[0])
        self.assertIn("t.me/c/", in_general[0])
        self.assertNotIn("API B", in_general[0])
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertEqual(sum("API B is better." in t for t in texts), 1, texts)   # the topic

    def test_today_general_gets_the_summary(self):
        adapter = Scripted("Compared them.\nREPORT_BACK: API B is better.")
        channel = channel_for("allow", adapter=adapter)
        channel._topic_origins[(GROUP, 41)] = (GROUP, None)
        run(channel.handle_event(admin("@HermesBot which API?", thread_id=41)))
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any("API B is better." in t and "↩ from" in t for t in texts), texts)


class ARequestInGeneralWithNoTopicTests(unittest.TestCase):

    def test_general_hears_why_but_not_the_brief(self):
        channel = channel_for()
        channel.is_coordinator = lambda now=None: True
        channel.bot = TopicBot(fail="Bad Request: not enough rights")
        run(channel.handle_event(admin("@HermesBot please check the CI flakes")))
        last = channel._test_sender.last
        self.assertIn("could not open a topic", last)
        self.assertIn("not enough rights", last)
        self.assertNotIn("Request from", last)


class BusEchoesTests(unittest.TestCase):

    def echo(self, channel, text="Done: #300 is fixed."):
        from swarmly.telegram.poll import PollOutcome, _echo_reply
        channel._sender = None
        channel.bot = object()
        spy = Spy(channel)

        async def record(*args, **kw):
            return None
        with mock.patch.object(type(channel), "_send_locked", record):
            run(_echo_reply(channel, "codex", AdapterReply(text=text), PollOutcome()))
        return spy.posts

    def test_transport_only_neither_picks_a_topic_by_its_words_nor_posts_in_general(self):
        channel = channel_for()
        channel._learn_topic_refs(GROUP, 9400, "PR #300 lives here")
        self.assertEqual(self.echo(channel), [])

    def test_today_the_echo_goes_to_the_topic_its_items_name(self):
        channel = channel_for(general_turns="allow")
        channel._learn_topic_refs(GROUP, 9400, "PR #300 lives here")
        posts = self.echo(channel)
        self.assertEqual([(chat, thread) for chat, thread, _ in posts], [(GROUP, 9400)])


class RelayPointerTests(unittest.TestCase):

    def relay(self, channel):
        from swarmly.telegram.relay import post_and_notify

        class Bus:
            def __init__(self, root_dir, transport):
                self.root = Path(root_dir)

            def signing_key(self, alias):
                return "key", "ed25519"

            def post_message(self, message, **kw):
                return self.root / "msg.json"
        with TemporaryDirectory() as tmp, mock.patch("swarmly.bus.DurableBus", Bus):
            return run(post_and_notify(channel, to="Codex", text="please review #300",
                                       root=Path(tmp),
                                       transport=types.SimpleNamespace(kind="folder")))

    def test_transport_only_sends_no_pointer_to_general(self):
        channel = channel_for()
        result = self.relay(channel)
        self.assertEqual(result.telegram, "skipped")
        self.assertIn("transport-only", result.error)
        self.assertEqual(channel._test_sender.sent, [])

    def test_today_the_pointer_and_preview_go_to_general(self):
        channel = channel_for(general_turns="allow")
        result = self.relay(channel)
        self.assertEqual(result.telegram, "sent")
        self.assertIn("please review #300", channel._test_sender.last)


class TheSendCommandTests(unittest.TestCase):

    def test_it_refuses_general_and_the_pulse(self):
        from swarmly.telegram import cli
        config = make_config(limits={"general_turns": "never"})
        for topic in (None, PULSE):
            with self.subTest(topic=topic), mock.patch.object(
                    cli.AgentChannel, "_pulse_topic", lambda self: PULSE):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = run(cli._send(config, "hello", topic))
                self.assertEqual(code, 1)
                self.assertIn('"refused"', out.getvalue())
                self.assertIn("transport-only", err.getvalue())


if __name__ == "__main__":
    unittest.main()
