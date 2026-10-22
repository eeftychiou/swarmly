"""Tests for ``/cleanup`` (issue #63): the dry run, the deleting forms, the confirmation.

No network and no aiogram: the Telegram calls are faked, both at the ``Cleanup`` seam
(a host with the five methods it needs) and at the channel's aiogram seam (a fake bot), so
the tests pin what the command decides and what it asks Telegram to do -- never a live group.

Run: python -m unittest tests.test_cleanup -v
"""

from __future__ import annotations

import asyncio
import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram.adapters.base import load_state
from tests.aio import run
from swarmly.telegram.channel import AgentChannel, ChannelEvent
from swarmly.telegram.cleanup import (CONFIRM_SECONDS, LEDGER_TTL_SECONDS, Cleanup,
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
                 message_batches=None) -> None:
        self.topics = list(topics)
        self.chat_id = chat_id
        self.delete_ok = delete_ok
        self.message_batches = message_batches      # None: every id goes; else per-batch count
        self.deleted_topics = []
        self.deleted_messages = []
        self.forgotten = []

    def cleanup_home_chat(self):
        return self.chat_id

    def cleanup_topics(self):
        return list(self.topics)

    async def cleanup_delete_topic(self, chat_id, thread_id):
        self.deleted_topics.append(int(thread_id))
        return self.delete_ok and int(thread_id) not in getattr(self, "refuse", set())

    async def cleanup_delete_messages(self, chat_id, ids):
        self.deleted_messages.append([int(i) for i in ids])
        if self.message_batches is not None:
            return self.message_batches.pop(0) if self.message_batches else 0
        return len(ids)

    async def cleanup_forget_topic(self, chat_id, thread_id):
        self.forgotten.append(int(thread_id))


def topic(thread_id, name="work", activity=NOW - 30 * HOUR, refs=(), states=None):
    return TopicInfo(thread_id=thread_id, chat_id=GROUP, title=name,
                     last_activity=activity, refs=tuple(refs),
                     states=dict(states or {}))


class CleanupCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger_path = self.root / "state" / "general-hermes.json"
        self.ledger = GeneralLedger(self.ledger_path)
        self.codes = ["c0ffee", "c0ffe1", "c0ffe2", "c0ffe3"]
        self.now = NOW

    def cleanup(self, host, **kwargs) -> Cleanup:
        kwargs.setdefault("handle", "@HermesBot")
        kwargs.setdefault("idle_hours", 24.0)
        return Cleanup(host, self.ledger, clock=lambda: self.now,
                       code=lambda: self.codes.pop(0), **kwargs)

    def ask(self, cleanup, *words, is_admin=True):
        return run(cleanup.handle_args(list(words), is_admin=is_admin, chat_id=GROUP))

    def code_in(self, text: str) -> str:
        match = re.search(r"confirm ([0-9a-f]+)", text)
        self.assertIsNotNone(match, text)
        return match.group(1)


class DryRunTests(CleanupCase):
    def test_the_dry_run_lists_topics_with_their_state_and_deletes_nothing(self):
        host = FakeHost([
            topic(42, "Claudebot: 37 fixed", refs=["#37"], states={"#37": "merged"}),
            topic(43, "DeepSeek: notices", activity=NOW - 2 * HOUR, refs=["#64"],
                  states={"#64": "open"}),
            topic(44, "side quest", activity=None),
        ])
        text = self.ask(self.cleanup(host))
        self.assertIn("dry run: nothing is deleted", text)
        self.assertIn("Topics (3)", text)
        self.assertIn("42 · Claudebot: 37 fixed · idle 30h · #37 merged", text)
        self.assertIn("43 · DeepSeek: notices · idle 2h · #64 open", text)
        self.assertIn("44 · side quest", text)
        self.assertIn("no linked item", text)
        self.assertIn("topics done -- 1 topic(s)", text)      # only #42 is done and idle
        self.assertEqual(host.deleted_topics, [])
        self.assertEqual(host.deleted_messages, [])

    def test_the_dry_run_reports_the_general_messages_it_could_delete(self):
        self.ledger.note(GROUP, [10, 11, 12], now=NOW - HOUR)
        self.ledger.note(GROUP, [1, 2], now=NOW - LEDGER_TTL_SECONDS - HOUR)   # too old
        text = self.ask(self.cleanup(FakeHost()))
        self.assertIn("3 message(s) this bot can still delete", text)
        self.assertIn("2 recorded longer than 48h ago it can no longer delete", text)
        self.assertIn("general -- the 3 message(s)", text)

    def test_a_ref_github_could_not_check_is_unknown_not_done(self):
        host = FakeHost([topic(42, "#37 work", refs=["#37"], states={"#37": None})])
        text = self.ask(self.cleanup(host))
        self.assertIn("#37 unknown", text)
        self.assertIn("topics done -- 0 topic(s)", text)

    def test_a_topic_linking_a_done_and_an_open_item_is_not_done(self):
        """#63 re-review: reading ``states`` instead of ``refs`` marked a topic done when only
        some of the items it names were closed -- and deleted work still in flight."""
        host = FakeHost([topic(42, "#37, #64", refs=["#37", "#64"],
                               states={"#37": "merged", "#64": "open"})])
        text = self.ask(self.cleanup(host))
        self.assertIn("topics done -- 0 topic(s)", text)

    def test_a_ref_missing_from_states_is_not_done(self):
        host = FakeHost([topic(42, "#37, #64", refs=["#37", "#64"], states={"#37": "closed"})])
        self.assertIn("topics done -- 0 topic(s)", self.ask(self.cleanup(host)))

    def test_a_topic_with_no_recorded_activity_is_still_offered_once_its_item_is_done(self):
        host = FakeHost([topic(42, "#37 work", activity=None, refs=["#37"],
                               states={"#37": "closed"})])
        text = self.ask(self.cleanup(host))
        self.assertIn("idle 24h", text)
        self.assertIn("topics done -- 1 topic(s)", text)


