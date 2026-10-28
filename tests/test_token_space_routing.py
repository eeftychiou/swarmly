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


def linked(table):
    """The registry's links as the channel reads them (#472): a ref -> its topic."""
    return lambda refs: next((table[ref] for ref in refs if ref in table), None)


class RepliesAreNotMovedByWhatTheyNameTests(unittest.TestCase):

    def reply_in_a_topic(self, channel, body="Progress on #300: tests pass."):
        return channel._topic_elsewhere(event("@HermesBot status?", thread_id=9400), body)

    def test_transport_only_keeps_a_reply_where_its_turn_ran(self):
        channel = channel_for(work_in_topics=True,
                              adapter=Scripted("Reviewing #300 now; verdict to follow."))
        channel.state_link.topic_for_refs = linked({"#300": (GROUP, 9500)})
        spy = Spy(channel)
        run(channel.handle_event(event("@HermesBot please review #300", thread_id=9400)))
        self.assertEqual([(chat, thread) for chat, thread, _ in spy.posts], [(GROUP, 9400)])
        self.assertIn("Reviewing #300", spy.posts[0][2])

    def test_a_reply_that_gets_past_the_gate_is_neither_posted_nor_remembered(self):
        channel = channel_for(adapter=Scripted("the answer"))

        async def no_gate(ev):
            return None
        channel._general_gate = channel._general_request = no_gate
        run(channel.handle_event(admin("@HermesBot hello")))
        self.assertTrue(channel.adapter.seen, "the turn ran")
        self.assertFalse(any("the answer" in s["text"] for s in channel._test_sender.sent))
        log = channel._topic_memory._topics.get((GROUP, None), {}).get("log", [])
        self.assertFalse(any(text == "the answer" for _id, _who, text in log), log)

    def test_a_linked_reply_moves_but_never_into_the_pulse(self):
        channel = channel_for(general_turns="allow", work_in_topics=True)
        channel.state_link.topic_for_refs = linked({"#300": (GROUP, 9500),
                                                    "#301": (GROUP, PULSE)})
        self.assertEqual(self.reply_in_a_topic(channel), (GROUP, 9500))
        # Live, 13:57: work moved into the pulse by a number it named.
        self.assertIsNone(self.reply_in_a_topic(channel, "Progress on #301."))

    def test_a_second_pulse_topic_is_the_pulse_too(self):
        channel = channel_for()
        channel.topics.remember(GROUP, 3500, "\U0001f4e1 Swarm pulse")
        self.assertEqual(channel.token_barred(GROUP, 3500), "Swarm pulse")
        channel.state_link.topic_for_refs = linked({"#300": (GROUP, 3500)})
        self.assertIsNone(channel.topic_for_text("Codex is busy on #300"),
                          "the pulse is never a work topic")


class TopicPinTests(unittest.TestCase):

    def pin(self, channel, thread_id):
        channel.is_coordinator = lambda now=None: True
        return channel._handle_topic_pin(admin("/topic #300", thread_id=thread_id))

    def test_transport_only_says_only_notices_follow(self):
        answer = self.pin(channel_for(), 9400)
        self.assertIn("notices about them", answer)

    def test_today_replies_follow(self):
        self.assertIn("replies about them", self.pin(channel_for("allow"), 9400))

    def test_the_pulse_takes_no_items(self):
        channel = channel_for()
        self.assertIn("works inside a work topic", self.pin(channel, PULSE))


