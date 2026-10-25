"""The startup self-test: do this bot and its peers see each other in the group?"""

from __future__ import annotations

import asyncio
import heapq
import importlib.util
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.channel import AgentChannel, ChannelEvent
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.selftest import REPLY_GAP_SECONDS, describe, read_selftest
from swarmly.telegram.wire import Envelope, MessageType, format_message

GROUP = -100777
BOTS = {"Codex": ("codex_bot", 11), "Hermes": ("hermes_bot", 22), "Antigravity": ("agy_bot", 33),
        "Claude": ("claude_bot", 44)}
ALL = list(BOTS)


class VirtualTime:
    """A virtual clock: every sleep wakes in time order, and no real time passes."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.now = 1000.0
        self.queue: List[Any] = []
        self.count = 0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        fut = self.loop.create_future()
        self.count += 1
        heapq.heappush(self.queue, (self.now + max(float(seconds), 0.0), self.count, fut))
        await fut

    async def run(self, group: "Group", seconds: float) -> None:
        until = self.now + seconds
        while True:
            for _ in range(3):
                await group.deliver()
                for _ in range(20):
                    await asyncio.sleep(0)
            while self.queue and self.queue[0][2].done():
                heapq.heappop(self.queue)
            if not self.queue or self.queue[0][0] > until:
                self.now = max(self.now, until)
                return
            at, _, fut = heapq.heappop(self.queue)
            self.now = max(self.now, at)
            fut.set_result(None)


class Group:
    """A fake Telegram group: what one bot sends, the bots that can see it receive."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.log: List[Dict[str, Any]] = []      # everything ever sent, in order
        self.channels: Dict[str, AgentChannel] = {}
        self.blind: set = set()          # (reader, author) pairs that never arrive

    def sender_for(self, alias: str) -> Any:
        async def send(payload: str, reply_to: Any = None) -> str:
            self.sent.append({"from": alias, "text": payload})
            self.log.append({"from": alias, "text": payload})
            return payload
        return send

    async def deliver(self) -> int:
        """Hand every message sent so far to every other bot that can see it."""
        count = 0
        while self.sent:
            msg = self.sent.pop(0)
            username, user_id = BOTS[msg["from"]]
            for alias, chan in self.channels.items():
                if alias == msg["from"] or (alias, msg["from"]) in self.blind:
                    continue
                await chan.handle_event(ChannelEvent(
                    text=msg["text"], chat_id=GROUP, author_id=user_id,
                    author_username=username, author_is_bot=True, message_id=1))
                count += 1
            await asyncio.sleep(0)
        return count


class TestPeerSelfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self._close)
        self.group = Group()

    def _close(self) -> None:
        asyncio.set_event_loop(None)
        self.loop.close()

    def run_async(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def channel(self, alias: str, peers: List[str], vt: Optional[VirtualTime] = None,
                pacing: float = 0, selftest_seconds: float = 0) -> AgentChannel:
        username, user_id = BOTS[alias]
        cfg = AgentConfig.from_dict({
            "agent": {"name": username, "alias": alias, "bot_token": "",
                      "home_channel_id": GROUP, "admin_user_id": 1},
            "peers": {p: BOTS[p][0] for p in peers},
            "adapter": {"type": "echo"},
            "limits": {"min_seconds_between_messages": pacing, "max_messages_per_minute": 1000,
                       "max_group_messages_per_minute": 1000,
                       "min_seconds_between_agent_turns": 0,
                       "selftest_seconds": selftest_seconds},
            "bus": {"repo": str(Path(self.tmp.name) / alias), "poll_minutes": 0},
        })

        async def no_wait(_s: float) -> None:
            await asyncio.sleep(0)
        if vt is not None:
            chan = AgentChannel(cfg, adapter=EchoAdapter(alias),
                                sender=self.group.sender_for(alias), clock=vt.clock,
                                sleeper=vt.sleep)
            chan.selftest.clock = vt.clock
        else:
            chan = AgentChannel(cfg, adapter=EchoAdapter(alias),
                                sender=self.group.sender_for(alias), sleeper=no_wait)
        self.group.channels[alias] = chan
        return chan

    async def boot(self, chan: AgentChannel) -> None:
        """What start() does for the self-test, without aiogram."""
        await chan.send(chan.online_notice())
        chan.selftest.start(True)

    async def settle(self, rounds: int = 6) -> None:
        for _ in range(rounds):
            await self.group.deliver()
            for _ in range(5):
                await asyncio.sleep(0)

    def test_two_bots_that_see_each_other_pass(self):
        codex = self.channel("Codex", ["Hermes"])
        hermes = self.channel("Hermes", ["Codex"])

        async def go() -> None:
            await self.boot(codex)
            await self.boot(hermes)
            await self.settle()
            await codex.selftest.report()
            await hermes.selftest.report()
        self.run_async(go())
        for chan in (codex, hermes):
            result = read_selftest(chan.config)
            self.assertTrue(result["ok"], describe(result))
        # No warning line was posted, and no model turn ran on any of it.
        self.assertFalse(any("self-test" in m["text"] for m in self.group.log))
        self.assertEqual(codex.stats.handled + hermes.stats.handled,
                         codex.stats.machine_events + hermes.stats.machine_events)

    def test_a_one_way_link_is_reported_from_both_ends(self):
        codex = self.channel("Codex", ["Hermes"])
        hermes = self.channel("Hermes", ["Codex"])
        self.group.blind.add(("Hermes", "Codex"))       # Hermes never receives Codex

        async def go() -> None:
            await self.boot(codex)
            await self.boot(hermes)
            await self.settle()
            await codex.selftest.report()
            await hermes.selftest.report()
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(go())
        codex_view = read_selftest(codex.config)["peers"]["Hermes"]
        hermes_view = read_selftest(hermes.config)["peers"]["Codex"]
        self.assertTrue(codex_view["status"].startswith("Hermes did not confirm"))
        # Hermes cannot tell "offline" from "invisible": Codex's answer never reaches it.
        self.assertIn("no answer", hermes_view["status"])

    def test_an_offline_peer_gets_one_warning_line_and_a_late_start_clears_it(self):
        codex = self.channel("Codex", ["Hermes"])

        async def first() -> None:
            await self.boot(codex)
            await self.settle()
            await codex.selftest.report()
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(first())
        warnings = [m for m in self.group.log if "self-test" in m["text"]]
        self.assertFalse(read_selftest(codex.config)["ok"])
        self.assertIn("no answer", read_selftest(codex.config)["peers"]["Hermes"]["status"])

        hermes = self.channel("Hermes", ["Codex"])

        async def later() -> None:
            await self.boot(hermes)
            await self.settle()
        self.run_async(later())
        self.assertTrue(read_selftest(codex.config)["ok"])      # updated without a rerun
        self.assertEqual(len(warnings), 1)                      # one line, not one per peer

    def test_seen_answers_are_batched_and_a_restart_is_answered_again(self):
        vt = VirtualTime(self.loop)
        codex = self.channel("Codex", ["Hermes", "Antigravity"], vt)
        hermes = self.channel("Hermes", ["Codex"], vt)
        agy = self.channel("Antigravity", ["Codex"], vt)

        def codex_seen() -> List[Any]:
            return [m for m in self.group.log
                    if m["from"] == "Codex" and "intent=SEEN" in m["text"]]

        async def go() -> None:
            await self.boot(hermes)
            await self.boot(agy)
            online = [m for m in self.group.sent if "intent=ONLINE" in m["text"]]
            await vt.run(self.group, 30)            # both ONLINEs inside one window
            self.group.sent.extend(online[:1])      # Hermes restarts straight away
            await vt.run(self.group, 600)
        stamps: List[float] = []
        real = self.group.sender_for("Codex")

        async def stamped(payload: str, reply_to: Any = None) -> str:
            stamps.append(vt.now)
            return await real(payload, reply_to)
        codex._sender = stamped
        self.run_async(go())
        texts = [m["text"] for m in codex_seen()]
        self.assertEqual(len(texts), 2, texts)
        self.assertIn("ref=Hermes,Antigravity", texts[0])
        self.assertIn("ref=Hermes\n", texts[1])
        self.assertGreaterEqual(stamps[1] - stamps[0], REPLY_GAP_SECONDS)

    def test_a_peer_queued_while_waiting_for_the_slot_is_in_the_same_answer(self):
        codex = self.channel("Codex", ["Hermes", "Antigravity"])
        release = asyncio.Event()

        async def busy_slot(*_: object) -> None:
            await release.wait()                    # another message holds the pacing slot
        codex._wait_for_slot = busy_slot             # type: ignore[assignment]

        def codex_seen() -> List[str]:
            return [m["text"] for m in self.group.log if "intent=SEEN" in m["text"]]

        async def go() -> None:
            codex.selftest._queue_seen("Hermes")
            for _ in range(20):
                await asyncio.sleep(0)
            codex.selftest._queue_seen("Antigravity")   # arrives during the wait
            release.set()
            for _ in range(50):
                await asyncio.sleep(0)
        self.run_async(go())
        self.assertEqual(len(codex_seen()), 1, codex_seen())
        self.assertIn("ref=Hermes,Antigravity", codex_seen()[0])
        self.assertEqual(codex.selftest.pending, [])

    def test_a_failed_answer_is_retried_once(self):
        codex = self.channel("Codex", ["Hermes"])
        calls: List[int] = []
        real_send = codex.send

        async def flaky(text: Any, **kw: Any) -> Any:
            calls.append(1)
            if len(calls) <= 2:
                text()                               # composed, then the send fails
                raise RuntimeError("telegram down")
            return await real_send(text, **kw)
        codex.send = flaky                           # type: ignore[assignment]
        now = [1000.0]

        async def virtual_sleep(seconds: float) -> None:
            now[0] += seconds
            await asyncio.sleep(0)
        codex.selftest.clock = lambda: now[0]
        codex._sleep = virtual_sleep

        async def go() -> None:
            codex.selftest._queue_seen("Hermes")
            for _ in range(50):
                await asyncio.sleep(0)
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(go())
        self.assertEqual(len(calls), 2)              # the first try and one retry
        self.assertNotIn("Hermes", codex.selftest.replied)
        self.assertEqual(codex.selftest.pending, [])

    def test_a_bot_restarted_within_minutes_still_passes(self):
        now = [1000.0]
        codex = self.channel("Codex", ["Hermes"])
        hermes = self.channel("Hermes", ["Codex"])
        waits: List[float] = []

        async def virtual_sleep(seconds: float) -> None:
            waits.append(seconds)
            now[0] += seconds
            await asyncio.sleep(0)
        for chan in (codex, hermes):
            chan.selftest.clock = lambda: now[0]
            chan._sleep = virtual_sleep

        async def go() -> None:
            await self.boot(codex)
            await self.boot(hermes)
            await self.settle()
            now[0] += 30                             # Hermes crashes and comes back
            restarted = self.channel("Hermes", ["Codex"])
            restarted.selftest.clock = lambda: now[0]
            restarted._sleep = virtual_sleep
            await self.boot(restarted)
            await self.settle(10)
            await restarted.selftest.report()
        self.run_async(go())
        result = read_selftest(self.group.channels["Hermes"].config)
        self.assertTrue(result["ok"], describe(result))
        self.assertTrue(any(w >= 80 for w in waits), waits)   # held to the reply gap

    def test_frozen_bot_logs_but_posts_nothing(self):
        codex = self.channel("Codex", ["Hermes"])
        codex._frozen = True

        async def go() -> None:
            await codex.selftest.report()
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(go())
        self.assertFalse(read_selftest(codex.config)["ok"])
        self.assertFalse(any("self-test" in m["text"] for m in self.group.log))

    def test_start_schedules_the_report(self):
        codex = self.channel("Codex", ["Hermes"])
        codex.config.limits.selftest_seconds = 30

        async def go() -> None:
            await self.boot(codex)
            await self.settle()
            await codex.selftest._report_task
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(go())
        self.assertFalse(read_selftest(codex.config)["ok"])
        self.assertEqual(sum("self-test" in m["text"] for m in self.group.log), 1)

    def test_check_shows_the_last_result(self):
        import contextlib
        import io
        from swarmly.telegram import cli
        codex = self.channel("Codex", ["Hermes"])
        codex.selftest.write({"at": 0, "ok": False,
                              "peers": {"Hermes": {"status": "no answer"}}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_async(cli._check(codex.config))
        self.assertIn("bot-to-bot test : PROBLEM", out.getvalue())
        self.assertIn("Hermes: no answer", out.getvalue())

    def test_selftest_can_be_turned_off(self):
        codex = self.channel("Codex", ["Hermes"])
        codex.config.limits.selftest_seconds = 0

        async def go() -> None:
            await self.boot(codex)
            await self.settle()
        self.run_async(go())
        self.assertIsNone(read_selftest(codex.config))

    # -- at the shipped pacing, on a virtual clock ------------------------------------

    def fleet(self, vt: VirtualTime, running: List[str],
              stagger: float = 15.0) -> Dict[str, AgentChannel]:
        chans = {a: self.channel(a, [p for p in ALL if p != a], vt, pacing=60,
                                 selftest_seconds=180) for a in running}

        async def boot_at(chan: AgentChannel, delay: float) -> None:
            await vt.sleep(delay)
            await self.boot(chan)
        for i, chan in enumerate(chans.values()):
            self.loop.create_task(boot_at(chan, i * stagger))
        return chans

    def warnings(self) -> List[Dict[str, Any]]:
        return [m for m in self.group.log if "self-test" in m["text"]]

    def test_four_bots_staggered_at_default_pacing_pass_with_no_warning(self):
        vt = VirtualTime(self.loop)
        chans = self.fleet(vt, ALL)
        self.run_async(vt.run(self.group, 3600))
        self.assertEqual(self.warnings(), [])
        for chan in chans.values():
            result = read_selftest(chan.config)
            self.assertTrue(result["ok"], describe(result))

    def test_a_peer_busy_with_a_long_reply_causes_no_false_warning(self):
        vt = VirtualTime(self.loop)
        chans = self.fleet(vt, ALL)
        busy = format_message(Envelope(type=MessageType.NOTE, to="*", intent="FYI"), "part")
        for _ in range(3):                           # three paced parts ahead of its answers
            self.loop.create_task(chans["Hermes"].send(busy))
        self.run_async(vt.run(self.group, 3600))
        self.assertEqual(self.warnings(), [])

    def test_a_real_outage_is_still_reported_once_per_bot(self):
        vt = VirtualTime(self.loop)
        chans = self.fleet(vt, ["Codex", "Hermes", "Antigravity"])      # Claude never starts
        started = vt.now
        stamps: Dict[str, float] = {}
        for alias, chan in chans.items():
            real = self.group.sender_for(alias)

            async def stamped(payload: str, reply_to: Any = None, real: Any = real,
                              alias: str = alias) -> str:
                if "self-test" in payload:
                    stamps[alias] = vt.now
                return await real(payload, reply_to)
            chan._sender = stamped
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(vt.run(self.group, 3600))
        self.assertEqual(sorted(m["from"] for m in self.warnings()),
                         ["Antigravity", "Codex", "Hermes"])
        self.assertTrue(all(t - started > 180 for t in stamps.values()), stamps)  # rechecked
        for chan in chans.values():
            peer = read_selftest(chan.config)["peers"]
            self.assertEqual(peer["Claude"]["kind"], "silent")
            self.assertTrue(all(peer[a]["kind"] == "ok" for a in peer if a != "Claude"))
        line = next(m["text"] for m in self.warnings() if m["from"] == "Codex")
        self.assertIn("Nothing from Claude, while other peers were heard", line)

        # A restart with the same problem does not post it again.
        before = len(self.warnings())
        again = self.channel("Codex", [p for p in ALL if p != "Codex"], vt, pacing=60,
                             selftest_seconds=180)

        async def restart() -> None:
            await self.boot(again)
        self.loop.run_until_complete(restart())
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(vt.run(self.group, 3600))
        self.assertEqual(len(self.warnings()), before)
        self.assertFalse(read_selftest(again.config)["ok"])

    def test_staggered_start_with_pending_updates_has_no_false_warning(self):
        # Telegram hands a starting bot the updates it missed, oldest first: a late bot
        # works through a backlog of earlier ONLINEs before it answers anything.
        vt = VirtualTime(self.loop)
        chans = {a: self.channel(a, [p for p in ALL if p != a], vt, pacing=60,
                                 selftest_seconds=180) for a in ALL}
        self.group.channels.clear()

        async def boot_at(alias: str, delay: float) -> None:
            await vt.sleep(delay)
            chan = chans[alias]
            backlog = list(self.group.log)          # posted before it came up
            await self.boot(chan)
            self.group.channels[alias] = chan
            for msg in backlog:
                username, user_id = BOTS[msg["from"]]
                await chan.handle_event(ChannelEvent(
                    text=msg["text"], chat_id=GROUP, author_id=user_id,
                    author_username=username, author_is_bot=True, message_id=1))
        for i, alias in enumerate(ALL):
            self.loop.create_task(boot_at(alias, 45 * i))
        self.run_async(vt.run(self.group, 3600))
        self.assertEqual(self.warnings(), [])
        for chan in chans.values():
            result = read_selftest(chan.config)
            self.assertTrue(result["ok"], describe(result))

    def test_the_start_that_sticks_after_a_crash_loop_passes(self):
        vt = VirtualTime(self.loop)
        chans = self.fleet(vt, ["Codex", "Hermes", "Antigravity"], stagger=0)
        self.run_async(vt.run(self.group, 400))
        for _ in range(6):                           # Claude crash-loops every 30 s ...
            looping = self.channel("Claude", [p for p in ALL if p != "Claude"], vt,
                                   pacing=60, selftest_seconds=180)
            self.run_async(self.boot(looping))
            self.run_async(vt.run(self.group, 30))
            looping.selftest.cancel()
            looping._frozen = True                  # a dead process sends nothing more
        self.run_async(vt.run(self.group, 30))
        last = self.channel("Claude", [p for p in ALL if p != "Claude"], vt, pacing=60,
                            selftest_seconds=180)      # ... then stays up
        self.run_async(self.boot(last))
        self.run_async(vt.run(self.group, 3600))
        result = read_selftest(last.config)
        self.assertTrue(result["ok"], describe(result))
        self.assertEqual([m for m in self.warnings() if m["from"] == "Claude"], [])
        self.assertGreater(max(chans["Codex"].selftest.streak.values()), 3)   # gap had grown

    def test_the_warning_names_who_cannot_read_whom_and_the_fix(self):
        codex = self.channel("Codex", ["Hermes"])
        hermes = self.channel("Hermes", ["Codex"])
        self.group.blind.add(("Hermes", "Codex"))       # hermes_bot cannot read codex_bot

        async def go() -> None:
            await self.boot(codex)
            hermes.selftest.can_read_all = False
            await self.boot(hermes)
            hermes.selftest.can_read_all = False
            await self.settle()
            await codex.selftest.report()
            await hermes.selftest.report()
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(go())
        by = {m["from"]: m["text"] for m in self.warnings()}
        self.assertIn("Hermes has never confirmed it can read @codex_bot", by["Codex"])
        self.assertIn("Fix on @hermes_bot: in @BotFather: /mybots, pick @hermes_bot",
                      by["Codex"])
        self.assertIn("the likely cause is Hermes itself", by["Hermes"])
        self.assertIn("can_read_all_group_messages is false", by["Hermes"])

    def test_the_owner_listed_as_a_peer_is_never_tested_or_mentioned(self):
        codex = self.channel("Codex", ["Hermes"])
        codex.config.peers["Alice"] = "1"                 # admin_user_id
        codex.config.peers["Someone"] = "@some_person"   # a human handle, not a bot
        problems = codex.config.validate()
        self.assertEqual(sum("is not a bot" in p for p in problems), 2, problems)
        self.assertEqual(codex.selftest.peers(), ["Hermes"])

        async def go() -> None:
            await codex.selftest.report()
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(go())
        self.assertEqual(list(read_selftest(codex.config)["peers"]), ["Hermes"])

    @unittest.skipUnless(importlib.util.find_spec("aiogram"), "needs aiogram")
    def test_self_test_traffic_is_sent_silently(self):
        codex = self.channel("Codex", ["Hermes"])
        calls: List[Dict[str, Any]] = []

        class Bot:
            async def send_message(self, **kw: Any) -> Any:
                calls.append(kw)
                return type("Msg", (), {"message_id": len(calls)})()
        codex._sender = None
        codex.bot = Bot()

        async def go() -> None:
            await codex.send(codex.online_notice(), silent=True)
            codex.selftest._queue_seen("Hermes")
            for _ in range(50):
                await asyncio.sleep(0)
            await codex.selftest.report()
            await codex.send("an ordinary reply")
        with self.assertLogs("swarmly.telegram.selftest", "WARNING"):
            self.run_async(go())
        self.assertEqual([c["disable_notification"] for c in calls], [True, True, True, None])

    def test_a_frozen_bot_still_answers_seen(self):
        codex = self.channel("Codex", ["Hermes"])
        hermes = self.channel("Hermes", ["Codex"])
        codex._frozen = True

        async def go() -> None:
            await self.boot(hermes)
            await self.settle()
            await hermes.selftest.report()
        self.run_async(go())
        self.assertTrue(read_selftest(hermes.config)["peers"]["Codex"]["they_see_us"])


if __name__ == "__main__":
    unittest.main()
