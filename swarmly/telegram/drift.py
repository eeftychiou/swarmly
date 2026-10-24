"""The drift ledger: rulebook violations the transport saw, per bot, and what they trigger.

The admin, 24 Oct: the rulebook stays, but what code can check is checked in transport space,
and the violations are counted as the sign of drift. A bot whose score crosses a line first
gets the broken rule quoted back in its next turn (a *reminder*); one that keeps drifting has
its topic's session retired and re-reads the rulebook from a fresh one (a *reset*).

Each transport keeps the ledger of its **own** bot: the checks run where the act happens (an
outgoing message, a refused command), so the bot that broke a rule is the one that holds the
row and acts on it. The role guard runs as a separate process; it appends to
``<ledger>.inbox.jsonl`` and the ledger folds that file in on its next read.

Stdlib only, like ``guard.py``: the guard writes the inbox without importing this package.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: kind -> (severity, the rule as the bot's rulebook states it). Hard = 3, medium = 2, soft = 1.
RULES: Dict[str, Tuple[int, str]] = {
    "addressed_frozen_peer": (3, "Never hand work to a peer that is paused, frozen or DOWN: "
                                 "give it to the stand-in the transport names, or to the "
                                 "coordinator."),
    "merge_gate_refused": (3, "Merge only when a reviewer's merge verdict, a QA pass and green "
                              "CI are all on the PR's current head."),
    "unknown": (1, "Follow the rulebook."),
}

#: A score at or over this puts the broken rules into the bot's next turn.
REMIND_AT = 3
#: A score at or over this (or two hard violations) retires the topic's session.
RESET_AT = 6
#: Only violations this recent count towards the score.
WINDOW_SECONDS = 6 * 3600.0
#: The ledger keeps this many rows.
KEEP = 300


@dataclass
class Violation:
    kind: str
    severity: int
    rule: str
    evidence: str = ""
    topic: str = ""                 # "<chat>:<thread>" or "" for none
    at: float = 0.0
    reset: bool = False             # this row is a reset marker, not a violation


@dataclass
class Decision:
    """What the transport does after recording a violation."""

    score: int
    hard: int
    remind: bool
    reset: bool
    resets_today: int = 0


@dataclass
class DriftLedger:
    path: Optional[Path]
    clock: Any = time.time
    rows: List[Violation] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rows = self._load()

    # -- storage ----------------------------------------------------------------------
    @property
    def inbox(self) -> Optional[Path]:
        return self.path.with_suffix(".inbox.jsonl") if self.path is not None else None

    def _load(self) -> List[Violation]:
        if self.path is None or not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict) and row.get("kind"):
                out.append(Violation(**{k: row[k] for k in Violation.__dataclass_fields__
                                        if k in row}))
        return out

    def _save(self) -> None:
        if self.path is None:
            return
        self.rows = self.rows[-KEEP:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps([asdict(r) for r in self.rows], indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def fold_inbox(self) -> List[Violation]:
        """Rows another process (the role guard) appended since the last read."""
        inbox = self.inbox
        if inbox is None or not inbox.exists():
            return []
        try:
            lines = inbox.read_text(encoding="utf-8").splitlines()
            inbox.unlink()
        except OSError:
            return []
        found = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("kind"):
                found.append(self.make(str(row["kind"]), str(row.get("evidence") or ""),
                                       str(row.get("topic") or ""), at=row.get("at")))
        if found:
            self.rows.extend(found)
            self._save()
        return found

    # -- recording --------------------------------------------------------------------
    def make(self, kind: str, evidence: str = "", topic: str = "",
             at: Optional[float] = None) -> Violation:
        severity, rule = RULES.get(kind, RULES["unknown"])
        moment = float(at) if isinstance(at, (int, float)) else float(self.clock())
        return Violation(kind=kind, severity=severity, rule=rule, evidence=evidence[:300],
                         topic=topic, at=moment)

    def record(self, kind: str, evidence: str = "", topic: str = "") -> Decision:
        self.fold_inbox()
        self.rows.append(self.make(kind, evidence, topic))
        self._save()
        return self.decide()

    def mark_reset(self, topic: str = "") -> None:
        self.rows.append(Violation(kind="reset", severity=0, rule="", topic=topic,
                                   at=float(self.clock()), reset=True))
        self._save()

    # -- reading ----------------------------------------------------------------------
    def live(self) -> List[Violation]:
        """The violations that count: inside the window and after the last reset."""
        now = float(self.clock())
        out: List[Violation] = []
        for row in self.rows:
            if row.reset:
                out = []
                continue
            if now - row.at <= WINDOW_SECONDS:
                out.append(row)
        return out

    def decide(self) -> Decision:
        live = self.live()
        score = sum(r.severity for r in live)
        hard = sum(1 for r in live if r.severity >= 3)
        day_ago = float(self.clock()) - 24 * 3600.0
        resets = sum(1 for r in self.rows if r.reset and r.at >= day_ago)
        return Decision(score=score, hard=hard, remind=score >= REMIND_AT,
                        reset=score >= RESET_AT or hard >= 2, resets_today=resets)

    def reminder(self) -> str:
        """The rules broken since the last reset, quoted once each, with the latest evidence."""
        seen: Dict[str, Violation] = {}
        for row in self.live():
            seen[row.kind] = row
        if not seen:
            return ""
        lines = [f"- {row.rule} (last: {row.evidence})" if row.evidence else f"- {row.rule}"
                 for row in seen.values()]
        return ("[Rule reminder from the transport: it recorded these violations of yours. "
                "Re-read your rulebook and follow it:\n" + "\n".join(lines) + "]")

    def summary(self, alias: str) -> str:
        d = self.decide()
        live = self.live()
        head = (f"{alias}: drift score {d.score} (remind at {REMIND_AT}, reset at {RESET_AT}), "
                f"{d.hard} hard, {d.resets_today} reset(s) in 24h")
        if not live:
            return head + ". No violations in the window."
        rows = [f"· {time.strftime('%H:%M', time.localtime(r.at))} {r.kind} "
                f"(+{r.severity}) {r.evidence}" for r in live[-10:]]
        return head + "\n" + "\n".join(rows)


def append_inbox(ledger_path: str, kind: str, evidence: str = "", topic: str = "") -> None:
    """For another process (the role guard): add one violation to the ledger's inbox."""
    if not ledger_path:
        return
    inbox = Path(ledger_path).with_suffix(".inbox.jsonl")
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "evidence": evidence[:300],
                                     "topic": topic, "at": time.time()}) + "\n")
    except OSError:
        pass
