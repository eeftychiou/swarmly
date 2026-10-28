"""Tests for ``/cleanup`` (issue #63; simplified 25 Oct): one command deletes every finished
topic, at once.

No network and no aiogram: the Telegram calls are faked, both at the ``Cleanup`` seam
(a host with the methods it needs) and at the channel's aiogram seam (a fake bot), so
the tests pin what the command decides and what it asks Telegram to do -- never a live group.

Run: python -m unittest tests.test_cleanup -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram.adapters.base import load_state
from tests.aio import run
from swarmly.telegram.channel import AgentChannel, ChannelEvent
from swarmly.telegram.cleanup import (FINISHED_IDLE_SECONDS, LEDGER_TTL_SECONDS,
                                      SINGLE_DELETE_MAX, Cleanup, DeleteUnavailable,
                                      GeneralLedger, TopicInfo)
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.topics import TopicMap
from swarmly.telegram.topic_memory import TopicMemory
from swarmly.telegram.adapters import EchoAdapter

GROUP = -1001234567890
ADMIN_ID = 1234567890
OTHER_ID = 555
NOW = 1_700_000_000.0
HOUR = 3600.0


class FakeHost:
    """The channel side of /cleanup, recorded instead of called."""

    def __init__(self, topics=(), chat_id=GROUP, delete_ok=True,
                 message_batches=None, protected=(), refuse_messages=()) -> None:
        self.topics = list(topics)
        self.chat_id = chat_id
        self.delete_ok = delete_ok
        self.message_batches = message_batches      # None: every id goes; else per-batch count
        #: Ids Telegram will not delete: a whole batch holding one is refused (#395).
        self.refuse_messages = {int(i) for i in refuse_messages}
        #: When set, every call raises as though Telegram never answered (#398).
        self.unavailable = None
        #: When set, every call raises this instead -- an error that is not DeleteUnavailable.
        self.crash = None
        #: True: every call answers None -- the host's "could not ask" (#400).
        self.say_nothing = False
        self.protected = set(protected)
        self.deleted_topics = []
        self.deleted_messages = []
        self.forgotten = []

    def cleanup_home_chat(self):
        return self.chat_id

    def cleanup_topics(self):
        return list(self.topics)

    def cleanup_protected_topics(self):
        return set(self.protected)

    async def cleanup_delete_topic(self, chat_id, thread_id):
        self.deleted_topics.append(int(thread_id))
        return self.delete_ok and int(thread_id) not in getattr(self, "refuse", set())

    async def cleanup_delete_messages(self, chat_id, ids):
        batch = [int(i) for i in ids]
        self.deleted_messages.append(batch)
        if self.unavailable:
            raise DeleteUnavailable(self.unavailable)
        if self.crash is not None:
            raise self.crash
        if self.say_nothing:
            return None                  # not answered: not a refusal (#400)
        if self.message_batches is not None:
            return self.message_batches.pop(0) if self.message_batches else 0
        if any(mid in self.refuse_messages for mid in batch):
            return 0                    # all-or-nothing, exactly as Telegram does it
        return len(batch)

    async def cleanup_forget_topic(self, chat_id, thread_id):
        self.forgotten.append(int(thread_id))


def topic(thread_id, name="work", activity=NOW - 30 * HOUR, open_reason=None):
    return TopicInfo(thread_id=thread_id, chat_id=GROUP, title=name,
                     last_activity=activity, open_reason=open_reason)


class CleanupCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger_path = self.root / "state" / "general-hermes.json"
        self.ledger = GeneralLedger(self.ledger_path)
        self.now = NOW
        #: The pauses between one-at-a-time deletes: recorded, never waited.
        self.slept: list = []

    def cleanup(self, host, **kwargs) -> Cleanup:
        kwargs.setdefault("handle", "Hermes")
        kwargs.setdefault("sleep", self.slept.append)
        return Cleanup(host, self.ledger, clock=lambda: self.now, **kwargs)

    def ask(self, cleanup, *words, is_admin=True):
        return run(cleanup.handle_args(list(words), is_admin=is_admin, chat_id=GROUP))


class OneCommandTests(CleanupCase):
    def test_cleanup_deletes_every_finished_topic_at_once(self):
        host = FakeHost([topic(10, "Round 3"), topic(11, "Round 5.5"),
                         topic(12, "Live work", open_reason="open work item L1 #268")])
        reply = self.ask(self.cleanup(host))
        self.assertEqual(host.deleted_topics, [10, 11])
        self.assertEqual(host.forgotten, [10, 11])
        self.assertIn("deleted 2 topic(s): Round 3 (10), Round 5.5 (11)", reply)
        self.assertIn("Live work (12) (open work item L1 #268)", reply)

    def test_protected_and_kept_topics_stay(self):
        host = FakeHost([topic(3418, "Swarm pulse"), topic(13481, "Swarm test"), topic(20)],
                        protected={3418})
        reply = self.ask(self.cleanup(host, keep=[13481]))
        self.assertEqual(host.deleted_topics, [20])
        self.assertIn("Swarm pulse (3418) (protected)", reply)
        self.assertIn("Swarm test (13481) (protected)", reply)

    def test_a_topic_active_in_the_last_hour_is_not_finished(self):
        recent = NOW - FINISHED_IDLE_SECONDS / 2
        host = FakeHost([topic(30, "Just opened", activity=recent)])
        reply = self.ask(self.cleanup(host))
        self.assertEqual(host.deleted_topics, [])
        self.assertIn("active 30m ago", reply)

    def test_a_topic_with_no_recorded_activity_is_kept(self):
        """Review: a topic this bot opened minutes ago had no activity and was deleted."""
        host = FakeHost([topic(31, activity=None)])
        reply = self.ask(self.cleanup(host))
        self.assertEqual(host.deleted_topics, [])
        self.assertIn("no activity recorded", reply)

    def test_work_that_starts_during_the_run_keeps_its_topic(self):
        host = FakeHost([topic(60, "A"), topic(61, "B")])
        real_delete = host.cleanup_delete_topic

        async def delete(chat_id, thread_id):
            host.topics = [topic(61, "B", open_reason="a turn is running")]
            return await real_delete(chat_id, thread_id)
        host.cleanup_delete_topic = delete
        reply = self.ask(self.cleanup(host))
        self.assertEqual(host.deleted_topics, [60])
        self.assertIn("B (61) (a turn is running)", reply)

    def test_nothing_to_delete_is_said_plainly(self):
        reply = self.ask(self.cleanup(FakeHost([])))
        self.assertIn("deleted 0 topic(s).", reply)

    def test_a_failed_delete_is_reported_and_not_forgotten(self):
        host = FakeHost([topic(40, "Stuck")], delete_ok=False)
        reply = self.ask(self.cleanup(host))
        self.assertEqual(host.forgotten, [])
        self.assertIn("Could not delete", reply)

    def test_the_reply_never_starts_with_the_bots_own_handle(self):
        reply = self.ask(self.cleanup(FakeHost([]), handle="@Claude_obot"))
        self.assertNotIn("@Claude_obot", reply)


class OtherFormTests(CleanupCase):
    def test_preview_deletes_nothing(self):
        host = FakeHost([topic(10, "Round 3"), topic(12, open_reason="a turn is running")])
        reply = self.ask(self.cleanup(host), "preview")
        self.assertEqual(host.deleted_topics, [])
        self.assertIn("would delete 1 topic(s): Round 3 (10)", reply)
        self.assertIn("a turn is running", reply)

    def test_topic_deletes_one_named_topic_now(self):
        host = FakeHost([topic(50, "One", open_reason="open: x")])
        self.assertIn("deleted 1 topic(s): One (50)", self.ask(self.cleanup(host), "topic", "50"))
        self.assertEqual(host.deleted_topics, [50])

    def test_topic_refuses_a_protected_one(self):
        host = FakeHost([topic(3418)], protected={3418})
        self.assertIn("protected", self.ask(self.cleanup(host), "topic", "3418"))
        self.assertEqual(host.deleted_topics, [])

    def test_general_deletes_the_ledger_oldest_first_and_forgets_what_went(self):
        self.ledger.note(GROUP, [5, 6, 7], now=NOW - HOUR)
        host = FakeHost()
        reply = self.ask(self.cleanup(host), "general")
        self.assertEqual(host.deleted_messages, [[5, 6, 7]])
        self.assertIn("deleted 3 General message(s)", reply)
        self.assertEqual(self.ledger.recent(GROUP, NOW), [])

    def test_a_batch_that_only_partly_goes_is_finished_one_id_at_a_time(self):
        # The 100 are asked first; the 40 a prefix-reporting host took are kept, and the 70
        # left are asked again one at a time (#398 nit: the old name said "stops").
        from swarmly.telegram.cleanup import DELETE_BATCH
        self.ledger.note(GROUP, list(range(1, DELETE_BATCH + 11)), now=NOW - HOUR)
        host = FakeHost(message_batches=[40])
        self.ask(self.cleanup(host), "general")
        self.assertEqual(len(host.deleted_messages), 72)

    def test_one_undelatable_id_does_not_cost_the_others(self):
        # #395, live 27 Oct: the pulse recorded peers' posts as its own notices, and one id
        # Telegram refused dropped the whole batch of 100 -- so nothing was ever deleted.
        self.ledger.note(GROUP, [5, 6, 7, 8], now=NOW - HOUR)
        host = FakeHost(refuse_messages={6})
        reply = self.ask(self.cleanup(host), "general")
        self.assertEqual(host.deleted_messages[0], [5, 6, 7, 8])   # the batch first
        self.assertIn("deleted 3 General message(s)", reply)
        self.assertIn("Telegram refused 1 (not this bot's, or already gone): 6", reply)
        # What went and what Telegram refused are both settled: neither is offered again.
        self.assertEqual(self.ledger.recent(GROUP, NOW), [])

    def test_what_telegram_never_answered_is_kept_for_the_next_cleanup(self):
        # #398: a 429 or a network blip is not a refusal -- dropping those ids would forget
        # messages that are still perfectly deletable.
        self.ledger.note(GROUP, [5, 6], now=NOW - HOUR)
        host = FakeHost()
        host.unavailable = "Flood control exceeded, retry in 30"
        reply = self.ask(self.cleanup(host), "general")
        self.assertIn("deleted 0 General message(s)", reply)
        self.assertIn("2 could not be asked about", reply)
        self.assertEqual(self.ledger.recent(GROUP, NOW), [5, 6])     # still offered
        host.unavailable = None
        self.assertIn("deleted 2 General message(s)", self.ask(self.cleanup(host), "general"))
        self.assertEqual(self.ledger.recent(GROUP, NOW), [])

    def test_a_host_that_never_answers_keeps_the_ids_whatever_the_shape(self):
        """#400: an error, or a host that says ``None``, is not a refusal.

        A host with no bot attached or a broken transport used to answer 0 for every call,
        which read as "Telegram refused them all" and dropped every id from the ledger -- one
        outage lost every notice that was due. Only a call that comes back with a count
        settles an id.
        """
        shapes = {"raises": {"crash": RuntimeError("no bot attached")},
                  "says None": {"say_nothing": True}}
        for label, settings in shapes.items():
            with self.subTest(label):
                self.ledger.note(GROUP, [5, 6], now=NOW - HOUR)
                host = FakeHost()
                for field, value in settings.items():
                    setattr(host, field, value)
                reply = self.ask(self.cleanup(host), "general")
                self.assertIn("2 could not be asked about", reply)
                self.assertEqual(self.ledger.recent(GROUP, NOW), [5, 6], label)

    def test_the_one_at_a_time_fallback_pauses_between_calls(self):
        # #398: the fallback is one Bot API call per id; unpaced it walked into ~30/s.
        self.ledger.note(GROUP, [5, 6, 7], now=NOW - HOUR)
        host = FakeHost(refuse_messages={6})
        self.ask(self.cleanup(host), "general")
        self.assertEqual(len(host.deleted_messages), 4)      # the batch, then one per id
        self.assertEqual(len(self.slept), 2)                 # a pause between the three
        self.assertTrue(all(pause > 0 for pause in self.slept))

    def test_the_one_at_a_time_fallback_is_capped_and_the_rest_is_kept(self):
        from swarmly.telegram.cleanup import DELETE_BATCH
        # Three whole batches refused: the fallback would need 300 single calls, the cap
        # stops it at SINGLE_DELETE_MAX and keeps the rest for the next /cleanup (#398).
        ids = list(range(1, 3 * DELETE_BATCH + 6))
        self.ledger.note(GROUP, ids, now=NOW - HOUR)
        host = FakeHost(refuse_messages={mid for mid in ids if mid % DELETE_BATCH == 2})
        reply = self.ask(self.cleanup(host), "general")
        self.assertIn("could not be asked about", reply)     # capped, not refused
        self.assertEqual(len(host.deleted_messages), 3 + SINGLE_DELETE_MAX)
        self.assertEqual(len(self.ledger.recent(GROUP, NOW)), len(ids) - SINGLE_DELETE_MAX)

    def test_a_non_admin_gets_a_refusal_and_nothing_else(self):
        host = FakeHost([topic(10)])
        reply = self.ask(self.cleanup(host), is_admin=False)
        self.assertIn("only the admin runs /cleanup", reply)
        self.assertEqual(host.deleted_topics, [])

    def test_an_unknown_form_gets_a_usage_line(self):
        self.assertIn("/cleanup preview", self.ask(self.cleanup(FakeHost()), "everything"))


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "general.json"

    def test_the_ledger_prunes_at_48_hours_and_counts_what_it_dropped(self):
        ledger = GeneralLedger(self.path)
        ledger.note(GROUP, [1, 2], now=NOW - LEDGER_TTL_SECONDS - 1)
        ledger.note(GROUP, [3], now=NOW - HOUR)
        self.assertEqual(ledger.recent(GROUP, NOW), [3])
        self.assertEqual(ledger.expired(GROUP), 2)

    def test_the_ledger_survives_a_restart(self):
        GeneralLedger(self.path).note(GROUP, [7, 8], now=NOW - HOUR)
        data = load_state(self.path, "ledger")
        self.assertIn(str(GROUP), data["chats"])
        again = GeneralLedger(self.path)
        self.assertEqual(again.recent(GROUP, NOW), [7, 8])

    def test_a_deleted_id_is_no_longer_offered(self):
        ledger = GeneralLedger(self.path)
        ledger.note(GROUP, [1, 2], now=NOW - HOUR)
        ledger.drop(GROUP, [1])
        self.assertEqual(ledger.recent(GROUP, NOW), [2])

    def test_the_ledger_is_bounded(self):
        from swarmly.telegram.cleanup import LEDGER_MAX_PER_CHAT
        ledger = GeneralLedger(self.path)
        ledger.note(GROUP, list(range(1, LEDGER_MAX_PER_CHAT + 6)), now=NOW - HOUR)
        self.assertEqual(len(ledger.recent(GROUP, NOW)), LEDGER_MAX_PER_CHAT)


# ---------------------------------------------------------------------------------------
# The channel side: an admin's /cleanup, the fake bot's deletes, and the peers' TOPIC_GONE
# ---------------------------------------------------------------------------------------

CONFIG = """
agent:
  name: HermesBot
  alias: Hermes
  bot_token: "123456:TEST"
  home_channel_id: -1001234567890
  admin_user_id: 1234567890
