"""
Non-blocking OS file locks that end with the process holding them.

The poll and ``run`` lock markers are JSON files that say WHO holds a lock; this
module decides WHETHER a contender may take it. ``fcntl.flock`` (POSIX) and
``msvcrt.locking`` (Windows) are atomic and released by the kernel when the holder
exits, so no contender ever sees a half-written marker as stale and no crashed
holder blocks forever. The lock lives on a separate sidecar file because a Windows
byte-range lock would also block other processes from reading the marker.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Dict

__all__ = ["gate_path", "holds", "try_lock", "unlock"]

#: Sidecars this process holds, by absolute path. A second claim from the same process
#: is refused here: two channels in one process are contenders like any others.
_HELD: Dict[str, int] = {}
_GUARD = threading.Lock()

if sys.platform == "win32":
    import msvcrt

    def _lock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def gate_path(marker: Path) -> Path:
    """The sidecar that carries the OS lock for ``marker``. It is never deleted:
    removing a locked file would let a new contender lock a fresh inode."""
    return marker.with_name(marker.name + ".os")


def _key(path: Path) -> str:
    return os.path.abspath(str(path))


def try_lock(path: Path) -> bool:
    """Take the OS lock on ``path`` without waiting. False while anyone holds it,
    this process included. Raises OSError when the sidecar cannot be opened."""
    key = _key(path)
    with _GUARD:
        if key in _HELD:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            return False
        _HELD[key] = fd
        return True


def holds(path: Path) -> bool:
    """Whether this process holds the OS lock on ``path``."""
    with _GUARD:
        return _key(path) in _HELD


def unlock(path: Path) -> None:
    """Release the OS lock on ``path`` if this process holds it."""
    with _GUARD:
        fd = _HELD.pop(_key(path), None)
    if fd is None:
        return
    try:
        _unlock_fd(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
