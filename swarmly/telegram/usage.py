"""The turn ledger behind ``/usage``: one row per model turn (issue #125).

A bot that cannot say what it spent is a bot whose spending the admin has to guess at. Every
turn already knows what it cost when the runtime says so -- Claude reports ``total_cost_usd``,
Codex reports input tokens -- so the ledger records what was reported and, where a runtime is
silent, records **unknown** rather than a zero that would read as free.

Three rules shape it.

**Durable, and bounded.** Rows are appended to a JSONL file beside the transport's other state
and survive a restart. The file is capped: past :data:`MAX_ROWS` the oldest rows are dropped and
the drop is *counted*, so the summary can say "the ledger starts at ..." instead of implying it
saw everything.

**Reported, never guessed.** ``tokens_in``/``tokens_out``/``cost_usd`` are ``None`` when the
runtime did not say, and every rendering says "unknown" for them. A missing number is a fact
about the runtime, not a licence to interpolate.

**No identifiers.** A row names the discussion by its token (``tg-307``, or ``general``), the
model by name, and nothing else: no chat ids, no tokens, no paths (#22). The rendering is safe
to post in the group and to hand to a dashboard (#117).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Rows kept on disk. A busy round is tens of rows; this is a fortnight of a very busy bot.
MAX_ROWS = 2000

#: Outcomes a row can have. ``ok`` is a completed turn; the rest explain themselves.
OK = "ok"
ERROR = "error"
STOPPED = "stopped"
NO_REPLY = "no-reply"


@dataclass(frozen=True)
class Entry:
    """One model turn, as it happened."""

    alias: str
    started: float
    ended: float = 0.0
    topic: str = "general"
    outcome: str = OK
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    #: Reported cost in USD, when the runtime exposes it. Never estimated.
    cost_usd: Optional[float] = None
    model: str = ""
    #: What the runtime says is left, when it says anything at all.
    quota_left: Optional[float] = None
    quota_resets_at: Optional[float] = None

    @property
    def duration(self) -> float:
        return max(0.0, (self.ended or self.started) - self.started)

    def to_dict(self) -> Dict[str, Any]:
        return {"alias": self.alias, "started": self.started, "ended": self.ended,
                "topic": self.topic, "outcome": self.outcome, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "cost_usd": self.cost_usd, "model": self.model,
                "quota_left": self.quota_left, "quota_resets_at": self.quota_resets_at}

    @classmethod
    def from_dict(cls, data: Any) -> "Entry":
        if not isinstance(data, dict):
            raise ValueError(f"a ledger row must be an object, got {type(data).__name__}")
        alias = data.get("alias")
        started = data.get("started")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("a ledger row needs an alias")
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            raise ValueError("a ledger row needs 'started' (epoch seconds)")
        return cls(alias=alias.strip(), started=float(started),
                   ended=_number(data.get("ended")) or 0.0,
                   topic=str(data.get("topic") or "general"),
                   outcome=str(data.get("outcome") or OK),
                   tokens_in=_count(data.get("tokens_in")),
                   tokens_out=_count(data.get("tokens_out")),
                   cost_usd=_money(data.get("cost_usd")),
                   model=str(data.get("model") or ""),
                   quota_left=_money(data.get("quota_left")),
                   quota_resets_at=_number(data.get("quota_resets_at")))

    @classmethod
    def from_reply(cls, *, alias: str, started: float, ended: float, topic: str,
                   reply: Any) -> "Entry":
        """A row from an adapter reply: the meta it reported, and nothing it did not.

        Adapters differ in what they report (Claude a cost, Codex input tokens), so every field
        is read defensively and left ``None`` when absent.
        """
        meta: Dict[str, Any] = dict(getattr(reply, "meta", None) or {})
        ok = bool(getattr(reply, "ok", False))
        outcome = OK if ok else (STOPPED if meta.get("stopped") else ERROR)
        if not ok and meta.get("timed_out"):
            outcome = ERROR
        if reply is not None and getattr(reply, "empty", False) is True:
            outcome = NO_REPLY
        return cls(
            alias=alias, started=started, ended=ended or started, topic=topic or "general",
            outcome=outcome,
            tokens_in=_count(meta.get("tokens_in") if meta.get("tokens_in") is not None
                             else meta.get("input_tokens")),
            tokens_out=_count(meta.get("tokens_out") if meta.get("tokens_out") is not None
                              else meta.get("output_tokens")),
            cost_usd=_money(meta.get("cost_usd") if meta.get("cost_usd") is not None
                            else meta.get("total_cost_usd")),
            model=str(meta.get("model") or ""),
            quota_left=_money(meta.get("quota_left")),
            quota_resets_at=_number(meta.get("quota_resets_at")))


class Ledger:
    """The rows on disk, appended durably and rotated so the file cannot grow without end."""

    def __init__(self, path: Optional[Path], *, alias: str = "", max_rows: int = MAX_ROWS,
                 clock: Any = None) -> None:
        self.path = Path(path) if path is not None else None
        self.alias = alias
        self.max_rows = max(1, int(max_rows))
        self.clock = clock or time.time
        self.entries: List[Entry] = []
        #: Rows dropped by rotation, so a summary can say where its history really starts.
        self.dropped = 0
        self._load()

    # -- storage --------------------------------------------------------------------
    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:                                  # pragma: no cover - unreadable file
            return
        rows: List[Entry] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(Entry.from_dict(json.loads(line)))
            except (ValueError, TypeError):
                continue                                  # one bad row does not lose the rest
        if len(rows) > self.max_rows:                     # rotate what an older build left
            self.dropped += len(rows) - self.max_rows
            rows = rows[-self.max_rows:]
        self.entries = rows

    def _write_all(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
            tmp.write_text("".join(json.dumps(row.to_dict(), sort_keys=True) + "\n"
                                   for row in self.entries), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:                                   # pragma: no cover - a full disk
            pass

    # -- writing -------------------------------------------------------------------
    def append(self, entry: Entry) -> Entry:
        """Record one turn. Past the cap the oldest rows go, and the loss is counted."""
        self.entries.append(entry)
        if len(self.entries) > self.max_rows:
            drop = len(self.entries) - self.max_rows
            self.dropped += drop
            del self.entries[:drop]
            self._write_all()                             # a rotation rewrites, it does not append
            return entry
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
            except OSError:                                # pragma: no cover - a full disk
                pass
        return entry

    def clear(self) -> None:
        self.entries = []
        self.dropped = 0
        self._write_all()

    # -- reading -------------------------------------------------------------------
    def since(self, moment: float) -> List[Entry]:
        return [row for row in self.entries if row.started >= moment]

    def totals(self, moment: float = 0.0) -> Dict[str, Any]:
        """Tokens and cost over the rows since *moment*, with unknowns still unknown.

        A total is only a number when every row it covers reported one: one silent runtime
        among ten is enough to make the sum a guess, so the summary says how many rows were
        known instead.
        """
        rows = self.since(moment)
        if not rows:
            # Nothing ran, so nothing was spent: zero is known, not unknown (a pulse beat with
            # no turn read as all nulls).
            return {"turns": 0, "ok": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0,
                    "cost_usd": 0.0, "cost_known": 0, "tokens_in_known": 0,
                    "tokens_out_known": 0, "since": moment}
        known_cost = [row.cost_usd for row in rows if row.cost_usd is not None]
        known_in = [row.tokens_in for row in rows if row.tokens_in is not None]
        known_out = [row.tokens_out for row in rows if row.tokens_out is not None]
        return {
            "turns": len(rows),
            "ok": sum(1 for row in rows if row.outcome == OK),
            "failed": sum(1 for row in rows if row.outcome not in (OK, NO_REPLY)),
            "tokens_in": sum(known_in) if known_in else None,
            "tokens_out": sum(known_out) if known_out else None,
            "cost_usd": round(sum(known_cost), 4) if known_cost else None,
            # Each side is counted on its own: a runtime that reports input only (Codex) must
            # not make the output column look more covered than it is (#125 review).
            "cost_known": len(known_cost),
            "tokens_in_known": len(known_in), "tokens_out_known": len(known_out),
            "since": moment,
        }

    def quota(self) -> Tuple[Optional[float], Optional[float]]:
        """What the runtime last said about quota: ``(left, resets_at)``, or unknowns."""
        for row in reversed(self.entries):
            if row.quota_left is not None or row.quota_resets_at is not None:
                return row.quota_left, row.quota_resets_at
        return None, None

    def render(self, *, now: Optional[float] = None, since_boot: Optional[float] = None,
               limit: int = 8) -> str:
        """The ``/usage`` answer: today, since boot, quota, and the last few turns."""
        moment = self.clock() if now is None else float(now)
        start_of_day = moment - (moment % 86400)           # UTC days: no local path, no tz data
        today = self.totals(start_of_day)
        lines = [f"USAGE {self.alias} turns={len(self.entries)}"
                 + (f" dropped={self.dropped}" if self.dropped else "")]
        lines.append("today: " + _totals_line(today))
        if since_boot is not None:
            lines.append("since boot: " + _totals_line(self.totals(float(since_boot))))
        left, resets = self.quota()
        when = time.strftime("%H:%MZ", time.gmtime(resets)) if resets else "unknown"
        lines.append(f"quota left={_tokens(left)} resets={when}"
                     if left is not None or resets else "quota: unknown (not reported)")
        latest = self.entries[-max(1, int(limit)):]
        if latest:
            lines.append("last: " + ", ".join(
                f"{time.strftime('%H:%M', time.gmtime(row.started))} "
                f"{row.topic}:{row.outcome}" for row in latest))
        if self.dropped:
            lines.append(f"ledger starts at "
                         f"{time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(self.entries[0].started))}"
                         f" ({self.dropped} older rows dropped)")
        return "\n".join(lines)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: Any) -> Optional[int]:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _money(value: Any) -> Optional[float]:
    number = _number(value)
    return round(number, 6) if number is not None and number >= 0 else None


def _tokens(value: Optional[int]) -> str:
    return "unknown" if value is None else str(int(value))


def _money_text(value: Optional[float]) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def _totals_line(totals: Dict[str, Any]) -> str:
    """One line for a window: turns, tokens and cost, each saying how much was reported."""
    turns = totals["turns"]
    return (f"{turns} turns, in={_partial(totals['tokens_in'], totals['tokens_in_known'], turns)} "
            f"out={_partial(totals['tokens_out'], totals['tokens_out_known'], turns)} "
            f"cost={_partial_money(totals['cost_usd'], totals['cost_known'], turns)}")


def _partial(value: Optional[int], known: int, turns: int) -> str:
    """A token total that says how much of it was reported.

    `in=7` over ten turns where the runtimes said nothing about nine is not a total: it is the
    sum of the one row that spoke. A number is printed as a total only when every row it covers
    reported one; otherwise the count of reporting rows sits beside it (#125 review).
    """
    if value is None:
        return f"unknown (0/{turns} reported)" if turns else "unknown"
    if known >= turns:
        return str(int(value))
    return f"{int(value)} ({known}/{turns} reported)"


def _partial_money(value: Optional[float], known: int, turns: int) -> str:
    if value is None:
        return f"unknown (0/{turns} reported)" if turns else "unknown"
    if known >= turns:
        return f"${value:.4f}"
    return f"${value:.4f} ({known}/{turns} reported)"
