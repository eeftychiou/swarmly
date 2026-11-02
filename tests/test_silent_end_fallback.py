"""A turn that owed an answer may not end in silence (#497).

Live, 31 Oct: a turn answered an assignment addressed to it with ``NO_REPLY`` and its topic
went quiet. Nothing was posted, so the sender could not tell a lost reply from a dead
transport without asking. An admin's message and an assignment that names this agent now get
a short fallback line instead; unaddressed peer traffic and the transport's own briefs keep
their silence, which is what the sentinel is for.
"""

import unittest

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from tests.aio import run
from tests.test_telegram_channel import ADMIN_ID, event, make_channel


class Silent(EchoAdapter):
    """A runtime that chose the sentinel: the transport never posts ``NO_REPLY``."""

    async def respond(self, message):
        return AdapterReply(text="NO_REPLY")


def posted(channel):
    return [entry["text"] for entry in channel._test_sender.sent]


class SilentEndTests(unittest.TestCase):
    def test_an_admin_question_never_ends_in_silence(self):
        channel = make_channel(adapter=Silent("Hermes"))
        run(channel.handle_event(event(
            "@HermesBot please take #497", author_id=ADMIN_ID, username="admin",
            is_bot=False, replies_to_bot=True, thread_id=9400, message_id=71)))
        lines = posted(channel)
        self.assertEqual(len(lines), 1)
        self.assertIn("finished the turn without a reply", lines[0])

    def test_an_assignment_addressed_to_this_agent_never_ends_in_silence(self):
        channel = make_channel(adapter=Silent("Hermes"))
        run(channel.handle_event(event(
            "@HermesBot develop #497", author_id=404, username="CodexBot", is_bot=True,
            thread_id=9401, message_id=72)))
        lines = posted(channel)
        self.assertEqual(len(lines), 1)
        self.assertIn("finished the turn without a reply", lines[0])

    def test_the_transports_own_brief_may_still_end_in_silence(self):
        channel = make_channel(adapter=Silent("Hermes"))
        brief = event("carry on from where you stopped", author_id=ADMIN_ID,
                      username="admin", is_bot=False, thread_id=9402, message_id=73)
        brief.self_task = True
        run(channel.handle_event(brief))
        self.assertEqual(posted(channel), [])

    def test_unaddressed_peer_traffic_posts_nothing(self):
        channel = make_channel(adapter=Silent("Hermes"))
        run(channel.handle_event(event(
            "moving the deploy ticket forward", author_id=404, username="CodexBot",
            is_bot=True, thread_id=9403, message_id=74)))
        self.assertEqual(posted(channel), [])

    def test_a_real_answer_is_posted_once_and_not_padded(self):
        channel = make_channel()                 # EchoAdapter: a real line
        run(channel.handle_event(event(
            "@HermesBot please take #497", author_id=ADMIN_ID, username="admin",
            is_bot=False, replies_to_bot=True, thread_id=9404, message_id=75)))
        lines = posted(channel)
        self.assertEqual(len(lines), 1)
        self.assertNotIn("finished the turn without a reply", lines[0])


if __name__ == "__main__":
    unittest.main()
