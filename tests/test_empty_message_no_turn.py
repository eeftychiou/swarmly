"""A message with no text and nothing attached is never a turn (live, 25 Oct: the coordinator
answered "your message reached me with no text" in a topic the admin had just opened)."""

import types
import unittest

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.channel import attachments_from_aiogram
from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, run


class Counting(EchoAdapter):
    def __init__(self):
        super().__init__("Hermes")
        self.prompts = []

    async def respond(self, message):
        self.prompts.append(message)
        return await super().respond(message)


def admin_reply(text, **kw):
    """The admin, replying to this bot in a work topic: addressed, so it is a turn."""
    return event(text, author_id=ADMIN_ID, username="admin", is_bot=False,
                 replies_to_bot=True, thread_id=9400, **kw)


class EmptyMessageTests(unittest.TestCase):
    def test_no_text_and_nothing_attached_is_no_turn(self):
        adapter = Counting()
        channel = make_channel(adapter=adapter)
        self.assertIsNone(run(channel.handle_event(admin_reply("", message_id=5))))
        self.assertIsNone(run(channel.handle_event(admin_reply("   ", message_id=6))))
        self.assertEqual(adapter.prompts, [])
        self.assertEqual(channel._test_sender.sent, [])

    def test_the_same_message_with_text_is_a_turn(self):
        adapter = Counting()
        channel = make_channel(adapter=adapter)
        run(channel.handle_event(admin_reply("please review #12", message_id=7)))
        self.assertEqual(len(adapter.prompts), 1)

    def test_an_attachment_without_a_caption_is_still_a_turn(self):
        adapter = Counting()
        channel = make_channel(adapter=adapter)
        photo = {"kind": "photo", "file_id": "x", "name": "screen.png"}
        run(channel.handle_event(admin_reply("", message_id=8, attachments=[photo])))
        self.assertEqual(len(adapter.prompts), 1)

    def test_a_sticker_in_general_is_still_recorded_for_cleanup(self):
        channel = make_channel(adapter=Counting())
        sticker = event("", author_id=ADMIN_ID, username="admin", is_bot=False, message_id=31)
        self.assertIsNone(run(channel.handle_event(sticker)))
        self.assertIn(31, channel.ledger.recent(GROUP))


class RoundVideoTests(unittest.TestCase):
    def test_a_round_video_is_an_attachment_so_it_still_gets_a_turn(self):
        message = types.SimpleNamespace(
            photo=None, document=None, voice=None, audio=None, video=None,
            video_note=types.SimpleNamespace(file_id="round", file_size=700))
        found = attachments_from_aiogram(message)
        self.assertEqual([item["file_id"] for item in found], ["round"])
        self.assertEqual(found[0]["name"], "video_note.mp4")


if __name__ == "__main__":
    unittest.main()
