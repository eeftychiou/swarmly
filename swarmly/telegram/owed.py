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
  is the same debt in a second book -- one that a restart loses -- so the wiring opens a row
  when a hand-off is made and closes it on the peer's reply **in that discussion**
  (``_note_owed_handoffs``/``_close_owed_handoff``); the nudge ladder still runs on the
  in-memory book, and the row is the copy the swarm and a successor can read.
* admin questions: only observable in a topic this transport polls (there is no DM polling), and
  the row it opens is a reminder, never an action.

Nothing here evaluates rows, wakes a bot, or reads a model's text: that is #163 and #165. These
are the model, the persistence, and pure functions from facts to rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

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
    "lgtm": VERDICT_MERGE,
    "fix-first": VERDICT_FIX, "fix first": VERDICT_FIX, "fix": VERDICT_FIX,
    "changes requested": VERDICT_FIX, "changes-requested": VERDICT_FIX,
    "rework": VERDICT_FIX, "request changes": VERDICT_FIX,
    "pass": VERDICT_PASS, "passed": VERDICT_PASS, "green": VERDICT_PASS,
    "fail": VERDICT_FAIL, "failed": VERDICT_FAIL, "red": VERDICT_FAIL,
}

#: The words whose meaning is the *role's*, not the word's: "ok" from QA is a pass (nothing is
#: owed) and "ok" from the reviewer is its merge verdict. One shared table mapped it to merge,
#: so a QA "ok" never counted as a pass and stalled the merge row for ever (#173 review, QA).
_ROLE_WORDS = {
    "qa": {"ok": VERDICT_PASS, "pass": VERDICT_PASS, "green": VERDICT_PASS,
           "fail": VERDICT_FAIL, "fail-first": VERDICT_FAIL},
    "review": {"ok": VERDICT_MERGE, "lgtm": VERDICT_MERGE, "approved": VERDICT_MERGE},
}

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def normalize_verdict(value: Any, kind: str = "") -> str:
    """A verdict, in the one vocabulary the rows are written in ("" when it is not one).

    *kind* is the role that said it (``review``/``qa``) when the caller knows: a word like
    "ok" means a pass from QA and a merge from a reviewer, and the role decides it.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    role = _ROLE_WORDS.get(str(kind or "").strip().lower(), {})
    return role.get(text) or _VERDICT_WORDS.get(text, text)


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


def alias_of(value: Any) -> str:
    """A debtor or waiter name, for callers outside this module (#164)."""
    return _alias(value)


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
    #: The observation that last woke somebody about this row (#163): a wake needs news, so
    #: the row carries what it already told someone, and how often it has.
    woken: str = ""
    wakes: int = 0
    #: When the last wake went out, so a wake that nobody acted on can be told from one whose
    #: turn is still running (#163 review: an ignored wake never escalated).
    woken_at: float = 0.0
    #: Set when a repeat went to the admin instead of the debtor, so it happens once.
    escalated: bool = False
    #: How far up the stall ladder this row has climbed (#164): 0 nothing, 1 reminded, 2 the
    #: coordinator was told, 3 the admin was. Each rung happens once, and the ladder stops there.
    stalls: int = 0
    #: What tells two debts of the same kind, item and debtor apart: the topic a hand-off was
    #: made in, or a digest of the question an admin row is about. Without it, one peer's two
    #: hand-offs in two topics are one row, and a reply in either closes both (#173 review).
    key_extra: str = ""
    #: Something about this row a reader must see rather than infer: today, an open item whose
    #: next actor cannot be derived (#164's second invariant). Shown in `/owed`, never acted on.
    flag: str = ""
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
        self.woken = str(self.woken or "").strip()
        self.wakes = int(self.wakes or 0)
        try:
            self.woken_at = float(self.woken_at or 0.0)
        except (TypeError, ValueError) as exc:
            raise OwedError(f"a row wake time must be epoch seconds (got "
                            f"{self.woken_at!r})") from exc
        self.escalated = bool(self.escalated)
        self.stalls = int(self.stalls or 0)
        self.key_extra = str(self.key_extra or "").strip()
        self.flag = str(self.flag or "").strip()
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
        """``<kind>:<item>[@<head>][@<key_extra>]:<debtor>`` -- the settlement rule, in a string."""
        item = self.item or "-"
        if self.head:
            item = f"{item}@{self.head}"
        if self.key_extra:
            item = f"{item}@{self.key_extra}"
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
        """One line for ``/owed``: who owes what, on what, and how late it is.

        The subject is only added when ``owes`` does not already name it: "owes a next step on
        #146 on #146" reads as a bug, not as a table (#195 review).
        """
        text = f"{self.debtor} owes {self.owes}"
        if str(self.subject).lower() not in str(self.owes).lower():
            # "for", not a second "on": a verdict row read "owes a review verdict on 22e68c5
            # on #170", two prepositions in one line (#199 review).
            text += f" for {self.subject}"
        if self.waiting_on:
            text += f" ({self.waiting_on} waiting)"
        if self.observed:
            text += f" [{self.observed}]"
        if self.flag:
            text += f" ⚑ {self.flag}"
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
            "topic": self.topic, "key_extra": self.key_extra, "flag": self.flag,
            "woken": self.woken, "wakes": int(self.wakes),
            "woken_at": float(self.woken_at), "escalated": bool(self.escalated),
            "stalls": int(self.stalls),
            "deadline": self.deadline, "state": self.state,
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
            topic=str(data.get("topic") or ""), key_extra=str(data.get("key_extra") or ""),
            flag=str(data.get("flag") or ""),
            woken=str(data.get("woken") or ""), wakes=int(data.get("wakes") or 0),
            woken_at=data.get("woken_at") or 0.0,
            escalated=bool(data.get("escalated")), stalls=int(data.get("stalls") or 0),
            deadline=data.get("deadline") or 0.0,
            state=str(data.get("state") or OPEN),
            closed_by=str(data.get("closed_by") or ""),
            writer=str(data.get("writer") or ""),
            # A stored row without its creation time is old, never "now": the fold lets a row
            # created after a close beat that close, so a missing stamp must not read as new.
            created_at=data.get("created_at") or data.get("updated_at") or 1.0,
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
                 place: str = "", opened_by: str = "", at: Optional[float] = None,
                 deadline: float = 0.0) -> List[Row]:
    """A peer mentioned with a request owes an answer -- **each** peer, not just the first.

    One row per addressee, so a line addressed to two peers opens two debts -- the case that
    missed DeepSeek on 22 Oct. *place* is what tells two hand-offs in tokened and untokened
    discussions apart: the topic token when there is one, else a stable digest of the place
    (never a chat id: these rows travel on the bus). These rows are the **durable** record of a hand-off: the channel's
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
                        topic=topic, key_extra=place or topic, deadline=deadline,
                        created_at=moment, updated_at=moment))
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
    if not str(head or "").strip():
        # A head-bound debt cannot exist without its head (#173 review), so a caller that has
        # no head yet -- an item with a PR but no head, a closed PR, an API hiccup in a tick --
        # opens nothing rather than raising inside the transport's loop.
        logger.debug("owed: %s has no head; opening nothing", where or "the item")
        return []
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
              if normalize_verdict(value, kind) in (VERDICT_FIX, VERDICT_FAIL)]
    if not alias or not failed or not str(head or "").strip():
        # A fix row is bound to the head it is about: with no head there is nothing to answer,
        # and a poll that momentarily lacks one must not raise (#173 review).
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
    if not alias or not ci_green or not str(head or "").strip():
        # No head, no merge row: "all three on this head" cannot be judged without one, and a
        # tick that momentarily has no head must not raise (#173 review).
        return None
    values = {kind: normalize_verdict(value, kind) for kind, value in (verdicts or {}).items()}
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
    # Every question is its own debt: without this, one admin row swallows the next question
    # the moment it is observed (#173 review).
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:7] if text else ""
    return Row(kind="admin", item=item, debtor=ADMIN, owes="an answer",
               key_extra=f"{topic or '-'}#q{digest}" if digest else "",
               waiting_on=_alias(asked_by), opened_by=f"question: {text[:120]}" if text
               else "a question to the admin", topic=topic, deadline=deadline,
               created_at=moment, updated_at=moment)


# -- closing rows on facts ---------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    """Something that happened, which may answer one or more rows. Never prose.

    ``kind`` is one of ``ci``, ``merge``, ``head``, ``verdict``, ``reply``, ``close``,
    ``finished``, ``dismiss``; a fact about an item carries its ``item``, and the facts bound to
    one carry the ``head`` they are about. ``finished`` and ``dismiss`` are about a discussion:
    their ``item`` is its place (the topic token, or the digest of an untokened one), and a
    ``finished`` carries ``at``, when the discussion's work finished. A row is closed by the fact
    that answers *it* -- which is why the same fact closes a CI row and leaves a progress row
    alone.
    """

    kind: str
    item: str = ""
    head: str = ""
    who: str = ""
    verdict: str = ""             # for a verdict fact: "merge", "pass", "fix-first", ...
    note: str = ""
    at: float = 0.0               # for a finished fact: when the discussion's work finished

    def __post_init__(self) -> None:
        if self.kind not in ("ci", "merge", "head", "verdict", "reply", "close",
                             "finished", "dismiss"):
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
        # A result that does not say which head it is about answers nothing (#173 review).
        return f"CI {fact.verdict or 'concluded'}" if same_item and head and same_head             and row.kind == "ci" else None
    if fact.kind == "merge":
        # The merge answers every debt bound to that item: the result, the verdicts, the fix
        # and the merge itself.
        # A hand-off is bound to the item it names (#428): the peer it asked for an answer owes
        # nothing once that work has landed.
        if same_item and row.kind in ("ci", "verdict", "fix", "merge", "progress", "handoff"):
            return f"merged ({head or item})" if head else "merged"
        return None
    if fact.kind == "head":
        # A new head answers the debts bound to the head it *replaced* -- and only those. The
        # new head opens new rows and closes none of them: a fix row for this head is still
        # owed while the head is this one (#175 review: the sweep's own head fact closed the
        # fix-first row it had just opened).
        if not (same_item and head and row.head and row.head != head):
            return None
        if row.kind in ("verdict", "fix"):
            return f"new head {head}"
        return None
    if fact.kind == "verdict":
        if not same_item or row.kind != "verdict" or not head:
            return None              # a verdict that names no head answers no verdict debt
        if who and row.debtor and who.lower() != row.debtor.lower():
            return None
        if head and row.head and head != row.head:
            return None              # a verdict on older code answers nothing here
        return f"{row.debtor} filed {fact.verdict or 'a verdict'}"
    if fact.kind == "reply":
        # A reply answers the hand-off **where it was made**: the topic it was posted in, or the
        # item it names. A place it names must match the row's own place -- a bot answering in
        # tg-1 does not answer a question asked in tg-2 -- and a reply that names no place
        # answers only rows that name none, so the fallback stays a fallback (#173 review, QA).
        here = (item or "").lower()
        if here:
            # The place a row records is its topic token, or -- in a discussion with no token
            # (General, an unbound topic) -- the digest the channel keyed it by.
            same_place = same_item or here in (str(row.topic or "").lower(),
                                               str(row.key_extra or "").lower())
        else:
            same_place = not row.topic and not row.item and not row.key_extra
        if not same_place:
            return None
        if row.kind == "handoff" and (not who or who.lower() == row.debtor.lower()):
            return f"{row.debtor} answered"
        if row.kind == "admin" and who.lower() == ADMIN.lower():
            return "the admin answered"
        return None
    if fact.kind == "close":
        if same_item and row.kind in ("progress", "verdict", "handoff"):
            return f"{item} closed"
        return None
    here = (item or "").lower()
    in_place = bool(here) and here in (str(row.topic or "").lower(),
                                       str(row.key_extra or "").lower())
    if fact.kind == "finished":
        # Every work item a discussion links finished (merged, closed): a hand-off made there
        # before that is moot -- the work it asked about is over, and the debtor may never
        # speak there again (live, 27 Oct: "Codex owes an answer" on a QA of a PR merged the
        # night before, and nothing could close it). One made after it is new work.
        if in_place and row.kind == "handoff" and fact.at and row.created_at < fact.at:
            return "the topic's work is done"
        return None
    if fact.kind == "dismiss":
        # The admin's word: the decision the stall ladder's last rung asks for. Any open row
        # placed in the discussion it was given in, or only the named debtor's.
        if in_place and (not who or who.lower() == row.debtor.lower()):
            return "dismissed by the admin"
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
    rule: **closed beats open** -- a debt answered is answered, whoever noticed first -- unless
    the open row was *created* after that close: then it is the same debt owed again (a second
    hand-off in one topic), not a stale copy. A sweep refreshing a stale open copy moves its
    ``updated_at``, never its ``created_at``, so that cannot resurrect a settled debt (#298
    review). Among two rows of the same state the newest ``updated_at`` wins, then the one that
    has an ``observed`` value, then the highest writer name (so the fold is deterministic
    rather than dependent on the order the files were read).
    """
    best: Dict[str, Row] = {}
    for row in rows:
        seen = best.get(row.id)
        if seen is None:
            best[row.id] = row
            continue
        if seen.is_open != row.is_open:
            opened, closed = (seen, row) if seen.is_open else (row, seen)
            best[row.id] = opened if opened.created_at > closed.updated_at else closed
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
                 root: Optional[Path] = None, clock: Any = time.time,
                 shared: Optional[Callable[[], List["Row"]]] = None,
                 published: Optional[Callable[[], List["Row"]]] = None):
        self.alias = _alias(alias)
        if not self.alias:
            raise OwedError("an owed store needs the alias of the bot that holds it")
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.transport = transport
        self.root = Path(root) if root is not None else self.state_dir
        self.clock = clock
        #: The other bots' rows, when the table lives on the state store (``state.owed:
        #: store``): read with this bot's own copy, in place of the bus documents.
        self.shared = shared
        #: What this bot published to the store itself: read only for the ids its state
        #: directory no longer holds (a lost directory), never over the ones it does -- the
        #: directory is the truth for its rows, and a re-opened id must not stay closed.
        self.published = published
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
        self.save_all([row])
        return row

    def save_all(self, rows: Sequence[Row]) -> List[Row]:
        """Write several rows and push them as **one** bus commit (#173 review).

        One push per row meant three pushes for one reply addressed to three peers. Every row
        is written first -- state copy and bus document -- and then the documents go up
        together. A refusal is reported after the local copies are on disk, so nothing this
        bot observed is lost to a bus that would not take it.
        """
        batch = list(rows or [])
        prepared: List[Tuple[Row, str]] = []
        for row in batch:
            row.writer = row.writer or self.alias
            row.updated_at = float(self.clock())
            path = self._file(row)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(row.to_dict(), sort_keys=True), encoding="utf-8")
            transport = self.transport
            if transport is None or not getattr(transport, "ref", None):
                continue                # no bus: this checkout is the plane
            # The state copy's name already ends in `.json`; the document's name is that name,
            # so the bus path is `<alias>/<id>.json` and not `<alias>/<id>.json.json` (#173 QA).
            name = path.name if path is not None else f"{row.id}.json"
            relpath = f"{OWED_RELPATH}/{self.safe_alias}/{name}"
            if not is_bus_document(relpath):
                raise OwedPushError(f"refusing to write outside {OWED_RELPATH}: {relpath!r}")
            if self.root is None:
                raise OwedPushError("no checkout root to write the bus document into")
            # The document the bus carries has to be *written* before it can be committed: the
            # local state copy is this bot's, the document under the checkout is the swarm's, and
            # a push of a path nothing wrote is a push of nothing (#173 review).
            document = self.root / relpath
            try:
                document.parent.mkdir(parents=True, exist_ok=True)
                document.write_text(json.dumps(row.to_dict(), sort_keys=True), encoding="utf-8")
            except OSError as exc:
                raise OwedPushError(f"the row document could not be written: {exc}") from exc
            prepared.append((row, relpath))
        if prepared:
            first, _relpath = prepared[0]
            more = f" (+{len(prepared) - 1} more)" if len(prepared) > 1 else ""
            # #235: a push refused by a race used to leave the row local for good. What the
            # bus did not take is retried with the next write, so a row that lost one race is
            # delivered by the round that follows instead of waiting for someone to observe it
            # again -- and only a bus that refuses every attempt leaves it marked.
            undelivered = list(dict.fromkeys(
                [relpath for _row, relpath in prepared] + self._undelivered()))
            try:
                if hasattr(self.transport, "commit_and_push"):
                    self.transport.commit_and_push(
                        f"owed: {first.id} {first.state} [{self.safe_alias}]{more}",
                        paths=undelivered)
            except Exception as exc:                     # noqa: BLE001 - reported, not swallowed
                self._mark_undelivered(undelivered)
                raise OwedPushError(f"the row is local only: {exc}") from exc
            self._mark_undelivered([])
        return batch

    @property
    def _undelivered_path(self) -> Optional[Path]:
        """Where the "local only" marker lives -- beside the owed rows, never among them.

        Not inside ``owed/``: :meth:`load` reads every ``*.json`` there as a row, and a marker
        would show up as an unreadable debt.
        """
        return None if self.state_dir is None else self.state_dir / "owed-undelivered.json"

    def _undelivered(self) -> List[str]:
        """Row documents a refused push left local only, still waiting for a delivery."""
        path = self._undelivered_path
        if path is None:
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, str) and is_bus_document(item)
                and self.root is not None and (self.root / item).is_file()]

    def _mark_undelivered(self, relpaths: Sequence[str]) -> None:
        """Record what the bus has not taken yet; an empty list clears the marker."""
        path = self._undelivered_path
        if path is None:
            return
        try:
            if relpaths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(sorted(set(relpaths))), encoding="utf-8")
            else:
                path.unlink(missing_ok=True)     # no marker yet: nothing to clear
        except OSError:                                  # a marker that cannot be written is
            logger.warning("owed: could not update the local-only marker %s", path)

    def load(self) -> List[Row]:
        """Every row this bot can read: its own state copy **and** the bus documents.

        The state copy is the writer's; the documents under the checkout are the swarm's. A
        successor that read only its own state directory answered "nothing is owed" while the
        bus held rows (#173 review, QA), so the two are folded here and every reader -- /owed,
        settlement, the nudge -- sees the same table.
        """
        self._unreadable = []
        rows: List[Row] = self.held(self._unreadable)
        in_directory = {row.id for row in rows}
        on_store = self.shared is not None
        if self.root is not None:
            # On the store, this bot's state directory is the truth for the ids it holds: its own
            # bus document for such an id is history (a document closed before the switch would
            # hide a re-opened id for good -- #298 review). For every other id the document still
            # counts: some closures exist only there (live, 25 Oct: skipping them all brought
            # back debts settled the day before).
            own = (self.safe_alias, in_directory) if on_store else None
            rows.extend(self._read_documents(self.root, self._unreadable, skip=own))
        if on_store:
            try:
                rows.extend(self.shared())
                if self.published is not None:
                    # What this bot published fills the ids its directory lost, and is folded
                    # with any old bus document for them: a re-open published after the
                    # switch was created after that close and wins (#328 review). Filtering by
                    # the ids the documents added would drop it for a closed copy.
                    rows.extend(row for row in self.published() if row.id not in in_directory)
            except Exception as exc:                 # noqa: BLE001 - never "nothing is owed"
                self._unreadable.append("state store")
                if type(exc) is RuntimeError:        # not ready yet (a start): no traceback
                    logger.info("owed: the store's rows cannot be read yet: %s", exc)
                else:                                # a subclass (RecursionError, ...) is a bug
                    logger.warning("owed: the store's rows could not be read", exc_info=True)
        return fold(rows)

    def held(self, unreadable: Optional[List[str]] = None) -> List[Row]:
        """The rows in this bot's own state directory, unfolded: the ones it holds, whoever
        wrote them (a close or a stall it recorded on a peer's row is here too)."""
        directory = self.directory
        rows: List[Row] = []
        if directory is not None and directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    rows.append(Row.from_dict(json.loads(path.read_text(encoding="utf-8"))))
                except Exception:                    # noqa: BLE001 - one bad file, one row lost
                    if unreadable is not None:
                        unreadable.append(path.name)
                    logger.warning("owed: unreadable row %s; skipping it", path.name)
        return rows

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

    @staticmethod
    def _read_documents(root: Path, unreadable: Optional[List[str]] = None,
                        skip: Optional[Tuple[str, Set[str]]] = None) -> List[Row]:
        """Every row document under *root*'s `.agents/owed/`, as read (not yet folded).

        A document that cannot be read is reported to *unreadable* as well as logged: a table
        that says "owed: nothing" because one file was corrupt is the failure mode this module
        exists to avoid (#173 review).
        """
        base = Path(root) / OWED_RELPATH
        if not base.is_dir():
            return []
        rows: List[Row] = []
        for path in sorted(base.glob("*/*.json")):
            try:
                row = Row.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if skip is not None and path.parent.name == skip[0] and row.id in skip[1]:
                    continue                    # the writer's own state copy holds this id
                rows.append(row)
            except Exception:                        # noqa: BLE001 - one bad document, one row
                logger.warning("owed: unreadable row document %s; skipping it", path)
                if unreadable is not None:
                    unreadable.append(path.name)
        return rows

    @staticmethod
    def documents(root: Path) -> List[Row]:
        """Every row document under *root*'s `.agents/owed/`, folded across writers.

        This is how a successor inherits the table: it reads the checkout, not its own state
        directory, and :func:`fold` settles the ids two bots both observed. It is the
        inheritance path **whenever a transport is configured**; a checkout-only deployment
        (no bus at all) has no document to read, and its rows live in the state directory
        alone -- which is the plane there, and which a successor on the same host inherits.
        :meth:`load` reads the same documents, so ``/owed`` and settlement never answer from a
        narrower view than this.
        """
        return sorted(fold(OwedStore._read_documents(root)), key=lambda row: row.id)

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
