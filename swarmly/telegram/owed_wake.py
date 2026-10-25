"""Deciding who to wake, and with what news (round 5.5, issue #163).

Item 3 built the rows: facts open and close them. This is item 4's first half -- **the
decision** -- and it is deliberately a pure function over rows, so the round-5 replays can be
frozen as facts in a test instead of replayed by hand.

Four rules, all from the issue:

* **No change, no turn.** A row wakes nobody until the value observed for it differs from the
  value that last woke someone about it (:attr:`Row.woken`). The standing `gh issue view 120`
  check that cost round 5 half a day is exactly the case this removes: an unchanged issue is
  an unchanged observation.
* **The wake names the delta.** ``CI on #146 went from pending to green at c166987``, not
  "check again": the text carries what changed, so the turn can act without re-reading the
  world first.
* **A wake needs news.** The same bot, the same row, the same observation twice is not news;
  the next step goes to the admin instead (:class:`Wake.escalated`), and only once.
* **A claim that contradicts the facts is flagged, not believed.** A bot saying "pushed the
  fix" while the head did not move gets one short notice and the row stays open -- there is no
  path here that closes a row from a bot's word.

Two things this half learned the hard way (#163 review):

* **A wake needs a bot behind it.** ``CI`` owes a result and ``admin`` owes an answer; neither
  has a turn to start, so a row they hold is *observed* and never woken. The transport passes
  its roster (*bots*) and anything off it is treated the same way.
* **An ignored wake has to be able to escalate.** Counting only the wakes that carried news
  made the escalation unreachable: once the news was spent the row went quiet forever and the
  stall this item exists to catch stayed silent. So a wake is given :data:`WAKE_GRACE`, and
  after that the *same* observation earns one nudge, then the admin. A row that was seeded
  with a value and never woken (``wakes == 0``) still says nothing -- that is the standing
  check, not an ignored wake.

Waking the bot -- resolving the row's discussion to a chat and thread, holding the wake while
that topic is busy (#160), and the per-round budget (#164) -- is the other half, and it lives
in the transport.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from swarmly.telegram.owed import ADMIN, CI, Row

#: How many times a row may wake its debtor before a repeat goes to the admin. The rule is
#: "a wake needs news", and after this many wakes the news is owed by someone else.
MAX_WAKES = 2

#: How long a wake is given before the same observation counts as ignored: ten minutes, five
#: beats at the default heartbeat, which is longer than the turn it started usually takes.
WAKE_GRACE = 600.0

#: How many turns one tick may start. A table can hold a dozen changed rows at once (a merge
#: that closes several, a new head that opens three), and waking every debtor in one pass is
#: the burst #164 exists to bound; until that item lands, one tick stays small.
MAX_WAKES_PER_TICK = 2

#: The shortest head we will match by prefix: a four-character fragment matches too much to
#: call "the same commit".
MIN_HEAD = 7


@dataclass(frozen=True)
class Wake:
    """One turn to start: who, where, and the news that is worth it."""

    row: str                    # the row's id
    debtor: str                 # the bot to wake, or ``admin``
    topic: str                  # where the turn goes: the row's discussion, or the admin's
    text: str                   # the delta, in one line
    escalated: bool = False     # this one goes to the admin, not to the debtor


def _short(head: object) -> str:
    """A head the way a human says it: lower case, and the short form when it is a full one."""
    text = str(head or "").strip().lower()
    return text[:MIN_HEAD] if len(text) >= MIN_HEAD else text


def same_head(one: object, two: object) -> bool:
    """Whether two heads name the same commit: equal, or one the short form of the other.

    Bots announce seven-character SHAs while ``gh`` and git answer with forty, so comparing the
    strings as they came made every correct claim a contradiction (#163 review).
    """
    left = str(one or "").strip().lower()
    right = str(two or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= MIN_HEAD and right.startswith(left):
        return True
    return len(right) >= MIN_HEAD and left.startswith(right)


def _addressable(debtor: object, bots: Optional[Iterable[str]]) -> bool:
    """Whether a wake has a bot behind it: a debtor some chat handler can start a turn for."""
    name = str(debtor or "").strip().lstrip("@")
    if not name or name.lower() in (CI.lower(), ADMIN.lower()):
        return False
    if bots is None:
        return True                     # no roster given: any named bot is assumed real
    known = {str(bot or "").strip().lstrip("@").lower() for bot in bots}
    return bool(known) and name.lower() in known


def _where(row: Row) -> str:
    """What the row is about, once: ``a result on c166987 on #146``, head only if it is new.

    The head is added only when ``owes`` does not already name it: a ``ci`` row owes "a result
    on c166987", and saying that twice reads as a bug rather than as a fact (#163 review).
    """
    where = f"{row.owes} on {row.subject}"
    head = str(row.head or "").strip()
    if head and _short(head) not in where.lower():
        where += f" at {head}"
    return where


def delta(row: Row) -> str:
    """One line saying what changed: the row, and the value before and after."""
    return f"{_where(row)}: {row.woken or 'unknown'} to {row.observed or 'unknown'}"


def _repeat(row: Row, moment: float, admin_topic: str) -> Optional[Wake]:
    """The wake an unchanged row still earns: one nudge after the grace, then the admin."""
    if row.wakes <= 0:
        return None                     # seeded, never woken: nothing has been ignored
    if row.woken_at and moment - float(row.woken_at) < WAKE_GRACE:
        return None                     # the turn that woke it is probably still running
    if row.wakes >= MAX_WAKES:
        if row.escalated:
            return None                 # said once, not every beat
        return Wake(row=row.id, debtor=ADMIN, topic=admin_topic,
                    text=(f"{row.debtor} has not acted on {row.owes} on {row.subject} after "
                          f"{row.wakes} wake-ups, and nothing changed since"),
                    escalated=True)
    return Wake(row=row.id, debtor=row.debtor, topic=row.topic,
                text=(f"{_where(row)} -- still {row.observed}, nothing has changed since the "
                      f"last wake ({row.wakes} of {MAX_WAKES})"))


def plan(rows: Iterable[Row], *, now: Optional[float] = None,
         bots: Optional[Iterable[str]] = None, admin_topic: str = "") -> List[Wake]:
    """The wakes this table earns right now -- none when nothing changed.

    A row wakes its debtor when the value observed for it differs from the value that last
    woke anyone. A wake nobody acted on is nudged once after :data:`WAKE_GRACE`, and after
    :data:`MAX_WAKES` wakes with no news the row escalates to the admin instead, once: after
    that the table stops asking, because a debt nobody acts on is the admin's to see, not a
    bot's to be nagged about. An escalation goes to *admin_topic* -- the transport passes the
    place the admin is spoken to, never the row's own discussion (#163 review).
    """
    moment = time.time() if now is None else float(now)
    wakes: List[Wake] = []
    for row in sorted(rows, key=lambda item: item.id):
        if not row.is_open:
            continue
        observed = str(row.observed or "").strip()
        if not observed:
            continue                       # nothing observed yet: no news either way
        if not _addressable(row.debtor, bots):
            continue                       # CI, the admin, or nobody this swarm can wake
        woken = str(row.woken or "").strip()
        if observed == woken:
            repeat = _repeat(row, moment, admin_topic)
            if repeat is not None:
                wakes.append(repeat)
            continue                       # no news: no turn, unless the last one was ignored
        wakes.append(Wake(row=row.id, debtor=row.debtor, topic=row.topic, text=delta(row)))
    return wakes


def mark_woken(rows: Sequence[Row], wakes: Sequence[Wake], *, now: Optional[float] = None
               ) -> List[Row]:
    """The rows a wake changed, ready to save: its ``woken`` value, count, and time.

    The transport saves what this returns, so a restart cannot wake the same bot about the
    same observation twice -- the wake is a fact, and its record lives with the row. News after
    an escalation starts the count again: that debt is a new one, and it may stall on its own
    (#163 review).
    """
    moment = time.time() if now is None else float(now)
    by_id = {row.id: row for row in rows}
    touched: List[Row] = []
    for wake in wakes:
        row = by_id.get(wake.row)
        if row is None:
            continue
        if wake.escalated:
            row.escalated = True
        else:
            observed = str(row.observed or "").strip()
            if observed != str(row.woken or "").strip():
                row.wakes = 0              # new news: a fresh count, and no stale escalation
                row.escalated = False
            row.woken = observed
            row.wakes = int(row.wakes) + 1
            row.woken_at = moment
        row.updated_at = moment
        touched.append(row)
    return touched


@dataclass(frozen=True)
class Contradiction:
    """A bot's claim that the facts do not support: one notice, and the row stays open."""

    debtor: str
    item: str
    claim: str
    fact: str


def contradictions(claims: Iterable[tuple], *, heads: dict) -> List[Contradiction]:
    """Claims contradicted by the facts, one per claim.

    *claims* are ``(debtor, item, claim, head)`` as the transport saw them in a reply --
    "pushed the fix", "head 38f1977". *heads* maps an item to the head the facts show. A claim
    that names a head the item does not have is a contradiction: the bot believes the head
    moved and it did not, which is the round-5 case of a fix announced but never pushed. The
    two heads are compared through :func:`same_head`, so a short SHA the facts agree with is
    not a contradiction.
    """
    out: List[Contradiction] = []
    for claim in claims or ():
        try:
            debtor, item, said, head = claim
        except (TypeError, ValueError):
            continue
        seen = str((heads or {}).get(str(item), "") or "").strip().lower()
        claimed = str(head or "").strip().lower()
        if not claimed or not seen or same_head(claimed, seen):
            continue
        out.append(Contradiction(debtor=str(debtor), item=str(item), claim=str(said),
                                 fact=f"the head is {_short(seen)}, not {_short(claimed)}"))
    return out


def notice(contradiction: Contradiction) -> str:
    """The one short line a topic gets when a claim and the facts disagree."""
    return (f"{contradiction.debtor} said {contradiction.claim} on {contradiction.item}, but "
            f"{contradiction.fact}. The item stays open: the facts decide, not the claim.")
