"""The coordinator lease (issue #83): who leads, at which term, decided by the bus.

Exactly one bot may merge, keep the tracking checklist, wake the round report and poll GitHub. If
that bot dies the swarm has to keep going, and if it comes back the swarm must not end up with two
of them. That is a distributed lock, so it is built the way the rest of this round was: as facts on
the bus that no writer can forge and no slow push can backdate.

**Append-only, one file per renewal.** ``.agents/leases/<term>-<alias>-<nonce>.json``. A single
mutable lease file would have the last-writer-wins race that #92 spent ten heads removing, and a
renewal that loses a push race would silently overwrite the holder it never saw.

**Folded, not read.** Every bot folds the claims: the highest term wins, ties are broken by
succession rank and then alias, and claims are ordered by the bus's own arrival order
(:meth:`GitTransport.arrival_rank`). A claim the bus can rank but cannot place is skipped, exactly
as a wake claim is — falling back to anything the writer says about itself would hand the ordering
back to the writer.

**Liveness is a lease, not a guess.** A claim carries ``expires_at``; a holder that stops renewing
loses it, and ``limits.takeover_minutes`` decides how many renewals may be missed before the
succession happens. A bot that is FROZEN or posted OFFLINE yields on resume instead of racing.

**No authority means peer.** If nothing can be read, or the lease has expired and nobody claimed
the next term, this bot is a peer: no GitHub polling, no round writes. Saying so is part of the
contract, so :meth:`LeaseState.describe` always answers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from swarmly.transports.base import LEASES_RELPATH, is_bus_document

logger = logging.getLogger("swarmly.telegram.lease")

#: One claim per renewal: `<term>-<alias>-<nonce>.json`.
_CLAIM = re.compile(r"^(?P<term>\d+)-(?P<alias>[A-Za-z0-9_]+)-(?P<nonce>[A-Za-z0-9]+)\.json$")


class LeaseError(ValueError):
    """A lease claim, or a fold of them, that cannot be used."""


class LeasePushError(LeaseError):
    """The claim was written here but the bus did not take it."""


@dataclass(frozen=True)
class LeaseClaim:
    """One bot's claim to the role, for one term, from one moment."""

    term: int
    alias: str
    expires_at: float
    renews_at: float = 0.0
    #: Succession rank at the time of the claim (lower is earlier in the order).
    rank: int = 0
    #: Where the claim sits in the bus's history: higher means it arrived earlier.
    arrival: int = -1
    nonce: str = ""
    #: The holder gave the role up (FROZEN, OFFLINE, superseded by `/role`): the lease is over
    #: at this term, without waiting for it to lapse.
    yielded: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"term": self.term, "alias": self.alias, "expires_at": self.expires_at,
                "renews_at": self.renews_at, "rank": self.rank, "nonce": self.nonce,
                "yielded": self.yielded}

    @classmethod
    def from_dict(cls, data: Any) -> "LeaseClaim":
        if not isinstance(data, dict):
            raise LeaseError(f"a lease claim must be an object, got {type(data).__name__}")
        term = data.get("term")
        alias = data.get("alias")
        expires = data.get("expires_at")
        if not isinstance(term, int) or isinstance(term, bool) or term < 0:
            raise LeaseError(f"a lease claim needs an integer term (got {term!r})")
        if not isinstance(alias, str) or not alias.strip():
            raise LeaseError("a lease claim needs an alias")
        if not isinstance(expires, (int, float)) or isinstance(expires, bool):
            raise LeaseError("a lease claim needs expires_at (epoch seconds)")
        return cls(term=term, alias=alias.strip(), expires_at=float(expires),
                   renews_at=float(data.get("renews_at") or 0.0),
                   rank=int(data.get("rank") or 0), nonce=str(data.get("nonce") or ""),
                   yielded=bool(data.get("yielded")))

    def sort_key(self) -> tuple:
        """The claim the bus saw first wins. Rank and alias only break a tie.

        Ordering by rank first let a better-ranked claimant write term N+1 *after* another bot
        had already acted and still win the fold -- two acting coordinators, from nothing but
        arriving late with a nicer role (#93 B3a).
        """
        return (-self.arrival, self.rank, self.alias.lower(), self.nonce)


