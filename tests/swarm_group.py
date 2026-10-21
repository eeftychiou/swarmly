"""An in-process multi-bot swarm, so the behaviours that only show up together get tested (#85).

Every other Telegram test builds a single :class:`AgentChannel` and fakes the peers with
hand-made events. That leaves the swarm-level behaviours -- a coordinator dying, a takeover, a
recovery -- specified (#81, #82, #83, #84, #86) but never exercised *together*: a bot reacting to
another bot reacting to a third.

This module is the group those tests run in:

* **one forum group**, ``GROUP``, with topics: whatever a bot posts is delivered to every other
  live bot as a :class:`ChannelEvent`, exactly as the real transport would;
* **one durable plane** (:class:`Bus`): every bot reads and appends to the same ref, and
  ``commit_depth`` is the order the writes arrived -- which is the fact the lease fold orders
  claims on. A bot cut off the plane reads ``None`` and its pushes raise, so a lost remote looks
  from inside a channel like a lost remote;
* **one clock**: the channel's injected clock, the sleeper and the presence clock are all the
  group's, so a test says ``group.advance(15 * 60)`` instead of sleeping;
* **a fake GitHub** with PR state and checks, answering the ``gh`` check a WAKE would run;
* **failures on purpose**: :meth:`Group.kill` (no BYE), :meth:`Group.restart`,
  :meth:`Group.partition` (Telegram delivery between two bots), :meth:`Group.cut_off` (the
  durable plane) and :meth:`Group.heal`.

Adapters are stubs: nothing here starts a CLI, so a scenario is deterministic and runs in
seconds. A turn that should do something (merge a PR, hand work on) is scripted, and what a bot
*says* is read back from the group's log.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    import yaml  # noqa: F401
    import cryptography  # noqa: F401
    HAVE_SWARM_DEPS = True
except ImportError:                                        # pragma: no cover
    HAVE_SWARM_DEPS = False

needs_swarm = unittest.skipUnless(HAVE_SWARM_DEPS, "needs the swarm extra")

from swarmly.telegram.adapters import EchoAdapter                 # noqa: E402
from swarmly.telegram.adapters.base import AdapterReply, IncomingMessage  # noqa: E402
from swarmly.telegram.channel import AgentChannel, ChannelEvent    # noqa: E402
from swarmly.telegram.config import AgentConfig                  # noqa: E402
from swarmly.telegram.presence import Pulse                      # noqa: E402

#: The forum group every bot in the harness posts into.
GROUP = -1_002_000_000_001
#: The human who owns the swarm.
ADMIN_ID = 909_090
ADMIN_USERNAME = "alice"

#: The swarm's bots: the aliases the documentation uses, and the usernames they answer to.
USERNAMES = {
    "Claudebot": "Claude_obot",
    "Codex": "codex_dd_bot",
    "Antigravity": "agy_dd_bot",
    "Reasonix": "reasonix_dd_bot",
    "DeepSeek": "DeepSeek_dd_bot",
}
#: Telegram user ids, distinct per bot: a bot's own message must not look like anybody else's.
USER_IDS = {alias: 900 + index for index, alias in enumerate(USERNAMES)}


def run(coro):
    """Drive one coroutine to completion, as a transport would."""
    return asyncio.run(coro)


def username(alias: str) -> str:
    return USERNAMES.get(alias, f"{alias}_bot")


class Clock:
    """The group's one clock: the channel's monotonic clock, its sleeper, and presence time.

    It starts at *wall* time on purpose. ``LeaseStore`` stamps a claim with
    ``time.time()`` whatever the channel is given, and a lease is compared against the channel's
    clock -- so a fake clock starting at zero would make every claim look like it expired in 1970.
    """

    def __init__(self, start: Optional[float] = None) -> None:
        self.t = float(time.time() if start is None else start)

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t

    async def sleep(self, seconds: float) -> None:
        """A sleeper that costs no wall time: the swarm's minutes pass instantly."""
        self.advance(seconds)


