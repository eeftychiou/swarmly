"""
The busy queue and long-reply splitting, both ported from ductor's Telegram transport.

* An addressed message that arrives while a turn is running is held and run next
  (``limits.busy_behavior: queue``, the default), instead of being dropped.
* A reply longer than one Telegram message is split into up to ``limits.max_reply_parts``
  messages instead of being cut off; later parts are marked ``↪ (k/n)`` and never start a
  turn on the receiving side.
"""

from __future__ import annotations

import asyncio
import re
import unittest
from html import escape

from swarmly.telegram.config import ChannelLimits, ConfigError
from swarmly.telegram.wire import Envelope, MessageType, format_message, parse_message, utf16_length
from tests.test_telegram_channel import ADMIN_ID, _GatedAdapter, event, make_channel, make_config, run


def gated(**limits):
    channel = make_channel(make_config(limits=limits) if limits else None)
    adapter = _GatedAdapter("Hermes")
    channel.adapter = adapter
    return channel, adapter


async def _settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def hold_and_send(channel, adapter, texts, *, cancel_index=None, thread_id=None,
                  drain=False):
    """Hold a turn open, send ``texts`` while it runs, then release and collect everything."""

    async def scenario():
        first = asyncio.create_task(channel.handle_event(
            event("@HermesBot: hold the turn open", message_id=1, thread_id=thread_id)))
        await adapter.entered.wait()
        waiting = [asyncio.create_task(channel.handle_event(
            event(text, message_id=10 + index, thread_id=thread_id)))
            for index, text in enumerate(texts)]
        await _settle()
        snapshot = channel._busy_waiting
        if cancel_index is not None:
            waiting[cancel_index].cancel()
            await _settle()
        adapter.release.set()
        results = await asyncio.gather(first, *waiting, return_exceptions=True)
        if drain:
            # #184: a held message runs as its own task when the queue drains. Wait for it
            # inside this loop: a task still pending when `run()` closes the loop is cancelled,
            # and the test would read "never answered" instead of "answered a moment later".
            for _ in range(60):
                if not channel._held_overflow():
                    await _settle(5)
                    break
                await _settle(5)
        return snapshot, results

    return run(scenario())