class OffTheLoopTests(CleanupCase):
    """Live: the topic states are GitHub lookups, and a dry run held the bot for 52 s."""

    def test_the_topics_are_gathered_in_a_worker_thread(self):
        import threading
        host = FakeHost([topic(5)])
        threads = []
        original = host.cleanup_topics

        def gathered():
            threads.append(threading.current_thread() is threading.main_thread())
            return original()

        host.cleanup_topics = gathered
        cleanup = self.cleanup(host)
        for words in ((), ("topics", "done"), ("topics", "all"), ("topic", "5")):
            self.ask(cleanup, *words)
        self.assertTrue(threads)
        self.assertNotIn(True, threads)


class RefusalTests(CleanupCase):
    def test_a_non_admin_gets_a_refusal_and_nothing_else(self):
        host = FakeHost([topic(42, "#37", refs=["#37"], states={"#37": "merged"})])
        cleanup = self.cleanup(host)
        for words in ((), ("topics", "done"), ("topics", "all"), ("topic", "42"),
                      ("general",), ("confirm", "c0ffee")):
            text = self.ask(cleanup, *words, is_admin=False)
            self.assertIn("only the admin runs /cleanup", text)
        self.assertEqual(host.deleted_topics, [])
        self.assertEqual(host.deleted_messages, [])

    def test_an_unknown_form_gets_a_usage_line(self):
        text = self.ask(self.cleanup(FakeHost()), "everything")
        self.assertIn("takes one of", text)


