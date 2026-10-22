"""Invariants, deadlines and the wake budget (round 5.5, issue #164).

Items 3 and 4 built the table and the wakes. This is what keeps the table honest when nobody
is looking: an open item with no live row is an **orphan** (nobody owes the next step), every
open PR should name a **next actor**, every open row has a **deadline** whatever the model
declared, and a round's coordinator wakes are **budgeted** so drift slows the swarm down
instead of spending its way through it.

Everything here is a pure function over rows and round items, except :class:`WakeBudget`, which
is the one small durable counter. The transport decides where a turn goes; this decides what
is owed and when it is overdue.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from swarmly.telegram.owed import ADMIN, CI, Row, alias_of

logger = logging.getLogger("swarmly.telegram.owed_guard")

#: The three rungs of one stalled row's ladder, in order, each happening once.
STALL_REMINDER, STALL_COORDINATOR, STALL_ADMIN = "reminder", "coordinator", "admin"
STALL_STEPS: Tuple[str, ...] = (STALL_REMINDER, STALL_COORDINATOR, STALL_ADMIN)

#: The item states that mean "no debt here any more".
FINISHED = ("merged", "closed", "done")

#: The CI values that count as a result.
RESULTS = ("green", "pass", "passed", "ok", "fail", "failed", "red", "error")

#: A verdict value that settles a reviewer's or QA's debt.
SETTLED = ("merge", "pass", "passed", "green", "approved", "lgtm", "ok")


def _ci(item: Mapping[str, Any]) -> str:
    return str(item.get("ci") or "").strip().lower()


def _verdict(item: Mapping[str, Any], kind: str) -> str:
    entry = item.get(kind)
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("verdict") or "").strip().lower()


def finished(item: Mapping[str, Any]) -> bool:
    """Is this item answered for good (merged, closed, or marked done)?"""
    return str(item.get("state") or "").strip().lower() in FINISHED


def gap(item: Mapping[str, Any], *, key: str = "") -> str:
    """The concrete gap on one open item: what is already true, and what is not.

    The #146 case at 09:13 read ``PR #146: CI green, merge verdict, QA pass, not merged`` --
    the facts that hold, then the one that does not. A coordinator who reads it needs nothing
    else to know where the item is stuck.
    """
    number = str(item.get("pr") or "").strip()
    where = f"PR #{number}" if number else (f"#{key}" if key else "the item")
    facts: List[str] = []
    ci = _ci(item)
    facts.append(f"CI {ci}" if ci else "no CI result")
    for kind, name in (("review", "merge verdict"), ("qa", "QA pass")):
        facts.append(name if _verdict(item, kind) in SETTLED else f"no {name}")
    facts.append("not merged")
    return f"{where}: " + ", ".join(facts)


def next_actor(item: Mapping[str, Any], roles: Mapping[str, str]) -> str:
    """Whoever has to act next on this item, or ``""`` when nothing can be derived.

    ``CI`` is returned for an item whose CI has no result yet: that is a fact the transport
    waits on, not a bot to wake, and the caller decides which of the two it needs. An empty
    answer on an open item is the dashboard's flag -- a PR nobody is on.
    """
    if finished(item):
        return ""
    if (_verdict(item, "review") in ("fix-first", "fix first", "fix", "changes requested",
                                    "changes-requested", "rework")
            or _verdict(item, "qa") in ("fail", "failed", "red")):
        return alias_of(roles.get("developer"))
    ci = _ci(item)
    if ci not in RESULTS:
        return "CI"                       # waiting on a result, not on a bot
    if ci in ("fail", "failed", "red", "error"):
        return alias_of(roles.get("developer"))     # a red run is the developer's to fix
    if _verdict(item, "review") not in SETTLED:
        return alias_of(roles.get("review"))
    if _verdict(item, "qa") not in SETTLED:
        return alias_of(roles.get("qa"))
    return alias_of(roles.get("coordinator"))       # everything but the merge is in


def orphans(items: Iterable[Tuple[str, Mapping[str, Any]]], rows: Sequence[Row], *,
            coordinator: str, roles: Optional[Mapping[str, str]] = None,
            at: Optional[float] = None, deadline: float = 0.0) -> List[Row]:
    """An open item with no live row: whoever has to act next owes the next step on it.

    This is #146 at 09:13, where no WAKE was armed and nothing in the table said the round was
    stuck. The row carries the concrete :func:`gap`, so its ``observed`` value changes when the
    item moves and the wake is news again.

    The debtor is :func:`next_actor` when that names a bot, and the coordinator otherwise: a
    reviewer with a merge in their hands is the one to remind, not the coordinator, while an
    item waiting on CI (``next_actor`` says ``CI``) is the coordinator's -- no bot owes a
    waiting, and an item nobody can be derived for is the coordinator's to assign.
    """
    moment = time.time() if at is None else float(at)
    lead = alias_of(coordinator)
    if not lead:
        return []
    live = {str(row.item or "").strip() for row in rows if row.is_open and row.item}
    out: List[Row] = []
    for key, item in items or ():
        label = f"#{str(key).lstrip('#')}"
        if finished(item) or label in live:
            continue
        actor = alias_of(next_actor(item, roles or {})) if roles is not None else ""
        if actor in (CI, ADMIN):
            actor = ""                  # a fact this transport waits on, or the admin: no bot
        debtor = actor or lead
        out.append(Row(kind="progress", item=label, debtor=debtor,
                       owes=f"a next step on {label}", observed=gap(item, key=key),
                       waiting_on=lead if debtor.lower() != lead.lower() else "",
                       opened_by=(f"the item is open and {debtor} has the next step"
                                  if actor else "the item is open and nothing owes it"),
                       deadline=deadline, created_at=moment, updated_at=moment))
    return out


@dataclass(frozen=True)
class Stall:
    """One rung of one row's ladder, due now."""

    row: str
    step: str                  # reminder | coordinator | admin
    debtor: str
    text: str