class TestBusyQueue(unittest.TestCase):
    TEXTS = ("@HermesBot: verify PR #148", "@HermesBot: and issue #14", "@HermesBot: and the docs")

    def test_a_message_sent_mid_turn_runs_after_the_turn(self):
        channel, adapter = gated()
        snapshot, results = hold_and_send(channel, adapter, self.TEXTS[:1])
        self.assertEqual(snapshot, 1, "the message waits behind the running turn")
        self.assertEqual(adapter.calls, 2)
        self.assertIn("verify PR #148", results[1])
        self.assertEqual(channel.stats.busy_queued, 1)
        self.assertEqual(channel.stats.busy_dropped, 0)
        self.assertEqual(channel._busy_waiting, 0)

    def test_queued_messages_run_in_arrival_order(self):
        channel, adapter = gated()
        hold_and_send(channel, adapter, self.TEXTS)
        replies = [sent["text"] for sent in channel._test_sender.sent]
        order = [next(i for i, text in enumerate(self.TEXTS) if text.split(": ")[1] in reply)
                 for reply in replies[1:]]
        self.assertEqual(order, [0, 1, 2])

    def test_messages_queued_behind_a_topic_turn_get_one_turn_in_arrival_order(self):
        channel, adapter = gated(max_peer_turns_per_topic=0)
        hold_and_send(channel, adapter, self.TEXTS, thread_id=4)
        self.assertEqual(adapter.calls, 2, "the running turn plus one for the three queued")
        reply = [sent["text"] for sent in channel._test_sender.sent][-1]
        where = [reply.index(text.split(": ")[1]) for text in self.TEXTS]
        self.assertEqual(where, sorted(where))
        self.assertIn("arrived while your last turn ran", reply)

    def test_a_full_queue_holds_the_overflow_and_every_message_is_answered(self):
        """#184's acceptance: overfill the queue and see every message answered, in order.

        Before: past `busy_queue_max` the message was dropped with "its queue is full" (live,
        22 Oct, twice) -- a hand-off nobody saw again.
        """
        channel, adapter = gated(busy_queue_max=2)
        with self.assertLogs("swarmly.telegram", level="WARNING") as captured:
            _, results = hold_and_send(channel, adapter, self.TEXTS, drain=True)
        self.assertEqual(channel.stats.busy_queued, 2, "two waited in memory")
        self.assertEqual(channel.stats.busy_held, 1, "the third was held, not dropped")
        self.assertEqual(channel.stats.busy_dropped, 0)
        self.assertEqual(adapter.calls, 4, "the running turn plus all three answered")
        # The call that overflowed returned None: its message did not run *there*, it ran as a
        # held turn of its own once the queue drained -- which is what `adapter.calls` shows.
        self.assertIsNone(results[3])
        replies = [sent["text"] for sent in channel._test_sender.sent]
        order = [next(i for i, text in enumerate(self.TEXTS) if text.split(": ")[1] in reply)
                 for reply in replies[1:] if any(text.split(": ")[1] in reply
                                                for text in self.TEXTS)]
        self.assertEqual(order, [0, 1, 2], f"arrival order, not completion order: {replies}")
        # And the topic is told once that the bot is behind, with the depth -- not that the
        # message was thrown away.
        self.assertTrue(any("behind by" in reply and "kept" in reply for reply in replies),
                        replies)
        self.assertTrue(any("busy queue" in record.getMessage() for record in captured.records))

    def test_the_held_queue_has_its_own_ceiling_and_keeps_the_oldest(self):
        """#184 review: holding without a bound is memory and an inbox nobody can drain.

        Past ``limits.busy_held_max`` the *newest* arrival is the one dropped -- the oldest are
        kept, because oldest-first is the order the queue is drained in -- and the topic is told
        plainly that this one could not be kept, rather than being told it was.
        """
        channel, adapter = gated(busy_queue_max=1, busy_held_max=1)
        texts = self.TEXTS + ("@HermesBot: and the changelog",)
        with self.assertLogs("swarmly.telegram", level="WARNING") as captured:
            hold_and_send(channel, adapter, texts, drain=True)
        self.assertEqual(channel.stats.busy_queued, 1, "one waited in memory")
        self.assertEqual(channel.stats.busy_held, 1, "one was held -- the ceiling is 1")
        self.assertEqual(channel.stats.busy_dropped, 2, "the two arrivals past the ceiling")
        replies = [sent["text"] for sent in channel._test_sender.sent]
        answered = [text for text in texts
                    if any(text.split(": ")[1] in reply for reply in replies)]
        self.assertEqual(answered, list(texts[:2]), f"the oldest are kept: {replies}")
        # The refusal is said out loud -- a silent drop is the bug this item fixes -- and it is
        # not disguised as a hold; one line per window, not one per message.
        refused = [reply for reply in replies if "could not be kept" in reply]
        self.assertEqual(len(refused), 1, replies)
        self.assertIn("held queue is full", refused[0])
        self.assertTrue(any("already held (ceiling 1)" in record.getMessage()
                            for record in captured.records), captured.output)

    def test_a_held_message_that_cannot_start_stays_held_and_recorded(self):
        """#184 review: the record goes only once the held turn exists.

        `_drain_overflow` used to pop the message, clear its inbox record, and *then* try to
        start a task -- so the no-loop path put the event back in memory, but the message was
        already gone from the inbox, which is the one loss this item exists to prevent.
        """
        channel, _ = gated()
        held = event("@HermesBot: hold me", message_id=77)
        channel._hold_overflow(held)
        self.assertIn((held.chat_id, held.message_id), channel._inbox)
        channel._drain_overflow()               # no running loop here: nothing can start
        self.assertEqual(channel._held_overflow(), [held], "still held for the next turn end")
        self.assertIn((held.chat_id, held.message_id), channel._inbox,
                      "a message that never started must still be there for a restart")

    def test_a_queued_peer_message_waits_out_the_turn_cooldown_instead_of_being_dropped(self):
        channel, adapter = gated(min_seconds_between_agent_turns=60)
        hold_and_send(channel, adapter, self.TEXTS[:1])
        self.assertEqual(adapter.calls, 2)
        self.assertTrue(any(abs(seconds - 60) < 1 for seconds in channel._test_slept),
                        channel._test_slept)

    def test_a_cancelled_waiter_leaves_the_queue(self):
        channel, adapter = gated()
        snapshot, results = hold_and_send(channel, adapter, self.TEXTS[:2], cancel_index=0)
        self.assertEqual(snapshot, 2)
        self.assertIsInstance(results[1], asyncio.CancelledError)
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(channel._busy_waiting, 0)

    def test_drop_mode_keeps_the_old_behaviour(self):
        channel, adapter = gated(busy_behavior="drop")
        _, results = hold_and_send(channel, adapter, self.TEXTS[:1])
        self.assertIsNone(results[1])
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(channel.stats.busy_dropped, 1)
        self.assertEqual(channel.stats.busy_queued, 0)

    def test_queue_settings_are_validated_at_load(self):
        for bad in ({"busy_queue_max": 0}, {"busy_queue_max": "many"},
                    {"busy_held_max": 0}, {"busy_held_max": "many"},
                    {"max_reply_parts": 0}, {"max_reply_parts": -2}):
            with self.assertRaises(ConfigError, msg=bad):
                make_config(limits=bad)
        self.assertEqual(ChannelLimits().max_reply_parts, 4)
        self.assertEqual(ChannelLimits().busy_held_max, 50)   # the held queue's own ceiling

    def test_max_reply_parts_is_clamped_to_the_protocol_cap(self):
        """Review of #29: the sender and the receiver share one cap, so a configured value above
        it is clamped here instead of emitting parts the receiver refuses."""
        from swarmly.telegram.config import MAX_REPLY_PARTS

        self.assertEqual(ChannelLimits(max_reply_parts=MAX_REPLY_PARTS + 5).max_reply_parts,
                         MAX_REPLY_PARTS)
        self.assertEqual(make_config(limits={"max_reply_parts": 25}).limits.max_reply_parts,
                         MAX_REPLY_PARTS)