class BusView:
    """One bot's connection to the durable plane. Fails when that bot is cut off it."""

    def __init__(self, bus: "Bus", alias: str) -> None:
        self.bus = bus
        self.alias = alias
        self.root = bus.root
        self.ref = bus.ref

    # -- reads ---------------------------------------------------------------------
    def refresh(self) -> bool:
        return self.bus.reachable(self.alias)

    def list_at_ref(self, relpath: str) -> Optional[List[str]]:
        if not self.bus.reachable(self.alias):
            return None                              # a ref that cannot be read is not empty
        prefix = relpath.rstrip("/") + "/"
        # Recursive, like `git ls-tree -r`: wake claims sit one directory down, per bot.
        return sorted(path[len(prefix):] for path in self.bus.files_for(self.alias)
                      if path.startswith(prefix) and path != relpath)

    def read_at_ref(self, relpath: str) -> Optional[str]:
        if not self.bus.reachable(self.alias):
            return None
        return self.bus.files_for(self.alias).get(relpath.replace("\\", "/"))

    def commit_depth(self, relpath: str) -> Optional[int]:
        return self.bus.depth_for(self.alias).get(relpath.replace("\\", "/"))

    # -- writes --------------------------------------------------------------------
    def commit_and_push(self, message: str, paths: Optional[Sequence[str]] = None) -> bool:
        if not self.bus.reachable(self.alias):
            raise RuntimeError("the bus remote is unreachable")
        self.bus.push(self.alias, message, paths or ())
        return True

    def describe(self) -> str:
        return f"harness bus at {self.bus.root}"


