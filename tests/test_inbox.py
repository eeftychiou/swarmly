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
from swarmly.telegram.inbox import event_from
from swarmly.telegram.inbox import MAX_AGE_SECONDS, RESUME_MAX_AGE_SECONDS, Inbox
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
        # Reading is read-only (#103): it says what is there, and prunes nothing.
        self.assertEqual(inbox.interrupted(), [])
        self.assertEqual(len(inbox._entries), 1)
        self.assertEqual(inbox.claim_replay(), ([], []))
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
        # A second claim while that resume is still running (a re-entered start) spends nothing.
        self.assertTrue(inbox.claiming())
        self.assertEqual(inbox.claim_replay(), ([], []))
        self.assertEqual(inbox.attempts(1, 2), 1)
        # Once the resume is done and (as here) it never closed its record, the next claim has
        # nothing left to spend: the one try is used, so the record is dropped, not re-run.
        inbox.released()
        self.assertFalse(inbox.claiming())
        runnable, dropped = inbox.claim_replay()
        self.assertEqual(runnable, [])
        self.assertEqual([entry.get("reason") for entry in dropped], ["twice"])
        self.assertEqual(inbox._entries, {})


class HardeningTests(unittest.TestCase):
    """Issue #103: the review of #102, one item at a time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = {"t": 1_000_000.0}

    def inbox(self, **kwargs):
        return Inbox(Path(self.tmp.name) / "inbox.json", clock=lambda: self.clock["t"], **kwargs)

    def channel(self):
        config = make_config(fallback={"gitbus_root": self.tmp.name})
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel.bot = object()
        channel._inbox.clock = lambda: self.clock["t"]
        return channel

    def test_reading_what_a_restart_would_resume_spends_nothing(self):
        inbox = self.inbox()
        inbox.record(ChannelEvent(text="work", chat_id=1, message_id=2))
        self.assertEqual(len(inbox.interrupted()), 1)
        self.assertEqual(len(inbox.interrupted()), 1)        # still there...
        self.assertEqual(inbox.attempts(1, 2), 0)           # ...and no try spent
        self.assertEqual(len(inbox.claim_replay()[0]), 1)   # only the claim spends it
        self.assertEqual(inbox.attempts(1, 2), 1)

    def test_a_restart_resumes_one_record_per_tick(self):
        channel = self.channel()
        for message_id in (1, 2, 3):
            channel._inbox.record(admin(f"@HermesBot job {message_id}", message_id=message_id))

        async def scenario():
            self.assertEqual(channel.replay_interrupted(), 1)     # the oldest, alone
            for _ in range(10):                                   # each resumes, then the next
                pending = [t for t in channel._self_tasks if not t.done()]
                if not pending:
                    break
                await asyncio.gather(*pending)

        asyncio.run(scenario())
        prompts = [m["text"] for m in channel._test_sender.sent if "cut off by a restart" in m["text"]]
        # All three come back, one after another and oldest first -- never three at once.
        self.assertEqual([p.split("    @HermesBot job ")[1][0] for p in prompts], ["1", "2", "3"],
                         prompts)
        self.assertEqual(channel._inbox._entries, {})

    def test_an_old_record_is_reported_and_not_resumed(self):
        channel = self.channel()
        channel._inbox.record(admin("@HermesBot ancient", message_id=9))
        self.clock["t"] += RESUME_MAX_AGE_SECONDS + 60

        async def scenario():
            self.assertEqual(channel.replay_interrupted(), 0)
            await asyncio.gather(*list(channel._self_tasks))

        asyncio.run(scenario())
        sent = [m["text"] for m in channel._test_sender.sent]
        self.assertFalse([t for t in sent if "cut off by a restart" in t], sent)
        reports = [t for t in sent if "did not resume the turn on message 9" in t]
        self.assertEqual(len(reports), 1, sent)
        self.assertIn("stale", reports[0])
        self.assertEqual(channel._inbox._entries, {})

    def test_a_completed_resume_leaves_no_record_at_all(self):
        channel = self.channel()
        channel._inbox.record(admin("@HermesBot finish me", message_id=5))

        async def scenario():
            channel.replay_interrupted()
            await asyncio.gather(*list(channel._self_tasks))

        asyncio.run(scenario())
        self.assertEqual(channel._inbox._entries, {}, "nothing left, not even the key")

class ResumeNoticeAndAttachmentsTests(unittest.TestCase):
    """Issue #103 items 7 and 8: a notice that fails must not spend the replay, and a request
    that came with files resumes with them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def channel(self):
        config = make_config(fallback={"gitbus_root": self.tmp.name})
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel.bot = object()
        return channel

    def test_a_failed_resume_notice_does_not_spend_the_replay(self):
        channel = self.channel()
        channel._inbox.record(admin("@HermesBot finish me", message_id=31))
        sent_notices = []

        async def send(text, **kwargs):
            if "↻ resuming" in text:
                sent_notices.append(text)
                raise RuntimeError("telegram is down")
            sent_notices.append(text)

        channel.send = send

        async def scenario():
            self.assertEqual(channel.replay_interrupted(), 1)
            for _ in range(5):
                pending = [t for t in channel._self_tasks if not t.done()]
                if not pending:
                    break
                await asyncio.gather(*pending)

        asyncio.run(scenario())
        self.assertTrue(sent_notices, "the turn ran anyway")
        prompts = [t for t in sent_notices if "cut off by a restart" in t]
        self.assertEqual(len(prompts), 1, sent_notices)      # the resume still happened
        self.assertEqual(channel._inbox._entries, {}, "and it closed its record")

    def test_an_attachment_only_request_resumes_with_its_attachment(self):
        channel = self.channel()
        channel._inbox.record(event("", message_id=33, attachments=[
            {"file_id": "abc", "file_name": "log.txt", "mime_type": "text/plain"}]))
        seen = []

        async def capture(task, *args, **kwargs):
            seen.append(task)

        channel.handle_event = capture

        async def scenario():
            channel.replay_interrupted()
            for _ in range(5):
                pending = [t for t in channel._self_tasks if not t.done()]
                if not pending:
                    break
                await asyncio.gather(*pending)

        asyncio.run(scenario())
        resumed = [t for t in seen if "cut off by a restart" in str(getattr(t, "text", ""))]
        self.assertEqual(len(resumed), 1, seen)
        self.assertEqual(resumed[0].attachments[0]["file_name"], "log.txt")