class TestReplySplitting(unittest.TestCase):
    LIMIT = 4096

    def assert_each_part_fits(self, channel, parts):
        for part in parts:
            self.assertLessEqual(utf16_length(f"[{channel.config.identity.alias}] {part}"),
                                 self.LIMIT, part[:40])

    @staticmethod
    def strip_markers(parts):
        body = [parts[0].rsplit("\n\n(", 1)[0]]
        body += [part.split(") ", 1)[1] for part in parts[1:]]
        return body

    def test_a_short_reply_is_one_message(self):
        channel = make_channel()
        self.assertEqual(channel.split_outgoing("hello"), ["hello"])

    def test_a_long_reply_is_split_without_losing_text(self):
        channel = make_channel()
        text = "x" * 9000
        parts = channel.split_outgoing(text)
        self.assertEqual(len(parts), 3)
        # Each part carries the reply's correlation token inside its marker (#29 review).
        self.assertRegex(parts[0], r"\n\n\(1/3 #[0-9a-f]{6}\)\Z")
        self.assertRegex(parts[1], r"\A↪ \(2/3 #[0-9a-f]{6}\) ")
        self.assertRegex(parts[2], r"\A↪ \(3/3 #[0-9a-f]{6}\) ")
        refs = {re.search(r"#[0-9a-f]{6}", part).group(0) for part in parts}
        self.assertEqual(len(refs), 1, refs)          # one reply, one token
        self.assertEqual("".join(self.strip_markers(parts)), text)
        self.assert_each_part_fits(channel, parts)

    def test_every_part_is_sent_in_order(self):
        channel = make_channel()
        first = run(channel.send("y" * 9000))
        sent = [entry["text"] for entry in channel._test_sender.sent]
        self.assertEqual(len(sent), 3)
        self.assertEqual(first, sent[0])
        self.assertNotIn("truncated", "".join(sent))

    def test_splits_fall_on_paragraph_breaks(self):
        channel = make_channel()
        paragraphs = [f"paragraph {index}: " + "z" * 600 for index in range(14)]
        parts = channel.split_outgoing("\n\n".join(paragraphs))
        self.assertGreater(len(parts), 1)
        for body in self.strip_markers(parts):
            # A cut keeps the whitespace it fell on (issue #29: the parts must join back to
            # the original), so a body may end in the blank line the next part starts after.
            for chunk in (c for c in body.split("\n\n") if c.strip()):
                self.assertIn(chunk, paragraphs, "a paragraph was cut in the middle")

    def test_html_escaping_is_counted(self):
        channel = make_channel()
        parts = channel.split_outgoing("<&>" * 2500)          # 7500 chars, 30000 escaped
        for part in parts[:-1]:
            self.assertLessEqual(len(escape(part, quote=False)), self.LIMIT)
        self.assertNotIn("truncated", "".join(run_send(channel, "<&>" * 1000)))

    def test_emoji_are_counted_in_utf16_units(self):
        channel = make_channel()
        parts = channel.split_outgoing("😀" * 5000)            # 10000 UTF-16 units
        self.assertEqual(len(parts), 3)
        self.assert_each_part_fits(channel, parts)

    def test_text_beyond_the_last_part_is_clamped(self):
        channel = make_channel(make_config(limits={"max_reply_parts": 2}))
        sent = run_send(channel, "w" * 20000)
        self.assertEqual(len(sent), 2)
        self.assertIn("truncated", sent[-1])

    def test_the_envelope_header_stays_on_the_first_part(self):
        channel = make_channel()
        message = format_message(Envelope(type=MessageType.REQUEST, to="Codex", intent="REVIEW",
                                          id="abc123"), "v" * 9000)
        parts = channel.split_outgoing(message)
        envelope, _ = parse_message(parts[0])
        self.assertEqual(envelope.type, MessageType.REQUEST)
        self.assertEqual(envelope.id, "abc123")
        self.assertNotEqual(parse_message(parts[1])[0].type, MessageType.REQUEST)

    def test_a_continuation_part_never_starts_a_turn(self):
        channel = make_channel()
        for text in ("[Codex] ↪ (2/3) @HermesBot please also check this",
                     "@HermesBot [Codex] ↪ (3/3) the rest",
                     "↪ (2/2) Hermes: more"):
            respond, reason = channel.should_respond(event(text))
            self.assertFalse(respond, text)
            self.assertIn("continuation", reason)

    def test_a_human_message_with_an_arrow_is_still_answered(self):
        channel = make_channel()
        respond, _ = channel.should_respond(event("↪ (2/3) @HermesBot hi", is_bot=False,
                                                     author_id=ADMIN_ID))
        self.assertTrue(respond)


