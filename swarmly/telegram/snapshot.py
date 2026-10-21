"""The per-bot status snapshot a heartbeat carries (round 5 item 8, issue #124).

#82 already answers "is this peer alive?". A command centre (#117) has to answer a different
question -- *what is this bot doing, and is it stuck?* -- so every heartbeat carries one
structured record of it, taken in code, never by a model turn.

Three rules decide the shape.

**One record, two readers.** The snapshot rides the pulse (#82): it is written into the pulse
payload on the durable plane, so a bot that missed a beat still has a fresh-enough record to
render, and :meth:`AgentChannel.status_snapshot` gives the same record in-process.

**No identifiers.** A snapshot is a *status*, not a leak: the topic is named by its discussion
token (``tg-307``, or ``general``), never a chat or thread id, and nothing here carries a token,
a key or a path (#22). The record is small and public-shaped on purpose, because it ends up on
the bus where every peer reads it.

**Stale is a first-class answer.** A bot that died does not publish "I am down": its last
snapshot simply stops being renewed. :meth:`Snapshot.is_stale` says so from the snapshot's age
against the interval it was taken on, and :meth:`Snapshot.line` says which, so a dashboard can
show "no news" instead of pretending the last picture is current.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Running a turn, or waiting for one.
IDLE = "idle"
BUSY = "busy"
#: Paused by the admin, or frozen on a quota.
PAUSED = "paused"
FROZEN = "frozen"
#: A turn that has run past its own timeout: working, but not visibly.
STUCK = "stuck"
#: Reported by a *reader* that knows the bot has gone quiet (#82). A bot never says this of
#: itself: a dead bot publishes nothing.
DOWN = "down"

STATES = (IDLE, BUSY, PAUSED, FROZEN, STUCK, DOWN)

#: How many beats may pass before a snapshot is called stale.
MISSED_BEATS = 2


@dataclass(frozen=True)
class Snapshot:
    """One bot's state, as of the moment a heartbeat took it."""

    alias: str
    state: str
    #: When this state began, and when the snapshot was taken (wall clock, peer-comparable).
    since: float = 0.0
    at: float = 0.0
    #: The heartbeat interval it was taken on: what "stale" is measured against.
    every: float = 0.0
    #: The discussion, as its token (``general`` when none): never a chat or thread id.
    topic: str = "general"
    #: The turn in flight: how long it has been running, and when it is considered stuck.
    turn_elapsed: float = 0.0
    turn_timeout: float = 0.0
    #: Messages waiting for the turn lock.
    queue: int = 0
    #: Armed WAKE timers: ``(id, next fire at)``.
    wakes: Tuple[Tuple[str, float], ...] = ()
    model: str = ""
    effort: str = ""
    #: The revision this bot is running, for /diag's "which build is that?".
    rev: str = ""
    uptime: float = 0.0
    restarts: int = 0
    #: The coordinator lease, when this bot holds it (#83).
    term: int = 0
    lease_ends: float = 0.0

    # -- serialisation ---------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "alias": self.alias, "state": self.state, "since": self.since, "at": self.at,
            "every": self.every, "topic": self.topic,
            "turn_elapsed": self.turn_elapsed, "turn_timeout": self.turn_timeout,
            "queue": int(self.queue), "wakes": [[str(i), float(at)] for i, at in self.wakes],
            "model": self.model, "effort": self.effort, "rev": self.rev,
            "uptime": self.uptime, "restarts": int(self.restarts),
            "term": int(self.term), "lease_ends": self.lease_ends,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Snapshot":
        """A snapshot from its record. A malformed one is refused, never half-read."""
        if not isinstance(data, dict):
            raise ValueError(f"a snapshot must be an object, got {type(data).__name__}")
        alias = data.get("alias")
        state = data.get("state")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("a snapshot needs an alias")
        if state not in STATES:
            raise ValueError(f"a snapshot needs a known state (got {state!r})")
        wakes: List[Tuple[str, float]] = []
        for row in data.get("wakes") or []:
            if isinstance(row, (list, tuple)) and len(row) == 2:
                try:
                    wakes.append((str(row[0]), float(row[1])))
                except (TypeError, ValueError):
                    continue
        return cls(
            alias=alias.strip(), state=str(state),
            since=_num(data.get("since")), at=_num(data.get("at")),
            every=_num(data.get("every")), topic=str(data.get("topic") or "general"),
            turn_elapsed=_num(data.get("turn_elapsed")),
            turn_timeout=_num(data.get("turn_timeout")),
            queue=int(_num(data.get("queue"))), wakes=tuple(wakes),
            model=str(data.get("model") or ""), effort=str(data.get("effort") or ""),
            rev=str(data.get("rev") or ""), uptime=_num(data.get("uptime")),
            restarts=int(_num(data.get("restarts"))), term=int(_num(data.get("term"))),
            lease_ends=_num(data.get("lease_ends")),
        )

    # -- reading it ------------------------------------------------------------------
    def age(self, now: float) -> float:
        """Seconds since this snapshot was taken (0 for a clock that went backwards)."""
        return max(0.0, float(now) - float(self.at or 0.0))

    def is_stale(self, now: float, *, missed: int = MISSED_BEATS) -> bool:
        """Whether this snapshot has missed its renewals: what a reader renders as "no news"."""
        if self.every <= 0:
            return False                     # no interval configured: nothing to be late for
        return self.age(now) > self.every * max(1, int(missed))

    def line(self, now: Optional[float] = None) -> str:
        """One line for a dashboard or /status: state, how long, and what is waiting."""
        moment = time.time() if now is None else float(now)
        parts = [f"{self.alias}:{self.state}"]
        if self.since and moment >= self.since:
            parts.append(f"for {_duration(moment - self.since)}")
        if self.state == BUSY and self.turn_elapsed:
            parts.append(f"turn {_duration(self.turn_elapsed)}"
                         + (f"/{_duration(self.turn_timeout)}" if self.turn_timeout else ""))
        if self.topic and self.topic != "general":
            parts.append(self.topic)
        if self.queue:
            parts.append(f"queue={self.queue}")
        if self.wakes:
            parts.append(f"wakes={len(self.wakes)}")
        if self.term:
            parts.append(f"term={self.term}")
        age = self.age(moment)
        parts.append(f"{'stale ' if self.is_stale(moment) else ''}age={_duration(age)}")
        return " ".join(parts)


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def freshest(snapshots: Sequence[Snapshot], alias: str) -> Optional[Snapshot]:
    """The latest snapshot for *alias*: peers' records arrive out of order."""
    mine = [row for row in snapshots if row.alias.strip().lower() == alias.strip().lower()]
    return max(mine, key=lambda row: row.at) if mine else None
