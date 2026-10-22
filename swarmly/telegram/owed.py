"""The expectations table: who owes what to whom (round 5.5, issue #162).

The rule the round is built on: **facts open and close rows; the model only hints.** A model
that drifts, forgets what it waits on, or says "done" changes nothing here. What changes a row
is a fact the transport observed -- a head pushed, CI concluding, a verdict filed, an issue
closed, a reply in the topic -- so the swarm slows down when a model drifts, but never stalls.

**A row.** One debt: who owes it (a bot, ``CI``, or ``admin``), what is owed, on which item and
head, who is waiting, the fact that opened it, the last value observed for it, and a deadline.
Rows live in the transport's state and on the bus, so a successor coordinator inherits the open
ones the way it inherits WAKE state (#81) and the round state (#86).

**Identity is the settlement rule.** A row's id is ``<kind>:<item>[@<head>]:<debtor>``, so
re-observing the same fact updates ``observed`` instead of opening a second row, and the fact
that answers a debt closes exactly the rows whose id it completes. There is deliberately **no**
code path that closes a row from prose: a bot saying "done" is not a fact, and everything in
this module works on facts.

**Missing is not empty.** A row file that cannot be read is reported as ``unreadable`` and left
alone, never treated as "no debt": an empty table would tell a coordinator that nothing is owed
while the round is stalled.

**Where each fact comes from** (#162 review). The row model is only as good as its sources, so
they are named here rather than left to the wiring:

* roles: the **roster**, never the round state -- ``identity.role`` per bot plus the ``ROLE:``
  line ``membership.py`` applies. ``RoundState.items`` carries an owner and the two verdicts; it
  has no role map.
* heads and CI: a **poll of the round's items** -- ``items[*].pr`` with its ``head``, walked by
  the transport -- not a ``ROUND_STATE`` block, which is a model's line and therefore a hint.
  Until that poll runs, these rows do not open; no block will open them.
* hand-offs: the durable row **is** the source. The channel's in-memory ``_pending_handoffs``
  is the same debt in a second book, and the wiring makes the nudge read these rows so a nudge
  cannot fire on a closed row or a row outlive a nudge.
* admin questions: only observable in a topic this transport polls (there is no DM polling), and
  the row it opens is a reminder, never an action.

Nothing here evaluates rows, wakes a bot, or reads a model's text: that is #163 and #165. These
are the model, the persistence, and pure functions from facts to rows.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from swarmly.transports.base import OWED_RELPATH, is_bus_document

logger = logging.getLogger("swarmly.telegram.owed")

#: The schema version of one row. A reader refuses what it does not know.
VERSION = 1

#: The kinds of debt the transport can infer. ``handoff``: a peer was asked something.
#: ``ci``: a head needs a result. ``verdict``: a reviewer or QA owes a verdict on a head.
#: ``fix``: a fix-first verdict or a QA fail means a new head is owed. ``merge``: everything
#: landed and the coordinator owes the merge. ``progress``: an unticked round item with an
#: owner. ``admin``: a question the admin owes an answer to (a reminder, never an action).
KINDS = ("handoff", "ci", "verdict", "fix", "merge", "progress", "admin")

#: Debtors that are not bots.
CI = "CI"
ADMIN = "admin"

#: The kinds whose debt is bound to one head: a result, a verdict, a fix and a merge are all
#: about a specific commit, and `id` carries the head so a fact on another head answers nothing.
HEAD_BOUND = ("ci", "verdict", "fix", "merge")

#: The states a row may be in.
OPEN, CLOSED = "open", "closed"
STATES = (OPEN, CLOSED)


#: The verdict vocabulary the inferences match on, and the words a bot actually writes.
#: A reviewer's "changes requested" must open the developer's fix row, so the vocabulary is
#: normalized here rather than compared as a free string (#162 review).
VERDICT_MERGE, VERDICT_FIX, VERDICT_PASS, VERDICT_FAIL = "merge", "fix-first", "pass", "fail"
_VERDICT_WORDS = {
    "merge": VERDICT_MERGE, "approved": VERDICT_MERGE, "approve": VERDICT_MERGE,
    "lgtm": VERDICT_MERGE, "ok": VERDICT_MERGE,
    "fix-first": VERDICT_FIX, "fix first": VERDICT_FIX, "fix": VERDICT_FIX,
    "changes requested": VERDICT_FIX, "changes-requested": VERDICT_FIX,
    "rework": VERDICT_FIX, "request changes": VERDICT_FIX,
    "pass": VERDICT_PASS, "passed": VERDICT_PASS, "green": VERDICT_PASS,
    "fail": VERDICT_FAIL, "failed": VERDICT_FAIL, "red": VERDICT_FAIL,
}

_SHA_RE = __import__("re").compile(r"^[0-9a-f]{7,40}$")


def normalize_verdict(value: Any) -> str:
    """A verdict, in the one vocabulary the rows are written in ("" when it is not one)."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return _VERDICT_WORDS.get(text, text)