def run_send(channel, text):
    run(channel.send(text))
    return [entry["text"] for entry in channel._test_sender.sent]


from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)


class TestBusyLineAndTimeout(unittest.TestCase):
    """Resilience after a live run: a bot in a long turn says it is busy, and a turn that hits
    the time limit in a resumable session continues instead of being dropped."""

    def test_a_message_queued_behind_a_long_turn_gets_one_busy_line(self):
        channel, adapter = gated()
        now = [1000.0]
        channel._clock = lambda: now[0]

        async def scenario():
            first = asyncio.create_task(channel.handle_event(
                event("@HermesBot: a long one", message_id=1)))
            await adapter.entered.wait()
            early = asyncio.create_task(channel.handle_event(event("@HermesBot: a", message_id=2)))
            await _settle()
            now[0] += 200
            later = [asyncio.create_task(channel.handle_event(event(f"@HermesBot: {n}",
                                                                    message_id=n)))
                     for n in (3, 4)]
            await _settle()
            adapter.release.set()
            await asyncio.gather(first, early, *later)
        run(scenario())
        busy = [m["text"] for m in channel._test_sender.sent if "⏳" in m["text"]]
        self.assertEqual(len(busy), 1, busy)
        self.assertIn("is busy with a turn running since", busy[0])

    def test_a_timed_out_session_turn_continues(self):
        from swarmly.telegram.adapters.base import AdapterReply
        from swarmly.telegram.adapters import EchoAdapter

        class Slow(EchoAdapter):
            prompts: list = []

            def topic_mode(self, context_id):
                return "session"

            async def respond(self, message):
                self.prompts.append(message.text)
                if len(self.prompts) == 1:
                    return AdapterReply(ok=False, error="Hermes timed out after 900s",
                                        meta={"timed_out": True})
                if len(self.prompts) == 2:
                    return AdapterReply(ok=False, error="Hermes timed out after 900s",
                                        meta={"timed_out": True})
                return AdapterReply(text="done")

        channel = make_channel(make_config(limits={"max_peer_turns_per_topic": 0}))
        channel.adapter = Slow("Hermes")
        channel.adapter.prompts = []

        async def scenario():
            await channel.handle_event(event("@HermesBot: draw the charts", thread_id=4))
            for _ in range(50):
                await asyncio.sleep(0)
        run(scenario())
        prompts = channel.adapter.prompts
        self.assertEqual(len(prompts), 2, "one continue, not a loop")
        self.assertIn("hit the time limit", prompts[1])
        sent = [m["text"] for m in channel._test_sender.sent]
        self.assertTrue(sent[0].endswith("continues where it stopped in its next turn."))
        self.assertIn("⏳", sent[0])
        self.assertIn("❌", sent[-1])

    def test_a_rotated_session_is_announced(self):
        from swarmly.telegram.adapters import EchoAdapter
        from swarmly.telegram.adapters.base import AdapterReply

        class Rotating(EchoAdapter):
            async def respond(self, message):
                return AdapterReply(text="done", meta={"rotated": True})

        channel = make_channel()
        channel.adapter = Rotating("Hermes")
        run(channel.handle_event(event("@HermesBot: go on", thread_id=4)))
        sent = [m["text"] for m in channel._test_sender.sent]
        self.assertTrue(any("♻️" in t and "handover" in t for t in sent), sent)
