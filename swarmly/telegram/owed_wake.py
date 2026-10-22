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

Waking the bot -- resolving the row's discussion to a chat and thread, holding the wake while
that topic is busy (#160), and the per-round budget (#164) -- is the other half, and it lives
in the transport.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from swarmly.telegram.owed import Row

#: How many times a row may wake its debtor before a repeat goes to the admin. The rule is
#: "a wake needs news", and after this many wakes the news is owed by someone else.
MAX_WAKES = 2

#: How many turns one tick may start. A table can hold a dozen changed rows at once (a merge
#: that closes several, a new head that opens three), and waking every debtor in one pass is
#: the burst #164 exists to bound; until that item lands, one tick stays small.
MAX_WAKES_PER_TICK = 2


@dataclass(frozen=True)
class Wake:
    """One turn to start: who, where, and the news that is worth it."""

    row: str                    # the row's id
    debtor: str                 # the bot to wake, or ``admin``
    topic: str                  # the discussion token the debt lives in
    text: str                   # the delta, in one line
    escalated: bool = False     # this one goes to the admin, not to the debtor


def delta(row: Row) -> str:
    """One line saying what changed: the row, and the value before and after."""
    before = row.woken or "unknown"
    after = row.observed or "unknown"
    head = f" at {row.head}" if row.head else ""
    return f"{row.owes} on {row.subject}{head}: {before} to {after}"


def plan(rows: Iterable[Row], *, now: Optional[float] = None) -> List[Wake]:
    """The wakes this table earns right now -- none when nothing changed.

    A row wakes its debtor when the value observed for it differs from the value that last
    woke anyone. A row that has already woken its debtor :data:`MAX_WAKES` times and still
    shows the same value escalates to the admin instead, once: after that the table stops
    asking, because a debt nobody acts on is the admin's to see, not a bot's to be nagged
    about.
    """
    moment = time.time() if now is None else float(now)
    wakes: List[Wake] = []
    for row in sorted(rows, key=lambda item: item.id):
        if not row.is_open:
            continue
        observed = str(row.observed or "").strip()
        woken = str(row.woken or "").strip()
        if not observed:
            continue                       # nothing observed yet: no news either way
        if observed == woken:
            if row.wakes >= MAX_WAKES and not row.escalated:
                wakes.append(Wake(row=row.id, debtor="admin", topic=row.topic,
                                  text=(f"{row.debtor} has not acted on {row.owes} on "
                                        f"{row.subject} after {row.wakes} wake-ups, and "
                                        f"nothing changed since"),
                                  escalated=True))
            continue                       # no news: no turn
        wakes.append(Wake(row=row.id, debtor=row.debtor, topic=row.topic, text=delta(row)))
    return wakes


def mark_woken(rows: Sequence[Row], wakes: Sequence[Wake], *, now: Optional[float] = None
               ) -> List[Row]:
    """The rows a wake changed, ready to save: its ``woken`` value and its wake count.

    The transport saves what this returns, so a restart cannot wake the same bot about the
    same observation twice -- the wake is a fact, and its record lives with the row.
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
            row.woken = str(row.observed or "").strip()
            row.wakes = int(row.wakes) + 1
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
    moved and it did not, which is the round-5 case of a fix announced but never pushed.
    """
    out: List[Contradiction] = []
    for claim in claims or ():
        try:
            debtor, item, said, head = claim
        except (TypeError, ValueError):
            continue
        seen = str((heads or {}).get(str(item), "") or "").strip().lower()
        claimed = str(head or "").strip().lower()
        if not claimed or not seen or claimed == seen:
            continue
        out.append(Contradiction(debtor=str(debtor), item=str(item), claim=str(said),
                                 fact=f"the head is {seen}, not {claimed}"))
    return out


def notice(contradiction: Contradiction) -> str:
    """The one short line a topic gets when a claim and the facts disagree."""
    return (f"{contradiction.debtor} said {contradiction.claim} on {contradiction.item}, but "
            f"{contradiction.fact}. The item stays open: the facts decide, not the claim.")