def normalize_head(value: Any) -> str:
    """A head as rows compare it: lower case, 7..40 hex, else refused.

    The round state stores expanded shas and ``gh pr checks`` reports the full one, while a
    reply or an id may carry seven characters, so comparing raw strings would miss a match
    (#162 review). Expansion against a known full sha stays the round state's job
    (``rounds.normalize_sha``); this keeps the head honest and comparable.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not _SHA_RE.match(text):
        raise OwedError(f"a row head must be a 7..40 character sha (got {value!r})")
    return text


class OwedError(ValueError):
    """A row, or a row file, that cannot be read or written."""


class OwedPushError(OwedError):
    """The row was written here but the bus did not take it: the swarm cannot see it."""


def _alias(value: Any) -> str:
    """A debtor or waiter name, as rows compare them: trimmed, one spelling per bot."""
    return str(value or "").strip().lstrip("@").strip()


@dataclass
class Row:
    """One debt. ``id`` is what makes re-observing a fact an update, not a duplicate."""

    kind: str
    item: str                       # "#170", or "" for a debt not tied to an item
    debtor: str                     # "Reasonix", "CI", "admin"
    owes: str                       # "a verdict on 22e68c5", "an answer"
    waiting_on: str = ""            # who is blocked by this debt
    opened_by: str = ""             # the fact that opened it
    observed: str = ""              # the last value seen, for #163's delta
    head: str = ""                  # the head this debt is bound to, when it is
    topic: str = ""                 # the discussion it belongs to (a tg- token)
    deadline: float = 0.0           # absolute; 0 means "no deadline"
    state: str = OPEN
    closed_by: str = ""             # the fact that closed it
    #: The bot that wrote this row, like `.agents/wakes/<alias>/` (#162 review): two observers
    #: of the same debt write the same id in different directories, and `fold` reconciles them.
    writer: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = VERSION

    def __post_init__(self) -> None:
        self.kind = str(self.kind or "").strip()
        if self.kind not in KINDS:
            raise OwedError(f"unknown row kind {self.kind!r} (expected one of {list(KINDS)})")
        self.item = str(self.item or "").strip()
        self.debtor = _alias(self.debtor) or (CI if self.kind == "ci" else "")
        if not self.debtor:
            raise OwedError(f"a {self.kind} row needs a debtor")
        self.owes = str(self.owes or "").strip() or "something"
        self.waiting_on = _alias(self.waiting_on)
        self.head = normalize_head(self.head)
        if self.kind in HEAD_BOUND and not self.head:
            # The head is in the id on purpose: a verdict on older code must close nothing,
            # so a debt that is *about* a head cannot exist without naming it.
            raise OwedError(f"a {self.kind} row must name the head it is bound to")
        self.writer = _alias(self.writer)
        self.state = str(self.state or OPEN).strip().lower()
        if self.state not in STATES:
            raise OwedError(f"unknown row state {self.state!r} (expected one of {list(STATES)})")
        if self.deadline:
            try:
                self.deadline = float(self.deadline)
            except (TypeError, ValueError) as exc:
                raise OwedError(f"a row deadline must be epoch seconds (got "
                                f"{self.deadline!r})") from exc
        now = time.time()
        self.created_at = float(self.created_at or now)
        self.updated_at = float(self.updated_at or self.created_at)

    # -- identity --------------------------------------------------------------------
    @property
    def id(self) -> str:
        """``<kind>:<item>[@<head>]:<debtor>`` -- the whole settlement rule, in one string."""
        item = self.item or "-"
        if self.head:
            item = f"{item}@{self.head}"
        return f"{self.kind}:{item}:{self.debtor}"

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    @property
    def subject(self) -> str:
        """How the item is said in a line: ``#170@22e68c5``, ``#162``, or the topic."""
        return self.item or self.topic or "-"

    # -- reading it ------------------------------------------------------------------
    def line(self, now: Optional[float] = None) -> str:
        """One line for ``/owed``: who owes what, on what, and how late it is."""
        text = f"{self.debtor} owes {self.owes} on {self.subject}"
        if self.waiting_on:
            text += f" ({self.waiting_on} waiting)"
        if self.observed:
            text += f" [{self.observed}]"
        if self.deadline:
            moment = time.time() if now is None else float(now)
            left = self.deadline - moment
            text += (" overdue" if left < 0 else f" {_duration(left)} left")
        return text

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version, "kind": self.kind, "item": self.item,
            "debtor": self.debtor, "owes": self.owes, "waiting_on": self.waiting_on,
            "opened_by": self.opened_by, "observed": self.observed, "head": self.head,
            "topic": self.topic, "deadline": self.deadline, "state": self.state,
            "closed_by": self.closed_by, "writer": self.writer,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Row":
        if not isinstance(data, dict):
            raise OwedError(f"a row must be an object, got {type(data).__name__}")
        version = data.get("version", VERSION)
        if version != VERSION:
            raise OwedError(f"a row of version {version!r} cannot be read here (this build "
                            f"knows {VERSION})")
        return cls(
            kind=str(data.get("kind") or ""), item=str(data.get("item") or ""),
            debtor=str(data.get("debtor") or ""), owes=str(data.get("owes") or ""),
            waiting_on=str(data.get("waiting_on") or ""),
            opened_by=str(data.get("opened_by") or ""),
            observed=str(data.get("observed") or ""), head=str(data.get("head") or ""),
            topic=str(data.get("topic") or ""), deadline=data.get("deadline") or 0.0,
            state=str(data.get("state") or OPEN),
            closed_by=str(data.get("closed_by") or ""),
            writer=str(data.get("writer") or ""),
            created_at=data.get("created_at") or 0.0,
            updated_at=data.get("updated_at") or 0.0, version=VERSION)


