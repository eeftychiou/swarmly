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

class ResumedTurnTests(unittest.TestCase):
    """Issue #84: a turn killed by a die-off comes back once, and never becomes a loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def channel(self):
        config = make_config(fallback={"gitbus_root": self.tmp.name})
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel.bot = object()
        return channel

    def record(self, channel, *, message_id=42, text="@HermesBot carry on with #84",
               thread_id=77):
        channel._inbox.record(admin(text, message_id=message_id, thread_id=thread_id))

    def test_a_resume_quotes_the_request_and_says_when_it_was_cut_off(self):
        first = self.channel()
        self.record(first)
        second = self.channel()                          # the restart

        async def scenario():
            self.assertEqual(second.replay_interrupted(), 1)
            await asyncio.gather(*list(second._self_tasks))

        asyncio.run(scenario())
        sent = [m["text"] for m in second._test_sender.sent]
        notices = [t for t in sent if "↻ resuming the turn cut off at" in t]
        self.assertEqual(len(notices), 1, sent)
        prompts = [t for t in sent if "cut off by a restart" in t]
        # The prompt names the sender, quotes the request, and tells it to check first -- and
        # it goes to the topic the request came from, not to General.
        self.assertEqual(len(prompts), 1, sent)
        self.assertIn("carry on with #84", prompts[0])
        self.assertIn("Check what you already pushed or posted", prompts[0])
        self.assertEqual(self.channel()._inbox.interrupted(), [], "the record is closed")

    def test_a_resume_that_is_cut_off_too_is_dropped_and_reported(self):
        first = self.channel()
        self.record(first)
        # Try #2 is claimed and written to disk, then the process is killed outright -- no
        # Python cleanup runs, which is exactly what a die-off looks like.
        self.assertEqual(len(self.channel()._inbox.claim_replay()[0]), 1)
        third = self.channel()

        async def scenario():
            self.assertEqual(third.replay_interrupted(), 0)     # no third try
            await asyncio.gather(*list(third._self_tasks))

        asyncio.run(scenario())
        sent = [m["text"] for m in third._test_sender.sent]
        reports = [t for t in sent if "after two restarts" in t]
        self.assertEqual(len(reports), 1, sent)
        self.assertIn("message 42", reports[0])
        self.assertEqual(third._inbox._entries, {}, "dropped, not kept for a fourth try")

    def test_a_turn_that_ended_normally_leaves_nothing_to_resume(self):
        channel = self.channel()
        asyncio.run(channel.handle_event(admin("@HermesBot hello", message_id=8)))
        self.assertEqual(self.channel()._inbox.interrupted(), [])
        self.assertEqual(self.channel().replay_interrupted(), 0)

    def test_the_claim_counts_the_try_before_the_replay_starts(self):
        inbox = Inbox(Path(self.tmp.name) / "inbox.json")
        inbox.record(ChannelEvent(text="work", chat_id=1, message_id=2))
        runnable, dropped = inbox.claim_replay()
        self.assertEqual([attempt for attempt, _ in runnable], [1])
        self.assertEqual(dropped, [])
        self.assertEqual(inbox.attempts(1, 2), 1)
        # A second claim -- a restart before the first resume finished -- gives up instead.
        self.assertEqual(inbox.claim_replay()[0], [])
        self.assertEqual(len(inbox.claim_replay()[1]), 0)    # already dropped by the call above
        self.assertEqual(inbox._entries, {})
