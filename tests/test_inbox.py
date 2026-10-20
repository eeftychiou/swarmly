"""A message accepted for a turn survives a restart (inbox.py).

Live, round 4: a restart dropped a coordinator's unposted reply and a peer's queued hand-off,
and the swarm waited all night. A message is kept on disk from the moment it is accepted until
its turn is done, and the next start runs whatever is left.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.channel import ChannelEvent
from swarmly.telegram.inbox import MAX_AGE_SECONDS, Inbox
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config


def admin(text, message_id=10, thread_id=None):
    return event(text, author_id=ADMIN_ID, username="alice", is_bot=False,
                 message_id=message_id, thread_id=thread_id)


class InboxTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def channel(self):
        config = make_config(fallback={"gitbus_root": self.tmp.name})
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        return make_channel(config)

    def test_an_answered_message_leaves_nothing_behind(self):
        channel = self.channel()
        asyncio.run(channel.handle_event(admin("@HermesBot hello")))
        self.assertTrue(channel._test_sender.sent)
        self.assertEqual(self.channel()._inbox.interrupted(), [])

    def test_an_interrupted_message_is_run_again_on_the_next_start(self):
        first = self.channel()
        first._inbox.record(admin("@HermesBot carry on with #82", message_id=42))
        second = self.channel()                         # the restart

        async def scenario():
            self.assertEqual(second.replay_interrupted(), 1)
            await asyncio.gather(*second._self_tasks)

        asyncio.run(scenario())
        replies = [s for s in second._test_sender.sent if "carry on with #82" in s["text"]]
        self.assertEqual(len(replies), 1, second._test_sender.sent)
        self.assertEqual(self.channel()._inbox.interrupted(), [], "done once it ran")

    def test_a_peer_hand_off_is_replayed_as_the_peer_s(self):
        first = self.channel()
        first._inbox.record(event("@HermesBot: pushed 275ca33 on #94", message_id=7))
        self.assertTrue(str(first._inbox.path).startswith(self.tmp.name))
        replayed = self.channel()._inbox.interrupted()
        self.assertEqual(replayed[0]["author_username"], "CodexBot")
        self.assertTrue(replayed[0]["author_is_bot"])

    def test_commands_are_never_kept(self):
        channel = self.channel()
        asyncio.run(channel.handle_event(admin("/status@HermesBot", message_id=11)))
        self.assertEqual(channel._inbox._entries, {})

    def test_the_same_message_twice_at_once_runs_once(self):
        channel = self.channel()
        channel._inflight.add((admin("x").chat_id, 12))
        self.assertIsNone(asyncio.run(channel.handle_event(admin("@HermesBot hi", message_id=12))))
        self.assertEqual(channel._test_sender.sent, [])

    def test_a_cancelled_turn_while_stopping_is_kept(self):
        channel = self.channel()
        started = asyncio.Event()

        async def hang(*args, **kwargs):
            started.set()
            await asyncio.sleep(3600)

        channel._run_turn = hang

        async def scenario():
            task = asyncio.create_task(channel.handle_event(admin("@HermesBot long job",
                                                                    message_id=13)))
            await started.wait()
            channel._shutting_down = True
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual([e["message_id"] for e in self.channel()._inbox.interrupted()], [13])

    def test_old_entries_are_not_replayed(self):
        clock = {"t": 1_000_000.0}
        inbox = Inbox(Path(self.tmp.name) / "inbox.json", clock=lambda: clock["t"])
        inbox.record(ChannelEvent(text="old", chat_id=1, message_id=1))
        clock["t"] += MAX_AGE_SECONDS + 1
        self.assertEqual(inbox.interrupted(), [])
        self.assertEqual(inbox._entries, {})


if __name__ == "__main__":
    unittest.main()
