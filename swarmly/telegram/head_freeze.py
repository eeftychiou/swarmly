"""The head under review is frozen, and a claimed SHA is checked (#183).

Round 5.5 lost work to moving heads. #173 went through about eleven heads and #133 through
eight; rebuilding #173 dropped a fix, and a bot reported `621722f` while the PR was still at
`a7b0355`. The review machinery was never wrong about a verdict -- it was pinned to a head --
but nothing said out loud that the head had moved, and nothing checked a claimed SHA against
the PR.

Three rules, and this is the pure half of all three:

* **A claim is checked against the fact.** When a bot names a SHA as a PR's head, the head the
  PR actually has decides. On a mismatch the transport posts one notice and the row stays open:
  the facts decide, not the claim (the rule #164 set for the table, applied to a head).
* **No rewrites under review.** A branch that is rewritten while a verdict is open on its old
  head is flagged once per (item, old head, new head). Nits become follow-up issues instead of
  new heads, and a rewrite that was needed is said to be needed rather than slipping by.
* **"Ready" means mergeable.** An item called ready while GitHub says it conflicts with main is
  not ready, whatever the message says.

The transport decides where a notice goes and how it is remembered; this decides what is worth
saying and in what words.

**Where the head comes from, exactly.** Every check here compares against the head the *round
state* carries (``RoundState.items[*].head``), which is the PR's ``headRefOid`` after the
transport reads it -- today a head a bot reported to the round, and the PR's own value once
#165's polling lands. That is the fact the table is built from, so it is the fact these rules
use; naming it here so no reader thinks a claim is being checked against a live GitHub read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from swarmly.telegram.owed import Row
from swarmly.telegram.owed_wake import same_head

#: A commit SHA as the swarm writes it: seven characters or more, hex.
_SHA = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})(?![0-9A-Za-z])")
#: An item as the swarm writes it: ``#173``.
_ITEM = re.compile(r"#(\d+)")

#: What makes a SHA next to an item a *claim about its head*: the shapes the swarm posts --
#: "#173 is at 840f8b0", "#173 @ 840f8b0", "head 840f8b0 on #173", "pushed 840f8b0 to #173".
_CLAIM_VERBS = ("at", "head", "pushed", "rebuilt", "rebased", "force-pushed", "force pushed",
                "now", "is", "@")


@dataclass(frozen=True)
class Claim:
    """One bot's statement that an item's head is a SHA."""

    item: str                      # "#173"
    head: str                      # what the message says the head is
    said: str                      # the phrase, for the notice


@dataclass(frozen=True)
class Mismatch:
    """A claim the facts do not support."""

    item: str
    claimed: str
    actual: str


@dataclass(frozen=True)
class Rewrite:
    """A head that moved while a verdict was open on the old one."""

    item: str
    head: str                      # the head the verdict is still owed on
    now: str                       # the head the item has
    debtor: str                    # who owes the verdict on the old head
    row: str


def claims(text: str, items: Iterable[str]) -> List[Claim]:
    """``(item, head)`` pairs *text* claims, for the items in *items*.

    A bare SHA is not a claim: the swarm writes commit ids for all sorts of reasons, and a
    notice about the wrong one is noise. A SHA counts only when it sits beside the item in one
    of the shapes the swarm posts ("#173 is at 840f8b0", "pushed 840f8b0 to #173").
    """
    body = str(text or "")
    known = {f"#{str(item).strip().lstrip('#')}" for item in items or ()}
    if not body or not known:
        return []
    out: List[Claim] = []
    for match in _ITEM.finditer(body):
        item = f"#{match.group(1)}"
        if item not in known:
            continue
        # The clause around the item, up to a sentence end: a claim and its SHA are written
        # together, and a SHA in the next sentence belongs to whatever that one is about.
        start = max(0, body.rfind(".", 0, match.start()) + 1)
        end = len(body)
        for stop in (body.find(".", match.end()), body.find("\n", match.end())):
            if stop != -1:
                end = min(end, stop)
        clause = body[start:end]
        for sha in _SHA.finditer(clause):
            before = clause[:sha.start()].rstrip().lower()
            word = before.split()[-1] if before.split() else ""
            if word.endswith("@") or word.strip("@").rstrip(",") in _CLAIM_VERBS:
                out.append(Claim(item=item, head=sha.group(1), said=clause.strip()))
                break                      # one head per item per clause
    return out


def mismatches(found: Iterable[Claim], *, heads: Mapping[str, str]) -> List[Mismatch]:
    """The claims whose SHA is not the head the facts show.

    Heads are compared through :func:`swarmly.telegram.owed_wake.same_head`, so the short form
    a bot writes and the forty characters ``gh`` answers with are the same commit, and a claim
    with no fact behind it (an item no round state knows) is not called a contradiction.
    """
    out: List[Mismatch] = []
    for claim in found or ():
        actual = str((heads or {}).get(claim.item, "") or "").strip()
        if not actual or same_head(claim.head, actual):
            continue
        out.append(Mismatch(item=claim.item, claimed=claim.head, actual=actual))
    return out


def claim_notice(mismatch: Mismatch) -> str:
    """The one line a topic gets when a claimed head and the PR's head disagree."""
    from swarmly.telegram.owed_wake import _short
    return (f"⚑ {mismatch.item} is at {_short(mismatch.actual)}, not "
            f"{_short(mismatch.claimed)}: the PR's head decides, not the claim. The debt stays "
            f"open on the head the facts show.")