class NewTopicOpensTheTopicItAsksForTests(unittest.TestCase):

    def scenario(self, general_turns, link=True):
        adapter = Scripted("NEW_TOPIC: Round on #300\nPlease handle #300.")
        channel = channel_for(general_turns, adapter=adapter)
        channel.bot = TopicBot()
        channel.state_link.topic_for_refs = linked({"#300": (GROUP, 9500)} if link
                                                   else {})

        async def go():
            await channel.handle_event(admin("@HermesBot start the round", thread_id=9400))
            await drain(channel)
        run(go())
        return channel

    def test_transport_only_opens_it(self):
        """Nothing links the item, so the brief gets a topic of its own."""
        channel = self.scenario("never", link=False)
        self.assertEqual(channel.bot.created, [(GROUP, "Hermes: Round on #300")])
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertFalse(any("continued in" in t for t in texts), texts)

    def test_the_linked_topic_takes_it(self):
        channel = self.scenario("allow")
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

    def test_a_topic_origin_still_gets_the_summary(self):
        adapter = Scripted("Compared them.\nREPORT_BACK: API B is better.")
        channel = channel_for(adapter=adapter)
        channel._topic_origins[(GROUP, 41)] = (GROUP, 9400)
        spy = Spy(channel)
        run(channel.handle_event(admin("@HermesBot which API?", thread_id=41)))
        there = [text for chat, thread, text in spy.posts if thread == 9400]
        self.assertTrue(any("API B is better." in t and "↩ from" in t for t in there), spy.posts)

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

    def echo(self, channel, text="Done: #300 is fixed.", **kw):
        from swarmly.telegram.poll import PollOutcome, _echo_reply
        channel._sender = None
        channel.bot = object()
        spy = Spy(channel)
        self.outcome = PollOutcome()

        async def record(*args, **kw):
            return None
        with mock.patch.object(type(channel), "_send_locked", record):
            run(_echo_reply(channel, "codex", AdapterReply(text=text), self.outcome, **kw))
        return spy.posts

    def test_transport_only_neither_picks_a_topic_by_its_words_nor_posts_in_general(self):
        channel = channel_for()
        channel.state_link.topic_for_refs = linked({"#300": (GROUP, 9400)})
        self.assertEqual(self.echo(channel), [])
        self.assertEqual(self.outcome.replies_unposted, 1)          # counted, not lost quietly

    def test_an_answer_to_a_mirrored_hand_off_goes_where_the_hand_off_was(self):
        channel = channel_for()
        channel._log_topic(GROUP, 9400, "Codex", "@HermesBot please run the suite", 77)
        posts = self.echo(channel, message_id=77)
        self.assertEqual([(chat, thread) for chat, thread, _ in posts], [(GROUP, 9400)])
        self.assertEqual(self.outcome.replies_posted, 1)

    def test_a_hand_off_seen_in_the_pulse_is_not_answered_there(self):
        channel = channel_for()
        channel._log_topic(GROUP, PULSE, "Codex", "pulse chatter", 78)
        self.assertEqual(self.echo(channel, message_id=78), [])

    def test_the_echo_goes_to_the_topic_its_items_are_linked_in(self):
        channel = channel_for(general_turns="allow")
        channel.state_link.topic_for_refs = linked({"#300": (GROUP, 9400)})
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


class RoleReasonTests(unittest.TestCase):

    def test_the_coordinators_reason_stays_out_of_general(self):
        from tests.test_membership import TrustTests, _Scripted
        for general_turns, shown in (("never", False), ("allow", True)):
            with self.subTest(general_turns=general_turns), TemporaryDirectory() as folder:
                channel = TrustTests().coordinator(folder, adapter=_Scripted(
                    "ROLE: @codex_x_bot reviewer: MODEL WORDS here\nCodex, please review."))
                channel.config.limits.general_turns = general_turns
                run(channel.handle_event(event("@HermesBot who reviews?", author_id=ADMIN_ID,
                                               username="alice", is_bot=False,
                                               thread_id=9400)))
                change = next(r["text"] for r in channel._test_sender.sent
                              if "intent=ROLE" in r["text"])
                self.assertEqual("MODEL WORDS" in change, shown, change)


class CliOutputTests(unittest.TestCase):

    def run_edit(self, error):
        channel = channel_for()

        class Bot:
            async def edit_message_text(self, **kw):
                raise RuntimeError(error)
        channel.bot = Bot()
        spy = Spy(channel)
        run(channel._edit_native(GROUP, 5, "the CLI's output", 9400))
        return spy.posts

    def test_a_failed_edit_posts_in_the_runs_topic(self):
        self.assertEqual([(c, t) for c, t, _ in self.run_edit("Bad Request: message to edit "
                                                              "not found")], [(GROUP, 9400)])

    def test_an_unchanged_screen_posts_nothing(self):
        self.assertEqual(self.run_edit("Bad Request: message is not modified"), [])


