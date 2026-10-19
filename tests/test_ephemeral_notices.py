"""Tests for short-lived notices (issue #64).

Each transport deletes its *own* machine notices in General -- ONLINE, SEEN, busy, paused,
moved, reminders -- once they are older than ``limits.ephemeral_notices_minutes`` (default
60, 0 disables). A person's message and a model reply are never touched: they are not
notices, so they are never recorded as one.

No network and no aiogram: the sender and the bot are fakes, so every test pins what the
transport records and what it asks Telegram to delete -- never a live group.

Run: python -m unittest tests.test_ephemeral_notices -v
"""

from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.cleanup import LEDGER_TTL_SECONDS, GeneralLedger
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.adapters import EchoAdapter

GROUP = -1001234567890
ADMIN_ID = 1234567890
#: Wall clock at import: the ledger prunes against ``time.time()``, so a fixed past epoch
#: would age out of the 48-hour window and every test would pass for the wrong reason.
NOW = time.time()
MINUTE = 60.0


def run(coro):
    return asyncio.run(coro)


class Sent:
    """What aiogram returns from a send: the transport reads ``message_id`` off it."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    """The aiogram seam: records the delete calls the sweep makes."""

    def __init__(self, fail: bool = False) -> None:
        self.messages = []
        self.fail = fail

    async def delete_messages(self, chat_id=None, message_ids=None):
        self.messages.append((chat_id, list(message_ids or [])))
        if self.fail:
            raise RuntimeError("Bad Request: message can't be deleted")


class NumberedSender:
    """A sender that hands out ascending message ids, like Telegram does."""

    def __init__(self, first: int = 500) -> None:
        self.next_id = first
        self.sent = []

    async def __call__(self, payload, reply_to_message_id=None):
        self.sent.append(payload)
        message_id = self.next_id
        self.next_id += 1
        return Sent(message_id)


def config_data(limit=60.0):
    return {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
        "peers": {"Codex": "CodexBot"},
        "adapter": {"type": "echo"},
        "limits": {"min_seconds_between_messages": 0, "selftest_seconds": 0,
                   "min_seconds_between_agent_turns": 0,
                   "ephemeral_notices_minutes": limit},
    }


class Case(unittest.TestCase):
    limit = 60.0

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = GeneralLedger(self.root / "state" / "general-hermes.json")
        self.sender = NumberedSender()
        self.config = AgentConfig.from_dict(config_data(self.limit))
        self.channel = AgentChannel(self.config, adapter=EchoAdapter("Hermes"),
                                    sender=self.sender, clock=lambda: NOW,
                                    sleeper=lambda seconds: None)
        self.bot = FakeBot()
        self.channel.bot = self.bot
        self.channel.ledger = self.ledger          # one ledger, the test's, not a second file

    def send(self, text: str, **kwargs):
        return run(self.channel.send(text, **kwargs))

    def sweep(self, now: float) -> int:
        return run(self.channel.sweep_ephemeral_notices(now=now))


class RecordingTests(Case):
    def test_a_notice_send_is_recorded_with_its_time(self):
        self.send("🟢 Hermes online (adapter: echo, rev: abc)", silent=True, notice=True)
        self.assertEqual(self.ledger.notices(GROUP), [500])

    def test_a_model_reply_is_not_a_notice(self):
        self.send("✅ done: the fix is on main")
        self.assertEqual(self.ledger.notices(GROUP), [])
        self.assertEqual(self.ledger.recent(GROUP), [500])      # still deletable by /cleanup

    def test_a_notice_in_a_topic_is_not_recorded(self):
        # Only General notices are swept: a topic's messages go with the topic.
        self.send("⏳ busy", chat_id=GROUP, thread_id=42, notice=True)
        self.assertEqual(self.ledger.notices(GROUP), [])

    def test_a_notice_survives_a_restart(self):
        self.send("🟢 online", silent=True, notice=True)
        again = GeneralLedger(self.root / "state" / "general-hermes.json")
        self.assertEqual(again.notices(GROUP), [500])


class SweepTests(Case):
    def test_a_notice_is_deleted_after_the_limit(self):
        self.send("🟢 online", silent=True, notice=True)
        at = time.time() + 61 * MINUTE
        self.assertEqual(self.sweep(at), 1)
        self.assertEqual(self.bot.messages, [(GROUP, [500])])

    def test_a_notice_is_kept_before_the_limit(self):
        self.send("🟢 online", silent=True, notice=True)
        self.assertEqual(self.sweep(time.time() + 59 * MINUTE), 0)
        self.assertEqual(self.bot.messages, [])

    def test_only_own_notices_are_deleted_humans_and_peers_are_not(self):
        # A person's message and a peer's message are in the ledger (so /cleanup may delete
        # them), and one of this transport's own notices is not: only the notice goes.
        self.ledger.note(GROUP, [11, 12], now=NOW - 2 * 60 * MINUTE)
        self.ledger.note_notice(GROUP, 500, now=NOW - 2 * 60 * MINUTE)
        self.assertEqual(self.sweep(NOW), 1)
        self.assertEqual(self.bot.messages, [(GROUP, [500])])
        self.assertEqual(self.ledger.recent(GROUP), [11, 12])

    def test_a_deleted_notice_is_no_longer_offered(self):
        self.ledger.note_notice(GROUP, 500, now=NOW - 2 * 60 * MINUTE)
        self.sweep(NOW)
        self.assertEqual(self.ledger.notices(GROUP), [])
        self.assertEqual(self.ledger.recent(GROUP), [])     # /cleanup stops offering it too
        self.assertEqual(self.sweep(NOW), 0)

    def test_what_telegram_refuses_is_not_retried_forever(self):
        self.ledger.note_notice(GROUP, 500, now=NOW - 2 * 60 * MINUTE)
        self.bot.fail = True
        self.assertEqual(self.sweep(NOW), 0)
        self.assertEqual(self.ledger.notices(GROUP), [])
        self.bot.messages.clear()
        self.assertEqual(self.sweep(NOW), 0)
        self.assertEqual(self.bot.messages, [])

    def test_a_notice_past_the_delete_window_is_pruned_not_offered(self):
        self.ledger.note_notice(GROUP, 500, now=NOW - LEDGER_TTL_SECONDS - 60)
        self.assertEqual(self.ledger.notices(GROUP), [])
        self.assertEqual(self.sweep(NOW), 0)
        self.assertEqual(self.bot.messages, [])

    def test_many_notices_go_in_batches(self):
        self.ledger.note_notice(GROUP, 1, now=NOW - 2 * 60 * MINUTE)
        self.ledger.note_notice(GROUP, 2, now=NOW - 2 * 60 * MINUTE)
        self.assertEqual(self.sweep(NOW), 2)
        self.assertEqual(self.bot.messages, [(GROUP, [1, 2])])


class DisabledTests(Case):
    limit = 0.0

    def test_zero_disables_the_sweep(self):
        self.ledger.note_notice(GROUP, 500, now=NOW - 10 * 60 * MINUTE)
        self.assertEqual(self.channel.ephemeral_notices_minutes(), 0.0)
        self.assertEqual(self.sweep(NOW), 0)
        self.assertEqual(self.bot.messages, [])
        self.assertEqual(self.ledger.notices(GROUP), [500])   # still the admin's to remove


class TickTests(Case):
    def test_the_tick_sweeps_and_survives_an_error(self):
        calls = []

        async def boom(now=None):
            calls.append(now)
            raise RuntimeError("Telegram is down")

        self.channel.sweep_ephemeral_notices = boom

        async def once():
            task = asyncio.create_task(self.channel._ephemeral_tick())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        run(once())
        self.assertEqual(len(calls), 1)                       # it ran, and did not die
        self.assertGreaterEqual(self.channel.stats.errors, 1)


if __name__ == "__main__":
    unittest.main()