class LeaseState(NamedTuple):
    """The folded lease: who holds the role, at which term, until when."""

    #: ok | absent | unreadable — never treat anything but `ok` as authority.
    status: str
    term: int
    holder: str
    expires_at: float
    claim: Optional[LeaseClaim] = None
    #: Claims the fold refused (unranked, malformed, or losing a tie), for /status and tests.
    ignored: tuple = ()

    @property
    def usable(self) -> bool:
        return self.status == "ok" and bool(self.holder)

    def live(self, now: float) -> bool:
        return self.usable and self.expires_at > now

    def holds(self, alias: str, now: float) -> bool:
        """Whether *alias* holds the role right now."""
        return self.live(now) and self.holder.strip().lower() == alias.strip().lower()

    def describe(self, now: Optional[float] = None) -> str:
        moment = time.time() if now is None else now
        if self.status == "unreadable":
            return "coordinator=none(unverifiable)"
        if self.status == "absent" or not self.holder:
            return "coordinator=none(no lease)"
        mark = "" if self.live(moment) else ", expired"
        return f"coordinator={self.holder}(term {self.term}{mark})"


def fold_claims(claims: Iterable[LeaseClaim]) -> LeaseState:
    """The honouring claim: the highest term, ties by succession rank then alias."""
    rows = [claim for claim in claims if claim.term >= 1]
    if not rows:
        return LeaseState(status="absent", term=0, holder="", expires_at=0.0)
    top = max(claim.term for claim in rows)
    same_term = sorted((claim for claim in rows if claim.term == top), key=LeaseClaim.sort_key)
    winner = same_term[0]
    # The holder's expiry is what its *latest* renewal says, and a yield ends the lease outright:
    # taking the maximum would let an earlier renewal outlive the claim that gave it up.
    mine = [claim for claim in same_term if claim.alias == winner.alias]
    if any(claim.yielded for claim in mine):
        return LeaseState(status="ok", term=top, holder=winner.alias, expires_at=0.0,
                          claim=winner, ignored=tuple(c.nonce for c in same_term[1:]))
    latest = max(mine, key=lambda claim: (claim.renews_at, claim.nonce))
    return LeaseState(status="ok", term=top, holder=winner.alias,
                      expires_at=latest.expires_at, claim=latest,
                      ignored=tuple(claim.nonce for claim in same_term[1:]))


def succession_order(role: str, *, order: Iterable[str] = ()) -> int:
    """A bot's place in the succession: the configured order, else the default by role."""
    names = [str(name).strip().lower() for name in order if str(name).strip()]
    if not names:
        names = list(DEFAULT_SUCCESSION)
    try:
        return names.index(str(role).strip().lower())
    except ValueError:
        return len(names)


#: Reviewer, then QA, then developer: the roles that can act without reviewing their own work
#: first, the ones that cannot last.
DEFAULT_SUCCESSION = ("reviewer", "qa-tester", "developer")


_PARSED_CLAIMS_CACHE: Dict[Tuple[str, str], Tuple[Optional[int], LeaseClaim]] = {}


