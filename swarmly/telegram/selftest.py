"""Startup self-test: can this bot and its peers actually see each other in the group?

Telegram only delivers one bot's messages to another when Bot-to-Bot Communication Mode is
on for the reader (in @BotFather) and the reader may read the group (admin, or Group
Privacy off). Nothing fails loudly when that is wrong: messages just never arrive. So each
transport checks it at startup, in both directions, with no model turn:

1. On start every transport already posts ``HELLO intent=ONLINE``.
2. A transport that receives a peer's ONLINE answers, batched, with
   ``HELLO intent=SEEN ref=<Alias>,<Alias>`` and a mention of each, so even a bot in
   privacy mode receives the answer. That tells the starter "they see me". A SEEN that
   names us is answered with a SEEN in turn if we had not answered that peer yet, so a peer
   that started before us learns it as well. The names go into the answer when the pacing
   slot frees, not before, so a peer that comes online while we wait is in the same answer.
   Answers to one peer are at least ``REPLY_GAP_SECONDS`` apart, more for a peer that keeps
   restarting; a sooner ONLINE is answered late, not dropped. A frozen bot still answers:
   the answer is silent and shows peers it is alive.
3. Any message from a peer bot tells this transport "I see them".

After ``limits.selftest_seconds`` the transport looks at what it has. If a peer is missing
it waits once more (long enough for every peer to get a paced answer out) before it
decides. Then it writes ``.agents/state/selftest-<alias>.json`` and the log, and posts one
silent line per missing peer, naming which bot cannot read which and what to change. The
same warning is not posted again until it changes or a day has passed. Later answers (a
peer that started after us) update the file. HELLO is machine traffic, so none of this
starts a model turn anywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from swarmly.telegram.wire import Envelope, MessageType, format_message

if TYPE_CHECKING:  # pragma: no cover
    from swarmly.telegram.channel import AgentChannel, ChannelEvent

logger = logging.getLogger("swarmly.telegram.selftest")

#: Wait this long after the first ONLINE before answering, to answer a burst at once.
SEEN_BATCH_SECONDS = 10.0
#: Answer a given peer at most this often, so a crash-looping bot cannot use up every
#: peer's send budget. An ONLINE that arrives sooner is answered when the gap is up,
#: never dropped: a restarted bot must still hear that its peers see it.
REPLY_GAP_SECONDS = 120.0
#: A peer that restarts again within this long of our last answer doubles its gap ...
RESTART_STREAK_SECONDS = 600.0
#: ... up to this.
MAX_REPLY_GAP_SECONDS = 1800.0
#: The same warning is posted again after this long, even if nothing changed.
REWARN_SECONDS = 24 * 3600.0
#: A ref can only carry aliases of this shape (header fields cannot hold spaces).
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]+$")

BOTFATHER = "in @BotFather: /mybots, pick {bot}, Bot Settings, Bot-to-Bot Communication, Enable"


def selftest_path(cfg: Any) -> Path:
    from swarmly.telegram.poll import state_dir
    safe = re.sub(r"[^a-z0-9_-]", "", (cfg.identity.alias or "agent").strip().lower()) \
        or "agent"
    return state_dir(cfg) / f"selftest-{safe}.json"


def read_selftest(cfg: Any) -> Optional[Dict[str, Any]]:
    """The last result written by this agent's transport, or None."""
    try:
        data = json.loads(selftest_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def describe(result: Dict[str, Any]) -> str:
    """One line per peer, for `swarmly-telegram check` and the log."""
    lines = []
    for alias, peer in sorted((result.get("peers") or {}).items()):
        lines.append(f"{alias}: {peer.get('status')}")
    return "; ".join(lines) or "no peers configured"


def _day(when: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(when or 0)))