def _duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# -- opening rows from facts -------------------------------------------------------------

def handoff_rows(*, asked: Iterable[str], asker: str = "", item: str = "", topic: str = "",
                 opened_by: str = "", at: Optional[float] = None,
                 deadline: float = 0.0) -> List[Row]:
    """A peer mentioned with a request owes an answer -- **each** peer, not just the first.

    One row per addressee, so a line addressed to two peers opens two debts -- the case that
    missed DeepSeek on 22 Oct. These rows are the **durable** record of a hand-off: the channel's
    in-memory ``_pending_handoffs`` is the same debt kept twice, and the wiring makes the nudge
    read these rows rather than its own book.
    """
    moment = time.time() if at is None else float(at)
    who = _alias(asker)
    rows: List[Row] = []
    for peer in asked:
        alias = _alias(peer)
        if not alias or (who and alias.lower() == who.lower()):
            continue                        # a bot does not owe itself an answer
        rows.append(Row(kind="handoff", item=item, debtor=alias, owes="an answer",
                        waiting_on=who, opened_by=opened_by or f"hand-off in {topic or 'a topic'}",
                        topic=topic, deadline=deadline, created_at=moment, updated_at=moment))
    return rows


def head_rows(*, item: str, head: str, roles: Mapping[str, str],
              opened_by: str = "", at: Optional[float] = None,
              deadline: float = 0.0) -> List[Row]:
    """A new head on a round item: CI owes a result, and the reviewer and QA owe verdicts.

    All three are bound to *that* head, and the head is required: a result or a verdict on
    older code answers nothing (#162 review). ``roles`` maps ``review``/``qa`` to an alias, and
    the caller resolves it from the **roster** (``identity.role`` and the ``ROLE:`` line
    ``membership.py`` applies) -- the round state has no role map. A role with no owner opens
    nothing: a debt nobody holds is a line to post, not a row to wake.
    """
    moment = time.time() if at is None else float(at)
    where = f"{item}@{head}" if head else item
    rows = [Row(kind="ci", item=item, head=head, debtor=CI, owes=f"a result on {head or item}",
                opened_by=opened_by or f"head {head} pushed" if head else "item opened",
                observed="pending", deadline=deadline, created_at=moment, updated_at=moment)]
    for role, what in (("review", "a review verdict"), ("qa", "a QA verdict")):
        alias = _alias(roles.get(role))
        if not alias:
            continue
        rows.append(Row(kind="verdict", item=item, head=head, debtor=alias,
                        owes=f"{what} on {head or item}",
                        opened_by=opened_by or f"head {head} pushed",
                        deadline=deadline, created_at=moment, updated_at=moment))
    logger.debug("owed: %s opens %d row(s)", where, len(rows))
    return rows