class LeaseStore:
    """Reads and appends coordinator lease claims on a bus transport."""

    def __init__(self, alias: str, *, transport: Any = None,
                 root: Optional[Path] = None, clock: Any = None,
                 ttl_seconds: float = 600.0, rank: int = 99):
        self.alias = alias
        safe = re.sub(r"[^a-z0-9_]", "", alias.strip().lower()) or "agent"
        self.safe_alias = safe
        self.transport = transport
        self.root = Path(root or getattr(transport, "root", None) or ".")
        self.clock = clock or time.time
        self.ttl_seconds = float(ttl_seconds)
        self.rank = int(rank)
        #: When the bus ref was last fetched, so a fold does not pay for a fetch each time.
        self._fetched_at: float = -1e9
        self._fetch_interval = 5.0

    # -- reading ---------------------------------------------------------------------
    def refresh(self, *, force: bool = False) -> bool:
        """Fetch the bus ref, so a claim written a moment ago by a peer is visible.

        Without it a fold reads the ref as of the last poll: this bot's own claim after a push,
        and a peer's claim from a minute ago, would both be invisible. It is rate-limited because
        a fork costs a subprocess, and forced after a write so the next read sees our own claim.
        """
        now = self.clock()
        if not force and (now - self._fetched_at) < self._fetch_interval:
            return True
        self._fetched_at = now
        transport = self.transport
        if transport is None or not getattr(transport, "ref", None):
            return True
        if not hasattr(transport, "refresh"):
            return True
        try:
            if transport.refresh() is False:
                return False
        except Exception:                              # noqa: BLE001 - a failed fetch is a no
            logger.warning("lease: the bus ref could not be refreshed", exc_info=True)
            return False
        return True

    def _names(self) -> Optional[List[str]]:
        transport = self.transport
        lister = getattr(transport, "list_at_ref", None)
        if lister is not None:
            try:
                return lister(LEASES_RELPATH)
            except Exception:                          # noqa: BLE001 - reported, not swallowed
                logger.warning("lease: the claim list could not be read", exc_info=True)
                return None
        folder = self.root / LEASES_RELPATH
        if not folder.is_dir():
            return []
        try:
            return sorted(p.name for p in folder.iterdir() if p.is_file())
        except Exception:                              # noqa: BLE001 - reported, not swallowed
            logger.warning("lease: the local claim directory could not be read", exc_info=True)
            return None

    def claims(self) -> Optional[List[LeaseClaim]]:
        """Every usable claim, or ``None`` when the bus could not be read."""
        self.refresh()

        transport = self.transport
        # Key the fold on the ref tip SHA: on an unchanged tip return the cached state (Reasonix B4).
        tip_sha = None
        if transport is not None and getattr(transport, "ref", None) and hasattr(transport, "_git"):
            try:
                proc = transport._git("rev-parse", "--verify", "--quiet", transport.ref)
                if proc.returncode == 0:
                    tip_sha = proc.stdout.strip()
            except Exception:
                pass
        if tip_sha and getattr(self, "_cached_ref_tip", None) == tip_sha:
            return list(getattr(self, "_cached_claims", []))

        names = self._names()
        if names is None:
            return None

        names_tuple = tuple(names)
        if getattr(self, "_cached_names", None) == names_tuple:
            return list(getattr(self, "_cached_claims", []))

        reader = getattr(transport, "read_at_ref", None)
        ranker = getattr(transport, "arrival_rank", None)

        # B4 (#93): only the claims at the highest term can win. A lease never goes back a term
        # and a holder's renewals stay at its term, so on a bus with hundreds of claims the fold
        # reads the handful at the top rather than one file (plus two git calls for the rank) per
        # claim ever written. The caches above stay: this trims what they are asked about.
        named = [(_CLAIM.match(name), name) for name in names
                 if name.endswith(".json") and "/" not in name and _CLAIM.match(name) is not None]
        if not named:
            return []
        top_term = max(int(match.group("term")) for match, _name in named)
        top_names = [name for match, name in named if int(match.group("term")) == top_term]

        claims_list: List[LeaseClaim] = []
        for name in top_names:
            if not name.endswith(".json") or "/" in name:
                continue
            match = _CLAIM.match(name)
            if match is None:
                continue
            relpath = f"{LEASES_RELPATH}/{name}"
            cache_key = (str(self.root), relpath)

            cached = _PARSED_CLAIMS_CACHE.get(cache_key)
            if cached is not None:
                claims_list.append(cached[1])
                continue

            arrival = None
            if ranker is not None:
                try:
                    arrival = ranker(relpath)
                except Exception:                      # noqa: BLE001 - unranked: not a claim
                    logger.warning("lease: %s could not be ranked; not counted", name,
                                   exc_info=True)
                    continue
                if arrival is None:
                    logger.warning("lease: %s has no place in the bus history; not counted", name)
                    continue

            text = None
            if reader is not None:
                text = reader(relpath)
            else:
                path = self.root / relpath
                text = path.read_text(encoding="utf-8") if path.is_file() else None
            if not text:
                continue
            try:
                parsed = LeaseClaim.from_dict(json.loads(text))
            except (ValueError, TypeError) as exc:
                logger.warning("lease: %s is not usable: %s", name, exc)
                continue

            claim = LeaseClaim(term=parsed.term, alias=parsed.alias,
                               expires_at=parsed.expires_at, renews_at=parsed.renews_at,
                               rank=parsed.rank, arrival=int(arrival if arrival is not None else -1),
                               nonce=match.group("nonce"), yielded=parsed.yielded)
            _PARSED_CLAIMS_CACHE[cache_key] = (arrival, claim)
            claims_list.append(claim)

        # B5/B4: Prune superseded renewals in-memory without repetitive git subprocess reads.
        by_key: Dict[Tuple[int, str], List[LeaseClaim]] = {}
        for claim in claims_list:
            by_key.setdefault((claim.term, claim.alias.lower()), []).append(claim)

        filtered: List[LeaseClaim] = []
        for group in by_key.values():
            if len(group) == 1:
                filtered.append(group[0])
                continue
            yields = [c for c in group if c.yielded]
            filtered.extend(yields)
            non_yields = [c for c in group if not c.yielded]
            if non_yields:
                best = max(non_yields, key=lambda c: (c.renews_at, c.nonce))
                filtered.append(best)

        if tip_sha:
            self._cached_ref_tip = tip_sha
        self._cached_names = names_tuple
        self._cached_claims = list(filtered)
        return filtered

    def state(self, *, now: Optional[float] = None) -> LeaseState:
        """The folded lease. ``unreadable`` is never ``absent``: one is a broken bus."""
        rows = self.claims()
        if rows is None:
            return LeaseState(status="unreadable", term=0, holder="", expires_at=0.0)
        return fold_claims(rows)

    # -- writing ---------------------------------------------------------------------
    def claim(self, *, term: Optional[int] = None, now: Optional[float] = None,
              promoted: bool = False) -> LeaseClaim:
        """Claim (or renew) the role for *term*, and return the claim that was written.

        ``term`` defaults to the current term, so a renewal keeps it; a takeover passes the next
        one. The claim is appended — never rewritten — and pushed once.
        """
        current = self.state(now=now)
        if current.status == "unreadable":
            raise LeaseError(f"the lease could not be read; refusing to claim blind "
                             f"({current.status})")
        wanted = current.term if term is None else int(term)
        stamp = self.clock() if now is None else now
        if wanted < current.term:
            raise LeaseError(f"term {wanted} is behind {current.term}; a lease never goes back")
        if wanted > current.term and current.live(stamp) and not promoted:
            # Anyone could otherwise write N+1 and usurp a coordinator that is alive and renewing:
            # a higher term needs a lapsed lease, or the admin's explicit promotion (#93 B3).
            raise LeaseError(
                f"term {wanted} over a live lease held by {current.holder} (term {current.term}); "
                "wait for it to lapse or take the role through /role")
        stamp = self.clock() if now is None else now
        if current.live(stamp):
            if wanted == current.term and current.holder.strip().lower() != self.alias.strip().lower():
                raise LeaseError(
                    f"lease at term {current.term} is currently held by {current.holder}; "
                    f"{self.alias} cannot claim at the same term")
        row = LeaseClaim(term=max(wanted, 1), alias=self.alias,
                         expires_at=stamp + self.ttl_seconds, renews_at=stamp, rank=self.rank,
                         nonce=uuid.uuid4().hex[:8])
        path = self.root / LEASES_RELPATH
        path.mkdir(parents=True, exist_ok=True)
        name = f"{row.term:06d}-{self.safe_alias}-{row.nonce}.json"
        relpath = f"{LEASES_RELPATH}/{name}"
        if not is_bus_document(relpath):
            raise LeaseError(f"refusing to write outside {LEASES_RELPATH}: {relpath!r}")
        (path / name).write_text(json.dumps(row.to_dict(), sort_keys=True, indent=1),
                                 encoding="utf-8")
        transport = self.transport
        if transport is not None and hasattr(transport, "commit_and_push"):
            try:
                transport.commit_and_push(f"leases: term {row.term} {self.alias}", paths=[relpath])
            except Exception as exc:                   # noqa: BLE001 - reported, not swallowed
                logger.warning("lease: %s was written but not pushed: %s", relpath, exc)
                raise LeasePushError(f"the lease claim is local only: {exc}") from exc
        self.refresh(force=True)      # our own claim has to be visible to the next fold
        return row

    def yield_lease(self, *, reason: str = "", now: Optional[float] = None) -> None:
        """Give the role up: a claim at the current term with an expiry in the past.

        A bot that is FROZEN, posted OFFLINE or superseded by ``/role`` writes this, so its
        successor does not have to wait for the lease to lapse by itself.
        """
        stamp = self.clock() if now is None else now
        current = self.state(now=stamp)
        row = LeaseClaim(term=max(current.term, 1), alias=self.alias, expires_at=stamp,
                         renews_at=stamp, rank=self.rank, nonce=uuid.uuid4().hex[:8],
                         yielded=True)
        path = self.root / LEASES_RELPATH
        path.mkdir(parents=True, exist_ok=True)
        name = f"{row.term:06d}-{self.safe_alias}-{row.nonce}.json"
        relpath = f"{LEASES_RELPATH}/{name}"
        if not is_bus_document(relpath):
            raise LeaseError(f"refusing to write outside {LEASES_RELPATH}: {relpath!r}")
        (path / name).write_text(json.dumps(row.to_dict(), sort_keys=True, indent=1),
                                 encoding="utf-8")
        transport = self.transport
        if transport is not None and hasattr(transport, "commit_and_push"):
            try:
                transport.commit_and_push(f"leases: term {row.term} {self.alias} yields "
                                          f"({reason or 'no reason given'})", paths=[relpath])
            except Exception as exc:                   # noqa: BLE001 - reported
                logger.warning("lease: %s was written but not pushed: %s", relpath, exc)
                raise LeasePushError(f"the yield is local only: {exc}") from exc
        self.refresh(force=True)


def lease_dir_for(config: Any) -> Optional[Path]:
    """The state dir beside the gitbus ledger, where a bot keeps its own lease cache."""
    from swarmly.telegram.poll import transport_state_root
    try:
        return transport_state_root(config) / ".agents" / "state"
    except Exception:                                  # noqa: BLE001 - optional
        return None
