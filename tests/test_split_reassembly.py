"""
Split replies at the receiver (issue #29).

A reply longer than one Telegram message goes out as ``↪ (k/n)`` parts
(``AgentChannel.split_outgoing``). The receiving transport joins them back into one message
before the turn runs: one turn, one message, equal to what the sender wrote — and when a part
never arrives, what did arrive is delivered with a note naming the missing parts.
"""

from __future__ import annotations

import unittest

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from swarmly.telegram.channel import split_part
from tests.test_telegram_channel import event, make_channel, run


class RecordingEcho(EchoAdapter):
    """Echo, but it keeps every message it was handed, for the assertions."""

    def __init__(self, alias: str = "Hermes") -> None:
        super().__init__(alias)
        self.seen = []

    async def respond(self, message):
        self.seen.append(message)
        return AdapterReply(text=f"[{self.alias}] ok")


def receiver() -> tuple:
    channel = make_channel()
    adapter = RecordingEcho()
    channel.adapter = adapter
    return channel, adapter


def outgoing(original: str, alias: str = "Codex") -> list:
    """The parts a sender posts for *original*: banner on every part, addressed on part 1."""
    sender = make_channel()
    parts = sender.split_outgoing(original)
    return [f"@HermesBot [{alias}] {part}" if index == 0 else f"[{alias}] {part}"
            for index, part in enumerate(parts)]


ORIGINAL = "\n\n".join(f"paragraph {index}: " + "y" * 200 for index in range(60))


class TestSplitPart(unittest.TestCase):
    def test_the_markers_are_read_off_each_part(self):
        self.assertEqual(split_part("body\n\n(1/3)"), (1, 3, "body"))
        self.assertEqual(split_part("[Codex] ↪ (2/3) more"), (2, 3, "more"))
        self.assertEqual(split_part("@HermesBot [Codex] ↪ (3/3)tail"), (3, 3, "tail"))

    def test_a_whole_message_is_not_a_part(self):
        for text in ("", "just a message", "a message that ends (1/1)", "↪ (1/2) not a part 1"):
            self.assertIsNone(split_part(text), text)


class TestReassembly(unittest.TestCase):
    def test_a_12000_character_reply_arrives_as_one_message(self):
        self.assertGreater(len(ORIGINAL), 12000)
        parts = outgoing(ORIGINAL)
        self.assertGreater(len(parts), 1)
        channel, adapter = receiver()

        async def replay():
            for index, part in enumerate(parts):
                await channel.handle_event(event(part, message_id=100 + index))

        run(replay())
        self.assertEqual(channel.stats.handled, 1)          # exactly one turn
        self.assertEqual(len(adapter.seen), 1, [m.text[:40] for m in adapter.seen])
        message = adapter.seen[0]
        self.assertIn(ORIGINAL, message.text)               # the original, whole
        self.assertEqual(message.text.count(ORIGINAL), 1)
        self.assertTrue(message.reassembled)

    def test_parts_that_arrive_out_of_order_are_joined_the_same(self):
        parts = outgoing(ORIGINAL)
        order = list(range(len(parts) - 2, len(parts))) + list(range(len(parts) - 2))
        channel, adapter = receiver()

        async def replay():
            for position, slot in enumerate(order):
                await channel.handle_event(event(parts[slot], message_id=500 + position))

        run(replay())
        self.assertEqual(len(adapter.seen), 1)
        self.assertIn(ORIGINAL, adapter.seen[0].text)

    def test_a_missing_part_is_delivered_with_a_note(self):
        parts = outgoing(ORIGINAL)
        self.assertGreaterEqual(len(parts), 3)
        channel, adapter = receiver()
        run(channel.handle_event(event(parts[0], message_id=1)))
        run(channel.handle_event(event(parts[-1], message_id=3)))
        self.assertEqual(adapter.seen, [])                  # held: the reply is incomplete
        # The window passes, and the next message from any bot sweeps the held reply out.
        channel._test_clock["t"] += 3600
        run(channel.handle_event(event("[Codex] unrelated, no mention", message_id=9)))
        self.assertEqual(len(adapter.seen), 1)
        delivered = adapter.seen[0].text
        self.assertIn("paragraph 0", delivered)             # what arrived
        self.assertIn("2", delivered)                       # which part is missing
        self.assertIn("never arrived", delivered)

    def test_a_fragment_without_its_first_part_starts_no_turn(self):
        channel, adapter = receiver()
        run(channel.handle_event(event("[Codex] ↪ (3/3) the rest", message_id=4)))
        self.assertEqual(adapter.seen, [])
        self.assertEqual(channel.stats.ignored, 1)

    def test_a_human_message_ending_in_a_part_marker_is_still_one_turn(self):
        channel, adapter = receiver()
        run(channel.handle_event(event("@HermesBot: totals\n\n(1/2)", author_id=1234567890,
                                       username="admin", is_bot=False)))
        self.assertEqual(len(adapter.seen), 1)

    def test_the_joined_message_is_answered_once_whatever_the_sender_splits(self):
        short = "\n\n".join(f"step {index}: " + "z" * 200 for index in range(30))
        parts = outgoing(short)
        self.assertGreater(len(parts), 1)
        channel, adapter = receiver()

        async def replay():
            for index, part in enumerate(parts):
                await channel.handle_event(event(part, message_id=700 + index))

        run(replay())
        self.assertEqual(len(adapter.seen), 1)
        self.assertIn(short, adapter.seen[0].text)


if __name__ == "__main__":
    unittest.main()