def fix_rows(*, item: str, head: str, developer: str, verdicts: Mapping[str, str],
             at: Optional[float] = None, deadline: float = 0.0) -> List[Row]:
    """A fix-first verdict or a QA fail means the developer owes a new head.

    Nothing else does: a merge verdict and a pass open no work for the developer, which is what
    keeps a reviewer's "looks good" from become a task.
    """
    moment = time.time() if at is None else float(at)
    alias = _alias(developer)
    failed = [kind for kind, value in (verdicts or {}).items()
              if normalize_verdict(value) in (VERDICT_FIX, VERDICT_FAIL)]
    if not alias or not failed:
        return []
    return [Row(kind="fix", item=item, head=head, debtor=alias,
                owes=f"a new head on {item}" + (f" after {head}" if head else ""),
                waiting_on=", ".join(sorted(failed)), opened_by=f"{', '.join(sorted(failed))} "
                f"on {head or item}", deadline=deadline, created_at=moment, updated_at=moment)]


def merge_row(*, item: str, head: str, coordinator: str, ci_green: bool,
              verdicts: Mapping[str, str], at: Optional[float] = None,
              deadline: float = 0.0) -> Optional[Row]:
    """CI green, a merge verdict and a QA pass on the same head: the coordinator owes a merge.

    All three on *this* head, or nothing: an approval on older code is not an approval of the
    code about to merge, which is the mistake the round pays for every time it happens.
    """
    moment = time.time() if at is None else float(at)
    alias = _alias(coordinator)
    if not alias or not ci_green:
        return None
    values = {kind: normalize_verdict(value) for kind, value in (verdicts or {}).items()}
    if values.get("review") != VERDICT_MERGE or values.get("qa") != VERDICT_PASS:
        return None
    return Row(kind="merge", item=item, head=head, debtor=alias,
               owes=f"a merge of {item}" + (f" at {head}" if head else ""),
               opened_by=f"CI green, review merge and QA pass on {head or item}",
               deadline=deadline, created_at=moment, updated_at=moment)


def progress_rows(*, items: Iterable[Mapping[str, Any]], at: Optional[float] = None,
                  deadline: float = 0.0) -> List[Row]:
    """An unticked round item with an owner means that owner owes progress.

    A ticked item, or one with no owner, opens nothing: an item nobody holds is the
    coordinator's to assign, not a bot's to be reminded about.
    """
    moment = time.time() if at is None else float(at)
    rows: List[Row] = []
    for entry in items or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("done") or entry.get("ticked") or entry.get("state") in ("merged", "closed",
                                                                             "done"):
            continue
        alias = _alias(entry.get("owner"))
        key = str(entry.get("key") or entry.get("item") or "").strip().lstrip("#")
        if not alias or not key:
            continue
        rows.append(Row(kind="progress", item=f"#{key}", debtor=alias,
                        owes=f"progress on #{key}", opened_by="the round item is open",
                        deadline=deadline, created_at=moment, updated_at=moment))
    return rows


def admin_row(*, question: str, item: str = "", topic: str = "", asked_by: str = "",
              at: Optional[float] = None, deadline: float = 0.0) -> Row:
    """A question addressed to the admin: the admin owes an answer. A reminder, never an action.

    Only a question in a topic this transport polls is observable -- there is no DM polling --
    so that topic, not a private message, is the trigger.
    """
    moment = time.time() if at is None else float(at)
    text = str(question or "").strip()
    return Row(kind="admin", item=item, debtor=ADMIN, owes="an answer",
               waiting_on=_alias(asked_by), opened_by=f"question: {text[:120]}" if text
               else "a question to the admin", topic=topic, deadline=deadline,
               created_at=moment, updated_at=moment)


