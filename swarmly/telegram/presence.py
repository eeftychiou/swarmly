"""Peer liveness: a heartbeat, and a peer that goes quiet is marked DOWN (issue #82).

The only thing that noticed a dead bot in round 3 was a hand-off timing out. `HELLO`/`BYE` are
posted on a clean start or stop, and a die-off posts neither, so a dead coordinator went unnoticed
until somebody happened to wait on it.

Three rules shape this module, all from the design review on #82.

**A pulse is cheap, so nobody is tempted to skip it.** Liveness rides the ephemeral Telegram line
(`STATUS intent=PULSE`), not a git-bus commit: six bots at one pulse every two minutes is ~180
commits an hour on the bus, forever, every one a push race with the real bus traffic. The bus
carries only a slow pulse plus ONLINE/BYE. This module is the *decision*, not the transport: it
takes liveness facts stamped with a clock the caller injects.

**DOWN is silence on both planes.** A bot whose git remote died is silent on the bus while alive
on Telegram, and a partitioned bot cannot write the pulse that would keep it UP. So a pulse, a
HELLO, or any message from a peer counts as liveness, and whatever plane is missing does not by
itself make a peer DOWN.

**DOWN is advisory.** It says "this peer has not been heard from", never "somebody else should
lead". The lease decides who coordinates (#83); using DOWN to start a takeover would let a
partition elect a second coordinator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Pulse:
    """One heartbeat, as a peer publishes it."""

    alias: str
    at: float
    rev: str = ""
    role: str = ""
    term: int = 0
    #: When this bot's currently running turn started, if one is running: a busy peer must not
    #: look dead however long its turn runs.
    busy_since: Optional[float] = None
    frozen: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"alias": self.alias, "at": self.at, "rev": self.rev, "role": self.role,
                "term": self.term, "busy_since": self.busy_since, "frozen": self.frozen}

    @classmethod
    def from_dict(cls, data: Any) -> "Pulse":
        if not isinstance(data, dict):
            raise ValueError(f"a pulse must be an object, got {type(data).__name__}")
        alias = data.get("alias")
        at = data.get("at")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("a pulse needs an alias")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            raise ValueError("a pulse needs 'at' (epoch seconds)")
        busy = data.get("busy_since")
        if busy is not None and (not isinstance(busy, (int, float))
                                 or isinstance(busy, bool)):
            raise ValueError("'busy_since' must be a number or null")
        term = data.get("term", 0)
        if not isinstance(term, int) or isinstance(term, bool) or term < 0:
            raise ValueError("'term' must be a non-negative integer")
        return cls(alias=alias.strip(), at=float(at), rev=str(data.get("rev") or ""),
                   role=str(data.get("role") or ""), term=term,
                   busy_since=None if busy is None else float(busy),
                   frozen=bool(data.get("frozen")))


@dataclass
class Peer:
    """What this bot knows about one peer."""

    alias: str
    last_seen: float
    busy_since: Optional[float] = None
    frozen: bool = False
    term: int = 0
    rev: str = ""
    role: str = ""
    #: Set once a DOWN episode has been reported, so a restart cannot repeat the notice.
    down_notified_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"alias": self.alias, "last_seen": self.last_seen,
                "busy_since": self.busy_since, "frozen": self.frozen, "term": self.term,
                "rev": self.rev, "role": self.role,
                "down_notified_at": self.down_notified_at}

    @classmethod
    def from_dict(cls, data: Any) -> "Peer":
        if not isinstance(data, dict):
            raise ValueError("a peer record must be an object")
        alias = data.get("alias")
        last_seen = data.get("last_seen")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("a peer record needs an alias")
        if not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool):
            raise ValueError("a peer record needs 'last_seen'")
        busy = data.get("busy_since")
        notified = data.get("down_notified_at")
        return cls(alias=alias.strip(), last_seen=float(last_seen),
                   busy_since=None if busy is None else float(busy),
                   frozen=bool(data.get("frozen")), term=int(data.get("term") or 0),
                   rev=str(data.get("rev") or ""), role=str(data.get("role") or ""),
                   down_notified_at=None if notified is None else float(notified))


class Presence:
    """The peers this bot can hear, and how long they have been quiet.

    Every fact arrives with the moment it was observed; nothing here reads the clock except
    :meth:`now`, which a test replaces. A restart starts a **grace period** of one interval
    before anyone can be judged DOWN: a bot that has just come up has not heard from anybody yet,
    and turning that into "everyone is down" would be a lie with consequences.
    """

    def __init__(self, base: float, *, down_after: float, grace: Optional[float] = None):
        self.started_at = base
        self.down_after = float(down_after)
        self.grace_until = base + float(self.down_after if grace is None else grace)
        self.peers: Dict[str, Peer] = {}

    # -- facts -----------------------------------------------------------------------
    def note(self, pulse: Pulse) -> Peer:
        """Record a heartbeat (or a HELLO, or any message) from *pulse.alias*."""
        peer = self.peers.get(pulse.alias) or Peer(alias=pulse.alias, last_seen=pulse.at)
        peer.last_seen = max(peer.last_seen, pulse.at)
        peer.busy_since = pulse.busy_since
        peer.frozen = pulse.frozen
        peer.term = max(peer.term, pulse.term)
        peer.rev = pulse.rev or peer.rev
        peer.role = pulse.role or peer.role
        if peer.down_notified_at is not None:
            peer.down_notified_at = None        # a new episode may be reported again
        self.peers[pulse.alias] = peer
        return peer

    def seen(self, alias: str, at: float) -> None:
        """Liveness from another plane: a Telegram message or a HELLO counts as a pulse."""
        self.note(Pulse(alias=alias, at=at))

    # -- questions -------------------------------------------------------------------
    def quiet_for(self, alias: str, now: float) -> Optional[float]:
        peer = self.peers.get(alias)
        return None if peer is None else max(0.0, now - peer.last_seen)

    def is_down(self, alias: str, now: float) -> bool:
        """Silent for longer than ``down_after``, and past the restart grace period.

        A peer we have never heard from is *unknown*, not DOWN: a newcomer, or a bot whose first
        pulse has not arrived yet, is not evidence of a die-off.
        """
        if now < self.grace_until:
            return False
        quiet = self.quiet_for(alias, now)
        return quiet is not None and quiet >= self.down_after

    def status(self, alias: str, now: float) -> str:
        """``up``, ``down``, ``busy`` or ``unknown`` for one peer."""
        peer = self.peers.get(alias)
        if peer is None:
            return "unknown"
        if self.is_down(alias, now):
            return "down"
        if peer.busy_since is not None:
            return "busy"
        return "up"

    def down_peers(self, now: float) -> List[str]:
        return sorted(alias for alias in self.peers if self.is_down(alias, now))

    def recover(self, alias: str, now: float) -> bool:
        """True when *alias* was DOWN and has just been heard from again."""
        peer = self.peers.get(alias)
        if peer is None:
            return False
        was_down = self.down_notified_at(alias) is not None
        return was_down and now - peer.last_seen < 1e-9

    # -- what the operator sees --------------------------------------------------------
    def line(self, now: float, *, order: Iterable[str] = ()) -> str:
        """The ``/status`` and ``/swarm`` fragment: ``peers=Claudebot:up,DeepSeek:down(12m)``."""
        names = list(order) or [alias for alias in self.peers]
        parts = []
        for alias in names:
            state = self.status(alias, now)
            if state == "down":
                quiet = int((self.quiet_for(alias, now) or 0.0) // 60)
                parts.append(f"{alias}:down({quiet}m)")
            elif state == "busy":
                peer = self.peers[alias]
                busy_for = int(max(0.0, now - (peer.busy_since or now)) // 60)
                parts.append(f"{alias}:busy({busy_for}m)")
            else:
                parts.append(f"{alias}:{state}")
        return "peers=" + (",".join(parts) if parts else "none")

    def summary(self, now: float) -> Dict[str, Any]:
        return {"peers": {alias: {"state": self.status(alias, now),
                                  "quiet_s": self.quiet_for(alias, now),
                                  "busy_since": peer.busy_since,
                                  "term": peer.term}
                          for alias, peer in self.peers.items()}}

    def down_notified_at(self, alias: str) -> Optional[float]:
        peer = self.peers.get(alias)
        return None if peer is None else peer.down_notified_at

    def mark_notified(self, alias: str, at: float) -> bool:
        """Claim the one admin notice for this DOWN episode. True only for the first caller."""
        peer = self.peers.get(alias)
        if peer is None or peer.down_notified_at is not None:
            return False
        peer.down_notified_at = at
        return True