class Bus:
    """The durable plane all bots share: one ref, one file per document, arrival order kept."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.ref = "origin/main"
        self.files: Dict[str, str] = {}
        #: relpath -> arrival order of the commit that added it (lower wrote it earlier).
        self.depth: Dict[str, int] = {}
        self.offline: Set[str] = set()
        #: alias -> (files, depths) as that bot last saw them: a push it has not fetched yet.
        self.stale: Dict[str, Tuple[Dict[str, str], Dict[str, int]]] = {}
        self.pushes: List[Tuple[str, str]] = []
        self._arrivals = 0

    # -- the transport contract ------------------------------------------------------
    def view(self, alias: str) -> BusView:
        return BusView(self, alias)

    def reachable(self, alias: str) -> bool:
        return alias not in self.offline

    def files_for(self, alias: str) -> Dict[str, str]:
        """The plane as *alias* sees it: the real one, or the snapshot it is stuck on."""
        snapshot = self.stale.get(alias)
        return snapshot[0] if snapshot else self.files

    def depth_for(self, alias: str) -> Dict[str, int]:
        snapshot = self.stale.get(alias)
        return snapshot[1] if snapshot else self.depth

    def snapshot(self, alias: str) -> None:
        """Hold *alias*'s view where it is: its next fetch has not happened yet.

        Two successors claiming at once is exactly this -- both read the lapsed lease, both
        write term N+1, and the fold decides. Writes still land; only the reads lag.
        """
        self.stale[alias] = (dict(self.files), dict(self.depth))

    def freshen(self, alias: str) -> None:
        self.stale.pop(alias, None)

    def children(self, relpath: str) -> List[str]:
        prefix = relpath.rstrip("/") + "/"
        names = {path[len(prefix):] for path in self.files if path.startswith(prefix)}
        return sorted(name for name in names if name and "/" not in name)

    def push(self, alias: str, message: str, paths: Sequence[str]) -> None:
        """Copy what *alias* wrote locally onto the plane, in arrival order."""
        self.pushes.append((alias, message))
        for relpath in paths:
            relpath = str(relpath).replace("\\", "/")
            path = self.root / relpath
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:                          # a document that was not written locally
                continue
            self.files[relpath] = text
            if relpath not in self.depth:            # a commit's depth never moves afterwards
                self.depth[relpath] = self._arrivals
                self._arrivals += 1

    # -- what the tests look at ------------------------------------------------------
    def documents(self, relpath: str) -> List[Dict[str, Any]]:
        out = []
        for name in self.children(relpath):
            try:
                out.append(json.loads(self.files[f"{relpath.rstrip('/')}/{name}"]))
            except (KeyError, ValueError):           # pragma: no cover - not expected
                continue
        return out


class FakeGitHub:
    """Pull requests, their checks, and the tracking issue -- enough for a drill."""

    def __init__(self) -> None:
        self.prs: Dict[int, Dict[str, Any]] = {}
        self.merges: List[Tuple[int, str]] = []
        self.issue: Dict[str, Any] = {"number": 88, "state": "open", "items": {}}

    def open_pr(self, number: int, *, head: str = "", title: str = "",
                checks: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        pr = {"number": number, "head": head or f"sha{number}", "title": title,
              "state": "open", "mergeable": True,
              "checks": dict(checks or {"lint": "pass", "unittest": "pending"})}
        self.prs[number] = pr
        return pr

    def set_checks(self, number: int, **checks: str) -> None:
        self.prs[number]["checks"].update(checks)

    def go_green(self, number: int) -> None:
        pr = self.prs[number]
        pr["checks"] = {name: "pass" for name in pr["checks"]}

    def pending(self, number: int) -> List[str]:
        return [name for name, state in self.prs[number]["checks"].items() if state != "pass"]

    def green(self, number: int) -> bool:
        return not self.pending(number)

    def merge(self, number: int, *, by: str, fence: Optional[Any] = None) -> bool:
        """Merge *number*, but only while *by* may still act: the fence is asked first.

        This is the whole point of the drill: a coordinator that has been superseded must not be
        able to merge, however long its own view of the world says otherwise.
        """
        pr = self.prs[number]
        if fence is not None and not fence():
            return False
        if pr["state"] != "open" or not self.green(number):
            return False
        pr["state"] = "merged"
        self.merges.append((number, by))
        return True

    def check_returncode(self, argv: Sequence[str]) -> Optional[int]:
        """The exit code a ``gh`` wake check would get: 0 ready, 8 still pending."""
        argv = list(argv)
        if argv[:3] == ["gh", "pr", "checks"] and len(argv) >= 4:
            try:
                number = int(argv[3])
            except ValueError:
                return 1
            pr = self.prs.get(number)
            if pr is None:
                return 1
            return 0 if self.green(number) else 8
        if argv[:2] == ["gh", "pr", "view"]:
            return 0
        return 0


class ScriptedAdapter(EchoAdapter):
    """A stub runtime: answers what the scenario scripted, and stays silent otherwise.

    Each entry is ``(needle, reply)``; the first needle found in the prompt wins and the entry is
    spent, so a script says "the first time you are asked to merge, answer this". ``turns``
    records every prompt this bot was actually given, which is how a scenario checks that a turn
    ran at all.
    """

    TYPE = "echo"

    def __init__(self, alias: str, *_args: Any, **_kwargs: Any) -> None:
        super().__init__(alias)
        self.script: List[Tuple[str, str]] = []
        self.fixed: Optional[str] = None
        self.turns: List[str] = []

    def on(self, needle: str, reply: str) -> "ScriptedAdapter":
        self.script.append((str(needle), str(reply)))
        return self

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        self.turns.append(message.text or "")
        for index, (needle, reply) in enumerate(self.script):
            if needle and needle in (message.text or ""):
                self.script.pop(index)
                return AdapterReply(text=reply)
        if self.fixed is not None:
            return AdapterReply(text=self.fixed)
        return None


class GroupSender:
    """A bot's Telegram connection: what it posts lands in the group log for the others.

    It stands in for aiogram's ``Bot`` and records what it was asked to send, so a scenario can
    read a bot's own words back without a network.
    """

    def __init__(self, group: "Group", alias: str) -> None:
        self.group = group
        self.alias = alias
        self.sent: List[Dict[str, Any]] = []

    async def __call__(self, payload: str, reply_to_message_id: Any = None) -> str:
        self.sent.append({"text": payload, "reply_to": reply_to_message_id})
        self.group.post(self.group.bots.get(self.alias), payload,
                        thread=reply_to_message_id, author=self.alias)
        return payload

    @property
    def last(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""

    def clear(self) -> None:
        self.sent.clear()


class Bot:
    """One bot in the group: its config, its channel, and what it has said."""

    def __init__(self, group: "Group", alias: str, role: str, config: AgentConfig,
                 channel: AgentChannel, adapter: ScriptedAdapter) -> None:
        self.group = group
        self.alias = alias
        self.role = role
        self.config = config
        self.channel = channel
        self.adapter = adapter
        self.alive = True

    # -- what the tests use ----------------------------------------------------------
    @property
    def sent(self) -> List[str]:
        return [row["text"] for row in self.channel._test_sender.sent]

    @property
    def last(self) -> str:
        return self.channel._test_sender.last

    def said(self, needle: str) -> List[str]:
        return [text for text in self.sent if needle in text]

    def pulse(self, *, at: Optional[float] = None, busy_since: Optional[float] = None,
              frozen: bool = False) -> str:
        """The ``STATUS intent=PULSE`` line this bot would post."""
        moment = self.group.clock.now() if at is None else at
        body = json.dumps(Pulse(alias=self.alias, at=moment, rev="harness", role=self.role,
                                term=self.group.term, busy_since=busy_since,
                                frozen=frozen).to_dict())
        return f"[A2A v1.1] STATUS to=* intent=PULSE ref={self.alias} rev=harness\n\n{body}"

    def heartbeat(self) -> None:
        """One beat: what the transport's heartbeat task posts every interval."""
        self.group.post(self, self.pulse(), thread=None)

    def lease_fence(self) -> bool:
        return bool(self.channel.lease_fence())