peers:
  Codex: CodexBot
adapter:
  type: echo
limits:
  min_seconds_between_messages: 0
  selftest_seconds: 0
"""


class FakeBot:
    """The aiogram seam: records the delete calls /cleanup makes."""

    def __init__(self, fail=False) -> None:
        self.topics = []
        self.messages = []
        self.fail = fail

    async def delete_forum_topic(self, chat_id=None, message_thread_id=None):
        self.topics.append((chat_id, message_thread_id))
        if getattr(self, "gone", False):
            raise RuntimeError("Telegram server says - Bad Request: TOPIC_ID_INVALID")
        if self.fail:
            raise RuntimeError("Bad Request: not enough rights")

    async def delete_messages(self, chat_id=None, message_ids=None):
        self.messages.append((chat_id, list(message_ids or [])))
        if self.fail:
            raise RuntimeError("Bad Request: message can't be deleted")


class FakeSender:
    def __init__(self) -> None:
        self.sent = []

    async def __call__(self, payload, reply_to_message_id=None):
        self.sent.append(payload)
        return payload

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""


class ChannelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "config.yaml"
        self.source.write_text(CONFIG, encoding="utf-8")
        data = {
            "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                      "home_channel_id": GROUP, "admin_user_id": ADMIN_ID,
                      "allowed_users": [OTHER_ID]},
            "peers": {"Codex": "CodexBot"},
            "adapter": {"type": "echo"},
            "bus": {"repo": str(self.root)},
            "limits": {"min_seconds_between_messages": 0, "selftest_seconds": 0,
                       "min_seconds_between_agent_turns": 0},
        }
        self.data = data
        self.config = AgentConfig.from_dict(data, source_path=self.source)
        self.sender = FakeSender()
        self.channel = self.make_channel()
        self.channel.bot = FakeBot()

    def make_channel(self) -> AgentChannel:
        channel = AgentChannel(self.config, adapter=EchoAdapter("Hermes"),
                               sender=self.sender, clock=lambda: 1000.0,
                               sleeper=lambda seconds: None)
        channel.bot = self.channel.bot if hasattr(self, "channel") else None
        return channel

    def prompt(self, channel=None) -> "AgentChannel":
        """Give a channel the state /cleanup has to forget."""
        channel = channel or self.channel
        channel.topics.remember(GROUP, 42, "DeepSeek: 37 fixed", current=True)
        channel.topics.touch(GROUP, 42)
        channel._topic_memory.record(GROUP, 42, "DeepSeek", "working on #37", 43)
        channel._models["tg-42"] = "deepseek-chat"
        channel._efforts["tg-42"] = "high"
        channel._pending_handoffs[(GROUP, 42, "Codex")] = {"at": 1.0, "message_id": 7,
                                                         "nudged": 0}
        channel._peer_turns[(GROUP, 42)] = 2
        return channel

    def admin_command(self, text: str, message_id: int = 500):
        event = ChannelEvent(text=text, chat_id=GROUP, author_id=ADMIN_ID,
                             author_username="admin", author_is_bot=False,
                             message_id=message_id)
        return run(self.channel.handle_event(event))

    def test_cleanup_deletes_a_finished_topic_without_a_turn(self):
        self.prompt()
        self.channel.topics.touch(GROUP, 42, when=1000.0 - 2 * HOUR)
        reply = self.admin_command("/cleanup@HermesBot")
        self.assertIn("deleted 1 topic(s): DeepSeek: 37 fixed (42)", reply)
        self.assertEqual(self.channel.bot.topics, [(GROUP, 42)])

    def test_the_real_open_work_checks(self):
        """Review: the running-turn check read a lock table that does not exist."""
        self.prompt()
        import asyncio as aio

        class Turn:
            def done(self):
                return False
        self.channel._turn_keys[Turn()] = (GROUP, 42)
        self.assertEqual(self.channel._open_work_in(GROUP, 42, {}), "a turn is running")
        self.channel._turn_keys.clear()
        self.assertIsNone(self.channel._open_work_in(GROUP, 42, {}))
        store = type("S", (), {"ready": True, "read": lambda self, key: {"links": [
            {"id": "L1", "role": "work", "status": "open", "phase": "review", "title": "#37"},
            {"id": "L2", "role": "work", "status": "abandoned", "phase": "development"}]}})()
        self.channel.state_link.store = store
        self.assertEqual(self.channel._open_work_in(GROUP, 42, {}), "open work item L1 #37")
        self.channel.state_link.store = None
        self.channel.state_link.routing_mode = "registry"
        self.assertIn("not readable", self.channel._open_work_in(GROUP, 42, {}))
        del aio

    def test_a_partly_unreadable_owed_table_keeps_everything(self):
        rows = []
        self.channel.owed_store = lambda: type("S", (), {"load": lambda self: rows,
                                                         "unreadable": ["x.json"]})()
        self.assertIn("*", self.channel._open_owed_topics())

    def test_a_bare_cleanup_naming_a_peer_is_left_to_that_peer(self):
        self.prompt()
        self.channel._answers_for_swarm = lambda: True
        self.channel.topics.touch(GROUP, 42, when=1000.0 - 2 * HOUR)
        self.admin_command("/cleanup @CodexBot")
        self.assertEqual(self.channel.bot.topics, [])

    def test_a_topic_with_an_open_owed_row_is_kept(self):
        self.prompt()
        self.channel.topics.touch(GROUP, 42, when=1000.0 - 2 * HOUR)
        from swarmly.telegram.owed import Row
        rows = [Row(kind="handoff", item="#37", debtor="Codex", owes="a reply", topic="tg-42")]
        self.channel.owed_store = lambda: type("S", (), {"load": lambda self: rows})()
        reply = self.admin_command("/cleanup@HermesBot")
        self.assertIn("deleted 0 topic(s).", reply)
        self.assertIn("open: Codex owes handoff on #37", reply)
        self.assertEqual(self.channel.bot.topics, [])

    def test_the_button_only_previews(self):
        self.prompt()
        self.channel.topics.touch(GROUP, 42, when=1000.0 - 2 * HOUR)
        reply = run(self.channel._run_control("swarm.cleanup", ""))
        self.assertIn("would delete 1 topic(s)", str(reply))
        self.assertEqual(self.channel.bot.topics, [])

    def test_anyone_but_the_admin_gets_the_refusal(self):
        # A person the swarm admits but who is not the admin: the command is answered with a
        # refusal, never run. (A total stranger is ignored before any command, as always.)
        event = ChannelEvent(text="/cleanup@HermesBot topics all", chat_id=GROUP,
                             author_id=OTHER_ID, author_username="someone",
                             author_is_bot=False, message_id=501)
        reply = run(self.channel.handle_event(event))
        self.assertIn("only the admin runs /cleanup", reply)
        self.assertEqual(self.channel.bot.topics, [])

    def test_a_configured_keep_list_reaches_the_command(self):
        """#63 re-review: the loader dropped ``limits.cleanup_keep`` (a ``default_factory``
        field), so ``topics all`` offered the very topics the operator asked to keep."""
        limits = dict(self.data["limits"], cleanup_keep=[42])
        config = AgentConfig.from_dict({**self.data, "limits": limits},
                                       source_path=self.source)
        self.assertEqual(config.limits.cleanup_keep, [42])
        self.channel = self.make_channel_from(config)
        self.channel.bot = FakeBot()
        self.prompt()
        self.assertEqual(self.channel.cleanup.keep, {42})
        self.channel.topics.touch(GROUP, 42, when=1000.0 - 2 * HOUR)
        ask = self.admin_command("/cleanup@HermesBot")
        self.assertIn("DeepSeek: 37 fixed (42) (protected)", ask)   # the only one, kept

    def make_channel_from(self, config) -> AgentChannel:
        return AgentChannel(config, adapter=EchoAdapter("Hermes"), sender=self.sender,
                            clock=lambda: 1000.0, sleeper=lambda seconds: None)

    def test_a_topic_already_gone_is_forgotten_not_failed(self):
        """Live, 23 Oct: Round 3 had been deleted in Telegram; /cleanup said it "could not be
        deleted" and kept listing it."""
        self.prompt()
        self.channel.bot.gone = True
        done = self.admin_command("/cleanup@HermesBot topic 42")
        self.assertIn("deleted 1 topic(s): DeepSeek: 37 fixed (42)", done)
        self.assertNotIn("Could not delete", done)
        self.assertIsNone(self.channel.topics.name(GROUP, 42))

    def test_confirming_deletes_the_topic_and_announces_topic_gone(self):
        self.prompt()
        done = self.admin_command("/cleanup@HermesBot topic 42")
        self.assertIn("deleted 1 topic(s): DeepSeek: 37 fixed (42)", done)
        self.assertEqual(self.channel.bot.topics, [(GROUP, 42)])
        # the state is gone, and the peers are told on the machine plane
        self.assertIsNone(self.channel.topics.name(GROUP, 42))
        self.assertEqual(self.channel._topic_memory.recap(GROUP, 42, current_id=None,
                                                          fresh=True, own_alias="Hermes"), "")
        self.assertNotIn("tg-42", self.channel._models)
        self.assertEqual([k for k in self.channel._pending_handoffs if k[1] == 42], [])
        note = [text for text in self.sender.sent if "intent=TOPIC_GONE" in text]
        self.assertEqual(len(note), 1)
        self.assertIn("ref=42", note[0])

    def test_general_deletes_only_what_the_ledger_recorded(self):
        self.channel.ledger.note(GROUP, [11, 12])
        self.admin_command("/cleanup@HermesBot general")
        sent = self.channel.bot.messages
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], GROUP)
        self.assertEqual(sent[0][1][:2], [11, 12])       # the ledger's ids, oldest first

    def test_a_peer_topic_gone_note_makes_this_bot_forget_it_too(self):
        self.prompt()
        from swarmly.telegram.wire import Envelope, MessageType, format_message
        text = format_message(Envelope(type=MessageType.NOTE, to="*", intent="TOPIC_GONE",
                                       ref="42"))
        event = ChannelEvent(text=text, chat_id=GROUP, author_id=999,
                             author_username="CodexBot", author_is_bot=True, message_id=504)
        before = len(self.sender.sent)
        run(self.channel.handle_event(event))
        self.assertEqual(len(self.sender.sent), before)      # machine traffic: no reply
        self.assertIsNone(self.channel.topics.name(GROUP, 42))
        self.assertNotIn("tg-42", self.channel._models)
        self.assertEqual(self.channel._topic_memory.recap(GROUP, 42, current_id=None,
                                                          fresh=True, own_alias="Hermes"), "")

    def test_a_refused_delete_keeps_the_state(self):
        self.prompt()
        self.channel.bot.fail = True
        text = self.admin_command("/cleanup@HermesBot topic 42")
        self.assertIn("Could not delete", text)
        self.assertIsNotNone(self.channel.topics.name(GROUP, 42))
        self.assertIn("tg-42", self.channel._models)
        self.assertEqual([t for t in self.sender.sent if "intent=TOPIC_GONE" in t], [])

    def test_the_ledger_learns_general_messages_it_sees_and_posts(self):
        event = ChannelEvent(text="hello", chat_id=GROUP, author_id=ADMIN_ID,
                             author_username="admin", author_is_bot=False, message_id=600)
        run(self.channel.handle_event(event))
        self.assertIn(600, self.channel.ledger.recent(GROUP))
        self.admin_command("/cleanup@HermesBot", message_id=601)   # its own reply in General


class TopicMapActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "topics-hermes.json"

    def test_activity_and_names_survive_a_restart(self):
        topics = TopicMap(self.path, home_chat_id=GROUP)
        topics.remember(GROUP, 42, "work", current=True)
        topics.touch(GROUP, 42, when=NOW - HOUR)
        again = TopicMap(self.path, home_chat_id=GROUP)
        self.assertEqual(again.last_seen(GROUP, 42), NOW - HOUR)
        self.assertEqual(again.entries(), [(GROUP, 42, "work")])

    def test_forget_drops_the_name_and_the_activity(self):
        topics = TopicMap(self.path, home_chat_id=GROUP)
        topics.remember(GROUP, 42, "work", current=True)
        topics.touch(GROUP, 42, when=NOW)
        self.assertTrue(topics.forget(GROUP, 42))
        self.assertIsNone(topics.name(GROUP, 42))
        self.assertIsNone(topics.last_seen(GROUP, 42))
        self.assertEqual(TopicMap(self.path).entries(), [])


class TopicMemoryForgetTests(unittest.TestCase):
    def test_a_forgotten_discussion_leaves_no_recap(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "topic-memory.json"
            memory = TopicMemory(path)
            memory.record(GROUP, 42, "Hermes", "the task", 43)
            self.assertTrue(memory.forget(GROUP, 42))
            self.assertEqual(memory.recap(GROUP, 42, current_id=None, fresh=True,
                                          own_alias="Hermes"), "")
            self.assertFalse(memory.forget(GROUP, 42))
            kept = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(kept["topics"], [])


class SessionForgetTests(unittest.TestCase):
    """A deleted topic's session goes with it, in every runtime that keeps one."""

    def test_the_reasonix_session_is_dropped_and_the_file_follows(self):
        from swarmly.telegram.adapters.reasonix import ReasonixAdapter
        from swarmly.telegram.config import AdapterSettings
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "reasonix-sessions.json"
            adapter = ReasonixAdapter("DeepSeek", AdapterSettings(type="reasonix", cwd=tmp))
            adapter.use_state_file(path)
            adapter._remember("tg-42", "session-1", rules="rules-digest")
            adapter._remember("tg-43", "session-2", rules="rules-digest")
            self.assertTrue(adapter.forget_session("tg-42"))
            self.assertIsNone(adapter.session_for("tg-42"))
            self.assertEqual(adapter.session_for("tg-43"), "session-2")
            self.assertNotIn("tg-42", json.loads(path.read_text(encoding="utf-8")))
            self.assertFalse(adapter.forget_session("tg-42"))    # nothing left to drop

    def test_a_runtime_with_no_per_topic_session_drops_nothing(self):
        self.assertFalse(EchoAdapter("Hermes").forget_session("tg-42"))
        self.assertFalse(EchoAdapter("Hermes").forget_session(None))

    def test_the_channel_tells_every_adapter_to_forget_the_topic(self):
        with TemporaryDirectory() as tmp:
            channel = AgentChannel(_config_for(Path(tmp)))
            seen = []

            class Recording(EchoAdapter):
                def forget_session(self, context_id):
                    seen.append(("main", context_id))
                    return True

            class ProjectAdapter(EchoAdapter):
                def forget_session(self, context_id):
                    seen.append(("project", context_id))
                    return True

            channel.adapter = Recording("Hermes")
            channel._project_adapters["demo"] = ProjectAdapter("Hermes")
            channel.topics.remember(GROUP, 42, "work", current=True)
            channel.forget_topic_state(GROUP, 42)
            self.assertEqual(sorted(seen), [("main", "tg-42"), ("project", "tg-42")])


def _config_for(root: Path) -> AgentConfig:
    source = root / "config.yaml"
    source.write_text(CONFIG, encoding="utf-8")
    return AgentConfig.from_dict({
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
        "peers": {"Codex": "CodexBot"},
        "adapter": {"type": "echo"},
        "bus": {"repo": str(root)},
        "limits": {"min_seconds_between_messages": 0, "selftest_seconds": 0,
                   "min_seconds_between_agent_turns": 0},
    }, source_path=source)


if __name__ == "__main__":
    unittest.main()