class PeerSelfTest:
    def __init__(self, channel: "AgentChannel", *, clock: Any = time.time) -> None:
        self.channel = channel
        self.clock = clock
        self.heard: Dict[str, float] = {}     # peer -> last message we received from it
        self.acked: Dict[str, float] = {}     # peer -> when it said it received our ONLINE
        self.replied: Dict[str, float] = {}   # peer -> when we last answered its ONLINE
        self.streak: Dict[str, int] = {}      # peer -> restarts in a row we answered quickly
        self.online_at: Dict[str, float] = {} # peer -> its latest ONLINE
        self.retried: set = set()             # peers whose failed answer was already re-queued
        self.pending: List[str] = []
        self.started_at: Optional[float] = None
        self.can_read_all: Optional[bool] = None
        self.reported = False
        self.previous: Dict[str, Any] = {}    # the file from the last run
        self.warned: Optional[Dict[str, Any]] = None
        self._seen_task: Optional[asyncio.Task] = None
        self._report_task: Optional[asyncio.Task] = None

    @property
    def cfg(self) -> Any:
        return self.channel.config

    def peers(self) -> List[str]:
        """The peers to test: every configured bot, never the human owner."""
        owner = set(self.cfg.owner_peers())
        return [alias for alias in self.cfg.peers if alias not in owner]

    # -- inbound ----------------------------------------------------------------------

    def observe(self, envelope: Envelope, event: "ChannelEvent") -> None:
        """Record what a peer bot's message says about visibility. Never raises."""
        try:
            self._observe(envelope, event)
        except Exception:                              # noqa: BLE001 - diagnostics only
            logger.warning("self-test: could not record a peer message", exc_info=True)

    def _observe(self, envelope: Envelope, event: "ChannelEvent") -> None:
        if not event.author_is_bot:
            return
        alias = self.cfg.peer_alias_for(event.author_username, event.author_id)
        if alias is None or alias not in self.peers():
            return
        now = self.clock()
        before = (alias in self.heard, alias in self.acked)
        self.heard[alias] = now
        if envelope.type == MessageType.HELLO:
            intent = (envelope.intent or "").upper()
            if intent == "ONLINE":
                last = self.replied.get(alias)
                quick = last is not None and now - last < RESTART_STREAK_SECONDS
                self.streak[alias] = self.streak.get(alias, 0) + 1 if quick else 0
                self.online_at[alias] = now
                self._queue_seen(alias)        # every new ONLINE gets an answer
            elif intent == "SEEN":
                me = self.cfg.identity.alias.strip().lower()
                named = {part.strip().lower() for part in (envelope.ref or "").split(",")}
                if me in named:
                    self.acked[alias] = now
                    if alias not in self.replied:
                        # They started first and have not heard from us: answer in kind.
                        self._queue_seen(alias)
        if self.reported and before != (alias in self.heard, alias in self.acked):
            result = self.result()             # a late peer: keep the file current
            self.write(result)
            logger.info("self-test: %s is now: %s", alias, result["peers"][alias]["status"])

    def _gap(self, alias: str) -> float:
        # The first quick restart keeps the normal gap; each one after that doubles it.
        return min(REPLY_GAP_SECONDS * 2 ** max(self.streak.get(alias, 0) - 1, 0),
                   MAX_REPLY_GAP_SECONDS)

    def _due(self, alias: str) -> float:
        due = self.replied.get(alias, float("-inf")) + self._gap(alias)
        online = self.online_at.get(alias)
        if online is not None:
            # However long the gap has grown, a start is answered inside its own self-test
            # window, or the bot that finally stays up would warn about healthy peers.
            window = float(getattr(self.cfg.limits, "selftest_seconds", 0) or 0) \
                or REPLY_GAP_SECONDS
            due = min(due, online + window)
        return due

    def _queue_seen(self, alias: str) -> None:
        if not ALIAS_RE.match(alias):
            logger.warning("self-test: alias %r cannot go in a SEEN ref (letters, digits, "
                           "_ and - only); %s will not learn that this bot sees it",
                           alias, alias)
            return
        if alias not in self.pending:
            self.pending.append(alias)
        if self._seen_task is not None and not self._seen_task.done():
            return                         # the running task loops until pending is empty
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._seen_task = loop.create_task(self._send_seen())

    async def _send_seen(self) -> None:
        try:
            while self.pending:
                now = self.clock()
                # Answer whoever is due first; a peer still inside its gap waits its turn.
                wait = min(self._due(a) - now for a in self.pending)
                await self.channel._sleep(max(SEEN_BATCH_SECONDS, wait))
                taken: Dict[str, Optional[float]] = {}

                def compose() -> Optional[str]:
                    # Called once the pacing slot is free: whoever queued meanwhile is in.
                    at = self.clock()
                    names = [a for a in self.pending if self._due(a) <= at]
                    if not names:
                        return None
                    self.pending = [a for a in self.pending if a not in names]
                    for alias in names:
                        taken[alias] = self.replied.get(alias)
                        self.replied[alias] = at
                    return format_message(
                        Envelope(type=MessageType.HELLO, to="*", intent="SEEN",
                                 ref=",".join(names)),
                        f"\U0001F440 {self.cfg.identity.alias} sees {', '.join(names)}")
                try:
                    await self.channel.send(compose, allow_when_frozen=True, silent=True)
                except Exception:                      # noqa: BLE001 - diagnostics only
                    logger.warning("self-test: could not answer %s", ", ".join(taken),
                                   exc_info=True)
                    if not taken:
                        # Failed before composing: the next message from a peer retries.
                        logger.warning("self-test: %s stay queued", ", ".join(self.pending))
                        return
                    for alias, before in taken.items():
                        # Not answered after all: undo, and try once more.
                        if before is None:
                            self.replied.pop(alias, None)
                        else:
                            self.replied[alias] = before
                        if alias not in self.retried:
                            self.retried.add(alias)
                            if alias not in self.pending:
                                self.pending.append(alias)
        except asyncio.CancelledError:
            raise
        except Exception:                              # noqa: BLE001 - diagnostics only
            logger.warning("self-test: the SEEN sender failed", exc_info=True)

    # -- the report -------------------------------------------------------------------

    def start(self, can_read_all: Optional[bool]) -> None:
        """Called once the ONLINE notice is out. Schedules the report."""
        self.started_at = self.clock()
        self.can_read_all = can_read_all
        self.previous = read_selftest(self.cfg) or {}
        warned = self.previous.get("warned")
        self.warned = warned if isinstance(warned, dict) else None
        if can_read_all is False:
            logger.info("self-test: Group Privacy is on for this bot, which is normal for a "
                        "group admin; the report below shows whether it reads its peers")
        seconds = float(getattr(self.cfg.limits, "selftest_seconds", 0) or 0)
        if seconds <= 0:
            logger.info("self-test is off (limits.selftest_seconds is 0)")
            return
        if not self.peers():
            logger.info("self-test skipped: no peers configured")
            return
        if self._report_task is not None and not self._report_task.done():
            self._report_task.cancel()
        self._report_task = asyncio.get_running_loop().create_task(self._report_after(seconds))

    def recheck_seconds(self, seconds: float) -> float:
        """How long to wait again before calling a peer missing.

        Every answer is a paced send, and a peer may be busy with its own queue (a long
        reply in several parts), so give each peer a few pacing slots.
        """
        pacing = float(getattr(self.cfg.limits, "min_seconds_between_messages", 0) or 0)
        return max(seconds, pacing * (len(self.peers()) + 3))

    async def _report_after(self, seconds: float) -> None:
        try:
            await self.channel._sleep(seconds)
            first = self.result()
            if not first["ok"]:
                delay = self.recheck_seconds(seconds)
                logger.info("self-test: %s; checking again in %.0fs before warning",
                            describe(first), delay)
                await self.channel._sleep(delay)
            await self.report()
        except asyncio.CancelledError:
            raise
        except Exception:                              # noqa: BLE001 - diagnostics only
            logger.warning("self-test: the report failed", exc_info=True)

    def _confirmed_before(self, alias: str) -> Optional[float]:
        peer = (self.previous.get("peers") or {}).get(alias) or {}
        when = peer.get("confirmed_at")
        return float(when) if isinstance(when, (int, float)) else None

    def result(self) -> Dict[str, Any]:
        peers: Dict[str, Dict[str, Any]] = {}
        now = self.clock()
        for alias in self.peers():
            we_see = alias in self.heard
            they_see = alias in self.acked
            before = self._confirmed_before(alias)
            if we_see and they_see:
                kind, status, confirmed = "ok", "ok", now
            elif we_see:
                kind, confirmed = "unconfirmed", before
                status = f"{alias} did not confirm it sees this bot" + (
                    f" (it did on {_day(before)})" if before else
                    " (never has: a setup problem, or it runs a version without the self-test)")
            else:
                # Includes "they see us but we do not see them": their answer cannot reach us.
                kind, confirmed = "silent", before
                status = "no answer (offline, or the bots cannot see each other)" + (
                    f"; it last confirmed on {_day(before)}" if before else "")
            peers[alias] = {"status": status, "kind": kind, "we_see_them": we_see,
                            "they_see_us": they_see, "last_heard": self.heard.get(alias),
                            "acked": self.acked.get(alias), "confirmed_at": confirmed}
        return {"alias": self.cfg.identity.alias, "at": now,
                "started_at": self.started_at, "can_read_all_group_messages": self.can_read_all,
                "ok": all(p["kind"] == "ok" for p in peers.values()), "peers": peers,
                "warned": self.warned}

    def write(self, result: Dict[str, Any]) -> None:
        path = selftest_path(self.cfg)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("self-test: could not write %s", path, exc_info=True)

    def _handle(self, alias: Optional[str] = None) -> str:
        if alias is None:
            name = (self.cfg.identity.name or self.cfg.identity.alias).lstrip("@")
            return f"@{name}"
        target = str(self.cfg.peers.get(alias) or "").strip().lstrip("@")
        return f"@{target}" if target and not target.isdigit() else alias

    def advice(self, result: Dict[str, Any]) -> List[str]:
        """One line per missing peer: which bot cannot read which, and what to change."""
        me, mine = self.cfg.identity.alias, self._handle()
        peers = result["peers"]
        missing = [a for a, p in peers.items() if p["kind"] != "ok"]
        heard_any = any(p["we_see_them"] for p in peers.values())
        lines: List[str] = []
        if missing and not heard_any:
            fix = BOTFATHER.format(bot=mine)
            if result.get("can_read_all_group_messages") is False:
                fix += (f"; Group Privacy is on for {mine} "
                        "(can_read_all_group_messages is false), so also make it a group admin")
            else:
                fix += f"; and check {mine} is a group admin"
            names = ", ".join(sorted(peers))
            lines.append(f"No peer was heard ({names}). Most often they are simply not running "
                         "yet: start them and this check passes on its own. If they are running "
                         f"and still unheard, the likely cause is {me} itself: {fix}.")
        for alias in missing:
            peer, theirs = peers[alias], self._handle(alias)
            before = peer.get("confirmed_at")
            if peer["kind"] == "unconfirmed":
                head = (f"{alias} confirmed it could read {mine} on {_day(before)} but has not "
                        "now" if before else f"{alias} has never confirmed it can read {mine} "
                        "(or it runs a version without the self-test)")
                lines.append(f"{head}. Fix on {theirs}: {BOTFATHER.format(bot=theirs)}; "
                             f"and make {theirs} a group admin (or turn its Group Privacy off).")
            elif heard_any:
                when = f" (it last confirmed on {_day(before)})" if before else ""
                lines.append(f"Nothing from {alias}{when}, while other peers were heard: its "
                             f"transport is probably not running, or {theirs} is not in the "
                             "group.")
            elif before:
                lines.append(f"Nothing from {alias}, which last confirmed on {_day(before)}.")
        return lines

    async def report(self) -> Dict[str, Any]:
        result = self.result()
        self.reported = True
        if result["ok"]:
            self.warned = None
            result["warned"] = None
            self.write(result)
            logger.info("self-test passed: %s", describe(result))
            return result
        lines = self.advice(result)
        logger.warning("self-test: %s. %s", describe(result), " ".join(lines))
        signature = json.dumps(sorted([a, p["kind"], bool(p.get("confirmed_at"))]
                                      for a, p in result["peers"].items()))
        now = self.clock()
        repeat = self.warned is not None and self.warned.get("signature") == signature \
            and now - float(self.warned.get("at") or 0) < REWARN_SECONDS
        if repeat or getattr(self.channel, "_frozen", False):
            # Already posted (or frozen, which means quiet): the log and the file have it.
            self.write(result)
            return result
        line = format_message(
            Envelope(type=MessageType.NOTE, to="*", intent="FYI", ref="selftest"),
            "⚠️ self-test: " + "\n".join(lines))
        try:
            await self.channel.send(line, allow_when_frozen=True, silent=True)
            self.warned = {"signature": signature, "at": now}
            result["warned"] = self.warned
        except Exception:                              # noqa: BLE001 - the log has it
            logger.warning("self-test: could not post the result", exc_info=True)
        self.write(result)
        return result

    def cancel(self) -> None:
        for task in (self._seen_task, self._report_task):
            if task is not None and not task.done():
                task.cancel()