class Group:
    """A swarm of bots in one forum group, on one bus, on one clock."""

    def __init__(self, root: Path, *,
                 succession: Optional[Sequence[str]] = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = Clock()
        self.bus = Bus(self.root)
        self.github = FakeGitHub()
        self.bots: Dict[str, Bot] = {}
        self.log: List[Dict[str, Any]] = []      # every line posted, in order
        self.cut: Set[frozenset] = set()         # Telegram delivery stops between these pairs
        self.term = 0
        self.succession = tuple(succession or ())
        self._message_id = 1000
        self._pending: List[Dict[str, Any]] = []

    # -- the swarm -------------------------------------------------------------------
    def join(self, alias: str, role: str, *, replies: Optional[str] = None) -> Bot:
        """Add a bot to the group, as if it had started for the first time."""
        adapter = ScriptedAdapter(alias)
        if replies is not None:
            adapter.fixed = replies
        config = self.config_for(alias, role)
        channel = self.channel_for(config, adapter)
        bot = Bot(self, alias, role, config, channel, adapter)
        self.bots[alias] = bot
        return bot

    def config_for(self, alias: str, role: str) -> AgentConfig:
        peers = {other: username(other) for other in USERNAMES if other != alias}
        data: Dict[str, Any] = {
            "agent": {"name": username(alias), "alias": alias, "role": role,
                      "bot_token": f"{USER_IDS[alias]}:TEST", "home_channel_id": GROUP,
                      "admin_user_id": ADMIN_ID},
            "peers": peers,
            "adapter": {"type": "echo"},
            "bus": {"repo": str(self.root)},
            "limits": {"min_seconds_between_messages": 0, "min_seconds_between_agent_turns": 0,
                       "selftest_seconds": 0, "heartbeat_minutes": 2, "peer_down_after": 3,
                       "wake_claim_settle_seconds": 0},
        }
        if self.succession:
            data["swarm"] = {"succession": list(self.succession)}
        config = AgentConfig.from_dict(data)
        config.source_path = self.root / "config.yaml"       # so state is kept and restored
        return config

    def channel_for(self, config: AgentConfig, adapter: ScriptedAdapter) -> AgentChannel:
        alias = config.identity.alias
        channel = AgentChannel(config, adapter=adapter, sender=self.sender_for(alias),
                               clock=self.clock.now, sleeper=self.clock.sleep)
        channel.presence_clock = self.clock.now
        channel.bus_transport = lambda _root: self.bus.view(alias)   # type: ignore[assignment]
        channel._test_sender = channel._sender
        self._use_group_clock(channel)
        return channel

    def _use_group_clock(self, channel: AgentChannel) -> None:
        """Point the channel's stores at the group's clock.

        A channel is given a clock, but its lease, wake and round stores are built with the real
        ``time.time``. A drill advances the group clock by ten minutes to lapse a lease, so a
        claim stamped with real time would be ten minutes "old" the moment it was written and the
        swarm would look like it agreed with the test rather than with itself. Every store reads
        the same clock the channel does.
        """
        for name in ("lease_store", "wake_store", "round_store"):
            original = getattr(channel, name, None)
            if original is None:
                continue

            def clocked(original=original):
                store = original()
                try:
                    store.clock = self.clock.now
                except AttributeError:               # pragma: no cover - a store without one
                    pass
                return store

            setattr(channel, name, clocked)
        channel._wall = self.clock.now          # quota freezes and their TTLs, too

    def get(self, alias: str) -> Bot:
        return self.bots[alias]

    def live(self) -> List[Bot]:
        return [bot for bot in self.bots.values() if bot.alive]

    # -- failures on purpose ---------------------------------------------------------
    def kill(self, alias: str) -> None:
        """Drop a bot with no BYE and no flush: a process that died, not one that stopped."""
        self.bots[alias].alive = False

    def restart(self, alias: str) -> Bot:
        """Bring a bot back from its state directory: same alias, same files, new process."""
        old = self.bots[alias]
        adapter = ScriptedAdapter(alias)
        adapter.script = list(old.adapter.script)
        adapter.fixed = old.adapter.fixed
        channel = self.channel_for(old.config, adapter)
        bot = Bot(self, alias, old.role, old.config, channel, adapter)
        self.bots[alias] = bot
        return bot

    def partition(self, first: str, second: str) -> None:
        """Stop Telegram delivery between two bots, both ways."""
        self.cut.add(frozenset((first, second)))

    def cut_off(self, alias: str) -> None:
        """Cut a bot off the durable plane: its reads fail and its pushes raise."""
        self.bus.offline.add(alias)

    def delay_bus(self, alias: str) -> None:
        """Freeze a bot's view of the plane: its pushes land, but it has not fetched them."""
        self.bus.snapshot(alias)

    def heal(self, alias: str, other: Optional[str] = None) -> None:
        if other is None:
            self.bus.offline.discard(alias)
            self.bus.freshen(alias)
            self.cut = {pair for pair in self.cut if alias not in pair}
            return
        self.cut.discard(frozenset((alias, other)))

    def delivery_blocked(self, sender: str, receiver: str) -> bool:
        return frozenset((sender, receiver)) in self.cut

    # -- posting, delivering ---------------------------------------------------------
    def sender_for(self, alias: str) -> "GroupSender":
        return GroupSender(self, alias)

    def post(self, bot: Optional[Bot], text: str, *, thread: Optional[int] = None,
             author: Optional[str] = None, deliver: bool = True) -> None:
        """Record a line as *author* said it, and queue it for the other bots."""
        who = author or (bot.alias if bot is not None else "admin")
        self._message_id += 1
        row = {"author": who, "text": text, "thread": thread, "id": self._message_id,
               "at": self.clock.now()}
        self.log.append(row)
        if deliver:
            self._pending.append(row)
        self._seen_by(who, text)

    def say(self, alias: str, text: str, *, thread: Optional[int] = None) -> None:
        """An admin-style message from *alias*, as if it had posted it itself."""
        self.post(self.bots.get(alias), text, thread=thread, author=alias)

    def admin(self, text: str, *, thread: Optional[int] = None) -> None:
        self.post(None, text, thread=thread, author=ADMIN_USERNAME)

    def _seen_by(self, author: str, text: str) -> None:
        """Any message from a peer is liveness (#82): presence notes it without a turn."""
        if author == ADMIN_USERNAME:
            return
        for bot in self.live():
            if bot.alias == author:
                continue
            if self.delivery_blocked(author, bot.alias):
                continue
            try:
                bot.channel.presence().seen(author, self.clock.now())
            except Exception:                        # noqa: BLE001 - liveness is never fatal
                pass

    def _event(self, row: Dict[str, Any], receiver: str) -> ChannelEvent:
        author = row["author"]
        is_admin = author == ADMIN_USERNAME
        return ChannelEvent(text=row["text"], chat_id=GROUP,
                            author_id=ADMIN_ID if is_admin else USER_IDS.get(author, 1),
                            author_username=author if is_admin else username(author),
                            author_is_bot=not is_admin, replies_to_bot=False,
                            message_id=row["id"], thread_id=row["thread"],
                            attachments=[], raw=None)

    def pump(self, rounds: int = 40) -> int:
        """Deliver every queued line, and whatever it provokes, until the group is quiet."""
        delivered = 0
        for _ in range(rounds):
            if not self._pending:
                break
            batch, self._pending = self._pending, []
            for row in batch:
                for bot in self.live():
                    if bot.alias == row["author"]:
                        continue
                    if self.delivery_blocked(row["author"], bot.alias):
                        continue
                    delivered += 1
                    try:
                        run(bot.channel.handle_event(self._event(row, bot.alias)))
                    except Exception as exc:         # noqa: BLE001 - a failed turn is not a stop
                        row.setdefault("errors", []).append(f"{bot.alias}: {exc!r}")
            self.drain()
        return delivered

    def drain(self) -> None:
        """Let the channels' own background work (mirrors, receipts) finish."""
        for bot in self.live():
            tasks = [task for task in getattr(bot.channel, "_mirror_tasks", set())
                     if not task.done()]
            if tasks:
                run(asyncio.gather(*tasks, return_exceptions=True))

    # -- time ------------------------------------------------------------------------
    def advance(self, seconds: float) -> float:
        return self.clock.advance(seconds)

    def tick(self, seconds: float = 0.0, *, lease: bool = True, presence: bool = True,
             wakes: bool = False) -> Dict[str, Any]:
        """Advance the clock, then one maintenance pass per live bot."""
        if seconds:
            self.advance(seconds)
        out: Dict[str, Any] = {"lease": {}, "presence": {}, "wake": {}}
        for bot in self.live():
            channel = bot.channel
            if lease:
                try:
                    claim = run(channel.check_lease())
                    out["lease"][bot.alias] = claim
                    self.term = max(self.term, int(getattr(claim, "term", 0) or 0))
                except Exception as exc:             # noqa: BLE001 - a failed tick is data
                    out["lease"][bot.alias] = exc
            if presence:
                try:
                    out["presence"][bot.alias] = run(channel.check_peer_liveness())
                except Exception as exc:             # noqa: BLE001
                    out["presence"][bot.alias] = exc
            if wakes:
                try:
                    out["wake"][bot.alias] = run(channel._wake_tick(now=time.time()))
                except Exception as exc:             # noqa: BLE001
                    out["wake"][bot.alias] = exc
        self.pump()
        return out

    def beats(self, every: float = 120.0, times: int = 1) -> None:
        """Every live bot pulses, *times* times, one *every* apart -- the heartbeat task."""
        for _ in range(times):
            for bot in self.live():
                bot.heartbeat()
            self.pump()
            self.advance(every)

    # -- reading the group back ------------------------------------------------------
    def lines(self, needle: str = "", *, author: Optional[str] = None) -> List[str]:
        return [row["text"] for row in self.log
                if (author is None or row["author"] == author) and needle in row["text"]]

    def said_by(self, alias: str, needle: str = "") -> List[str]:
        """What one bot posted, filtered by a needle -- the group's view, not the bot's."""
        return self.lines(needle, author=alias)
