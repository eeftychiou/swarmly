"""Child CLIs this transport started, and the reaper for the ones it left behind (issue #68).

A turn runs a CLI (`agy`, `codex`, `claude`, …) as a child process. When the transport is
*killed* rather than stopped — a crash, a `kill -9`, a power cut, a Windows service restart —
those children keep running: they hold the model session, edit files, and answer nothing,
because the transport that would read their output is gone. A restart then starts a second
CLI on the same conversation.

So each child's pid is recorded, together with the identity of both processes (their start
times, as the OS reports them). On start the transport reads that file and ends the children
whose **parent transport is dead** — never a live transport's children, and never a pid whose
start time no longer matches the one recorded (the pid was reused by an unrelated process,
and killing it would kill something innocent).

Identity is compared as an opaque string per platform: Linux reads `/proc/<pid>/stat`, other
POSIX hosts `ps -o lstart=`, Windows asks PowerShell for `StartTime`. When the identity cannot
be read, nothing is killed: a wrong kill is worse than an orphan.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger("swarmly.telegram.children")

#: A `ps`/PowerShell answer is a local read; it must never hang a transport start.
IDENTITY_TIMEOUT_SECONDS = 5.0

def _run_quiet(argv: Sequence[str], timeout: float = IDENTITY_TIMEOUT_SECONDS) -> str:
    """A local process query: its stdout, or "" when it fails for any reason."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def process_identity(pid: int) -> Optional[str]:
    """The process's start time as an opaque string, or None when it cannot be read."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "win32":
        # PowerShell is present on every supported Windows; wmic is not (it was removed in
        # recent builds), so StartTime -- in 100-ns ticks -- is the identity we can read.
        out = _run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"(Get-Process -Id {int(pid)} -ErrorAction Stop).StartTime.Ticks"])
        return out or None
    stat = Path(f"/proc/{int(pid)}/stat")
    try:
        text = stat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if text:
        # The 22nd field is starttime (clock ticks since boot); field 2 is parenthesised and
        # may contain spaces, so count from the last ')'.
        tail = text[text.rfind(")") + 2:].split()
        if len(tail) > 19:
            return f"proc-start={tail[19]}"
    out = _run_quiet(["ps", "-o", "lstart=", "-p", str(int(pid))])
    return out or None


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists (zombies count as gone)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a probe on Windows: it can terminate the process.
        out = _run_quiet(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"])
        return str(int(pid)) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # it exists, we just may not signal it
    except OSError:
        return False
    return True


def kill_pid(pid: int) -> None:
    """End a process and its children: the tree on Windows, the group on POSIX."""
    if not isinstance(pid, int) or pid <= 0:
        return
    if sys.platform == "win32":
        killer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                              "taskkill.exe")
        _run_quiet([killer, "/PID", str(int(pid)), "/T", "/F"])
        return
    try:
        if os.getpgid(pid) == pid:      # its own group (start_new_session)
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        logger.warning("could not end pid %s: it is gone or not ours", pid)


@dataclass
class Child:
    """One recorded child CLI process."""

    pid: int
    transport_pid: int
    transport_identity: Optional[str]     # the transport's start time when it spawned us
    identity: Optional[str]               # the child's own start time
    command: str = ""
    recorded: float = 0.0

    def as_json(self) -> Dict[str, Any]:
        return {"pid": self.pid, "transport_pid": self.transport_pid,
                "transport_identity": self.transport_identity, "identity": self.identity,
                "command": self.command, "recorded": self.recorded}

    @classmethod
    def from_json(cls, data: Any) -> Optional["Child"]:
        if not isinstance(data, dict):
            return None
        try:
            pid = int(data.get("pid"))
            transport_pid = int(data.get("transport_pid"))
        except (TypeError, ValueError):
            return None
        identity = data.get("identity")
        transport_identity = data.get("transport_identity")
        try:
            recorded = float(data.get("recorded") or 0.0)
        except (TypeError, ValueError):
            recorded = 0.0
        return cls(pid=pid, transport_pid=transport_pid,
                   transport_identity=str(transport_identity) if transport_identity else None,
                   identity=str(identity) if identity else None,
                   command=str(data.get("command") or "")[:200], recorded=recorded)


def _usable_pid(pid: Any) -> bool:
    """A pid we can record or drop: a positive int, never a bool."""
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


class ChildRegistry:
    """The child pids one transport started, in its state directory.

    Reads and writes are whole-file: the file is small, and a half-written one must never
    make the next start reap the wrong processes (an unreadable file is treated as empty).
    """

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path

    def load(self) -> List[Child]:
        if self.path is None or not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("children: unreadable registry at %s; treating it as empty",
                           self.path)
            return []
        raw = data.get("children") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        children: List[Child] = []
        for entry in raw:
            child = Child.from_json(entry)
            if child is not None:
                children.append(child)
        return children

    def save(self, children: List[Child]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps({"children": [c.as_json() for c in children]},
                                      indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("children: could not save the registry to %s: %s", self.path, exc)

    def add(self, pid: Any, command: str = "",
            identity: Optional[Callable[[int], Optional[str]]] = None) -> None:
        """Record a just-started child, with the identity of it and of this transport.

        A process object with no pid (an injected runner in a test, a spawn that failed) is
        not an error: there is simply nothing to record, so nothing is.
        """
        if not _usable_pid(pid):
            return
        read = identity or process_identity
        child = Child(pid=int(pid), transport_pid=os.getpid(),
                      transport_identity=read(os.getpid()), identity=read(int(pid)),
                      command=str(command or ""), recorded=time.time())
        children = [c for c in self.load() if c.pid != child.pid]
        children.append(child)
        self.save(children)

    def remove(self, pid: Any) -> None:
        """The child exited: forget it (a missing file is not an error)."""
        if not _usable_pid(pid):
            return
        children = self.load()
        kept = [c for c in children if c.pid != int(pid)]
        if len(kept) != len(children):
            self.save(kept)


def reap_orphans(path: Optional[Path], *,
                 identity: Optional[Callable[[int], Optional[str]]] = None,
                 alive: Optional[Callable[[int], bool]] = None,
                 kill: Optional[Callable[[int], None]] = None) -> List[int]:
    """End the children a dead transport left behind; returns the pids that were killed.

    All three OS calls are injectable, so the decision -- which pid may be killed -- is
    testable without touching a real process:

    * a child whose transport is **alive** is left alone (its output still has a reader);
    * a child whose pid **no longer matches** the recorded start time is left alone: the pid
      was reused, and killing it would kill somebody else's process;
    * a child whose pid is gone has its record dropped;
    * everything else is a genuine orphan and is ended, tree and all.
    """
    registry = ChildRegistry(path)
    read = identity or process_identity
    is_alive = alive or pid_alive
    end = kill or kill_pid
    killed: List[int] = []
    keep: List[Child] = []
    for child in registry.load():
        if child.transport_pid == os.getpid():
            keep.append(child)                      # our own, mid-turn (never on a start)
            continue
        if is_alive(child.transport_pid):
            parent_identity = read(child.transport_pid)
            if (parent_identity is not None and child.transport_identity is not None
                    and parent_identity != child.transport_identity):
                logger.info("children: transport pid %s now belongs to another process, so "
                            "the one that spawned child %s is gone; treating it as an orphan",
                            child.transport_pid, child.pid)
            else:
                keep.append(child)                  # a live transport still reads its child
                continue
        if not is_alive(child.pid):
            logger.info("children: pid %s is already gone; dropping its record", child.pid)
            continue
        current = read(child.pid)
        if child.identity is None or current is None or current != child.identity:
            logger.warning("children: pid %s no longer matches its recorded start time; "
                           "not killing a process that may have reused the pid", child.pid)
            continue
        logger.warning("children: ending orphaned CLI pid %s (%s) left by transport pid %s",
                       child.pid, child.command or "no command recorded", child.transport_pid)
        end(child.pid)
        killed.append(child.pid)
    if len(keep) != len(registry.load()) or killed:
        registry.save(keep)
    return killed
