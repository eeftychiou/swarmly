"""The coordinator lease on the shared state store (topic registry, phase 3).

``state.lease: store`` makes the store's ``lease`` key the coordinator lease, in place of the
gitbus claims in ``.agents/leases``. The transport's lease logic (``check_lease``: renew,
bootstrap, succession, the named coordinator's reclaim, ``lease_fence``) is unchanged: it
talks to :class:`StoreLease`, which has the same small interface as
:class:`~swarmly.telegram.lease.LeaseStore` -- ``refresh``, ``state``, ``claim``,
``yield_lease``, ``ttl_seconds`` -- and is called from worker threads exactly as that one is.

What changes is where the decision is made. The fold (``swarmly/state/log.py``) gives one
winner per term by message order, judges expiry by Telegram's clock, and fences every
lease-holder write by term. Times cross the boundary converted: the fold keeps server time,
the transport compares against its own clock, so ``state()`` reports ``expires_at`` in the
bot's own clock (server expiry minus the offset the store measures).

``state.lease: shadow`` (the default once the store is on) keeps gitbus authoritative and only
compares the two leases; ``/state`` says whether they agree.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from swarmly.telegram.lease import LeaseError, LeaseState

logger = logging.getLogger("swarmly.telegram.store_lease")

#: A store call from a worker thread waits at most this long for the event loop.
CALL_SECONDS = 20.0


@dataclass
class StoreClaim:
    """What ``check_lease`` reads off a claim: its term, holder, renewal and expiry times."""

    term: int
    alias: str
    expires_at: float
    renews_at: float = 0.0
    nonce: str = ""
    yielded: bool = False


class StoreLease:
    """The lease interface of ``LeaseStore``, backed by the shared state store."""

    def __init__(self, store: Any, loop: asyncio.AbstractEventLoop, *, alias: str,
                 ttl_seconds: float = 600.0, clock: Any = None) -> None:
        self.store = store
        self.loop = loop
        self.alias = alias
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock or store.clock
        self.old_releases: tuple = ()

    # -- calls into the loop ---------------------------------------------------------------
    def _run(self, coro: Any) -> Any:
        """Run a store coroutine from a worker thread (or directly, in a test with no loop)."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            raise LeaseError("StoreLease is called from worker threads, not the event loop")
        if running is None and not self.loop.is_running():
            return self.loop.run_until_complete(coro)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(CALL_SECONDS)

    def _offset(self) -> float:
        """Server time minus this bot's clock, as the store measures it."""
        return float(self.store.server_now()) - float(self.store.clock())

    # -- the LeaseStore interface ----------------------------------------------------------
    def refresh(self, *, force: bool = False) -> bool:
        if not getattr(self.store, "ready", False):
            return False
        try:
            self._run(self.store.refresh())
            return True
        except Exception:                              # noqa: BLE001 - no answer, no authority
            logger.warning("store lease: refresh failed", exc_info=True)
            return False

    def state(self, *, now: Optional[float] = None) -> LeaseState:
        if not getattr(self.store, "ready", False):
            return LeaseState(status="unreadable", term=0, holder="", expires_at=0.0)
        lease = self.store.lease()
        if lease.term <= 0:
            return LeaseState(status="absent", term=0, holder="", expires_at=0.0)
        entry = self.store.log.entries.get("lease")
        renewed = float(entry.at) if entry is not None else 0.0
        offset = self._offset()
        local_expiry = float(lease.expires_at) - offset
        claim = StoreClaim(term=lease.term, alias=str(lease.holder or ""),
                           expires_at=local_expiry, renews_at=renewed - offset,
                           yielded=not lease.holder)
        return LeaseState(status="ok", term=lease.term, holder=str(lease.holder or ""),
                          expires_at=local_expiry if lease.holder else 0.0, claim=claim)

    def claim(self, *, term: Optional[int] = None, now: Optional[float] = None,
              promoted: bool = False) -> StoreClaim:
        current = self.state(now=now)
        if current.status == "unreadable":
            raise LeaseError("the store lease could not be read; refusing to claim blind")
        wanted = current.term if term is None else int(term)
        if wanted < current.term:
            raise LeaseError(f"term {wanted} is behind {current.term}; a lease never goes back")
        value = {"holder": self.store.alias, "term": max(wanted, 1), "ttl": self.ttl_seconds}
        if promoted:
            value["promoted"] = True
        result = self._run(self.store.write("lease", value, confirm=True))
        if not result.applied:
            raise LeaseError(f"the store refused the claim for term {value['term']}: "
                             f"{result.reason}")
        fresh = self.state()
        return fresh.claim or StoreClaim(term=value["term"], alias=self.store.alias,
                                         expires_at=fresh.expires_at)

    def yield_lease(self, *, reason: str = "", now: Optional[float] = None,
                    term: Optional[int] = None) -> None:
        current = self.state(now=now)
        wanted = current.term if term is None else int(term)
        if current.holder.strip().lower() != self.store.alias or wanted != current.term:
            return                                     # nothing of ours to give up
        result = self._run(self.store.write("lease", {"holder": None, "term": wanted}))
        if not result.applied:
            raise LeaseError(f"the store refused the yield of term {wanted}: {result.reason}")
        logger.info("store lease: yielded term %d (%s)", wanted, reason or "no reason given")