class DeletingFormTests(CleanupCase):
    def test_topics_done_asks_first_and_deletes_only_on_confirm(self):
        host = FakeHost([
            topic(42, "#37 done", refs=["#37"], states={"#37": "merged"}),
            topic(43, "#64 open", refs=["#64"], states={"#64": "open"}),
            topic(44, "#65 closed but fresh", activity=NOW - HOUR, refs=["#65"],
                  states={"#65": "closed"}),
            topic(45, "no item"),
        ])
        cleanup = self.cleanup(host)
        ask = self.ask(cleanup, "topics", "done")
        self.assertIn("will delete 1 topic(s)", ask)
        self.assertIn("cannot be undone", ask)
        self.assertEqual(host.deleted_topics, [])            # asking deletes nothing
        code = self.code_in(ask)

        done = self.ask(cleanup, "confirm", code)
        self.assertEqual(host.deleted_topics, [42])
        self.assertEqual(host.forgotten, [42])
        self.assertIn("deleted 1 topic(s): #42", done)

    def test_one_code_stands_for_one_deletion_once(self):
        host = FakeHost([topic(42, "#37", refs=["#37"], states={"#37": "merged"})])
        cleanup = self.cleanup(host)
        code = self.code_in(self.ask(cleanup, "topics", "done"))
        self.ask(cleanup, "confirm", code)
        again = self.ask(cleanup, "confirm", code)
        self.assertIn("no cleanup is waiting on that code", again)
        self.assertEqual(host.deleted_topics, [42])

    def test_a_code_expires(self):
        host = FakeHost([topic(42, "#37", refs=["#37"], states={"#37": "merged"})])
        cleanup = self.cleanup(host)
        code = self.code_in(self.ask(cleanup, "topics", "done"))
        self.now += CONFIRM_SECONDS + 1
        text = self.ask(cleanup, "confirm", code)
        self.assertIn("expired", text)
        self.assertEqual(host.deleted_topics, [])

    def test_a_wrong_code_does_nothing(self):
        host = FakeHost([topic(42, "#37", refs=["#37"], states={"#37": "merged"})])
        cleanup = self.cleanup(host)
        self.ask(cleanup, "topics", "done")
        text = self.ask(cleanup, "confirm", "beef")
        self.assertIn("no cleanup is waiting on that code", text)
        self.assertEqual(host.deleted_topics, [])

    def test_topics_all_spares_the_kept_topics(self):
        host = FakeHost([topic(42, "a"), topic(43, "b"), topic(44, "c")])
        cleanup = self.cleanup(host, keep=[43])
        ask = self.ask(cleanup, "topics", "all")
        self.assertIn("will delete 2 topic(s)", ask)
        self.ask(cleanup, "confirm", self.code_in(ask))
        self.assertEqual(sorted(host.deleted_topics), [42, 44])

    def test_topic_deletes_one_named_topic(self):
        host = FakeHost([topic(42, "a"), topic(43, "b")])
        cleanup = self.cleanup(host)
        ask = self.ask(cleanup, "topic", "43")
        self.assertIn("will delete 1 topic(s)", ask)
        self.ask(cleanup, "confirm", self.code_in(ask))
        self.assertEqual(host.deleted_topics, [43])

    def test_a_failed_delete_is_reported_and_not_forgotten(self):
        host = FakeHost([topic(42, "a")], delete_ok=False)
        cleanup = self.cleanup(host)
        ask = self.ask(cleanup, "topic", "42")
        text = self.ask(cleanup, "confirm", self.code_in(ask))
        self.assertIn("could not be deleted", text)
        self.assertEqual(host.forgotten, [])       # a topic still there is still remembered

    def test_general_deletes_the_ledger_oldest_first_and_forgets_what_went(self):
        self.ledger.note(GROUP, [30, 10, 20], now=NOW - HOUR)
        host = FakeHost()
        cleanup = self.cleanup(host)
        ask = self.ask(cleanup, "general")
        self.assertIn("will delete 3 message(s)", ask)
        text = self.ask(cleanup, "confirm", self.code_in(ask))
        self.assertEqual(host.deleted_messages, [[10, 20, 30]])     # oldest first
        self.assertIn("deleted 3 General message(s)", text)
        self.assertEqual(self.ledger.recent(GROUP, self.now), [])

    def test_general_stops_when_telegram_refuses_the_rest(self):
        self.ledger.note(GROUP, [1, 2, 3], now=NOW - HOUR)
        host = FakeHost(message_batches=[2])
        cleanup = self.cleanup(host)
        ask = self.ask(cleanup, "general")
        text = self.ask(cleanup, "confirm", self.code_in(ask))
        self.assertIn("deleted 2 General message(s)", text)
        self.assertEqual(self.ledger.recent(GROUP, self.now), [3])

    def test_nothing_to_delete_is_said_plainly(self):
        self.assertEqual(self.ask(self.cleanup(FakeHost()), "topics", "done"),
                         "@HermesBot: no topic has a closed or merged item and has been idle "
                         "24h. Nothing to delete.")
        self.assertIn("no General message", self.ask(self.cleanup(FakeHost()), "general"))


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
        channel._learn_topic_refs(GROUP, 42, "see #37")
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

    def test_the_dry_run_is_answered_by_the_transport_without_a_turn(self):
        self.prompt()
        reply = self.admin_command("/cleanup@HermesBot")
        self.assertIn("dry run: nothing is deleted", reply)
        self.assertIn("42 · DeepSeek: 37 fixed", reply)
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
        ask = self.admin_command("/cleanup@HermesBot topics all")
        self.assertIn("no topic to delete", ask)        # 42 is the only one, and it is kept

    def make_channel_from(self, config) -> AgentChannel:
        return AgentChannel(config, adapter=EchoAdapter("Hermes"), sender=self.sender,
                            clock=lambda: 1000.0, sleeper=lambda seconds: None)

    def test_confirming_deletes_the_topic_and_announces_topic_gone(self):
        self.prompt()
        ask = self.admin_command("/cleanup@HermesBot topic 42")
        self.assertIn("will delete 1 topic(s)", ask)
        code = re.search(r"confirm ([0-9a-f]+)", ask).group(1)
        done = self.admin_command(f"/cleanup@HermesBot confirm {code}", message_id=502)
        self.assertIn("deleted 1 topic(s): #42", done)
        self.assertEqual(self.channel.bot.topics, [(GROUP, 42)])
        # the state is gone, and the peers are told on the machine plane
        self.assertIsNone(self.channel.topics.name(GROUP, 42))
        self.assertEqual(self.channel._topic_memory.recap(GROUP, 42, current_id=None,
                                                          fresh=True, own_alias="Hermes"), "")
        self.assertNotIn("tg-42", self.channel._models)
        self.assertEqual([k for k in self.channel._pending_handoffs if k[1] == 42], [])
        self.assertNotIn(tuple((GROUP, 42)), self.channel._ref_topics.values())
        note = [text for text in self.sender.sent if "intent=TOPIC_GONE" in text]
        self.assertEqual(len(note), 1)
        self.assertIn("ref=42", note[0])

    def test_general_deletes_only_what_the_ledger_recorded(self):
        self.channel.ledger.note(GROUP, [11, 12])
        ask = self.admin_command("/cleanup@HermesBot general")
        code = re.search(r"confirm ([0-9a-f]+)", ask).group(1)
        self.admin_command(f"/cleanup@HermesBot confirm {code}", message_id=503)
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
        ask = self.admin_command("/cleanup@HermesBot topic 42")
        code = re.search(r"confirm ([0-9a-f]+)", ask).group(1)
        text = self.admin_command(f"/cleanup@HermesBot confirm {code}", message_id=505)
        self.assertIn("could not be deleted", text)
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