class ClaimReleaseTests(unittest.TestCase):
    """#104 review (Codex): a claim that starts nothing must not leave the queue stalled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def channel(self):
        config = make_config(fallback={"gitbus_root": self.tmp.name})
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel.bot = object()
        return channel

    def test_a_claim_that_starts_nothing_does_not_stall_the_queue(self):
        channel = self.channel()
        channel._inbox.record(admin("@HermesBot one", message_id=61))
        channel._inbox.record(admin("@HermesBot two", message_id=62))

        def fail(event, record):
            raise RuntimeError("the resume could not be started")

        channel._resume_turn = fail
        self.assertEqual(channel.replay_interrupted(), 0)
        self.assertFalse(channel._inbox.claiming(), "the claim is released, not held")

        started = []
        channel._resume_turn = lambda event, record: started.append(event.message_id)
        self.assertEqual(channel.replay_interrupted(), 1)      # the queue still moves
        self.assertEqual(started, [62])

    def test_an_unrebuildable_record_is_closed_not_left_on_disk(self):
        channel = self.channel()
        channel._inbox.record(admin("@HermesBot broken", message_id=63))
        channel._resume_turn = lambda event, record: None
        original = event_from

        def refuse(data, event_type):
            raise TypeError("cannot rebuild this one")

        import swarmly.telegram.inbox as inbox_module
        inbox_module.event_from = refuse
        try:
            self.assertEqual(channel.replay_interrupted(), 0)
        finally:
            inbox_module.event_from = original
        self.assertFalse(channel._inbox.claiming())
        self.assertEqual(channel._inbox._entries, {}, "dropped, not claimed forever")