# -- closing rows on facts ---------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    """Something that happened, which may answer one or more rows. Never prose.

    ``kind`` is one of ``ci``, ``merge``, ``head``, ``verdict``, ``reply``, ``close``; a fact
    about an item carries its ``item``, and the facts bound to one carry the ``head`` they are
    about. A row is closed by the fact that answers *it* -- which is why the same fact closes
    a CI row and leaves a progress row alone.
    """

    kind: str
    item: str = ""
    head: str = ""
    who: str = ""
    verdict: str = ""             # for a verdict fact: "merge", "pass", "fix-first", ...
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("ci", "merge", "head", "verdict", "reply", "close"):
            raise OwedError(f"unknown fact {self.kind!r}")


def closes(row: Row, fact: Fact) -> Optional[str]:
    """The reason *fact* closes *row*, or ``None`` when it does not answer it."""
    if not row.is_open:
        return None
    item, head = str(fact.item or "").strip(), str(fact.head or "").strip()
    who = _alias(fact.who)
    same_item = bool(item) and (item == row.item or item.lstrip("#") == row.item.lstrip("#"))
    try:
        head = normalize_head(head)
    except OwedError:
        return None                  # not a sha: it cannot be the head a debt is bound to
    same_head = (not head or not row.head or head == row.head)
    if fact.kind == "ci":
        # CI's own result on this head answers the CI row for it, and nothing else: a result
        # is not a review verdict, and a result on older code is not a result on this head.
        return f"CI {fact.verdict or 'concluded'}" if same_item and same_head             and row.kind == "ci" else None
    if fact.kind == "merge":
        # The merge answers every debt bound to that item: the result, the verdicts, the fix
        # and the merge itself.
        if same_item and row.kind in ("ci", "verdict", "fix", "merge", "progress"):
            return f"merged ({head or item})" if head else "merged"
        return None
    if fact.kind == "head":
        # A new head answers the verdicts for the *old* head -- and nothing else: the new head
        # opens new rows, it does not close the CI row of the head it replaced.
        if same_item and row.kind == "verdict" and row.head and head and row.head != head:
            return f"new head {head}"
        if same_item and row.kind == "fix":
            return f"new head {head}"
        return None
    if fact.kind == "verdict":
        if not same_item or row.kind != "verdict":
            return None
        if who and row.debtor and who.lower() != row.debtor.lower():
            return None
        if head and row.head and head != row.head:
            return None              # a verdict on older code answers nothing here
        return f"{row.debtor} filed {fact.verdict or 'a verdict'}"
    if fact.kind == "reply":
        if row.kind == "handoff" and (not who or who.lower() == row.debtor.lower()):
            if not same_item or row.item == item:
                return f"{row.debtor} answered"
        if row.kind == "admin" and who.lower() == ADMIN.lower():
            return "the admin answered"
        return None
    if fact.kind == "close":
        if same_item and row.kind in ("progress", "verdict"):
            return f"{item} closed"
        return None
    return None


def settle(rows: Sequence[Row], fact: Fact, *, now: Optional[float] = None) -> List[Row]:
    """Close the rows *fact* answers, and return the ones this call closed."""
    moment = time.time() if now is None else float(now)
    closed: List[Row] = []
    for row in rows:
        reason = closes(row, fact)
        if reason is None:
            continue
        row.state, row.closed_by, row.updated_at = CLOSED, reason, moment
        closed.append(row)
    return closed


def fold(rows: Iterable[Row]) -> List[Row]:
    """One row per id across writers (#162 review).

    Two observers of the same debt write the same id in their own directories. The reader's
    rule: **closed beats open** -- a debt answered is answered, whoever noticed first -- and
    among two opens the newest ``observed`` wins, then the newest write, then the lowest
    writer, so every reader folds to the same row and a debt is never counted twice.
    """
    best: Dict[str, Row] = {}
    for row in rows:
        seen = best.get(row.id)
        if seen is None:
            best[row.id] = row
            continue
        if seen.is_open and not row.is_open:
            best[row.id] = row
            continue
        if seen.is_open == row.is_open:
            if (row.updated_at, bool(row.observed), str(row.writer).lower()) >                     (seen.updated_at, bool(seen.observed), str(seen.writer).lower()):
                best[row.id] = row
    return list(best.values())


# -- persistence -------------------------------------------------------------------------

class OwedStore:
    """The rows this bot holds: one JSON file per row, on the checkout and on the bus.

    One file per row, never one mutable table: the bus transport retries a push onto the
    remote's tip, so two writers in one window both land and the later bytes win -- with one
    shared file that loses an update silently. A row's file is named by its own id, so a
    re-observation rewrites the same file and a different debt cannot collide with it.
    """

    def __init__(self, alias: str, *, state_dir: Optional[Path] = None, transport: Any = None,
                 root: Optional[Path] = None, clock: Any = time.time):
        self.alias = _alias(alias)
        if not self.alias:
            raise OwedError("an owed store needs the alias of the bot that holds it")
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.transport = transport
        self.root = Path(root) if root is not None else self.state_dir
        self.clock = clock
        self._unreadable: List[str] = []

    @property
    def safe_alias(self) -> str:
        return "".join(c if (c.isalnum() or c in "-_") else "-" for c in self.alias.lower())

    @property
    def directory(self) -> Optional[Path]:
        if self.state_dir is None:
            return None
        return self.state_dir / "owed"

    def _file(self, row: Row) -> Optional[Path]:
        directory = self.directory
        if directory is None:
            return None
        safe = "".join(c if (c.isalnum() or c in "-_@.") else "-" for c in row.id.lower())
        return directory / f"{safe[:120]}.json"

    def save(self, row: Row) -> Row:
        """Write one row under this bot's own directory, then publish it to the bus.

        The alias in the path is the **writer**, like `.agents/wakes/<alias>/`: two bots that
        observe the same debt each keep their own copy, and :func:`fold` reconciles them for a
        reader instead of one silently overwriting the other.
        """
        row.writer = row.writer or self.alias
        row.updated_at = float(self.clock())
        path = self._file(row)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(row.to_dict(), sort_keys=True), encoding="utf-8")
        transport = self.transport
        if transport is None or not getattr(transport, "ref", None):
            return row                  # no bus: this checkout is the plane
        relpath = f"{OWED_RELPATH}/{self.safe_alias}/{path.name if path else row.id}.json"
        if not is_bus_document(relpath):
            raise OwedPushError(f"refusing to write outside {OWED_RELPATH}: {relpath!r}")
        try:
            if hasattr(transport, "commit_and_push"):
                transport.commit_and_push(f"owed: {row.id} {row.state} [{self.safe_alias}]",
                                          paths=[relpath])
        except Exception as exc:                     # noqa: BLE001 - reported, not swallowed
            raise OwedPushError(f"the row is local only: {exc}") from exc
        return row

    def load(self) -> List[Row]:
        """Every row this bot can read, newest state per id."""
        directory = self.directory
        self._unreadable = []
        if directory is None or not directory.is_dir():
            return []
        rows: Dict[str, Row] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                row = Row.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:                        # noqa: BLE001 - one bad file, one row lost
                self._unreadable.append(path.name)
                logger.warning("owed: unreadable row %s; skipping it", path.name)
                continue
            rows[row.id] = row
        return list(rows.values())

    @property
    def unreadable(self) -> List[str]:
        """Row files this store could not read on the last :meth:`load` -- never "no debt"."""
        return list(self._unreadable)

    def open_rows(self, now: Optional[float] = None) -> List[Row]:
        """Open rows, the most overdue first; tie-broken by id so every bot reads one order."""
        moment = self.clock() if now is None else float(now)
        rows = [row for row in self.load() if row.is_open]
        return sorted(rows, key=lambda row: (row.deadline or float("inf"), row.id))

    def get(self, row_id: str) -> Optional[Row]:
        for row in self.load():
            if row.id == row_id:
                return row
        return None

    def observe(self, rows: Iterable[Row], *, now: Optional[float] = None) -> List[Row]:
        """Open rows from facts, keeping what an existing row has already learned.

        The same fact seen again updates ``observed`` and nothing else; a row that a fact has
        closed and a *new* fact re-opens starts from that fact (a new head is a new debt).
        """
        moment = self.clock() if now is None else float(now)
        existing = {row.id: row for row in self.load()}
        for row in rows:
            seen = existing.get(row.id)
            if seen is not None and seen.is_open:
                row.created_at = seen.created_at
                row.observed = seen.observed or row.observed
            row.updated_at = moment
        return list(rows)

    def summary(self, now: Optional[float] = None, *, limit: int = 10) -> str:
        """One line for ``/owed``: what is open, or that nothing is."""
        rows = self.open_rows(now)
        if not rows:
            broken = self.unreadable
            if broken:
                return (f"owed: cannot read {len(broken)} row file(s) "
                        f"({broken[0]}); this is not 'nothing is owed'")
            return "owed: nothing"
        shown = "; ".join(row.line(now) for row in rows[:limit])
        more = f" (+{len(rows) - limit} more)" if len(rows) > limit else ""
        return f"owed={len(rows)}: {shown}{more}"
