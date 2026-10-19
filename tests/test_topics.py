"""Forum topics as swarm discussions: names, context ids, parallel turns, sessions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Sequence

from swarmly.telegram.adapters import ClaudeThreadAdapter
from swarmly.telegram.adapters.base import AdapterReply, IncomingMessage
from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.topics import TopicMap, context_for, name_from_message, topic_name
from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, make_config, run


def human(text, **kw):
    return event(text, author_id=ADMIN_ID, username="alice", is_bot=False, **kw)


class Gate(EchoAdapter):
    """Holds every turn open until released; records what each turn was given."""

    def __init__(self):
        super().__init__("Hermes")
        self.seen: List[IncomingMessage] = []
        self.running = 0
        self.peak = 0
        self._release: Optional[asyncio.Event] = None

    @property
    def release(self) -> asyncio.Event:
        # Made inside the running loop: Python 3.9 binds an Event to the loop at creation.
        if self._release is None:
            self._release = asyncio.Event()
        return self._release

    def accepts_explicit_username_delivery(self) -> bool:
        return False                    # so peer messages see the cooldown

    async def respond(self, message):
        self.seen.append(message)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.release.wait()
        finally:
            self.running -= 1
        return AdapterReply(text=f"done: {message.context_id}")


async def settle(rounds=20):
    for _ in range(rounds):
        await asyncio.sleep(0)


class VirtualClock:
    """Sleeps wake in time order and every sleep yields, so concurrent sends really race."""

    def __init__(self):
        self.t = 1000.0
        self.sleepers: List[Any] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.t + max(float(seconds), 0.0), len(self.sleepers), future))
        await future

    async def drive(self, awaitable, until: float = float("inf")):
        task = asyncio.ensure_future(awaitable)
        while True:
            await settle(50)
            if task.done():
                return task.result()
            waiting = sorted(s for s in self.sleepers if not s[2].done())
            if not waiting or waiting[0][0] > until:
                self.t = max(self.t, until) if until != float("inf") else self.t
                return None
            at, _, future = waiting[0]
            self.t = max(self.t, at)
            future.set_result(None)


class ClockBot:
    """A fake Telegram bot on a virtual clock: topics in ``dead`` refuse posts, and a topic
    in ``flood`` answers its first post with a flood wait of that many seconds."""

    def __init__(self, clock: VirtualClock, dead=(), flood=None, on_refuse=None,
                 refuse_text=None):
        self.clock, self.dead, self.flood = clock, set(dead), dict(flood or {})
        self.on_refuse, self.refuse_text = on_refuse, refuse_text
        self.accepted: List[Dict[str, Any]] = []
        self.refused = 0

    async def send_message(self, **kw):
        from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
        await asyncio.sleep(0)
        thread = kw.get("message_thread_id")
        if thread in self.dead:
            self.refused += 1
            if self.on_refuse is not None:
                self.on_refuse()
            raise TelegramBadRequest(method=None, message="Bad Request: TOPIC_CLOSED")
        if self.refuse_text is not None and kw["text"].startswith(self.refuse_text):
            raise TelegramBadRequest(method=None, message="Bad Request: not enough rights")
        if self.flood.get(thread):                  # seconds, or a list of them in turn
            waits = self.flood[thread]
            wait = (waits.pop(0) if isinstance(waits, list) else self.flood.pop(thread)) - 1
            raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=wait)
        self.accepted.append({"t": self.clock.t, "thread": thread, "text": kw["text"]})
        return types.SimpleNamespace(message_id=len(self.accepted))


def most_in_a_minute(times: Sequence[float]) -> int:
    times = sorted(times)
    return max(sum(1 for t in times if start <= t < start + 60) for start in times)


class NamesTests(unittest.TestCase):
    def test_context_ids_come_from_the_topic_id_only(self):
        self.assertIsNone(context_for(GROUP, None))
        self.assertEqual(context_for(GROUP, 7, home_chat_id=GROUP), "tg-7")
        # An unknown home group is not "every chat is home": without the secret there is no
        # id (the turn is refused), and with it each chat is tagged, so two chats never
        # collide on one context id.
        self.assertIsNone(context_for(GROUP, 7))
        with mock.patch.dict(os.environ, {"SWARMLY_REDACTION_SECRET": "shared-secret"}):
            self.assertNotEqual(context_for(GROUP, 7), context_for(-1009, 7))
        # Issue #22: a context id in another group carries a tag, never the chat id -- and
        # without the swarm's shared secret there is no tag at all (fail closed).
        self.assertIsNone(context_for(-1009, 7, home_chat_id=GROUP))
        with mock.patch.dict(os.environ, {"SWARMLY_REDACTION_SECRET": "shared-secret"}):
            other = context_for(-1009, 7, home_chat_id=GROUP)
            self.assertNotIn("1009", other)
            self.assertTrue(other.startswith("tg-") and other.endswith("-7"), other)
            self.assertEqual(other, context_for(-1009, 7, home_chat_id=GROUP))   # stable
            self.assertNotEqual(other, context_for(-1008, 7, home_chat_id=GROUP))

    def test_topic_names_fit(self):
        self.assertEqual(topic_name("t42", "fix   the\nlogin timeout"),
                         "t42 · fix the login timeout")
        self.assertEqual(len(topic_name("t1", "x" * 300)), 128)

    def test_names_come_from_the_creation_message_or_its_reply(self):
        created = types.SimpleNamespace(forum_topic_created=types.SimpleNamespace(name="t3 · a"))
        self.assertEqual(name_from_message(created), ("t3 · a", True))
        edited = types.SimpleNamespace(forum_topic_edited=types.SimpleNamespace(name="renamed"))
        self.assertEqual(name_from_message(edited), ("renamed", True))
        reply = types.SimpleNamespace(forum_topic_created=None, forum_topic_edited=None,
                                      reply_to_message=created)
        self.assertEqual(name_from_message(reply), ("t3 · a", False))
        self.assertEqual(name_from_message(types.SimpleNamespace(reply_to_message=None)),
                         (None, False))

    def test_a_name_never_moves_or_claims_a_discussion(self):
        topics = TopicMap(home_chat_id=GROUP)
        topics.remember(GROUP, 5, "t9 · deploy")
        topics.remember(GROUP, 6, "tg-5 · not really")
        topics.remember(GROUP, 5, "deploy (renamed)")
        topics.remember(GROUP, 5, "t9 · deploy", current=False)     # the stale creation name
        self.assertEqual(topics.name(GROUP, 5), "deploy (renamed)")
        self.assertEqual((topics.context(GROUP, 5), topics.context(GROUP, 6)), ("tg-5", "tg-6"))

    def test_the_names_survive_a_restart_and_a_corrupt_file_is_kept_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "topics.json"
            TopicMap(path).remember(GROUP, 5, "deploy")
            self.assertEqual(TopicMap(path).name(GROUP, 5), "deploy")
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(TopicMap(path).name(GROUP, 5))          # starts empty
            kept = [p.name for p in path.parent.iterdir() if ".corrupt-" in p.name]
            self.assertEqual(len(kept), 1)
            self.assertFalse(path.exists())


class ChannelTests(unittest.TestCase):
    def channel(self, parallel=1, **limits):
        channel = make_channel(make_config(limits={"max_parallel_turns": parallel, **limits}))
        gate = Gate()
        channel.adapter = gate
        return channel, gate

    def test_the_discussion_reaches_the_agent(self):
        channel, gate = self.channel()
        gate.release.set()

        async def scenario():
            channel.topics.remember(GROUP, 5, "t9 · deploy")
            await channel.handle_event(human("@HermesBot status?", thread_id=5))
            await channel.handle_event(human("@HermesBot and here?", message_id=2))
        run(scenario())
        topic, general = gate.seen
        self.assertEqual((topic.context_id, topic.topic_name), ("tg-5", "t9 · deploy"))
        self.assertEqual(topic.text, "@HermesBot status?")          # commands still match
        self.assertIsNone(general.context_id)

    def test_the_prompt_names_the_discussion(self):
        from swarmly.telegram.adapters.base import discussion_line, format_channel_prompt
        msg = IncomingMessage(text="hi", author_alias="Alice", context_id="t9",
                              topic_name='a "quoted"] name')
        line = discussion_line(msg)
        self.assertIn('discussion t9 (Telegram topic: a quoted name)', line)
        self.assertIn(f"- {line}", format_channel_prompt("Alice", "hi", discussion=line))
        self.assertEqual(discussion_line(IncomingMessage(text="hi", author_alias="Alice")), "")
        self.assertNotIn("DISCUSSION", format_channel_prompt("Alice", "hi"))

    def test_parallel_needs_an_adapter_with_a_session_per_discussion(self):
        from swarmly.telegram.config import ConfigError
        for kind in ("antigravity_thread", "codex_thread"):
            with self.assertRaises(ConfigError):
                make_config(limits={"max_parallel_turns": 2, "allow_shared_cwd": True},
                            adapter={"type": kind})
        make_config(limits={"max_parallel_turns": 2, "allow_shared_cwd": True},
                    adapter={"type": "claude_thread"})

    def test_antigravity_takes_parallel_turns(self):
        """#67: the adapter keeps one conversation per topic, so >1 is not interleaving."""
        config = make_config(limits={"max_parallel_turns": 2, "allow_shared_cwd": True},
                             adapter={"type": "antigravity"})
        self.assertEqual(config.limits.max_parallel_turns, 2)
        self.assertEqual(config.adapter.type, "antigravity")

    def test_antigravity_with_a_pinned_conversation_is_clamped(self):
        """#67: a pinned conversation is one context for every topic -- refuse >1 there."""
        from swarmly.telegram.config import ConfigError
        limits = {"max_parallel_turns": 2, "allow_shared_cwd": True}
        with TemporaryDirectory() as tmp:
            pinned = (
                ("adapter.conversation is set",
                 {"type": "antigravity", "conversation": "12345678-1234-5678-1234-567812345678"}),
                ("adapter.command names a conversation",
                 {"type": "antigravity", "command": "agy -p {prompt} -c"}),
                ("adapter.command names a conversation",
                 {"type": "antigravity", "command": "agy -p {prompt} --continue"}),
                ("adapter.command names a conversation",
                 {"type": "antigravity",
                  "command": "agy -p {prompt} --conversation 12345678-1234-5678-1234-567812345678"}),
            )
            for reason, adapter in pinned:
                with self.assertRaises(ConfigError, msg=reason) as caught:
                    make_config(limits=limits, adapter=adapter)
                self.assertIn("pins a single one", str(caught.exception), reason)
                self.assertIn(reason, str(caught.exception), reason)

            # The .active_conversation file in the working folder pins one just as firmly.
            cwd = Path(tmp)
            (cwd / ".active_conversation").write_text("12345678-1234-5678-1234-567812345678",
                                                      encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                make_config(limits=limits, adapter={"type": "antigravity", "cwd": str(cwd)})
            self.assertIn(".active_conversation", str(caught.exception))

            # Without the file (same folder, pin gone) the same config loads.
            (cwd / ".active_conversation").unlink()
            self.assertEqual(
                make_config(limits=limits, adapter={"type": "antigravity", "cwd": str(cwd)})
                .limits.max_parallel_turns, 2)

    def test_parallel_turns_in_one_folder_need_consent(self):
        from swarmly.telegram.config import ConfigError
        with self.assertRaises(ConfigError) as caught:
            make_config(limits={"max_parallel_turns": 2}, adapter={"type": "claude_thread"})
        self.assertIn("allow_shared_cwd", str(caught.exception))
        make_config(limits={"max_parallel_turns": 2}, adapter={"type": "echo"})

    def test_the_peer_cooldown_is_per_topic_when_parallel(self):
        channel, gate = self.channel(parallel=2, min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)   # no paced sleeps
        gate.release.set()

        async def scenario():
            await channel.handle_event(event("@HermesBot one", thread_id=4))
            await channel.handle_event(event("@HermesBot two", thread_id=5, message_id=2))
            await channel.handle_event(event("@HermesBot three", thread_id=4, message_id=3))
        run(scenario())
        # #144: topics have no bot-to-bot cooldown (the peer cap brakes loops there), so
        # nothing is dropped or held.
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot one", "@HermesBot two", "@HermesBot three"])
        self.assertFalse([s for s in channel._test_slept if s >= 59])

    def test_the_peer_cooldown_still_drops_in_general(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)
        gate.release.set()

        async def scenario():
            await channel.handle_event(event("@HermesBot one"))
            await channel.handle_event(event("@HermesBot two", message_id=2))
        run(scenario())
        self.assertEqual([m.text for m in gate.seen], ["@HermesBot one"])

    def test_a_stop_drops_a_queued_message_waiting_out_the_cooldown(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)

        async def stop_while_waiting(seconds):
            channel._stop_generation += 1           # what /stop does to held messages
            channel._test_clock["t"] += seconds

        async def scenario():
            first = asyncio.ensure_future(channel.handle_event(event("@HermesBot one")))
            await settle()
            second = asyncio.ensure_future(
                channel.handle_event(event("@HermesBot two", message_id=2)))
            await settle()
            channel._sleep = stop_while_waiting
            gate.release.set()
            await first
            return await second
        self.assertIsNone(run(scenario()))
        self.assertEqual([m.text for m in gate.seen], ["@HermesBot one"])

    def test_a_held_peer_message_does_not_block_the_bot(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)

        async def scenario():
            hold = asyncio.Event()

            async def wait_for_hold(seconds):
                await hold.wait()
                channel._test_clock["t"] += seconds
            first = asyncio.ensure_future(channel.handle_event(event("@HermesBot one")))
            await settle()
            held = asyncio.ensure_future(
                channel.handle_event(event("@HermesBot two", message_id=2)))
            await settle()
            channel._sleep = wait_for_hold
            gate.release.set()
            await first
            await settle()
            await channel.handle_event(human("@HermesBot are you there?", message_id=3))
            during = [m.text for m in gate.seen]
            hold.set()
            await held
            return during
        during = run(scenario())
        self.assertEqual(during, ["@HermesBot one", "@HermesBot are you there?"])
        self.assertEqual(gate.seen[-1].text, "@HermesBot two")

    def test_held_peer_messages_keep_their_order(self):
        # Held messages share one deadline, and a real event loop may wake them in any
        # order; this sleeper wakes the newest first.
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)
        sleeping = []

        async def newest_first(seconds):
            future = asyncio.get_running_loop().create_future()
            sleeping.append((seconds, future))
            await future

        async def scenario():
            tasks = [asyncio.ensure_future(channel.handle_event(human("@HermesBot task")))]
            await settle()
            for n in (1, 2, 3):
                tasks.append(asyncio.ensure_future(
                    channel.handle_event(event(f"@HermesBot p{n}", message_id=1 + n))))
                await settle()
            channel._sleep = newest_first
            gate.release.set()
            for _ in range(20):
                await settle()
                pending = [(s, f) for s, f in sleeping if not f.done()]
                if not pending:
                    break
                channel._test_clock["t"] += max(s for s, _ in pending)
                pending[-1][1].set_result(None)
            await asyncio.gather(*tasks)
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot task", "@HermesBot p1", "@HermesBot p2", "@HermesBot p3"])

    def test_only_busy_queue_max_messages_are_held_in_general(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100, busy_queue_max=1)

        async def scenario():
            hold = asyncio.Event()

            async def wait_for_hold(seconds):
                await hold.wait()
                channel._test_clock["t"] += seconds
            first = asyncio.ensure_future(channel.handle_event(human("@HermesBot task 1")))
            await settle()
            p1 = asyncio.ensure_future(channel.handle_event(event("@HermesBot p1", message_id=2)))
            await settle()
            channel._sleep = wait_for_hold
            gate.release.set()
            await first
            await settle()                              # p1 is held now
            gate.release.clear()
            channel._test_clock["t"] += 61              # the cooldown no longer drops peers
            second = asyncio.ensure_future(
                channel.handle_event(human("@HermesBot task 2", message_id=3)))
            await settle()
            p2 = asyncio.ensure_future(channel.handle_event(event("@HermesBot p2", message_id=4)))
            await settle()                              # queued behind task 2
            gate.release.set()
            await second
            await settle()                              # p2 meets the cooldown: one is held
            hold.set()
            await asyncio.gather(p1, p2)
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot task 1", "@HermesBot task 2", "@HermesBot p1"])
        self.assertEqual(channel.stats.busy_dropped, 1)

    def test_a_queued_hand_off_is_not_held(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)

        async def scenario():
            gate.release.set()
            await channel.handle_event(human("@HermesBot warm up"))
            await channel.handle_event(human("@CodexBot build, then @HermesBot review",
                                             message_id=2))
            gate.release.clear()
            busy = asyncio.ensure_future(
                channel.handle_event(human("@HermesBot quick question", message_id=3)))
            await settle()
            handoff = asyncio.ensure_future(
                channel.handle_event(event("@HermesBot built, please review", message_id=4)))
            await settle()
            gate.release.set()
            await asyncio.gather(busy, handoff)
        run(scenario())
        self.assertEqual(gate.seen[-1].text, "@HermesBot built, please review")
        self.assertFalse([s for s in channel._test_slept if s >= 59])

    def _hold_behind_a_turn(self, channel, gate, during_hold):
        """Queue a peer message behind a General peer turn; when the turn ends it waits out
        the cooldown, and *during_hold* runs while it waits."""
        async def scenario():
            hold = asyncio.Event()
            holds = []

            async def wait_for_hold(seconds):
                holds.append(seconds)
                await hold.wait()
                channel._test_clock["t"] += seconds
            first = asyncio.ensure_future(channel.handle_event(event("@HermesBot one")))
            await settle()
            held = asyncio.ensure_future(
                channel.handle_event(event("@HermesBot two", message_id=2)))
            await settle()
            channel._sleep = wait_for_hold
            gate.release.set()
            await first
            await settle()
            result = await during_hold()
            hold.set()
            await held
            return holds, result
        return run(scenario())

    def test_a_held_message_waits_once_however_busy_the_chat(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)

        async def a_person_keeps_talking():
            await channel.handle_event(human("@HermesBot are you there?", message_id=3))
            channel._last_turn_at[(GROUP, None)] = channel._clock() + 30   # a later turn ended
        holds, _ = self._hold_behind_a_turn(channel, gate, a_person_keeps_talking)
        self.assertEqual(len(holds), 1)
        self.assertEqual(gate.seen[-1].text, "@HermesBot two")

    def test_held_peer_messages_stay_a_cooldown_apart(self):
        # The second held message measures its one wait from the end of the first one's
        # turn, so two held peer turns in General never run back to back.
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)
        waits = []

        async def scenario():
            hold = asyncio.Event()

            async def wait_for_hold(seconds):
                waits.append(seconds)
                await hold.wait()
                channel._test_clock["t"] += seconds
            task = asyncio.ensure_future(channel.handle_event(human("@HermesBot task")))
            await settle()
            held = [asyncio.ensure_future(channel.handle_event(
                event(f"@HermesBot p{n}", message_id=1 + n))) for n in (1, 2)]
            await settle()
            channel._sleep = wait_for_hold
            gate.release.set()
            await task
            await settle()                      # p1 waits out the cooldown, p2 behind it
            gate.release.clear()                # p1's turn stays open for now
            hold.set()
            await settle()
            gate.release.set()
            await asyncio.gather(*held)
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot task", "@HermesBot p1", "@HermesBot p2"])
        self.assertEqual(waits, [60, 60])

    def test_stop_counts_a_message_waiting_out_the_cooldown(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)

        async def stop():
            return await channel.handle_event(human("/stop@HermesBot", message_id=3))
        _, reply = self._hold_behind_a_turn(channel, gate, stop)
        self.assertIn("cleared 1 queued message", reply)
        self.assertEqual([m.text for m in gate.seen], ["@HermesBot one"])

    def test_the_hand_off_note_is_spent_only_to_beat_the_cooldown(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)
        gate.release.set()

        async def scenario():
            await channel.handle_event(human("@CodexBot build, then @HermesBot review"))
            channel._test_clock["t"] += 61
            await channel.handle_event(event("@HermesBot on it, will hand over", message_id=2))
            await channel.handle_event(event("@HermesBot built, please review", message_id=3))
            await channel.handle_event(event("@HermesBot and one more", message_id=4))
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot on it, will hand over", "@HermesBot built, please review"])

    def test_an_admin_dm_gets_no_hand_off_line(self):
        channel, gate = self.channel()
        gate.release.set()
        run(channel.handle_event(event("@HermesBot spec it, then @CodexBot build", chat_id=ADMIN_ID,
                                       author_id=ADMIN_ID, username="alice", is_bot=False)))
        self.assertNotIn("Also named", gate.seen[0].text)

    def test_only_the_first_bot_a_person_names_acts(self):
        channel, gate = self.channel()
        gate.release.set()

        async def scenario():
            await channel.handle_event(human("@CodexBot build it, @HermesBot review it"))
            await channel.handle_event(human("@HermesBot spec it, then @CodexBot build",
                                             message_id=2))
            await channel.handle_event(human("Codex: build it\n@HermesBot review",
                                             message_id=3))
            await channel.handle_event(human("@all @CodexBot and @HermesBot: report",
                                             message_id=4))
        run(scenario())
        first, everyone = gate.seen
        self.assertTrue(first.text.startswith("@HermesBot spec it, then @CodexBot build"))
        self.assertEqual(everyone.text, "@all @CodexBot and @HermesBot: report")

    def test_the_bot_that_acts_is_told_who_waits_for_it(self):
        channel, gate = self.channel()
        gate.release.set()
        run(channel.handle_event(human("@HermesBot spec it, then @CodexBot build")))
        self.assertIn("Also named in this message: @CodexBot", gate.seen[0].text)
        self.assertIn("hand the work on with an @mention", gate.seen[0].text)

    def test_the_owner_named_first_does_not_hold_a_bot_back(self):
        channel = make_channel(make_config(peers={"Alice": "alice", "Codex": "CodexBot"}))
        gate = Gate()
        channel.adapter = gate
        gate.release.set()
        run(channel.handle_event(human("@alice fyi. @HermesBot do it, then @CodexBot")))
        self.assertEqual(len(gate.seen), 1)

    def test_stop_and_status_reach_every_bot_named(self):
        channel, _ = self.channel()
        for text in ("/stop @CodexBot @HermesBot", "/status @CodexBot @HermesBot"):
            self.assertTrue(channel.should_respond(human(text))[0], text)

    def test_the_awaited_hand_off_skips_the_general_cooldown_once(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=60,
                                     max_messages_per_minute=100)
        gate.release.set()

        async def scenario():
            await channel.handle_event(human("@HermesBot warm up"))          # a recent turn
            await channel.handle_event(human("@CodexBot build, then @HermesBot review",
                                             message_id=2))                   # Hermes waits
            await channel.handle_event(event("@HermesBot built, please review",
                                             message_id=3))                   # the hand-off
            await channel.handle_event(event("@HermesBot one more thing", message_id=4))
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot warm up", "@HermesBot built, please review"])

    def test_pacing_settings_are_bounded(self):
        from swarmly.telegram.config import ConfigError
        make_config(limits={"max_messages_per_minute": 0})          # falls back, as before
        for limits in ({"min_seconds_between_messages": -1}, {"max_messages_per_minute": -1},
                       {"max_group_messages_per_minute": 0},
                       {"min_seconds_between_agent_turns": float("nan")}):
            with self.assertRaises(ConfigError, msg=str(limits)):
                make_config(limits=limits)

    def test_every_named_bot_acts_with_multi_mention_all(self):
        channel, gate = self.channel(multi_mention="all")
        gate.release.set()
        run(channel.handle_event(human("@CodexBot build it, @HermesBot review it")))
        self.assertEqual(len(gate.seen), 1)

    def test_a_peer_naming_two_bots_still_reaches_both(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=0)
        gate.release.set()
        run(channel.handle_event(event("@CodexBot done, @HermesBot your turn")))
        self.assertEqual(len(gate.seen), 1)

    def test_multi_mention_must_be_first_or_all(self):
        from swarmly.telegram.config import ConfigError
        with self.assertRaises(ConfigError):
            make_config(limits={"multi_mention": "last"})

    def test_peer_turns_in_a_topic_are_capped_until_a_human_posts(self):
        channel, gate = self.channel(parallel=2, min_seconds_between_agent_turns=0,
                                     max_messages_per_minute=100, max_peer_turns_per_topic=2)
        gate.accepts_explicit_username_delivery = lambda: True    # mentions skip the cooldown
        gate.release.set()

        async def scenario():
            for n in range(1, 4):
                await channel.handle_event(event(f"@HermesBot p{n}", thread_id=4, message_id=n))
            await channel.handle_event(event("@HermesBot other", thread_id=5, message_id=5))
            await channel.handle_event(human("carry on", thread_id=4, message_id=6))
            await channel.handle_event(event("@HermesBot p4", thread_id=4, message_id=7))
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot p1", "@HermesBot p2", "@HermesBot other", "@HermesBot p4"])

    def capped(self):
        channel, gate = self.channel(parallel=2, min_seconds_between_agent_turns=0,
                                     max_messages_per_minute=100, max_peer_turns_per_topic=1,
                                     min_seconds_between_messages=0)
        gate.accepts_explicit_username_delivery = lambda: True
        gate.release.set()
        return channel, gate

    def test_the_cap_says_so_once_and_leaves_general_alone(self):
        channel, gate = self.capped()

        async def scenario():
            for n in range(1, 4):
                await channel.handle_event(event(f"@HermesBot t{n}", thread_id=4, message_id=n))
            for n in range(5, 8):
                await channel.handle_event(event(f"@HermesBot g{n}", message_id=n))
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot t1", "@HermesBot g5", "@HermesBot g6", "@HermesBot g7"])
        notices = [m["text"] for m in channel._test_sender.sent if "paused after" in m["text"]]
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0].startswith("⚠️"))       # peers ignore it as a notice

    def test_progress_resets_the_run_of_bot_turns(self):
        channel, gate = self.capped()

        async def scenario():
            await channel.handle_event(event("@HermesBot PR #8 is up", thread_id=4,
                                             message_id=1))
            await channel.handle_event(event("@HermesBot merged #8, opened PR #9", thread_id=4,
                                             message_id=2))
            await channel.handle_event(event("@HermesBot still on #9", thread_id=4,
                                             message_id=3))   # nothing new: capped
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot PR #8 is up", "@HermesBot merged #8, opened PR #9"])
        notices = [m["text"] for m in channel._test_sender.sent if "paused after" in m["text"]]
        self.assertEqual(len(notices), 1)

    def test_a_short_sha_in_the_text_is_progress(self):
        channel, gate = self.capped()

        async def scenario():
            await channel.handle_event(event("@HermesBot pushed e391ac1, tests pass",
                                             thread_id=4, message_id=1))
            await channel.handle_event(event("@HermesBot pushed b0da422 with the name list",
                                             thread_id=4, message_id=2))
            await channel.handle_event(event("@HermesBot b0da422 again; deadbeef 1234567",
                                             thread_id=4, message_id=3))   # nothing new
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot pushed e391ac1, tests pass",
                          "@HermesBot pushed b0da422 with the name list"])

    def test_a_new_qa_verdict_is_progress_a_repeated_one_is_not(self):
        channel, gate = self.capped()

        async def scenario():
            await channel.handle_event(event("@HermesBot QA FAIL on #17", thread_id=4,
                                             message_id=1))
            await channel.handle_event(event("@HermesBot QA PASS on #17", thread_id=4,
                                             message_id=2))
            await channel.handle_event(event("@HermesBot QA passed on #17 again", thread_id=4,
                                             message_id=3))          # nothing new
        run(scenario())
        self.assertEqual([m.text for m in gate.seen],
                         ["@HermesBot QA FAIL on #17", "@HermesBot QA PASS on #17"])

    def test_no_reminder_right_behind_the_pause_notice(self):
        channel, gate = self.capped()

        async def scenario():
            for n in range(1, 4):
                await channel.handle_event(event(f"@HermesBot t{n}", thread_id=4, message_id=n))
        run(scenario())
        texts = [m["text"] for m in channel._test_sender.sent]
        self.assertEqual(sum("paused after" in t for t in texts), 1, texts)
        self.assertFalse(any("still paused here" in t for t in texts), texts)

    def test_a_peers_pause_notice_holds_off_the_reminder(self):
        channel, gate = self.capped()

        async def scenario():
            await channel.handle_event(event("@HermesBot first", thread_id=4, message_id=9))
            await channel.handle_event(event("[Codex] ⚠️ paused after 1 bot turns in a row in "
                                             "this topic. Post here to continue.", thread_id=4,
                                             message_id=1))
            await channel.handle_event(event("@HermesBot are we done?", thread_id=4,
                                             message_id=2))
        run(scenario())
        texts = [m["text"] for m in channel._test_sender.sent]
        self.assertFalse(any("still paused here" in t for t in texts), texts)
        self.assertFalse(any("paused after" in t for t in texts), texts)
        self.assertEqual([m.text for m in gate.seen], ["@HermesBot first"])

    def test_a_capped_topic_still_answers_ping_and_acks_requests(self):
        from swarmly.telegram.wire import Envelope, MessageType, format_message
        channel, gate = self.capped()
        request = format_message(Envelope(type=MessageType.REQUEST, to="Hermes", id="r1"),
                                 "@HermesBot do the thing")

        async def scenario():
            await channel.handle_event(event("@HermesBot first", thread_id=4))
            await channel.handle_event(event("/ping@HermesBot", thread_id=4, message_id=2))
            await channel.handle_event(event(request, thread_id=4, message_id=3))
        run(scenario())
        sent = [m["text"] for m in channel._test_sender.sent]
        self.assertTrue(any("PONG" in text or "pong" in text.lower() for text in sent), sent)
        self.assertTrue(any(" ACK " in text and "ref=r1" in text for text in sent), sent)
        self.assertEqual(len(gate.seen), 1)

    def test_commands_do_not_reset_the_cap(self):
        channel, gate = self.capped()

        async def scenario():
            await channel.handle_event(event("@HermesBot first", thread_id=4))
            await channel.handle_event(human("/status@HermesBot", thread_id=4, message_id=2))
            await channel.handle_event(event("@HermesBot again", thread_id=4, message_id=3))
        run(scenario())
        self.assertEqual(len(gate.seen), 1)

    def test_the_peer_turn_cap_holds_at_one_turn_at_a_time(self):
        channel, gate = self.channel(min_seconds_between_agent_turns=0,
                                     max_messages_per_minute=100, max_peer_turns_per_topic=1)
        gate.release.set()

        async def scenario():
            for n in range(1, 4):
                await channel.handle_event(event(f"@HermesBot p{n}", thread_id=4, message_id=n))
            await channel.handle_event(event("@HermesBot general", message_id=9))
        run(scenario())
        self.assertEqual([m.text for m in gate.seen], ["@HermesBot p1", "@HermesBot general"])

    def test_a_name_is_learned_from_any_message_in_the_topic(self):
        channel, gate = self.channel()
        gate.release.set()
        note = event("not for you", thread_id=6)
        note.topic_name = "t2 · docs"
        run(channel.handle_event(note))
        self.assertEqual(channel.topics.name(GROUP, 6), "t2 · docs")

    def test_one_turn_at_a_time_by_default(self):
        channel, gate = self.channel(parallel=1)

        async def scenario():
            first = asyncio.ensure_future(channel.handle_event(human("@HermesBot a", thread_id=4)))
            await settle()
            second = asyncio.ensure_future(channel.handle_event(
                human("@HermesBot b", thread_id=5, message_id=2)))
            await settle()
            queued = channel.stats.busy_queued
            gate.release.set()
            await asyncio.gather(first, second)
            return queued
        self.assertEqual(run(scenario()), 1)
        self.assertEqual(gate.peak, 1)

    def test_topics_run_in_parallel_up_to_the_cap(self):
        channel, gate = self.channel(parallel=2)

        async def scenario():
            turns = [asyncio.ensure_future(channel.handle_event(
                human(f"@HermesBot job {n}", thread_id=thread, message_id=n)))
                for n, thread in ((1, 4), (2, 5))]
            await settle()
            both = gate.running
            same_topic = asyncio.ensure_future(channel.handle_event(
                human("@HermesBot more for 4", thread_id=4, message_id=3)))
            third_topic = asyncio.ensure_future(channel.handle_event(
                human("@HermesBot job 6", thread_id=6, message_id=4)))
            await settle()
            waiting = (gate.running, channel.stats.busy_queued)
            gate.release.set()
            await asyncio.gather(*turns, same_topic, third_topic)
            return both, waiting
        both, waiting = run(scenario())
        self.assertEqual(both, 2)
        self.assertEqual(waiting, (2, 2))       # same topic waits; a third topic waits for a slot
        self.assertEqual(gate.peak, 2)
        self.assertEqual(len(gate.seen), 4)

    def start_two(self, channel):
        return [asyncio.ensure_future(channel.handle_event(
            human(f"@HermesBot job {n}", thread_id=thread, message_id=n)))
            for n, thread in ((1, 4), (2, 5))]

    def test_stop_in_general_stops_every_running_turn(self):
        channel, gate = self.channel(parallel=2)

        async def scenario():
            turns = self.start_two(channel)
            await settle()
            await channel.handle_event(human("/stop@HermesBot", message_id=9))
            return await asyncio.gather(*turns)
        results = run(scenario())
        self.assertEqual(results, [None, None])
        self.assertEqual(channel.stats.stopped, 2)
        self.assertIn("stopped 2 running turns", channel._test_sender.sent[-1]["text"])

    def test_stop_in_a_topic_stops_that_topic_only(self):
        channel, gate = self.channel(parallel=2)

        async def scenario():
            turns = self.start_two(channel)
            await settle()
            queued = asyncio.ensure_future(channel.handle_event(
                human("@HermesBot later", thread_id=4, message_id=3)))
            await settle()
            await channel.handle_event(human("/stop@HermesBot", thread_id=4, message_id=9))
            stopped_line = channel._test_sender.sent[-1]["text"]
            await settle()
            still = gate.running
            await channel.handle_event(human("/stop@HermesBot all", thread_id=4, message_id=10))
            return stopped_line, still, await asyncio.gather(*turns, queued)
        line, still, results = run(scenario())
        self.assertIn("stopped the running turn and cleared 1 queued message", line)
        self.assertIn("1 in other topics still running", line)
        self.assertEqual(still, 1)
        self.assertEqual(results, [None, None, None])
        self.assertEqual(len(gate.seen), 2)         # the queued message never ran

    def test_the_queue_cap_is_per_topic_when_parallel(self):
        channel, gate = self.channel(parallel=2, busy_queue_max=1)

        async def scenario():
            turns = self.start_two(channel)
            await settle()
            more = [asyncio.ensure_future(channel.handle_event(
                human(f"@HermesBot more {n}", thread_id=thread, message_id=n)))
                for n, thread in ((3, 4), (4, 5), (5, 4))]
            await settle()
            counts = (channel.stats.busy_queued, channel.stats.busy_dropped)
            gate.release.set()
            await asyncio.gather(*turns, *more)
            return counts
        self.assertEqual(run(scenario()), (2, 1))   # one waits in each topic; the extra drops

    def test_bus_deliveries_share_the_turn_admission(self):
        channel, gate = self.channel(parallel=2)

        async def bus_turn():
            async with channel.admitted():
                return await gate.respond(IncomingMessage(text="bus", author_alias="Codex"))

        async def scenario():
            turns = self.start_two(channel)
            await settle()
            bus = asyncio.ensure_future(bus_turn())
            await settle()
            waiting = gate.running
            gate.release.set()
            await asyncio.gather(*turns, bus)
            return waiting
        self.assertEqual(run(scenario()), 2)        # the bus turn waited for a slot
        self.assertEqual(gate.peak, 2)

    def test_a_closed_topic_gets_its_answer_in_general(self):
        from aiogram.exceptions import TelegramBadRequest
        channel, gate = self.channel()
        channel.topics.remember(GROUP, 4, "deploy")
        calls = []

        class Bot:
            async def send_message(self, **kw):
                calls.append(kw)
                if kw.get("message_thread_id") is not None:
                    raise TelegramBadRequest(method=None, message="Bad Request: TOPIC_CLOSED")
                return types.SimpleNamespace(message_id=77)
        channel.bot = Bot()
        run(channel._send_with_retry("done", 12, chat_id=GROUP, thread_id=4))
        self.assertEqual([c["message_thread_id"] for c in calls], [4, None])
        self.assertIsNone(calls[1]["reply_to_message_id"])
        self.assertIn("meant for topic #4, which is closed", calls[1]["text"])
        self.assertEqual(channel.stats.errors, 1)
        self.assertEqual(channel.stats.last_error, "")      # delivered, so /status shows no blocker

    def test_a_long_reply_still_fits_after_the_closed_topic_note(self):
        from aiogram.exceptions import TelegramBadRequest
        channel, gate = self.channel()
        calls = []

        class Bot:
            async def send_message(self, **kw):
                calls.append(kw)
                if kw.get("message_thread_id") is not None:
                    raise TelegramBadRequest(method=None, message="Bad Request: TOPIC_DELETED")
                return types.SimpleNamespace(message_id=1)
        channel.bot = Bot()
        limit = channel.config.limits.max_message_chars
        run(channel._send_with_retry("x" * (limit - 3) + "&amp;", None, chat_id=GROUP,
                                     thread_id=4))
        text = calls[-1]["text"]
        self.assertLessEqual(len(text), limit)
        self.assertTrue(text.endswith("which is closed or deleted)"))
        self.assertNotIn("&am\n", text)

    def fallback_bot(self, calls):
        from aiogram.exceptions import TelegramBadRequest

        class Bot:
            async def send_message(self, **kw):
                calls.append(kw)
                if kw.get("message_thread_id") is not None:
                    raise TelegramBadRequest(method=None, message="Bad Request: TOPIC_CLOSED")
                return types.SimpleNamespace(message_id=len(calls))
        return Bot()

    def test_a_plain_reply_stays_plain_when_its_mention_is_cut(self):
        channel, gate = self.channel()
        calls = []
        channel.bot = self.fallback_bot(calls)
        limit = channel.config.limits.max_message_chars
        text = "a < b " + "x" * (limit - 20) + " @CodexBot"
        mention = [{"type": "mention", "offset": len(text) - 9, "length": 9}]
        run(channel._send_with_retry(text, None, entities=mention, chat_id=GROUP, thread_id=4))
        self.assertIsNone(calls[-1]["parse_mode"])
        self.assertFalse(calls[-1]["entities"])

    def test_the_fallback_counts_utf16_units(self):
        from swarmly.telegram.wire import utf16_length
        channel, gate = self.channel()
        calls = []
        channel.bot = self.fallback_bot(calls)
        run(channel._send_with_retry("😀" * 2030, None, chat_id=GROUP, thread_id=4))
        self.assertLessEqual(utf16_length(calls[-1]["text"]),
                             channel.config.limits.max_message_chars)
        self.assertNotIn("\ufffd", calls[-1]["text"])

    def test_a_split_reply_tries_a_dead_topic_once(self):
        channel, gate = self.channel(max_messages_per_minute=100,
                                     min_seconds_between_messages=0)
        channel._sender = None
        calls = []
        channel.bot = self.fallback_bot(calls)
        limit = channel.config.limits.max_message_chars
        run(channel.send("word " * (limit // 2), chat_id=GROUP, thread_id=4))
        topics = [c["message_thread_id"] for c in calls]
        self.assertGreater(len(topics), 2)                  # split into several parts
        self.assertEqual(topics, [4] + [None] * (len(topics) - 1))
        self.assertEqual(sum("meant for topic" in c["text"] for c in calls), 1)
        self.assertEqual(channel.stats.errors, 1)

    def test_a_topic_name_never_addresses_a_peer_from_general(self):
        channel, gate = self.channel()
        channel.topics.remember(GROUP, 4, "@CodexBot run the deploy now")
        calls = []
        channel.bot = self.fallback_bot(calls)
        run(channel._send_with_retry("done", None, chat_id=GROUP, thread_id=4))
        self.assertNotIn("@", calls[-1]["text"])

    def test_parallel_topics_are_paced_each_on_their_own(self):
        channel, gate = self.channel(parallel=2, min_seconds_between_messages=60,
                                     max_messages_per_minute=1)

        async def scenario():
            await channel.send("one", chat_id=GROUP, thread_id=4)
            await channel.send("two", chat_id=GROUP, thread_id=5)
            first = list(channel._test_slept)
            await channel.send("three", chat_id=GROUP, thread_id=4)
            return first, list(channel._test_slept)
        first, after = run(scenario())
        self.assertTrue(all(s <= 1.0 for s in first))   # another topic waits 1s at most
        self.assertTrue(after and after[-1] >= 59)  # the same topic still does

    def test_a_burst_across_topics_stays_under_telegrams_cap(self):
        from swarmly.telegram.channel import GROUP_MESSAGES_PER_MINUTE, GROUP_MIN_GAP_SECONDS
        clock = VirtualClock()
        channel = make_channel(make_config(limits={
            "max_parallel_turns": 8, "allow_shared_cwd": True,
            "min_seconds_between_messages": 0, "max_messages_per_minute": 100}),
            clock=clock.now, sleeper=clock.sleep)
        channel._sender = None
        bot = channel.bot = ClockBot(clock)

        async def scenario():
            sends = [channel.send(f"m{n}", chat_id=GROUP, thread_id=100 + n % 25)
                     for n in range(50)]
            await clock.drive(asyncio.gather(*sends))
        run(scenario())
        times = [c["t"] for c in bot.accepted]
        self.assertEqual(len(times), 50)
        self.assertLessEqual(most_in_a_minute(times), GROUP_MESSAGES_PER_MINUTE)
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])),
                                GROUP_MIN_GAP_SECONDS)

    def test_a_flood_wait_in_one_topic_holds_the_others(self):
        clock = VirtualClock()
        channel = make_channel(make_config(limits={
            "max_parallel_turns": 2, "min_seconds_between_messages": 0}),
            clock=clock.now, sleeper=clock.sleep)
        channel._sender = None
        bot = channel.bot = ClockBot(clock, flood={4: 30})

        async def scenario():
            first = asyncio.ensure_future(channel.send("one", chat_id=GROUP, thread_id=4))
            await clock.drive(asyncio.sleep(0), until=1001.0)    # topic 4 is now waiting
            await clock.drive(asyncio.gather(
                first, channel.send("two", chat_id=GROUP, thread_id=5)))
        run(scenario())
        self.assertEqual([c["thread"] for c in bot.accepted], [4, 5])
        self.assertGreaterEqual(bot.accepted[1]["t"], 1030.0)   # 5 waited out the flood

    def test_a_dead_topic_and_a_live_one_keep_their_own_replies(self):
        clock = VirtualClock()
        channel = make_channel(make_config(limits={
            "max_parallel_turns": 2, "min_seconds_between_messages": 5,
            "max_messages_per_minute": 100}), clock=clock.now, sleeper=clock.sleep)
        channel._sender = None
        bot = channel.bot = ClockBot(clock, dead={4})
        limit = channel.config.limits.max_message_chars

        async def scenario():
            dead = asyncio.ensure_future(channel.send("word " * (limit // 2), chat_id=GROUP,
                                                      thread_id=4))
            live = [channel.send(f"live {n}", chat_id=GROUP, thread_id=5) for n in range(3)]
            await clock.drive(asyncio.gather(dead, *live))
        run(scenario())
        notes = [c for c in bot.accepted if "meant for topic" in c["text"]]
        self.assertEqual(len(notes), 1)                     # the dead topic is tried once
        self.assertEqual([c["thread"] for c in bot.accepted if "live" in c["text"]], [5, 5, 5])
        self.assertEqual(bot.refused, 1)

    def test_a_dead_topics_reply_never_interleaves_with_general(self):
        clock = VirtualClock()
        channel = make_channel(make_config(limits={
            "max_parallel_turns": 2, "min_seconds_between_messages": 5,
            "max_messages_per_minute": 100}), clock=clock.now, sleeper=clock.sleep)
        channel._sender = None
        bot = channel.bot = ClockBot(clock, dead={4})
        limit = channel.config.limits.max_message_chars

        async def scenario():
            await clock.drive(asyncio.gather(
                channel.send("dead " * (limit // 2), chat_id=GROUP, thread_id=4),
                channel.send("main " * (limit // 2), chat_id=GROUP)))
        run(scenario())
        general = [c for c in bot.accepted if c["thread"] is None]
        owners = [("main" if "main " in c["text"] else "dead") for c in general]
        self.assertEqual(owners, sorted(owners, key=owners.index))      # one reply, then the other
        times = [c["t"] for c in general]
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 5)

    def parallel(self, clock, **bot):
        channel = make_channel(make_config(limits={
            "max_parallel_turns": 3, "allow_shared_cwd": True,
            "min_seconds_between_messages": 5, "max_messages_per_minute": 100}),
            clock=clock.now, sleeper=clock.sleep)
        channel._sender = None
        channel.bot = ClockBot(clock, **bot)
        return channel, channel.bot

    @staticmethod
    def runs(posts, words):
        """Which reply each post belongs to, with repeats collapsed: a reply whose parts
        stay together shows up once."""
        owners = [next(w for w in words if f"{w} " in p["text"]) for p in posts
                  if any(f"{w} " in p["text"] for w in words)]
        return [o for i, o in enumerate(owners) if i == 0 or owners[i - 1] != o]

    def test_a_general_reply_started_after_the_refusal_waits_for_the_reroute(self):
        clock = VirtualClock()
        started = {}
        channel, bot = self.parallel(clock, dead={4})
        limit = channel.config.limits.max_message_chars
        bot.on_refuse = lambda: started.setdefault("main", asyncio.ensure_future(
            channel.send("main " * (limit // 2), chat_id=GROUP)))

        async def scenario():
            dead = channel.send("dead " * (limit // 2), chat_id=GROUP, thread_id=4)
            await clock.drive(dead)
            await clock.drive(started["main"])
        run(scenario())
        self.assertEqual(self.runs(bot.accepted, ("dead", "main")), ["dead", "main"])

    def test_two_dead_topics_reroute_one_after_the_other(self):
        clock = VirtualClock()
        channel, bot = self.parallel(clock, dead={4, 6})
        limit = channel.config.limits.max_message_chars

        async def scenario():
            await clock.drive(asyncio.gather(
                channel.send("four " * (limit // 2), chat_id=GROUP, thread_id=4),
                channel.send("six " * (limit // 2), chat_id=GROUP, thread_id=6)))
        run(scenario())
        self.assertEqual(sorted(self.runs(bot.accepted, ("four", "six"))), ["four", "six"])

    def test_a_note_that_fails_does_not_stop_the_reply(self):
        clock = VirtualClock()
        channel, bot = self.parallel(clock, dead={4}, refuse_text="(the message above")
        limit = channel.config.limits.max_message_chars
        text = "x" * (limit - 30) + " word " * (limit // 6)
        run(clock.drive(channel.send(text, chat_id=GROUP, thread_id=4)))
        # Every part of the split reply went out; only the note about the dead topic failed,
        # so the part count follows what the split makes of this text (marker width included).
        self.assertEqual(len(bot.accepted), len(channel.split_outgoing(text)))
        self.assertGreaterEqual(len(bot.accepted), 2)
        self.assertRegex(bot.accepted[-1]["text"], r"↪ \(\d+/\d+ #[0-9a-f]{6}\)")
        self.assertNotIn("the message above", "".join(entry["text"] for entry in bot.accepted))
        self.assertEqual(channel.stats.last_error, "")

    def test_flood_waits_do_not_use_up_the_attempts(self):
        clock = VirtualClock()
        channel, bot = self.parallel(clock, flood={4: [5, 5, 5, 5]})
        run(clock.drive(channel.send("done", chat_id=GROUP, thread_id=4)))
        self.assertEqual([c["thread"] for c in bot.accepted], [4])

    def test_a_reroute_stops_when_the_bot_is_frozen(self):
        clock = VirtualClock()
        channel, bot = self.parallel(clock, dead={4})
        bot.on_refuse = lambda: setattr(channel, "_frozen", True)
        limit = channel.config.limits.max_message_chars
        run(clock.drive(channel.send("word " * (limit // 2), chat_id=GROUP, thread_id=4)))
        self.assertEqual(bot.accepted, [])

    def test_a_failed_send_is_the_blocker(self):
        from aiogram.exceptions import TelegramBadRequest
        channel, gate = self.channel()
        channel._sender = None

        class Bot:                      # the topic is gone, and General refuses too
            async def send_message(self, **kw):
                if kw.get("message_thread_id") is not None:
                    raise TelegramBadRequest(method=None, message="Bad Request: TOPIC_CLOSED")
                raise TelegramBadRequest(method=None, message="Bad Request: not enough rights")
        channel.bot = Bot()
        with self.assertRaises(TelegramBadRequest):
            run(channel.send("done", chat_id=GROUP, thread_id=4))
        self.assertIn("not enough rights", channel.stats.last_error)
        self.assertEqual(channel.stats.errors, 2)       # the topic, then General

    def test_a_long_part_keeps_every_word_and_the_note_follows(self):
        channel, gate = self.channel()
        calls = []
        channel.bot = self.fallback_bot(calls)
        limit = channel.config.limits.max_message_chars
        text = "x" * (limit - 10) + " last"
        run(channel._send_with_retry(text, None, chat_id=GROUP, thread_id=4))
        self.assertEqual(calls[1]["text"], text)
        self.assertIn("the message above was meant for topic #4", calls[2]["text"])

    def test_one_turn_at_a_time_keeps_the_bot_wide_pace(self):
        channel, gate = self.channel(min_seconds_between_messages=60)

        async def scenario():
            await channel.send("one", chat_id=GROUP, thread_id=4)
            await channel.send("two", chat_id=GROUP, thread_id=5)
            return list(channel._test_slept)
        self.assertTrue(run(scenario()))

    def test_stop_all_must_follow_the_command(self):
        channel, gate = self.channel(parallel=2)

        async def scenario():
            turns = self.start_two(channel)
            await settle()
            await channel.handle_event(human("/stop@HermesBot this one, not all",
                                             thread_id=4, message_id=9))
            await settle()
            still = gate.running
            await channel.handle_event(human("/stop@HermesBot", message_id=10))
            await asyncio.gather(*turns)
            return still
        self.assertEqual(run(scenario()), 1)

    def test_max_parallel_turns_must_be_positive(self):
        from swarmly.telegram.config import ConfigError
        with self.assertRaises(ConfigError):
            make_config(limits={"max_parallel_turns": 0})
        with self.assertRaises(ConfigError):
            make_config(limits={"max_parallel_turns": 9, "allow_shared_cwd": True})


class PollLockTests(unittest.TestCase):
    def test_admission_restarts_the_poll_lock_ttl(self):
        from swarmly.telegram.poll import (acquire_poll_lock, poll_lock_path, refresh_poll_lock,
                                       release_poll_lock)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertTrue(acquire_poll_lock(repo, "hermes", now=100.0))
            try:
                refresh_poll_lock(repo, "hermes", now=500.0)
                stamp = json.loads(poll_lock_path(repo, "hermes").read_text())["acquired_at"]
                self.assertEqual(stamp, 500.0)
                self.assertFalse(acquire_poll_lock(repo, "hermes", ttl_seconds=300, now=700.0))
            finally:
                # Released before the directory goes: Windows cannot delete a held OS lock file.
                release_poll_lock(repo, "hermes")


class DiagTests(unittest.TestCase):
    def test_diag_in_a_topic_names_its_discussion_and_session(self):
        adapter = ClaudeThreadAdapter("Hermes", ClaudeThreadAdapter.default_settings(cwd="/w"))
        channel = make_channel(adapter=adapter)
        run(channel.handle_event(human("/diag@HermesBot", thread_id=7)))
        line = channel._test_sender.sent[-1]["text"]
        self.assertIn("context=tg-7", line)
        self.assertIn("session=" + ClaudeThreadAdapter.derived_session_id(
            "Hermes", "/w", "tg-7"), line)


class Runner:
    """Holds each Claude call until released; records argv."""

    def __init__(self):
        self.calls: List[List[str]] = []
        self._release: Optional[asyncio.Event] = None

    @property
    def release(self) -> asyncio.Event:
        if self._release is None:
            self._release = asyncio.Event()
        return self._release

    async def __call__(self, argv: Sequence[str], cwd: Optional[str], env: Dict[str, str],
                       timeout: float) -> Any:
        self.calls.append(list(argv))
        await self.release.wait()
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": "ok", "session_id": "x"}).encode()

        class Proc:
            returncode = 0

            async def communicate(self):
                return out, b""
        return Proc()


class ClaudeSessionTests(unittest.TestCase):
    def test_each_discussion_has_its_own_session_in_parallel(self):
        runner = Runner()
        adapter = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(
            cwd="/work", session="11111111-1111-1111-1111-111111111111"), runner=runner)

        def msg(context):
            return IncomingMessage(text="hi", author_alias="Alice", context_id=context)

        async def scenario():
            turns = [asyncio.ensure_future(adapter.respond(msg(c))) for c in (None, "t1", "t2")]
            await settle()
            started = len(runner.calls)
            runner.release.set()
            return started, await asyncio.gather(*turns)
        started, replies = run(scenario())
        self.assertEqual(started, 3, "different discussions do not wait for each other")
        flag = [("--resume" if "--resume" in argv else "--session-id") for argv in runner.calls]
        self.assertEqual(flag, ["--resume", "--session-id", "--session-id"])   # topics: new
        sessions = [argv[argv.index(f) + 1] for f, argv in zip(flag, runner.calls)]
        self.assertEqual(sessions[0], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(sessions[1], ClaudeThreadAdapter.derived_session_id(
            "Claude", "/work", "t1"))
        self.assertEqual(len(set(sessions)), 3)
        self.assertEqual(replies[1].meta["context_id"], "t1")

    def test_one_discussion_stays_in_order(self):
        runner = Runner()
        adapter = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(cwd="/w"),
                                      runner=runner)

        async def scenario():
            msg = IncomingMessage(text="hi", author_alias="Alice", context_id="t1")
            turns = [asyncio.ensure_future(adapter.respond(msg)) for _ in range(2)]
            await settle()
            started = len(runner.calls)
            runner.release.set()
            await asyncio.gather(*turns)
            return started
        self.assertEqual(run(scenario()), 1)


if __name__ == "__main__":
    unittest.main()