class TicketRepliesTests(unittest.TestCase):

    def flush(self, general_turns, thread_id):
        from swarmly.telegram.replies import ReplyStore
        with TemporaryDirectory() as folder:
            channel = channel_for(general_turns)
            channel.replies = ReplyStore(Path(folder) / "replies.db")
            ticket, _ = channel.replies.register(chat_id=GROUP, message_id=5,
                                                 recipient="alice", text="q?",
                                                 thread_id=thread_id)
            channel.replies.submit(ticket["id"], text="the answer")
            spy = Spy(channel)
            run(channel.flush_replies())
            return spy.posts

    def test_the_answer_goes_to_the_tickets_topic(self):
        self.assertEqual([(c, t) for c, t, _ in self.flush("never", 9400)], [(GROUP, 9400)])

    def test_an_older_ticket_follows_its_message_as_before(self):
        self.assertEqual([(c, t) for c, t, _ in self.flush("never", None)], [(GROUP, None)])

    def test_a_ticket_in_the_pulse_stops_and_raises_an_alert(self):
        """#313: `send` refused the answer before any attempt, so the ticket stayed `claimed`
        for good, with no retry, no alert, and the answer gone except for a log line."""
        from swarmly.telegram.replies import ReplyStore
        with TemporaryDirectory() as folder:
            channel = channel_for("never")
            channel.replies = ReplyStore(Path(folder) / "replies.db")
            ticket, _ = channel.replies.register(chat_id=GROUP, message_id=5,
                                                 recipient="alice", text="q?", thread_id=PULSE)
            channel.replies.submit(ticket["id"], text="the answer")
            spy = Spy(channel)
            run(channel.flush_replies())
            row = channel.replies.get(ticket["id"])
            self.assertEqual(row["state"], "unposted")
            self.assertIn("Swarm pulse is transport-only", row["detail"])
            self.assertEqual(spy.posts, [], "the answer is not handed to send at all")
            run(channel.flush_replies())                  # the next pass raises its alert
            self.assertEqual(len(spy.posts), 1, spy.posts)
            self.assertIn("unposted for message 5", spy.posts[0][2])
            self.assertIn("answer not posted", spy.posts[0][2])
            for _ in range(3):                            # the answer is never retried, and
                run(channel.flush_replies())              # the alert stops at its cap (#49)
            self.assertEqual(len(spy.posts), channel.config.limits.overdue_alert_cap, spy.posts)
            self.assertFalse([text for _, _, text in spy.posts if "the answer" in text])

    def test_a_late_answer_still_raises_its_alert(self):
        """The ticket's overdue alerts were spent before the answer came: its `unposted`
        alert must still go out (#321 review)."""
        from swarmly.telegram.replies import ReplyStore
        with TemporaryDirectory() as folder:
            channel = channel_for("never")
            channel.replies = ReplyStore(Path(folder) / "replies.db")
            ticket, _ = channel.replies.register(chat_id=GROUP, message_id=5, recipient="alice",
                                                 text="q?", thread_id=PULSE, timeout=1, now=0)
            channel.replies.queued(ticket["id"])
            spy = Spy(channel)
            for _ in range(3):
                run(channel.flush_replies())             # overdue: both alerts spent
            spent = len(spy.posts)
            self.assertEqual(spent, channel.config.limits.overdue_alert_cap)
            channel.replies.submit(ticket["id"], text="the answer, late")
            run(channel.flush_replies())                 # refused: unposted
            run(channel.flush_replies())                 # and alerted
            self.assertEqual(channel.replies.get(ticket["id"])["state"], "unposted")
            self.assertEqual(len(spy.posts), spent + 1, spy.posts)
            self.assertIn("unposted for message 5", spy.posts[-1][2])

    def late_answer(self, channel, folder, thread_id=9400):
        """A ticket whose overdue alerts are spent, then answered."""
        from swarmly.telegram.replies import ReplyStore
        channel.replies = ReplyStore(Path(folder) / "replies.db")
        ticket, _ = channel.replies.register(chat_id=GROUP, message_id=5, recipient="alice",
                                             text="q?", thread_id=thread_id, timeout=1, now=0)
        channel.replies.queued(ticket["id"])
        spy = Spy(channel)
        for _ in range(3):
            run(channel.flush_replies())
        self.assertEqual(len(spy.posts), channel.config.limits.overdue_alert_cap)
        channel.replies.submit(ticket["id"], text="the answer, late")
        return ticket, spy

    def test_a_late_answer_whose_send_fails_still_raises_its_alert(self):
        with TemporaryDirectory() as folder:
            channel = channel_for("never")
            ticket, spy = self.late_answer(channel, folder)
            spent = len(spy.posts)

            def down(payload, reply_to):
                raise RuntimeError("network down")
            real, channel._sender = channel._sender, down
            with self.assertRaises(RuntimeError):
                run(channel.flush_replies())
            channel._sender = real
            run(channel.flush_replies())
            self.assertEqual(channel.replies.get(ticket["id"])["state"], "delivery_unknown")
            self.assertEqual(len(spy.posts), spent + 2, spy.posts)   # the answer, then its alert
            self.assertIn("delivery_unknown", spy.posts[-1][2])

    def test_a_late_answer_cut_off_by_a_restart_still_raises_its_alert(self):
        with TemporaryDirectory() as folder:
            channel = channel_for("never")
            ticket, spy = self.late_answer(channel, folder)
            spent = len(spy.posts)
            channel.replies.claim_ready()
            channel.replies.attempting(ticket["id"])           # the process died mid-send
            channel.replies.recover()
            run(channel.flush_replies())
            self.assertEqual(channel.replies.get(ticket["id"])["state"], "delivery_unknown")
            self.assertEqual(len(spy.posts), spent + 1, spy.posts)
            self.assertIn("delivery_unknown", spy.posts[-1][2])

    def test_an_answer_dropped_because_its_topic_is_gone_is_not_recorded_as_sent(self):
        from swarmly.telegram.channel import TopicGone
        from swarmly.telegram.replies import ReplyStore
        with TemporaryDirectory() as folder:
            channel = channel_for("never")
            channel.replies = ReplyStore(Path(folder) / "replies.db")
            ticket, _ = channel.replies.register(chat_id=GROUP, message_id=5, recipient="alice",
                                                 text="q?", thread_id=9400)
            channel.replies.submit(ticket["id"], text="the answer")

            def gone(payload, reply_to):
                raise TopicGone()
            real, channel._sender = channel._sender, gone
            spy = Spy(channel)
            run(channel.flush_replies())
            row = channel.replies.get(ticket["id"])
            self.assertEqual((row["state"], row["detail"]),
                             ("unposted", "answer not posted: its topic is gone"))
            channel._sender = real
            run(channel.flush_replies())
            self.assertEqual(channel.replies.get(ticket["id"])["alerts"], 1)
            self.assertIn("its topic is gone", spy.posts[-1][2])

    def test_an_answer_with_nothing_to_send_does_not_stay_claimed(self):
        """An answer stored before submissions refused a bare NO_REPLY: `send` posts nothing
        and never tries, so `sent` did not apply and the ticket stayed `claimed`."""
        from swarmly.telegram.replies import ReplyStore
        with TemporaryDirectory() as folder:
            channel = channel_for("never")
            channel.replies = ReplyStore(Path(folder) / "replies.db")
            ticket, _ = channel.replies.register(chat_id=GROUP, message_id=5, recipient="alice",
                                                 text="q?", thread_id=9400)
            with channel.replies._db() as db:
                db.execute("UPDATE replies SET state='ready', text='@alice: NO_REPLY' WHERE id=?",
                           (ticket["id"],))
            run(channel.flush_replies())
            row = channel.replies.get(ticket["id"])
            self.assertEqual(row["state"], "unposted")
            self.assertIn("nothing to send", row["detail"])


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
