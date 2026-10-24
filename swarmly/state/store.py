"""The ``StateStore`` API, and the backends that need no Telegram: memory and a folder.

A store is a :class:`~swarmly.state.log.StateLog` plus a way to append to, and read from, one
shared log. Every backend gives the log a total order (``Op.pos``) and a server-ish time
(``Op.at``); the fold does the rest, identically everywhere.

The API, the same for every backend:

* ``read(key)`` / ``version(key)`` / ``keys(prefix)`` -- from the local fold, no I/O;
* ``await refresh()`` -- take in what other bots wrote;
* ``await write(key, value)`` -- a compare-and-set against the version this bot last saw;
  returns a :class:`WriteResult`. ``confirm=True`` waits until the backend can say the write
  kept its place (on Telegram a competing line may still arrive with a lower message id);
* ``await claim_lease(ttl)`` / ``renew_lease`` / ``release_lease`` -- the coordinator lease;
* ``state_hash()`` -- the same for every bot that has the same state.

A bot may write only keys it owns (``keys.py``); ``write`` refuses others before anything is
sent, and the fold would ignore them anyway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from swarmly.state import keys as K
from swarmly.state.log import Lease, Op, StateLog

logger = logging.getLogger("swarmly.state")


class StoreError(RuntimeError):
    """The store could not be read or written (fail closed: act on nothing)."""


@dataclass
class WriteResult:
    applied: bool
    key: str
    pos: Optional[int] = None
    version: int = 0
    reason: str = ""


class StateStore:
    """Shared behaviour; a backend implements ``_append``, ``_pull`` and, if it has
    snapshots, ``_load_snapshot``/``publish_snapshot``."""

    #: How long ``confirm=True`` waits for a competing write to show up.
    CONFIRM_SECONDS = 0.0

    def __init__(self, alias: str, *, clock: Any = time.time,
                 cache: Optional[Path] = None) -> None:
        self.alias = K.norm(alias)
        self.clock = clock
        self.log = StateLog()
        self.cache = Path(cache) if cache else None
        self.ready = False

    # -- reading ---------------------------------------------------------------------------
    def read(self, key: str) -> Optional[Dict[str, Any]]:
        return self.log.value(key)

    def version(self, key: str) -> int:
        return self.log.version(key)

    def keys(self, prefix: str = "") -> List[str]:
        return self.log.keys(prefix)

    def lease(self) -> Lease:
        return self.log.lease()

    def holder(self, now: Optional[float] = None) -> Optional[str]:
        return self.lease().held_by(self.clock() if now is None else now)

    def holds_lease(self, now: Optional[float] = None) -> bool:
        return K.norm(self.holder(now) or "") == self.alias

    def state_hash(self) -> str:
        return self.log.state_hash()

    # -- starting --------------------------------------------------------------------------
    async def start(self) -> None:
        """Local cache first (fast), then the shared snapshot and log (truth). Until this has
        succeeded the store is not ``ready`` and the caller must act on nothing."""
        cached = self._read_cache()
        if cached is not None:
            try:
                self.log = StateLog.load(cached)
            except (ValueError, KeyError, TypeError):
                logger.warning("state: the local cache is unreadable; ignoring it")
        await self._load_snapshot()
        await self.refresh()
        self.ready = True

    async def refresh(self) -> None:
        ops = await self._pull()
        self.log.add_all(ops)
        if self.log.gaps:
            # Missed writes: the snapshot has them (on Telegram, updates older than a day are
            # gone; the pin never is).
            logger.info("state: missed writes for %s; reading the snapshot again",
                        ", ".join(sorted(self.log.gaps)))
            await self._load_snapshot()
            self.log.add_all(await self._pull())
        self._write_cache()

    # -- writing ---------------------------------------------------------------------------
    async def write(self, key: str, value: Optional[Dict[str, Any]], *,
                    base: Optional[int] = None, confirm: bool = False) -> WriteResult:
        if not K.valid(key):
            return WriteResult(False, key, reason="unknown key")
        lease = self.lease()
        holder = lease.held_by(self.clock())
        if not K.may_write(key, self.alias, holder):
            return WriteResult(False, key, reason=f"{self.alias} may not write {key}")
        term = lease.term if K.norm(holder or "") == self.alias else 0
        if key == K.LEASE and value is not None:
            term = int(value.get("term") or term)
        return await self._send(key, value, base=base, term=term, confirm=confirm)

    async def _send(self, key: str, value: Optional[Dict[str, Any]], *, base: Optional[int],
                    term: int, confirm: bool) -> WriteResult:
        if base is None:
            await self.refresh()                       # fresh before every write (§1b)
            base = self.version(key)
        wire = {"k": key, "b": int(base), "t": term, "w": self.alias, "v": value}
        pos, at = await self._append(wire)
        op = Op.from_wire(wire, pos=pos, at=at, writer=self.alias)
        self.log.add(op)
        if confirm and self.CONFIRM_SECONDS > 0:
            await asyncio.sleep(self.CONFIRM_SECONDS)
        await self.refresh()                           # whatever raced us and is already here
        applied = pos not in self.log.rejected
        self._write_cache()
        return WriteResult(applied, key, pos=pos, version=self.version(key),
                           reason=self.log.rejected.get(pos, ""))

    async def claim_lease(self, ttl: float, *, confirm: bool = True) -> WriteResult:
        lease = self.lease()
        mine = K.norm(lease.holder or "") == self.alias
        live = lease.held_by(self.clock()) is not None
        term = lease.term if (mine and live) else lease.term + 1
        return await self.write(K.LEASE, {"holder": self.alias, "term": term, "ttl": ttl},
                                confirm=confirm)

    async def release_lease(self) -> WriteResult:
        lease = self.lease()
        return await self.write(K.LEASE, {"holder": None, "term": lease.term})

    # -- the cache -------------------------------------------------------------------------
    def _read_cache(self) -> Optional[Dict[str, Any]]:
        if self.cache is None or not self.cache.exists():
            return None
        try:
            data = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_cache(self) -> None:
        if self.cache is None:
            return
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps(self.log.dump(), separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, self.cache)
        except OSError:
            logger.debug("state: cache not written", exc_info=True)

    # -- backend hooks ---------------------------------------------------------------------
    async def _append(self, wire: Dict[str, Any]) -> Tuple[int, float]:
        raise NotImplementedError

    async def _pull(self) -> List[Op]:
        return []

    async def _load_snapshot(self) -> None:
        return None

    async def publish_snapshot(self) -> bool:
        return False


class MemoryLog:
    """One shared in-process log: the reference backend for tests and for a single host."""

    def __init__(self, clock: Any = time.time) -> None:
        self.clock = clock
        self.ops: List[Tuple[int, float, Dict[str, Any]]] = []
        self.snapshot: Optional[Dict[str, Any]] = None
        self.down = False

    def append(self, wire: Dict[str, Any]) -> Tuple[int, float]:
        if self.down:
            raise StoreError("the shared log is unreachable")
        pos = (self.ops[-1][0] if self.ops else 0) + 1
        at = float(self.clock())
        self.ops.append((pos, at, json.loads(json.dumps(wire))))
        return pos, at


class MemoryStore(StateStore):
    def __init__(self, alias: str, shared: MemoryLog, **kwargs: Any) -> None:
        super().__init__(alias, clock=kwargs.pop("clock", shared.clock), **kwargs)
        self.shared = shared
        self._seen = 0

    async def _append(self, wire: Dict[str, Any]) -> Tuple[int, float]:
        return self.shared.append(wire)

    async def _pull(self) -> List[Op]:
        if self.shared.down:
            raise StoreError("the shared log is unreachable")
        fresh = [Op.from_wire(w, pos=p, at=a) for p, a, w in self.shared.ops if p > self._seen]
        if fresh:
            self._seen = fresh[-1].pos
        return fresh

    async def _load_snapshot(self) -> None:
        if self.shared.snapshot:
            self.log.rebase(self.shared.snapshot)
            self._seen = max(self._seen, self.log.base_pos)

    async def publish_snapshot(self) -> bool:
        if not self.holds_lease():
            return False
        self.shared.snapshot = self.log.snapshot()
        return True


class DirStore(StateStore):
    """A folder as the log: ``ops/<pos>.json`` created exclusively, so positions are unique.

    For bots on one machine, or a network share where an exclusive create is atomic (SMB,
    NFS). A sync service (Drive, Dropbox) does not make a create exclusive across machines,
    so it cannot give a total order: use the Telegram store there.
    """

    def __init__(self, alias: str, root: Path, **kwargs: Any) -> None:
        super().__init__(alias, **kwargs)
        self.root = Path(root)
        self.ops_dir = self.root / "ops"
        self._seen = 0

    async def _append(self, wire: Dict[str, Any]) -> Tuple[int, float]:
        return await asyncio.to_thread(self._append_sync, wire)

    def _append_sync(self, wire: Dict[str, Any]) -> Tuple[int, float]:
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        at = float(self.clock())
        body = json.dumps({**wire, "at": at}, separators=(",", ":")).encode("utf-8")
        pos = self._last_on_disk() + 1
        for _ in range(1000):
            try:
                fd = os.open(self.ops_dir / f"{pos:012d}.json",
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                pos += 1
                continue
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            return pos, at
        raise StoreError("no free position in the op folder")

    def _last_on_disk(self) -> int:
        names = [p.stem for p in self.ops_dir.glob("*.json")] if self.ops_dir.exists() else []
        return max((int(n) for n in names if n.isdigit()), default=0)

    async def _pull(self) -> List[Op]:
        return await asyncio.to_thread(self._pull_sync)

    def _pull_sync(self) -> List[Op]:
        if not self.ops_dir.exists():
            return []
        out = []
        for path in sorted(self.ops_dir.glob("*.json")):
            if not path.stem.isdigit() or int(path.stem) <= self._seen:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break                               # still being written: next refresh
            out.append(Op.from_wire(data, pos=int(path.stem), at=float(data.get("at") or 0)))
        if out:
            self._seen = out[-1].pos
        return out

    async def _load_snapshot(self) -> None:
        path = self.root / "snapshot.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.log.rebase(data)
        self._seen = max(self._seen, self.log.base_pos)

    async def publish_snapshot(self) -> bool:
        if not self.holds_lease():
            return False
        path = self.root / "snapshot.json"
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(self.log.snapshot(), separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