def stall_text(row: Row, step: str, *, minutes: float) -> str:
    """What each rung says. A ladder that repeats itself is a ladder nobody reads."""
    waited = f"nothing has moved for {minutes:g} minutes"
    if step == STALL_REMINDER:
        return (f"{row.debtor} owes {row.owes} on {row.subject} and {waited}. This is the "
                f"reminder: act on it, or say in one line why not.")
    if step == STALL_COORDINATOR:
        return (f"{row.debtor} still owes {row.owes} on {row.subject} and {waited}. "
                f"Coordinator: reassign it or escalate it; the table says nobody else can.")
    return (f"{row.debtor} owes {row.owes} on {row.subject}, {waited}, and two rungs of "
            f"reminders have not moved it. The admin should decide.")


def stalls(rows: Iterable[Row], *, now: Optional[float] = None, minutes: float) -> List[Stall]:
    """The rungs due now: a row that has not moved for *minutes*, one rung at a time.

    "Has not moved" is measured on the row itself -- ``updated_at`` is touched only when a fact
    changed it -- so a beat that rewrites nothing cannot keep a stalled row looking fresh. Each
    rung happens once (:attr:`Row.stalls`), and the ladder stops at the admin.
    """
    if float(minutes or 0) <= 0:
        return []
    moment = time.time() if now is None else float(now)
    limit = float(minutes) * 60.0
    out: List[Stall] = []
    for row in sorted(rows, key=lambda item: item.id):
        if not row.is_open:
            continue
        age = moment - float(row.updated_at or 0)
        if age < limit:
            continue
        rung = int(row.stalls or 0)
        if rung >= len(STALL_STEPS):
            continue                      # the ladder is done: nothing left to climb
        step = STALL_STEPS[rung]
        out.append(Stall(row=row.id, step=step, debtor=row.debtor,
                         text=stall_text(row, step, minutes=float(minutes))))
    return out


def advance(rows: Sequence[Row], decided: Sequence[Stall], *,
            now: Optional[float] = None) -> List[Row]:
    """The rows a rung changed, ready to save: one rung each, once each."""
    moment = time.time() if now is None else float(now)
    by_id = {row.id: row for row in rows}
    touched: List[Row] = []
    for stall in decided:
        row = by_id.get(stall.row)
        if row is None:
            continue
        row.stalls = int(row.stalls or 0) + 1
        row.updated_at = moment
        touched.append(row)
    return touched


def budget_notice(round_key: str, cap: int) -> str:
    """What the admin hears when a round has spent its wakes for the day."""
    return (f"Owed wakes for {round_key or 'this round'} have reached the day's budget of "
            f"{cap}: wakes are paused until tomorrow. The table is still there -- `/owed` "
            f"lists what is owed -- so nothing is lost, but the swarm will not spend turns on "
            f"it. Decide, or raise limits.round_wake_budget.")


class WakeBudget:
    """How many coordinator wakes one round has spent today, in one small durable file.

    The cap is ``limits.round_wake_budget`` (#164). It counts *coordinator* wakes only: a
    developer's or a reviewer's wake is the debt's own, and starving those would stall the
    round instead of slowing it.
    """

    def __init__(self, path: Optional[Path], *, cap: int, clock: Any = time.time):
        self.path = Path(path) if path is not None else None
        self.cap = int(cap or 0)
        self.clock = clock

    @staticmethod
    def _day(moment: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(float(moment)))

    def _read(self) -> Dict[str, Any]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("owed: unreadable wake budget %s; counting from zero", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def used(self, round_key: str, *, now: Optional[float] = None) -> int:
        """Wakes this round has spent today."""
        day = self._day(self.clock() if now is None else float(now))
        entry = self._read().get(str(round_key or ""))
        if not isinstance(entry, dict) or entry.get("day") != day:
            return 0                      # a new day starts at zero
        try:
            return int(entry.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    def allow(self, round_key: str, *, now: Optional[float] = None) -> bool:
        """May one more coordinator wake go out for this round today?"""
        if self.cap <= 0:
            return True                   # no cap configured is no cap
        return self.used(round_key, now=now) < self.cap

    def spend(self, round_key: str, *, now: Optional[float] = None) -> int:
        """Count one wake and return the day's total (the file is the record, not memory)."""
        moment = self.clock() if now is None else float(now)
        key = str(round_key or "")
        data = self._read()
        total = self.used(key, now=moment) + 1
        data[key] = {"day": self._day(moment), "count": total}
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            except OSError as exc:                  # noqa: BLE001 - reported, not swallowed
                logger.warning("owed: the wake budget could not be written: %s", exc)
        return total