def rewrites(rows: Sequence[Row], *, heads: Mapping[str, str]) -> List[Rewrite]:
    """Verdicts still owed on a head its item no longer has: the branch moved under review.

    This is the shape that cost #173 a fix: a verdict was open on one head, the branch was
    rebuilt, and the review that was owed silently moved to code nobody had read yet. The rows
    already pin a verdict to a head (a new head opens new rows), so the flag is derivable from
    the table -- no extra book-keeping, and nothing here closes or moves a verdict.
    """
    out: List[Rewrite] = []
    for row in rows or ():
        if not row.is_open or row.kind != "verdict":
            continue
        actual = str((heads or {}).get(row.item, "") or "").strip()
        if not actual or not row.head or same_head(row.head, actual):
            continue
        out.append(Rewrite(item=row.item, head=row.head, now=actual, debtor=row.debtor,
                           row=row.id))
    return out


def rewrite_notice(rewrite: Rewrite) -> str:
    """The one line a rewritten head earns, addressed to the bot that owes the verdict."""
    from swarmly.telegram.owed_wake import _short
    return (f"⚑ {rewrite.item}'s head moved while a verdict was open: {rewrite.debtor} owes one "
            f"on {_short(rewrite.head)}, the PR is at {_short(rewrite.now)}. A rewrite under "
            f"review is a new review, not the old one moved -- nits go to follow-up issues.")


#: An item somebody calls ready: "#173 is ready", "#173 ready to merge", "ready: #173".
_READY = re.compile(r"#(\d+)[^\n.]{0,40}?\bready\b|\bready\b[^\n.]{0,20}?#(\d+)",
                    re.IGNORECASE)


def ready_claims(text: str, items: Iterable[str]) -> List[str]:
    """Items *text* calls ready, in the order they are named.

    "Ready" is a claim like a SHA is: #173 was called ready while it still conflicted with main,
    and the swarm moved on it. Only items a round state knows are returned -- a ready claim about
    something nobody tracks has no facts to check it against.
    """
    known = {f"#{str(item).strip().lstrip('#')}" for item in items or ()}
    out: List[str] = []
    for match in _READY.finditer(str(text or "")):
        item = f"#{match.group(1) or match.group(2)}"
        if item in known and item not in out:
            out.append(item)
    return out


def ready_blockers(item: Mapping[str, object], *, mergeable: Optional[str] = None,
                   heads: Optional[Mapping[str, str]] = None, item_key: str = "") -> List[str]:
    """Why this item is not ready to merge, for an item somebody has called ready.

    Two facts, and only the ones the caller has: ``mergeable`` from GitHub (``CONFLICTING`` is
    the case #173 hit when it was called ready while it still conflicted with main), and the
    head the item is on against the head any verdict was given on. An empty list means nothing
    the facts know about stands in the way -- never "it is ready".
    """
    label = item_key or str(item.get("key") or "")
    where = f"#{str(label).lstrip('#')}" if label else "the item"
    out: List[str] = []
    state = str(mergeable or item.get("mergeable") or "").strip().upper()
    if state == "CONFLICTING":
        out.append(f"{where} conflicts with main, so it is not ready whatever the message says")
    head = str(item.get("head") or "").strip()
    want = str((heads or {}).get(where, "") or "").strip()
    if head and want and not same_head(head, want):
        out.append(f"{where} is at {want[:7]}, not the head {head[:7]} the verdicts were given on")
    return out


def ready_notice(blockers: Sequence[str]) -> str:
    """One line for the first blocker -- a ready claim is answered with the reason it fails."""
    first = (blockers or [""])[0]
    return f"⚑ not ready: {first}." if first else ""


class FreezeSeen:
    """What has already been said, so a notice is said once and a restart does not repeat it.

    One small file beside the table: a rewrite sits in the table until a verdict lands, and a
    beat that said the same thing every minute would be the noise this item exists to remove.
    Entries age out: a notice about a head from a week ago is history, not a live flag.
    """

    #: How long a seen entry is remembered. A rewrite that is still open after this is news
    #: again -- the debt has outlived a week of nobody acting on it.
    KEEP_SECONDS = 7 * 24 * 3600

    def __init__(self, path: Optional[object] = None, *, clock=None):
        import time as _time
        self.path = path
        self.clock = clock or _time.time

    def _read(self) -> Dict[str, float]:
        if self.path is None:
            return {}
        import json
        from pathlib import Path
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, float] = {}
        for key, value in data.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def first(self, key: str, *, now: Optional[float] = None) -> bool:
        """Whether this is the first time *key* is said. Remembers it when it is."""
        moment = self.clock() if now is None else float(now)
        seen = self._read()
        seen = {name: at for name, at in seen.items() if moment - at < self.KEEP_SECONDS}
        if str(key) in seen:
            return False
        seen[str(key)] = moment
        if self.path is not None:
            import json
            from pathlib import Path
            try:
                path = Path(self.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(seen, sort_keys=True), encoding="utf-8")
            except OSError:
                pass                                  # said anyway; it may repeat, not vanish
        return True
