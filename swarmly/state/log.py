"""The fold: one ordered log of writes, read the same way by every bot.

This is the whole consistency story of the store (``docs/design/topic-registry.md`` §1b), and
it is pure: no I/O, no clock of its own, the same answer for the same log on every machine.

* **Total order.** Every write (an ``Op``) has a position: the Telegram message id of its line
  in Swarm pulse, or the sequence number of its file in a shared folder. Positions are unique
  and every reader sees the same ones. The fold applies ops in position order, whatever order
  they arrived in; an op that arrives late is slotted in and the state is folded again.
* **Compare-and-set.** An op names the version of the key it was written against (``base``).
  It applies only if that is still the key's version: of two writes against the same version,
  the lower position wins and the other is rejected (the writer re-reads and retries). An op
  whose base is *ahead* of the key means this reader missed writes -- a gap: the caller reads
  the snapshot again.
* **One writer per key** (``keys.py``), checked here on read: a line from a bot that does not
  own the key changes nothing for anybody.
* **The lease and fencing.** ``lease`` is a key like the others with its own rule: a claim for
  term ``n+1`` wins only once term ``n`` has expired (or was released, or is the claimer's own),
  and only the holder renews or releases. A write to a lease-holder key applies only if its
  writer holds the lease *at that op's time* and stamped it with the *current* term, so a
  deposed coordinator's late write is dropped by every reader. Time is the op's server time
  (Telegram's message date), never a bot's own clock, so every reader judges expiry alike.
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from swarmly.state import keys as K

SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class Op:
    pos: int                       # place in the total order
    key: str
    base: int                      # the key's version the writer saw
    term: int                      # the writer's lease term (0: it holds none)
    writer: str
    value: Optional[Dict[str, Any]]  # None deletes
    at: float                      # server time, seconds

    def to_wire(self) -> Dict[str, Any]:
        """The fields that travel in a line (position and time come from the transport)."""
        return {"k": self.key, "b": self.base, "t": self.term, "w": self.writer,
                "v": self.value}

    @classmethod
    def from_wire(cls, data: Dict[str, Any], *, pos: int, at: float,
                  writer: Optional[str] = None) -> "Op":
        """An op from a line. ``writer`` is who the transport says sent it, and wins over the
        line's own claim: a bot cannot write in another's name."""
        return cls(pos=int(pos), key=str(data["k"]), base=int(data["b"]),
                   term=int(data.get("t") or 0), writer=K.norm(writer or data.get("w") or ""),
                   value=data.get("v"), at=float(at))


@dataclass
class Entry:
    value: Optional[Dict[str, Any]]
    version: int
    term: int
    writer: str
    pos: int
    at: float

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "version": self.version, "term": self.term,
                "writer": self.writer, "pos": self.pos, "at": self.at}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Entry":
        return cls(value=data.get("value"), version=int(data["version"]),
                   term=int(data.get("term") or 0), writer=str(data.get("writer") or ""),
                   pos=int(data.get("pos") or 0), at=float(data.get("at") or 0))


@dataclass(frozen=True)
class Lease:
    holder: Optional[str]
    term: int
    expires_at: float
    since: int = 0                 # position of the claim that began this term

    def held_by(self, at: float) -> Optional[str]:
        """The holder at server time ``at``, or None once it has expired or was released."""
        return self.holder if self.holder and at < self.expires_at else None


NO_LEASE = Lease(holder=None, term=0, expires_at=0.0)


@dataclass
class StateLog:
    """The state as of a snapshot (``base``) plus the ops after it, in position order."""

    base: Dict[str, Entry] = field(default_factory=dict)
    base_pos: int = 0
    ops: List[Op] = field(default_factory=list)
    entries: Dict[str, Entry] = field(default_factory=dict)
    #: position -> why that op did not apply (lost a compare-and-set, not the owner, ...)
    rejected: Dict[int, str] = field(default_factory=dict)
    #: keys where an op arrived written against a version this reader has not seen
    gaps: Set[str] = field(default_factory=set)
    #: an op arrived at or below this log's base: something this reader compacted away may
    #: have missed it, so the caller reads the snapshot again
    late: bool = False

    # -- reading ---------------------------------------------------------------------------
    def get(self, key: str) -> Optional[Entry]:
        entry = self.entries.get(key)
        return entry if entry is not None and entry.value is not None else None

    def value(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self.get(key)
        return copy.deepcopy(entry.value) if entry is not None else None

    def version(self, key: str) -> int:
        entry = self.entries.get(key)
        return entry.version if entry is not None else 0

    def keys(self, prefix: str = "") -> List[str]:
        return sorted(k for k, e in self.entries.items()
                      if e.value is not None and k.startswith(prefix))

    def lease(self) -> Lease:
        entry = self.entries.get(K.LEASE)
        value = entry.value if entry is not None else None
        if not value:
            return NO_LEASE
        return Lease(holder=value.get("holder"), term=int(value.get("term") or 0),
                     expires_at=float(value.get("expires_at") or 0),
                     since=int(value.get("since") or 0))

    @property
    def last_pos(self) -> int:
        return self.ops[-1].pos if self.ops else self.base_pos

    def state_hash(self) -> str:
        """The same for every reader with the same state: what ``/state`` compares."""
        body = {k: [e.version, e.value] for k, e in sorted(self.entries.items())}
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    def hash_at(self, pos: int) -> Optional[str]:
        """The state hash as of position ``pos``; None when that is before this log's base."""
        if pos < self.base_pos:
            return None
        if pos >= self.last_pos:
            return self.state_hash()
        partial = StateLog(base=self.base, base_pos=self.base_pos,
                           ops=[o for o in self.ops if o.pos <= pos])
        partial._refold()
        return partial.state_hash()

    # -- writing ---------------------------------------------------------------------------
    def add(self, op: Op) -> bool:
        """Take one op from the log; True if it applies. Duplicates and ops already in the
        snapshot are ignored."""
        if op.pos <= self.base_pos:
            if op.pos not in self._base_positions():
                self.late = True
            return self._applied(op.pos)
        if any(o.pos == op.pos for o in self._near(op.pos)):
            return self._applied(op.pos)
        if self.ops and op.pos < self.ops[-1].pos:
            index = bisect.bisect_left([o.pos for o in self.ops], op.pos)
            self.ops.insert(index, op)
            self._refold()                   # a late arrival changes what came after it
        else:
            self.ops.append(op)
            self._apply(op)
        return self._applied(op.pos)

    def add_all(self, ops: Iterable[Op]) -> None:
        for op in sorted(ops, key=lambda o: o.pos):
            self.add(op)

    def _base_positions(self) -> Set[int]:
        """Positions of the ops the base's entries came from (a duplicate is not late)."""
        return {e.pos for e in self.base.values()}

    def _near(self, pos: int) -> List[Op]:
        index = bisect.bisect_left([o.pos for o in self.ops], pos)
        return self.ops[index:index + 1]

    def _applied(self, pos: int) -> bool:
        return pos not in self.rejected and (pos <= self.base_pos or
                                             any(o.pos == pos for o in self._near(pos)))

    def _refold(self) -> None:
        self.entries = {k: copy.deepcopy(v) for k, v in self.base.items()}
        self.rejected = {}
        self.gaps = set()
        for op in self.ops:
            self._apply(op)

    def _apply(self, op: Op) -> None:
        why = self._refusal(op)
        if why is not None:
            self.rejected[op.pos] = why
            return
        value = op.value
        if op.key == K.LEASE and value is not None:
            ttl = float(value.get("ttl") or 0)
            before = self.lease()
            # ``since``: the position of the claim that began this term. A renewal keeps it,
            # so two snapshots that disagree about one term can tell the first claim (the
            # legitimate one) from a later one by a bot that missed it.
            same = int(value["term"]) == before.term and                 K.norm(value.get("holder") or "") == K.norm(before.holder or "")
            value = {"holder": value.get("holder"), "term": int(value["term"]),
                     "expires_at": op.at + ttl if value.get("holder") else op.at,
                     "since": before.since if same and before.since else op.pos}
        self.entries[op.key] = Entry(value=copy.deepcopy(value), version=op.base + 1,
                                     term=op.term, writer=op.writer, pos=op.pos, at=op.at)

    def _refusal(self, op: Op) -> Optional[str]:
        if not K.valid(op.key):
            return "unknown key"
        current = self.version(op.key)
        if op.base > current:
            self.gaps.add(op.key)
            return f"gap: written against v{op.base}, this reader has v{current}"
        if op.base < current:
            return f"lost the compare-and-set: v{op.base} is already v{current}"
        lease = self.lease()
        if op.key == K.LEASE:
            return self._lease_refusal(op, lease)
        who = K.owner(op.key)
        if who == K.HOLDER:
            holder = lease.held_by(op.at)
            if holder is None or K.norm(holder) != op.writer:
                return f"not the lease holder (holder: {holder or 'nobody'})"
            if op.term != lease.term:
                return f"stale term {op.term} (current {lease.term})"
            return None
        if op.writer != who:
            return f"{op.writer} does not own {op.key}"
        return None

    @staticmethod
    def _lease_refusal(op: Op, lease: Lease) -> Optional[str]:
        value = op.value or {}
        try:
            term = int(value.get("term"))
        except (TypeError, ValueError):
            return "a lease write without a term"
        holder = K.norm(value.get("holder") or "") or None
        if holder is not None and holder != op.writer:
            return "a claim in another bot's name"
        current = K.norm(lease.holder or "") or None
        if term == lease.term:
            # renewing or releasing the term one holds
            if current is None or current != op.writer or op.at >= lease.expires_at:
                return f"term {term} is not this bot's to renew"
            return None
        if term == lease.term + 1:
            if holder is None:
                return "a claim must name its holder"
            if current is not None and current != op.writer and op.at < lease.expires_at                     and not value.get("promoted"):
                # A promotion (the admin's /role, or a holder presence marks DOWN) may take a
                # live term, as on gitbus; anything else waits for it to lapse.
                return f"term {lease.term} is still held by {current}"
            return None
        return f"term {term} does not follow term {lease.term}"

    # -- snapshots -------------------------------------------------------------------------
    def snapshot(self, upto: Optional[int] = None) -> Dict[str, Any]:
        """The state as of position ``upto`` (default: all of it), as a snapshot.

        A snapshot is published only up to ops old enough that no earlier line can still be
        on its way, because an op that arrives with a position at or below a snapshot's is
        ignored by a reader that started from that snapshot.
        """
        if upto is None or upto >= self.last_pos:
            entries, last = self.entries, self.last_pos
        else:
            partial = StateLog(base=self.base, base_pos=self.base_pos,
                               ops=[o for o in self.ops if o.pos <= upto])
            partial._refold()
            entries, last = partial.entries, max(self.base_pos, upto)
        lease = entries.get(K.LEASE)
        value = (lease.value or {}) if lease is not None else {}
        return {"v": SNAPSHOT_VERSION, "last_pos": last,
                "term": int(value.get("term") or 0), "holder": value.get("holder"),
                "entries": {k: e.to_dict() for k, e in sorted(entries.items())}}

    def dump(self) -> Dict[str, Any]:
        """Base and ops as they are, for the local cache: reloading folds the same state, and
        a late line can still slot in before ops the cache holds."""
        base = StateLog(base=self.base, base_pos=self.base_pos)
        base._refold()
        return {"base": base.snapshot(),
                "ops": [{**o.to_wire(), "pos": o.pos, "at": o.at} for o in self.ops]}

    @classmethod
    def load(cls, data: Dict[str, Any]) -> "StateLog":
        log = cls.from_snapshot(data.get("base"))
        log.add_all(Op.from_wire(o, pos=o["pos"], at=o["at"]) for o in data.get("ops") or [])
        return log

    @classmethod
    def from_snapshot(cls, data: Optional[Dict[str, Any]]) -> "StateLog":
        log = cls()
        if data:
            if int(data.get("v") or 0) != SNAPSHOT_VERSION:
                raise ValueError(f"snapshot version {data.get('v')} is not "
                                 f"{SNAPSHOT_VERSION}")
            log.base = {str(k): Entry.from_dict(v) for k, v in
                        (data.get("entries") or {}).items()}
            log.base_pos = int(data.get("last_pos") or 0)
            log._refold()
        return log

    def rebase(self, data: Dict[str, Any]) -> bool:
        """Adopt a newer snapshot, keeping the ops this reader has after it. True if adopted.

        A snapshot is refused when it is older than this reader's base, or when its lease term
        is behind the term this reader knows (§1b: a snapshot from an older term is ignored,
        as any stale write is). And it never replaces what this reader knows: an entry this
        reader holds with a higher (term, position) than the snapshot's is kept, because a
        deposed holder that missed its successor's claim can still post a snapshot *later* in
        the log that lacks the successor's writes (review, #263).
        """
        newer = StateLog.from_snapshot(data)
        if newer.base_pos < self.base_pos:
            return False
        if newer.lease().term < self.lease().term:
            return False
        if newer.lease().term == self.lease().term and self.lease().holder and                 K.norm(newer.lease().holder or "") != K.norm(self.lease().holder or "") and                 newer.lease().since > self.lease().since > 0:
            # The same term, claimed later by another bot that missed the first claim.
            return False
        merged = {k: copy.deepcopy(v) for k, v in newer.base.items()}
        for key, mine in self.entries.items():
            if mine.pos > newer.base_pos:
                continue                               # an op after the snapshot: re-applied
            theirs = merged.get(key)
            if theirs is None or self._mine_wins(key, mine, theirs):
                merged[key] = copy.deepcopy(mine)
        later = [o for o in self.ops if o.pos > newer.base_pos]
        self.base, self.base_pos, self.ops = merged, newer.base_pos, later
        self._refold()
        return True

    @staticmethod
    def _mine_wins(key: str, mine: Entry, theirs: Entry) -> bool:
        """Which of two entries for one key a merge keeps: the higher (term, position) --
        except for two holders of the *same* lease term, where the first claim of that term
        wins (the lower ``since``): the other bot claimed it without seeing that claim."""
        if key == K.LEASE and mine.value and theirs.value:
            if int(mine.value.get("term") or 0) == int(theirs.value.get("term") or 0) and                     K.norm(mine.value.get("holder") or "") !=                     K.norm(theirs.value.get("holder") or ""):
                return int(mine.value.get("since") or 0) < int(theirs.value.get("since") or 0)
        return (mine.term, mine.pos) > (theirs.term, theirs.pos)

    def compact(self, upto: Optional[int] = None) -> None:
        """Fold the ops up to ``upto`` (default: all) into the base -- after the state was
        published as a snapshot up to there. Never past what was published: a later op may
        still be overtaken by a line on its way."""
        upto = self.last_pos if upto is None else min(int(upto), self.last_pos)
        if upto <= self.base_pos:
            return
        folded = StateLog(base=self.base, base_pos=self.base_pos,
                          ops=[o for o in self.ops if o.pos <= upto])
        folded._refold()
        self.base, self.base_pos = folded.entries, upto
        self.ops = [o for o in self.ops if o.pos > upto]
        self._refold()

    def pending(self) -> List[Tuple[int, str]]:
        """(position, reason) of rejected ops, for ``/state`` and the log."""
        return sorted(self.rejected.items())
