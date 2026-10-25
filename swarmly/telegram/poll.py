"""
Durable bus poller core (issue #59 spec v1).

This module owns:

* the per-host one-shot poller that the ``bus-poll --once`` CLI subcommand runs;
* the single-writer lock around load → deliver → save (so two pollers do not race);
* the freeze-aware delivery path (state transitions still apply; actionable bodies
  are deferred, not dropped, while frozen);
* the offline-echo skip and the broadcast machine-event classifier integration;
* the adapter ``verify_delivery`` hook contract and the degrade rule.

It is intentionally transport-agnostic. ``AgentChannel.check_gitbus()`` is the
existing in-transport caller; ``bus_poll_once`` is the new shared core both it and
the new CLI command call.

The poller runs with **no Telegram token and no Telegram network**. Telegram is
optional and only consulted when an outbound echo is desired; the receipt of an
adapter response is the durable signal, not a Telegram post.
"""

from __future__ import annotations

import datetime
import inspect
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from swarmly.models import A2AMessage
from swarmly.telegram import oslock
from swarmly.telegram.adapters.base import (AdapterReply, AgentAdapter, IncomingMessage,
                                           is_silent_reply, plain)
from swarmly.telegram.config import (AgentConfig, DEFAULT_VERIFY_SETTLE_SECONDS,
                                 MAX_VERIFY_SETTLE_SECONDS)
from swarmly.telegram.wire import Envelope, MessageType, format_message, parse_message

logger = logging.getLogger("swarmly.telegram.poll")


# --------------------------------------------------------------------------------------
# Planted-token contract (issue #59 spec amendment 1)
# --------------------------------------------------------------------------------------


def planted_token_for(message: Any, ticket_id: Optional[str] = None) -> str:
    """Return the unique delivery-probe token to embed in the injected prompt.

    Spec amendment: every scheduled-poller injection MUST carry a unique planted
    token — the token IS the delivery-probe key in every adapter store. Default
    is the message/envelope id; when a reply-store ticket id is in scope it is
    already unique and already in the prompt, so it is reused.
    """
    if ticket_id:
        return str(ticket_id)
    if isinstance(message, str):
        return message
    message_id = str(getattr(message, "message_id", "") or "")
    if message_id:
        return message_id
    envelope_id = str(getattr(message, "envelope_id", "") or "")
    return envelope_id or ""


#: Marker we append to the prompt so adapters can pass the token to their CLI
#: without the agent stripping it. The marker is a stable, single-token phrase.
PLANTED_TOKEN_MARKER = "[BUS_DELIVERY_TOKEN="


def inject_planted_token(prompt: str, token: str) -> str:
    """Append a planted-token marker to the prompt the adapter will receive.

    The marker is a single line so adapters can scrape it trivially before
    forwarding the rest of the prompt to the underlying runtime.
    """
    if not token:
        return prompt
    return f"{prompt.rstrip()}\n\n{PLANTED_TOKEN_MARKER}{token}]"


#: Lock TTL — generous because the lock is a process-local best-effort marker;
#: the SQLite ``replies`` table already serialises concurrent bridges on the
#: same host. A stale lock from a crashed predecessor is reclaimed past this.
LOCK_TTL_SECONDS = 300


def poll_lock_path(repo_root: Path, alias: str) -> Path:
    """Path of the single-writer lock marker for an agent's poll cycle."""
    safe = re.sub(r"[^a-z0-9_-]", "", (alias or "agent").strip().lower()) or "agent"
    return Path(repo_root) / ".agents" / "state" / f"{safe}.poll.lock"


def _write_marker(path: Path, payload: str) -> None:
    """Replace a lock marker in one step: a reader sees the old marker or the new one,
    never an empty or half-written file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def acquire_poll_lock(repo_root: Path, alias: str, *, ttl_seconds: float = LOCK_TTL_SECONDS,
                      now: Optional[float] = None) -> bool:
    """Take the per-host single-writer lock.

    An OS file lock (:mod:`swarmly.telegram.oslock`) decides who wins: it is atomic, the
    kernel drops it when its holder exits, and only its holder reads or writes the
    marker, so a contender can never take a half-written marker for a stale one. The
    marker is a small JSON sidecar with ``acquired_at`` and the owner's pid; a marker
    younger than ``ttl_seconds`` from a holder that skipped the OS lock still refuses,
    and an unreadable one is reclaimed. The lock is scoped to this checkout: it
    serialises pollers that share the working copy (the in-transport tick and a
    scheduled CLI run on the same host).
    """
    path = poll_lock_path(repo_root, alias)
    gate = oslock.gate_path(path)
    try:
        if not oslock.try_lock(gate):
            return False
    except OSError:
        return False
    ts = time.time() if now is None else now
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        age = ts - float(existing["acquired_at"])
    except FileNotFoundError:
        age = ttl_seconds + 1.0
    except Exception:
        # Unreadable / malformed marker: nobody holds the OS lock, so nobody is
        # writing it -- reclaim it; a corrupt file must never block the poller forever.
        age = ttl_seconds + 1.0
    if age < ttl_seconds:
        # This process holds the OS lock, so a marker whose owner has exited was left by a
        # crash or a kill: reclaim it at once (live test: a killed transport's marker kept
        # the restarted one from polling for the whole TTL, without a word).
        try:
            owner = int(existing.get("owner"))
        except (TypeError, ValueError, AttributeError):
            owner = 0
        if owner <= 0 or owner == os.getpid() or pid_is_alive(owner):
            oslock.unlock(gate)
            logger.warning("poll: skipped, the poll lock %s is held by pid %s (%.0fs old)",
                           path, owner or "unknown", age)
            return False
        logger.warning("poll: took over the poll lock left by pid %s, which is gone", owner)
    try:
        _write_marker(path, json.dumps({"acquired_at": ts, "owner": f"{os.getpid()}"}))
    except OSError:
        oslock.unlock(gate)
        return False
    return True


def refresh_poll_lock(repo_root: Path, alias: str, *, now: Optional[float] = None) -> None:
    """Restart the lock's TTL if this process owns it: a poll that waited for the
    channel's turn admission must not look stale to another poller meanwhile."""
    path = poll_lock_path(repo_root, alias)
    if not oslock.holds(oslock.gate_path(path)):
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if str(existing.get("owner")) != f"{os.getpid()}":
            return
        existing["acquired_at"] = time.time() if now is None else now
        _write_marker(path, json.dumps(existing))
    except Exception:                                  # noqa: BLE001 - best effort
        logger.debug("poll: could not refresh the poll lock", exc_info=True)


def release_poll_lock(repo_root: Path, alias: str) -> None:
    """Release the per-host lock if this process owns it."""
    path = poll_lock_path(repo_root, alias)
    gate = oslock.gate_path(path)
    if not oslock.holds(gate):
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    try:
        if str(existing.get("owner")) == str(os.getpid()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    finally:
        oslock.unlock(gate)


# --------------------------------------------------------------------------------------
# Single-instance lock for the long-running transport (issue #77)
# --------------------------------------------------------------------------------------


# ``cli run`` had no guard of its own, so a restart task could race the live daemon
# and start a second ``getUpdates`` consumer on the same token. Telegram then splits
# the updates between the two consumers SILENTLY: each poller sees roughly half the
# messages, nothing raises, and the split stands until someone notices missing mail.
# The poll lock above cannot serve here -- it is a TTL marker for one bounded cycle,
# while ``run`` is expected to live for days. Its staleness rule is therefore "is the
# process behind the recorded pid still the one that took the marker", not "is the
# marker old": a TTL would either steal the lock from a healthy daemon or never let a
# crashed one go.


#: Exit code ``cli run`` returns when another live transport already holds the lock.
#: 4, not 3 (issue #97): 3 is the heartbeat-check family's stale/degraded code, and a
#: wrapper that maps both to one meaning would misread a refused start as a dead beat.
RUN_LOCK_EXIT_CODE = 4


#: Two readings of the same process's start time may differ by this much and still be
#: the same process (both come from the kernel's record, so the slack only absorbs
#: timestamp rounding). A larger gap means the pid was recycled by a newer process.
RUN_LOCK_START_TOLERANCE_SECONDS = 2.0


#: Boot stamps are wall-clock estimates of the boot instant, so NTP slew and
#: tick-counter drift move them by seconds over a long uptime. A reboot moves the
#: stamp by the previous daemon's uptime plus the down time -- hours for any transport
#: that actually ran -- so this window separates the two without reading drift as one.
RUN_LOCK_BOOT_TOLERANCE_SECONDS = 300.0


#: An unreadable marker is reclaimed only once it is older than this. Create-then-write
#: is not one atomic step, and reading a marker that a start is still writing as stale
#: is exactly how two pollers would both win.
RUN_LOCK_WRITE_GRACE_SECONDS = 30.0


def run_lock_path(repo_root: Path, alias: str) -> Path:
    """Path of the single-instance lock for the long-running ``run`` transport."""
    safe = re.sub(r"[^a-z0-9_-]", "", (alias or "agent").strip().lower()) or "agent"
    return Path(repo_root) / ".agents" / "state" / f"{safe}.run.lock"


def state_dir(cfg: Any) -> Path:
    """Where this transport keeps its own state files (``<root>/.agents/state``)."""
    return transport_state_root(cfg) / ".agents" / "state"


def transport_state_root(cfg: Any) -> Path:
    """Root of the checkout whose ``.agents/state`` holds the transport's lock.

    ``bus.repo`` (or its legacy ``fallback.gitbus_root`` alias) wins -- the same
    resolution the poller and the heartbeat use. The default is the checkout the
    transport runs from (``~/.a2a`` for an installed wheel), never the process CWD, so a ``run`` started from a scheduler
    directory and one started from a shell still meet on the same lock.
    """
    candidate = getattr(getattr(cfg, "bus", None), "repo", None) \
        or getattr(getattr(cfg, "fallback", None), "gitbus_root", None)
    if candidate:
        return Path(candidate).expanduser()
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file():
        return checkout
    # Installed from a wheel: parents[2] is site-packages, which is no place for state
    # (an upgrade may replace it). ~/.a2a already holds the logs.
    return Path.home() / ".a2a"


def _windows_kernel32():
    """``kernel32`` with the handle-returning calls typed.

    ctypes defaults a function's result to a 32-bit ``int``, which silently truncates
    handles on Win64; typing them once here keeps every probe honest.
    ``use_last_error=True`` is what makes ``ctypes.get_last_error()`` usable for
    telling "the process is gone" apart from "the process is not ours to open".
    """
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] \
        + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return kernel32


def pid_is_alive(pid: int) -> bool:
    """True when ``pid`` names a live process on this host.

    POSIX probes with ``os.kill(pid, 0)``. Windows must NOT: every signal but the
    console events is ``TerminateProcess`` there, so ``os.kill(pid, 0)`` would kill the
    very transport the question is about -- it probes through ``OpenProcess`` and
    ``GetExitCodeProcess`` instead, with no psutil dependency.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = _windows_kernel32()
            handle = kernel32.OpenProcess(0x1000, 0, pid)     # QUERY_LIMITED_INFORMATION
            if not handle:
                # ERROR_ACCESS_DENIED (5) = the process exists but is not ours to open:
                # alive. Any other failure (an invalid pid, first of all) is dead.
                return ctypes.get_last_error() == 5
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259                      # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:                                     # noqa: BLE001 - probe failure
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                           # alive, owned by another user
    except OSError:
        return False
    return True


def process_start_time(pid: int) -> Optional[float]:
    """Epoch time the process ``pid`` started, or ``None`` when not knowable.

    Used to defeat PID reuse: a live pid whose process started at a different time is
    NOT the process that took the lock. Linux reads field 22 of ``/proc/<pid>/stat``
    (clock ticks since boot) plus the kernel's own ``btime``; Windows reads the
    creation FILETIME of the process handle. Anything else degrades to ``None`` and
    that check is skipped -- pid liveness alone still decides.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = _windows_kernel32()
            handle = kernel32.OpenProcess(0x1000, 0, pid)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(handle, ctypes.byref(creation),
                                              ctypes.byref(exited),
                                              ctypes.byref(kernel_time),
                                              ctypes.byref(user_time))
                if not ok:
                    return None
                ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                # 100 ns ticks between the FILETIME origin (1601-01-01) and 1970-01-01.
                return (ticks - 116444736000000000) / 10_000_000.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:                                     # noqa: BLE001 - probe failure
            return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        start_ticks = float(stat[stat.rindex(")") + 2:].split()[19])   # field 22
        boot = host_boot_stamp()
        return None if boot is None else boot + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def host_boot_stamp() -> Optional[float]:
    """Best-effort epoch time of the current host boot, or ``None``.

    Compared with :data:`RUN_LOCK_BOOT_TOLERANCE_SECONDS`: a marker written on a
    previous boot cannot be trusted even when its pid happens to be alive, because the
    reboot may have handed that pid to an unrelated process. Linux uses the kernel's
    exact ``btime``; Windows derives the instant from ``GetTickCount64``.
    """
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    if os.name == "nt":
        try:
            kernel32 = _windows_kernel32()
            return time.time() - kernel32.GetTickCount64() / 1000.0
        except Exception:                                     # noqa: BLE001 - probe failure
            return None
    try:
        return time.time() - time.monotonic()
    except Exception:                                         # noqa: BLE001
        return None


@dataclass
class RunLockResult:
    """Outcome of one ``run`` single-instance lock attempt (issue #77)."""

    acquired: bool
    path: Path
    holder_pid: Optional[int] = None           # the live holder that refused us
    holder_started_at: Optional[float] = None  # epoch its marker was written
    note: str = ""                             # why not acquired / what was reclaimed


def _float_or_none(value: Any) -> Optional[float]:
    """``float(value)`` when it converts, ``None`` otherwise (probes never raise)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def run_lock_stale_reason(record: Dict[str, Any]) -> str:
    """Why the ``run`` lock ``record`` is stale, or "" while its holder is live.

    Checked in order of precision: a pid that no longer exists; a live pid whose
    process started at a different time (the pid was recycled); a boot stamp from a
    different boot. The boot check is last because it is the only approximate one.
    """
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return "the marker records no usable pid"
    if pid <= 0:
        return f"the marker records an invalid pid {pid}"
    if not pid_is_alive(pid):
        return f"pid {pid} is gone"
    recorded_start = _float_or_none(record.get("pid_start"))
    if recorded_start is not None:
        live_start = process_start_time(pid)
        if live_start is not None \
                and abs(live_start - recorded_start) > RUN_LOCK_START_TOLERANCE_SECONDS:
            return f"pid {pid} was recycled by a newer process"
    recorded_boot = _float_or_none(record.get("boot"))
    if recorded_boot is not None:
        live_boot = host_boot_stamp()
        if live_boot is not None \
                and abs(live_boot - recorded_boot) > RUN_LOCK_BOOT_TOLERANCE_SECONDS:
            return "the host rebooted since the lock was written"
    return ""


def _read_run_lock_marker(path: Path) -> Tuple[Dict[str, Any], str]:
    """Read an existing ``run`` marker: ``(record, stale_reason)``; "" = live holder.

    A marker older than :data:`RUN_LOCK_WRITE_GRACE_SECONDS` that cannot be read is
    reclaimed -- a corrupt file must never block the transport forever. A YOUNGER
    unreadable marker is reported as held: another start may be mid-write, and
    treating its half-written file as stale is how two pollers would both win.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict):
            raise ValueError("marker is not a JSON object")
    except FileNotFoundError:
        return {}, "the marker vanished while it was being read"
    except Exception:                                         # noqa: BLE001 - unreadable marker
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = RUN_LOCK_WRITE_GRACE_SECONDS
        if age < RUN_LOCK_WRITE_GRACE_SECONDS:
            return {}, ""
        return {}, "the unreadable marker was reclaimed"
    return record, run_lock_stale_reason(record)


def acquire_run_lock(repo_root: Path, alias: str) -> RunLockResult:
    """Take the single-instance lock that guards the long-running ``run``.

    An OS file lock (:mod:`swarmly.telegram.oslock`), held for the life of the transport,
    decides who wins, as for :func:`acquire_poll_lock`; the kernel drops it when the
    holder exits. The marker beside it is a small JSON record of the holder's ``pid``,
    its process start time (``pid_start``) and this host's boot stamp (``boot``), so a
    refused start can name the holder. Only the OS lock's holder reads and replaces
    the marker, so two contenders can never both judge it stale and both win. A LIVE
    holder is returned, never displaced; a stale marker (dead pid, recycled pid, other
    boot) is taken over and ``note`` says why.
    """
    path = run_lock_path(repo_root, alias)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return RunLockResult(False, path, note=f"state directory not writable: {exc}")
    gate = oslock.gate_path(path)
    try:
        gated = oslock.try_lock(gate)
    except OSError as exc:
        return RunLockResult(False, path, note=f"lock file not creatable: {exc}")
    reason = ""
    record: Dict[str, Any] = {}
    if gated and path.exists():
        record, reason = _read_run_lock_marker(path)
    elif not gated:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            record = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            record = {}
    if not gated or (path.exists() and not reason):
        if gated:
            oslock.unlock(gate)
        try:
            holder_pid: Optional[int] = int(record.get("pid"))
        except (TypeError, ValueError):
            holder_pid = None
        try:
            holder_started: Optional[float] = float(record.get("started_at"))
        except (TypeError, ValueError):
            holder_started = None
        return RunLockResult(False, path, holder_pid=holder_pid,
                             holder_started_at=holder_started,
                             note="held by a live transport")
    pid = os.getpid()
    started_at = time.time()
    payload = json.dumps({
        "pid": pid,
        "started_at": started_at,
        "pid_start": process_start_time(pid),
        "boot": host_boot_stamp(),
    })
    try:
        _write_marker(path, payload)
    except OSError as exc:
        oslock.unlock(gate)
        return RunLockResult(False, path, note=f"lock file not creatable: {exc}")
    return RunLockResult(True, path, holder_pid=pid, holder_started_at=started_at,
                         note=reason)


def release_run_lock(repo_root: Path, alias: str) -> None:
    """Release the ``run`` lock if this process is the one that holds it."""
    path = run_lock_path(repo_root, alias)
    gate = oslock.gate_path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        record = {}
    try:
        if str(record.get("pid")) == str(os.getpid()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    finally:
        oslock.unlock(gate)


#: Default delivery-verification attempts before a clean-negative is treated as
#: ``delivery_unknown`` and a BLOCKED envelope is escalated. Issue #59: probe
#: errors degrade silently; only clean negatives ×N escalate.
DEFAULT_VERIFY_ATTEMPTS = 3


@dataclass
class PollSettings:
    """Per-cycle knobs. Pulled out so callers can tweak (tests, operators)."""

    delivery_verify_attempts: int = DEFAULT_VERIFY_ATTEMPTS
    delivery_verify_delay_seconds: float = 0.0
    #: Issue #80: settle window between an injection and its first verify probe.
    verify_settle_seconds: float = DEFAULT_VERIFY_SETTLE_SECONDS


@dataclass
class PollOutcome:
    """What one poll cycle produced. Always populated, even on early skips."""

    delivered: List[str] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)
    skipped: Optional[str] = None
    telegram_ok: bool = True
    telegram_unavailable: bool = False
    adapter_replies: List[Tuple[str, Optional[AdapterReply]]] = field(default_factory=list)
    tokens: Dict[str, str] = field(default_factory=dict)        # message_id -> planted token
    delivery_verified: List[str] = field(default_factory=list)
    delivery_unknown: List[str] = field(default_factory=list)
    machine_events: List[str] = field(default_factory=list)
    escalations: List[Dict[str, str]] = field(default_factory=list)
    bus_writes: List[Dict[str, Any]] = field(default_factory=list)
    replies_unposted: int = 0          # agent replies with no Telegram transport to post them
    replies_posted: int = 0            # agent replies echoed to Telegram this cycle
    #: Issue #82: escalations whose bus write raised — the escalation is still
    #: raised (``escalations``), but its durable-plane record is missing.
    bus_write_failures: int = 0
    bus_write_failure_details: List[str] = field(default_factory=list)
    #: a2a/2 swarm lanes (``bus.swarm``): envelope ids handed to the adapter, the
    #: envelopes the projection quarantined (never delivered), and a read failure.
    lane_delivered: List[str] = field(default_factory=list)
    lane_deferred: List[str] = field(default_factory=list)
    #: lane items given up on after MAX_LANE_ATTEMPTS failed injections (escalated as stuck)
    lane_dead_letters: List[str] = field(default_factory=list)
    lane_quarantined: List[Dict[str, Any]] = field(default_factory=list)
    lane_error: Optional[str] = None
    #: Review P0-1: bus messages not delivered because their signature did not verify
    #: or their sender is not in ``bus.senders`` (``{"id", "sender", "reason"}``).
    rejected: List[Dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _seen_path(repo_root: Path, alias: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]", "", (alias or "agent").strip().lower()) or "agent"
    return Path(repo_root) / ".agents" / "state" / f"seen-{safe}.json"


def _deferred_path(repo_root: Path, alias: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]", "", (alias or "agent").strip().lower()) or "agent"
    return Path(repo_root) / ".agents" / "state" / f"deferred-{safe}.json"


def _heartbeat_path(repo_root: Path, alias: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]", "", (alias or "agent").strip().lower()) or "agent"
    return Path(repo_root) / ".agents" / "state" / f"{safe}.heartbeat.json"


def _write_heartbeat(repo_root: Path, alias: str, outcome: "PollOutcome",
                     transport: Optional[Dict[str, Any]] = None) -> None:
    """Dead-man watch emitter (issue #68): stamp every *completed* poll cycle.

    One small JSON file per alias whose ``at``/mtime advances on each cycle — a
    tokenless liveness signal host supervision can read without a model turn.
    Written by both the ``bus-poll --once`` CLI (the scheduled path) and the
    in-transport tick, so a stalled poller is visible either way. Best-effort:
    a heartbeat failure must never break a delivery cycle.

    Issue #123: the in-transport tick additionally writes a ``transport`` block
    (``pid``/``host``/``started_at``/``endpoint_ok``) so a beat written by the
    scheduled poller can no longer vouch for a transport that is dead or degraded.
    A poller-only write CARRIES OVER the previous block untouched -- erasing it
    would turn "transport dead" into "unknown (legacy beat)" exactly when the
    distinction matters.
    """
    try:
        path = _heartbeat_path(repo_root, alias)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "at": time.time(),
            "alias": alias,
            "delivered": len(outcome.delivered),
            "deferred": len(outcome.deferred),
            "rejected": len(outcome.rejected),
            "machine_events": len(outcome.machine_events),
            "escalations": len(outcome.escalations),
            # Issue #82: escalations raised is NOT escalations persisted — keep
            # both so a fresh beat cannot imply a durable-plane record.
            "bus_write_failures": outcome.bus_write_failures,
            "lane_delivered": len(outcome.lane_delivered),
            "lane_deferred": len(outcome.lane_deferred),
            "lane_dead_letters": len(outcome.lane_dead_letters),
            "lane_quarantined": len(outcome.lane_quarantined),
            "lane_error": outcome.lane_error,
        }
        if transport is not None:
            payload["transport"] = {**transport, "at": time.time()}
        else:
            try:
                if path.exists():
                    old = json.loads(path.read_text(encoding="utf-8-sig"))
                    if isinstance(old, dict) and isinstance(old.get("transport"), dict):
                        payload["transport"] = old["transport"]
            except Exception:                          # noqa: BLE001 - carry-over is best-effort
                pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:                                  # noqa: BLE001 - liveness is best-effort
        logger.warning("poll: heartbeat write failed", exc_info=True)


def heartbeat_status(cfg: Any, *, factor: float = 3.0,
                     now: Optional[float] = None) -> Dict[str, Any]:
    """Read the canonical liveness beat and classify it (issue #68).

    Contract (frozen after the #71 review): the canonical signal is the file
    ``<bus.repo>/.agents/state/<alias>.heartbeat.json`` — ``at`` (and the file
    mtime) advances on every completed poll cycle, whichever path ran it (the
    scheduled ``bus-poll --once`` CLI or the in-transport tick). The per-cycle
    log line is secondary, diagnostics only. A beat is *stale* when
    ``now - at > factor × interval``, the interval coming from
    ``bus.poll_minutes`` (falling back to ``fallback.gitbus_poll_minutes``).
    """
    repo = Path(cfg.bus.repo or cfg.fallback.gitbus_root or ".").expanduser()
    alias = cfg.identity.alias
    path = _heartbeat_path(repo, alias)
    interval = max(60.0, float(cfg.bus.poll_minutes or
                               cfg.fallback.gitbus_poll_minutes or 30) * 60.0)
    timestamp = time.time() if now is None else float(now)
    status: Dict[str, Any] = {
        "alias": alias,
        "path": str(path),
        "present": False,
        "at": None,
        "age_seconds": None,
        "interval_seconds": interval,
        "threshold_seconds": interval * max(1.0, float(factor)),
        "stale": True,
        "counters": {},
        "detail": "no heartbeat recorded yet",
    }
    if not path.exists():
        return status
    try:
        # utf-8-sig: other tools (PowerShell, editors) may write the beat with a BOM.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        at = float(data.get("at") or path.stat().st_mtime)
    except Exception:                                  # noqa: BLE001 - unreadable beat is stale
        status["detail"] = "heartbeat unreadable"
        return status
    age = max(0.0, timestamp - at)
    stale = age > status["threshold_seconds"]
    status.update({
        "present": True,
        "at": at,
        "age_seconds": age,
        "stale": stale,
        "counters": {key: data.get(key) for key in
                     ("delivered", "deferred", "machine_events", "escalations",
                      "bus_write_failures")},
        "detail": "stale beat" if stale else "fresh beat",
    })
    # Issue #123: classify the transport block. Absent block = legacy beat (cannot
    # infer; "absent", never a silent "alive"). Precedence: a dead pid on THIS host
    # is decisive; then the block going stale (the transport tick stopped COMPLETING
    # -- a blocked dispatch writes nothing); then degradation (the last dispatch had
    # no live endpoint: gated or fell back).
    block = data.get("transport")
    if isinstance(block, dict):
        block = dict(block)
        state, why = "alive", ""
        block_at = _float_or_none(block.get("at"))
        block_host = block.get("host")
        block_pid = block.get("pid")
        same_host = block_host in (None, "") or block_host == socket.gethostname()
        if same_host and isinstance(block_pid, int) and not pid_is_alive(block_pid):
            state, why = "dead", f"transport pid {block_pid} is not alive"
        elif block_at is not None and timestamp - block_at > status["threshold_seconds"]:
            state, why = "dead", (f"transport block last written {timestamp - block_at:.0f}s "
                                  f"ago (> {status['threshold_seconds']:.0f}s)")
        elif block.get("endpoint_ok") is False:
            state, why = "degraded", "no live endpoint on the last dispatch"
        status["transport"] = block
        status["transport_state"] = state
        status["transport_detail"] = why
    else:
        status["transport"] = None
        status["transport_state"] = "absent"
        status["transport_detail"] = "legacy beat (no transport block)"
    return status


def _load_seen(repo_root: Path, alias: str) -> Dict[str, None]:
    path = _seen_path(repo_root, alias)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("seen ledger root is not a dict")
        items = data.get("seen") or []
        return {str(item): None for item in items}
    except Exception as exc:                          # noqa: BLE001 - quarantine
        quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(quarantine)
            logger.warning("poll: corrupt seen ledger quarantined at %s", quarantine)
        except Exception:                              # noqa: BLE001
            logger.warning("poll: corrupt seen ledger could not be moved",
                           exc_info=True)
        return {}


_SAVE_SEEN_CAP = 5000


#: Review P0-4 F3: a message stamped up to this long before the baseline still counts as
#: new, so a sender whose clock runs behind is not dropped as history.
BASELINE_SKEW_SECONDS = 15 * 60


def _needs_baseline(repo_root: Path, alias: str) -> bool:
    """True when this agent has no usable seen ledger for this bus: its first cycle, a
    deleted ledger, or a corrupt one (quarantined here). Without a ledger every message
    on the bus would look new, so the cycle records the bus instead of delivering it."""
    path = _seen_path(repo_root, alias)
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Usable only with its id list and the baseline mark: a hand-written {} or
        # {"seen": []} (or a ledger from before the baseline existed) has no cutoff, so
        # every old message on the bus would look new (P0-4 re-review m1).
        since = data.get("since") if isinstance(data, dict) else None
        if (isinstance(data, dict) and isinstance(data.get("seen"), list)
                and isinstance(since, str) and _parse_utc(since)):
            return False
        if (isinstance(data, dict) and "since" not in data and data.get("seen")
                and isinstance(data["seen"], list)
                and all(isinstance(item, str) for item in data["seen"])):
            # A ledger written before the baseline mark existed (every bot's ledger at the
            # upgrade): its last write is when the bot last polled, so that is the cutoff.
            # Re-baselining it would drop the mail that arrived since then.
            # Never later than now: a future mtime (clock skew, a copied file) would make
            # every new message look like history.
            stamp = min(datetime.datetime.fromtimestamp(path.stat().st_mtime,
                                                        tz=datetime.timezone.utc),
                        datetime.datetime.now(datetime.timezone.utc))
            _save_seen(repo_root, alias, dict.fromkeys(data["seen"]),
                       since=stamp.isoformat())
            logger.info("poll: seen ledger upgraded; messages since %s still count as new",
                        stamp.isoformat())
            return False
    except Exception:                                  # noqa: BLE001 - corrupt: re-baseline
        pass
    quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        path.rename(quarantine)
        logger.warning("poll: unusable seen ledger quarantined at %s; taking a new baseline",
                       quarantine)
    except Exception:                                  # noqa: BLE001
        logger.warning("poll: corrupt seen ledger could not be moved", exc_info=True)
    return True


def _ledger_since(repo_root: Path, alias: str) -> Optional[str]:
    """When this agent's first cycle took its baseline (ISO UTC), if it recorded one."""
    try:
        data = json.loads(_seen_path(repo_root, alias).read_text(encoding="utf-8"))
        since = data.get("since") if isinstance(data, dict) else None
        return since if isinstance(since, str) and _parse_utc(since) else None
    except Exception:                                  # noqa: BLE001 - no cutoff then
        return None


def _parse_utc(value: Any) -> Optional[datetime.datetime]:
    try:
        parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _older_than(message: Any, since: Optional[str]) -> bool:
    """Review P0-4: a message written before the baseline is history, never delivered,
    even once its id has rotated out of the capped seen ledger. The cutoff allows
    :data:`BASELINE_SKEW_SECONDS` of clock skew; a message without a readable timestamp
    is not history (its id still has to be unseen)."""
    cutoff = _parse_utc(since) if since else None
    stamp = _parse_utc(getattr(message, "timestamp_utc", None))
    if cutoff is None or stamp is None:
        return False
    return stamp < cutoff - datetime.timedelta(seconds=BASELINE_SKEW_SECONDS)


def _save_seen(repo_root: Path, alias: str, seen: Dict[str, None],
               since: Optional[str] = None, force: bool = False) -> None:
    path = _seen_path(repo_root, alias)
    # Never persist a fresh empty ledger, except for the first-cycle baseline
    # (*force*): its existence is what says the baseline was taken.
    if not seen and not force:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cap = max(1, _SAVE_SEEN_CAP)
        # Insertion order: the cap drops the ids this agent recorded first. Never sort:
        # ids come from the files, so a sender could pick ids that push real ones out.
        items = list(seen)[-cap:]
        data: Dict[str, Any] = {"seen": items}
        if since:
            data["since"] = since
        # Review P0-4 F1: temp file + rename, so a crash mid-write never leaves a
        # truncated ledger (which would replay the bus).
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=0), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:                                  # noqa: BLE001 - legacy parity
        # The legacy ``_save_gitbus_seen`` never let a persist failure escape
        # (review round 2: the refactor had lost that guard).
        logger.warning("poll: could not persist the seen ledger", exc_info=True)


def _load_deferred(repo_root: Path, alias: str) -> List[Dict[str, Any]]:
    path = _deferred_path(repo_root, alias)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for item in (data.get("deferred") or []):
        if isinstance(item, dict):
            if item.get("id"):
                entry: Dict[str, Any] = {"id": str(item["id"]),
                         "text": str(item.get("text") or ""),
                         "sender": str(item.get("sender") or "")}
                if item.get("accepted") is True:
                    entry["accepted"] = True
                out.append(entry)
        elif item:
            # Legacy format: bare id, payload not captured (recovered from the
            # fetch window at drain time when still visible).
            out.append({"id": str(item), "text": "", "sender": ""})
    return out


def _save_deferred(repo_root: Path, alias: str, items: List[Dict[str, str]]) -> None:
    path = _deferred_path(repo_root, alias)
    # Nothing deferred: no file (a drained store is removed, not left empty).
    if not items:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [item if isinstance(item, dict)
                  else {"id": str(item), "text": "", "sender": ""}
                  for item in items]
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"deferred": normalized}, indent=0), encoding="utf-8")
    os.replace(tmp, path)


def _accepted_items(items: List[Any], cfg: AgentConfig, outcome: PollOutcome,
                    relevant: Optional[Callable[[Any], bool]] = None) -> List[Any]:
    """Review P0-1: the bus items this agent may act on.

    An item is ``(message, verified, status)``; only ``verified is True`` passes, unless
    ``bus.allow_unsigned``. With ``bus.senders`` set, the sender must also be listed.
    Rejected items are not marked seen, so registering a missing key later delivers
    them. Only rejections ``relevant`` keeps (by default all: the poller passes unseen
    mail addressed to this agent) are counted, in one warning per cycle, so history and
    other agents' mail do not raise it on every poll.
    """
    allowed = set(cfg.bus.senders)
    kept: List[Any] = []
    for item in items:
        try:
            message = item[0]
        except Exception:                              # noqa: BLE001
            continue
        message_id = str(getattr(message, "message_id", "") or "")
        sender = str(getattr(getattr(message, "sender", None), "agent_id", "")
                     or "").strip().lower()
        verified = len(item) > 1 and item[1] is True
        status = str(item[2]) if len(item) > 2 else "unverified"
        reason = ""
        if not verified and not cfg.bus.allow_unsigned:
            reason = status
        elif allowed and sender not in allowed:
            reason = "sender not in bus.senders"
        if not reason:
            kept.append(item)
        elif relevant is None or relevant(message):
            outcome.rejected.append({"id": message_id, "sender": sender, "reason": reason})
    if outcome.rejected:
        logger.warning("bus: %d message(s) not delivered (unverified signature or sender "
                       "not allowed), e.g. %s from %r: %s", len(outcome.rejected),
                       outcome.rejected[0]["id"], outcome.rejected[0]["sender"],
                       outcome.rejected[0]["reason"])
    return kept


def _gitbus_text(message: Any) -> str:
    payload = getattr(message, "payload", None) or {}
    if isinstance(payload, dict):
        for key in ("text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, ensure_ascii=False) if payload else ""
    return str(payload).strip()


# --------------------------------------------------------------------------------------
# Machine-event classifier (issue #59: SAME classifier used by transport + CLI poller)
# --------------------------------------------------------------------------------------


class MachineEventClassifier:
    """Classify an envelope as a machine-only event (recorded as state, never dispatched).

    Shared by the in-transport :meth:`AgentChannel._handle_machine_event` and the
    new :mod:`swarmly.telegram.poll` core so the bus-poll CLI subcommand and the
    ``check_gitbus`` transport tick agree on what counts as ``HELLO/RECEIPT/ALERT``
    traffic (issue #59 deliverable 8).
    """

    STATUS_INTENTS = frozenset({"ONLINE", "OFFLINE", "FROZEN", "RESUMED"})
    # Kept in step with AgentChannel._MACHINE_NOTE_INTENTS (review, #263): a store line or a
    # probe line must never open a model turn, whoever classifies it.
    NOTE_INTENTS = frozenset({"FYI", "CLOSED", "RECEIPT", "ALERT", "TOPIC_GONE", "CONTROL",
                              "PROBE", "STATE", "SNAPSHOT"})

    def __init__(self, channel: Any = None) -> None:
        # A channel may carry overridden sets (e.g. tests); fall back to the
        # canonical class-level sets when nothing is supplied.
        self._channel = channel

    @property
    def status_intents(self):
        return getattr(self._channel, "_MACHINE_STATUS_INTENTS",
                       self.STATUS_INTENTS)

    @property
    def note_intents(self):
        return getattr(self._channel, "_MACHINE_NOTE_INTENTS",
                       self.NOTE_INTENTS)

    def classify(self, envelope: Envelope) -> Optional[str]:
        """Return a machine-event kind (``hello``/``receipt``/``alert``/...), or ``None``.

        ``None`` means the envelope is NOT machine-only and must follow the normal
        routing path (REQUEST/RESPONSE/BLOCKED, plain prose, or any human message).
        """
        kind = (envelope.type or "").upper()
        intent = (envelope.intent or "").upper()
        if kind in (MessageType.RECEIPT, MessageType.ALERT):
            return kind.lower()
        if kind == MessageType.STATUS and intent in self.status_intents:
            return "status"
        if kind == MessageType.NOTE and intent in self.note_intents:
            return "note"
        if kind in (MessageType.HELLO, MessageType.BYE):
            return kind.lower()
        return None


def _is_machine_event(envelope: Envelope) -> bool:
    """Default classifier; ``True`` when the envelope is the machine-only kind."""
    return MachineEventClassifier().classify(envelope) is not None


def _is_freeze_control(envelope: Envelope) -> bool:
    """Whether this envelope is a STATUS FREEZE/RESUME that must apply even while frozen."""
    kind = (envelope.type or "").upper()
    intent = (envelope.intent or "").upper()
    return kind == MessageType.STATUS and intent in {"FREEZE", "RESUME"}


def _addresses_me(envelope: Envelope, cfg: AgentConfig) -> bool:
    """Whether the envelope is addressed at this agent (incl. broadcast)."""
    target = (envelope.to or "").strip().lstrip("@").lower()
    if not target or target == "*":
        return True
    return target in {cfg.identity.alias.strip().lower(),
                      cfg.identity.name.lstrip("@").lower()}


# --------------------------------------------------------------------------------------
# Outbound-fallback envelope field contract
# --------------------------------------------------------------------------------------


def build_fallback_envelope(*, sender: str, recipient: str,
                             original_message_id: Optional[Union[int, str]],
                             envelope_id: str,
                             ticket_id: Optional[str],
                             body: str,
                             envelope_type: str = MessageType.RESPONSE) -> str:
    """Build a fallback envelope that carries the dedupe keys spec #59 requires.

    The header carries ``id=<envelope-id>`` and ``ref=<ticket-id>``; the body carries
    ``message_id=<original Telegram message_id>`` and the same dedupe keys, so that any
    downstream reader can deduplicate against the original receipt even if the post
    lands twice (e.g. once via the offline echo path and once via the reconnect path).

    ``envelope_type`` defaults to RESPONSE (the offline-echo forward); the BLOCKED
    escalation path passes ``MessageType.BLOCKED`` (spec #59: "delivery_unknown +
    BLOCKED envelope" — review round 2 found the escalation mislabelled RESPONSE).
    """
    envelope = Envelope(
        type=envelope_type,
        to=recipient,
        ref=ticket_id or "",
        id=envelope_id,
        rev="",
    )
    dedupe_lines = [
        f"dedupe=envelope:{envelope_id}",
    ]
    if ticket_id:
        dedupe_lines.append(f"dedupe=ticket:{ticket_id}")
    if original_message_id is not None:
        dedupe_lines.append(f"message_id={original_message_id}")
    header = envelope.header()
    return f"{header}\n\n{body}\n\n" + "\n".join(dedupe_lines)


# --------------------------------------------------------------------------------------
# The shared poll core
# --------------------------------------------------------------------------------------


def _bus_incoming(text: str, sender: str, cfg: Any, token: str,
                  channel: Any = None) -> IncomingMessage:
    """The IncomingMessage shape both the main loop and the drain path inject.

    A bus turn runs the model and effort chosen with /model and /reasoning for the topic the
    text is about (General's when it names none), as a Telegram turn there would: without
    them it fell back to the CLI's default model (live run: Antigravity's bus turns ran
    the expensive default after its topics had been switched off it)."""
    sender = plain(sender, limit=32) or "unknown"      # review P0-8: a bus-controlled name
    choices = getattr(channel, "bus_turn_choices", None)
    model, effort = choices(text) if callable(choices) else (None, None)
    return IncomingMessage(
        text=text, author_alias=f"{sender} (gitbus)",
        mentions=dict(cfg.peers),
        maintainer_username=cfg.identity.maintainer_username,
        author_username=f"{sender} (gitbus)",
        response_instructions=inject_planted_token("", token),
        model=model, effort=effort,
        # Issue #31: a bus turn is held to the channel's reply budget too. The bus names no
        # topic, so this is the global number; a project override applies to its topics.
        reply_chars=cfg.limits.reply_chars,
    )


def _took(took: Callable[..., Any], ref: dict, sender: str) -> bool:
    """``took_telegram_copy`` with the mirror's sender when the channel takes it."""
    try:
        return bool(took(ref, sender=sender))
    except TypeError:                                  # an older or test channel
        return bool(took(ref))


async def _echo_reply(channel: Any, sender: str, reply: Any,
                      outcome: PollOutcome, thread_id: Optional[int] = None,
                      about: str = "", message_id: Any = None) -> None:
    """Best-effort Telegram echo of an adapter reply (transport up only).

    Never retried and never queued for reconnect (issue #59): a failure just
    marks the cycle ``telegram_unavailable``.
    """
    if not (reply is not None and getattr(reply, "text", "")) or is_silent_reply(reply.text):
        return
    echo_text = f"[gitbus \u2190 {sender}] {reply.text}"
    chat_id: Optional[int] = None
    of_message = getattr(channel, "topic_of_message", None)
    if thread_id is None and message_id is not None and callable(of_message):
        # A mirror names its Telegram message, not its topic (issue #22): the topic this bot
        # saw that message in is where the hand-off came from, so the answer goes there.
        found = of_message(message_id)
        if isinstance(found, tuple) and len(found) == 2:
            chat_id, thread_id = found
    find = getattr(channel, "topic_for_text", None)
    only = getattr(channel, "_transport_only_on", None)
    if callable(only) and only() is True:
        # General and Swarm pulse are transport-only: the words of the answer do not pick
        # its topic either (admin, 25 Oct: routing is decided in transport space).
        find = None
    if thread_id is None and callable(find):
        # The mirror names no topic (no shared secret): the topic already about the same
        # issue or PR, never General (round 2 export: these echoes all landed in General).
        try:
            known = find(f"{about}\n{reply.text}")
        except Exception:                              # noqa: BLE001 - General then
            known = None
        if known is not None:
            chat_id, thread_id = known
    barred = getattr(channel, "token_barred", None)
    place = barred(chat_id, thread_id) if callable(barred) else None
    if isinstance(place, str):
        # No topic of its own (bus mail that came from no topic, or a hand-off this bot never
        # saw in Telegram): not shown there. No bot takes a turn in General anyway.
        outcome.replies_unposted += 1
        logger.warning("poll: %s is transport-only, so %s's answer has no topic to be shown "
                       "in and is not posted: %r", place, sender, reply.text[:200])
        return
    if getattr(channel, "_sender", None) is None:
        if getattr(channel, "bot", None) is not None:
            # The running transport: post like any other reply. (Before, only an injected
            # test sender was used, so a live agent's answer to bus mail was dropped.)
            try:
                # A mirrored hand-off is answered in the topic it came from.
                if chat_id is None:
                    await channel.send(echo_text, thread_id=thread_id)
                else:
                    await channel.send(echo_text, chat_id=chat_id, thread_id=thread_id)
                outcome.replies_posted += 1
            except Exception:                          # noqa: BLE001
                outcome.telegram_ok = False
                outcome.telegram_unavailable = True
                logger.warning("poll: telegram echo failed", exc_info=True)
            return
        # bus-poll --once has no Telegram transport by design: say where the answer went.
        outcome.replies_unposted += 1
        logger.warning("poll: no Telegram transport in this process, so the agent's reply to "
                       "%s was not posted (the running transport posts replies): %s",
                       sender, reply.text[:500])
        return
    try:
        sender_callable = channel._sender
        if asyncio.iscoroutinefunction(sender_callable) or \
                asyncio.iscoroutinefunction(getattr(sender_callable, "__call__", None)):
            await sender_callable(echo_text)
        else:
            await _to_thread(sender_callable, echo_text)
    except Exception:                              # noqa: BLE001
        outcome.telegram_ok = False
        outcome.telegram_unavailable = True
        logger.warning("poll: telegram echo skipped (transport down)",
                       exc_info=True)


def _restore_freeze_flag(channel: Any) -> None:
    """Apply the persisted freeze flag without the transport's re-announce.

    Review B5: the one-shot CLI runs never call ``channel.start()``, so the
    persisted freeze was invisible to the outage poller. The transport's own
    ``_restore_freeze_state`` re-announces (paced send); the poller only needs
    the FLAG so it defers instead of injecting into a frozen agent.
    """
    if getattr(channel, "_frozen", False):
        return
    getter = getattr(channel, "_freeze_state_path", None)
    if getter is None:
        return
    try:
        path = getter()
    except Exception:                                  # noqa: BLE001
        return
    if path is None or not Path(path).exists():
        return
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("poll: unreadable freeze state at %s; ignoring", path)
        return
    if isinstance(data, dict) and data.get("frozen"):
        channel._frozen = True
        ref = data.get("ref")
        channel._freeze_ref = str(ref) if ref else None
        logger.info("poll: freeze flag restored from %s (ref=%s)", path, ref)


def _bus_channel_event(text: str, sender: str, cfg: Any):
    """Minimal ChannelEvent for machine-state bookkeeping on the bus path.

    Maps the bus sender (an agent id) to its configured Telegram handle so the
    in-transport bookkeeping (``_alias_for`` / ``resolve_prefix``) matches rows
    the same way Telegram ingress does (review B8).
    """
    from swarmly.telegram.channel import ChannelEvent
    handle = (sender or "").strip().lstrip("@")
    for alias_name, username in (getattr(cfg, "peers", None) or {}).items():
        if str(alias_name).strip().lower() == (sender or "").strip().lower():
            handle = str(username).lstrip("@")
            break
    return ChannelEvent(
        text=text, chat_id=cfg.identity.home_channel_id or 0,
        author_username=handle, author_is_bot=True)


def _default_bus_writer(repo: Path, alias: str, transport: Any = None):
    """Best-effort bus writer for BLOCKED escalations (review B2).

    Returns a ``writer(sender, recipient, body)`` callable that posts an
    ``ESCALATE_BLOCKER`` message to the durable bus. Any failure logs; the
    escalation remains in ``outcome.escalations`` regardless.
    """
    def _write(sender: str, recipient: str, body: str) -> None:
        _post_bus_envelope(Path(repo), sender, recipient, body, transport=transport)
    return _write


def _post_bus_envelope(repo: Path, sender: str, recipient: str,
                       body: str, transport: Any = None) -> None:
    """Post one escalation message to the project's durable bus.

    *transport* is the agent's configured bus transport (gitbus when ``None``).
    Guarded for gitbus: only a git checkout has a bus to write to; everything
    else is a logged no-op (the operator still sees the escalation in the CLI
    output).
    """
    from swarmly.transports import GitTransport
    is_git = transport is None or isinstance(transport, GitTransport)
    if is_git and not (repo / ".git").exists():
        logger.warning("poll: no git bus at %s; escalation not persisted to the "
                       "durable plane (it remains in this cycle's outcome)", repo)
        return
    from swarmly.bus import DurableBus
    from swarmly.models import (A2AMessage, MessageIntent, MessagePriority,
                            RecipientInfo, SenderInfo)
    bus = DurableBus(root_dir=repo, transport=None if is_git else transport)
    key, key_type = bus.signing_key(sender.lower())    # MissingKeyError: never unsigned
    message = A2AMessage(
        protocol="a2a/v1.0",
        message_id=f"msg-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-"
                   f"{uuid.uuid4().hex[:16]}",
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        sender=SenderInfo(agent_id=sender.lower(), model_family="custom",
                          framework="custom"),
        recipient=RecipientInfo(agent_id=(recipient or "*").lower().lstrip("@")),
        intent=MessageIntent.ESCALATE_BLOCKER, priority=MessagePriority.BLOCKING,
        signature="", payload={"text": body})
    bus.post_message(message, private_key=key, key_type=key_type, git_push=True)


class _Admission:
    """The channel's turn admission (its lock and a parallel slot), so a bus delivery never
    runs beside a chat turn the adapter cannot share. Test doubles without one run as is.
    ``refresh`` runs once admission is granted (it restarts the poll lock's TTL)."""

    def __init__(self, channel: Any, refresh: Optional[Callable[[], Any]] = None) -> None:
        admitted = getattr(channel, "admitted", None) if channel is not None else None
        self._inner = admitted() if callable(admitted) else None
        self._refresh = refresh

    async def __aenter__(self) -> None:
        if self._inner is not None:
            await self._inner.__aenter__()
        if self._refresh is not None:
            self._refresh()

    async def __aexit__(self, *exc: Any) -> Any:
        if self._inner is not None:
            return await self._inner.__aexit__(*exc)
        return None


async def bus_poll_once(channel: Any, *,
                        author_admin_id: Optional[int] = None,
                        delivery_verify_attempts: int = DEFAULT_VERIFY_ATTEMPTS,
                        delivery_verify_delay_seconds: float = 0.0,
                        verify_settle_seconds: Optional[float] = None,
                        settle_sleep: Optional[Callable[[float], Any]] = None,
                        writer: Optional[Callable[[str, str, str], Any]] = None,
                        escalator: Optional[Callable[[Dict[str, str]], Any]] = None,
                        settings: Optional[PollSettings] = None,
                        transport_compat: bool = False) -> PollOutcome:
    """Run one durable-bus poll cycle on an AgentChannel-shaped object.

    The shared core for the new CLI ``bus-poll --once`` subcommand and the existing
    ``AgentChannel.check_gitbus()`` transport tick. See module docstring for the
    invariants: no Telegram token required, single-writer lock, freeze-aware,
    broadcast machine-class, offline echo skip.
    """
    if settings is not None:
        delivery_verify_attempts = settings.delivery_verify_attempts
        delivery_verify_delay_seconds = settings.delivery_verify_delay_seconds
        if verify_settle_seconds is None:
            verify_settle_seconds = settings.verify_settle_seconds
    cfg: AgentConfig = channel.config
    if verify_settle_seconds is None:
        # Issue #80: the settle window is declared under the poller's ``bus:``
        # section (``bus.verify_settle_seconds``); the clamp bounds a bad config.
        verify_settle_seconds = getattr(cfg.bus, "verify_settle_seconds",
                                        DEFAULT_VERIFY_SETTLE_SECONDS)
    settle_seconds = _bounded_settle_seconds(verify_settle_seconds)
    outcome = PollOutcome()

    if not cfg.bus.enabled or cfg.bus.poll_minutes <= 0:
        outcome.skipped = "disabled"
        return outcome

    root = cfg.bus.repo or cfg.fallback.gitbus_root
    if not root:
        # Review P0-4: never poll a default bus (it used to be this package's own
        # checkout, whose inbox history went to every fresh clone).
        logger.warning("poll: no bus.repo configured; the durable bus is not polled")
        outcome.skipped = "no_bus_repo"
        return outcome
    repo = Path(root).expanduser()
    alias = cfg.identity.alias

    if writer is None:
        # Production default (review B2): the BLOCKED escalation must reach the
        # BUS even when no caller wires a writer. Best-effort — the escalation
        # stays in outcome.escalations for the caller either way.
        transport = None
        build = getattr(channel, "bus_transport", None)
        if build is not None:
            try:
                transport = build(repo)
            except Exception:                          # noqa: BLE001
                logger.warning("poll: bus transport unavailable for escalations",
                               exc_info=True)
        writer = _default_bus_writer(repo, alias, transport)

    # Gate 2 (review B6): a legitimate injection may outlive the static TTL —
    # derive the lock TTL from the adapter's own timeout so a slow adapter can
    # never have its lock reclaimed mid-cycle (never below the static floor).
    adapter_timeout = float(getattr(cfg.adapter, "timeout_seconds", 0) or 0)
    lock_ttl = max(LOCK_TTL_SECONDS, adapter_timeout * 2.0) if adapter_timeout > 0 \
        else LOCK_TTL_SECONDS
    if not acquire_poll_lock(repo, alias, ttl_seconds=lock_ttl):
        outcome.skipped = "busy"
        return outcome

    def refresh() -> None:
        refresh_poll_lock(repo, alias)
    try:
        # 1. Fetch + read (best-effort; refresh failure is tolerated).
        try:
            pull = getattr(channel, "_gitbus_pull", None)
            if pull is not None:
                await _to_thread(pull, repo)
        except Exception:                              # noqa: BLE001
            logger.warning("poll: git fetch failed; reading the current refs",
                           exc_info=True)

        reader = getattr(channel, "_gitbus_read", None)
        if reader is None:
            outcome.skipped = "no_reader"
            return outcome

        raw_items = await _to_thread(reader, repo)
        if _needs_baseline(repo, alias):
            # Review P0-4: without a usable ledger (first cycle, deleted or corrupt) the
            # cycle records what is already on the bus and delivers none of it; only
            # messages that arrive later reach the agent. Deferred entries are left out,
            # so they still drain; the rest of the cycle (drain, lanes, heartbeat) runs.
            pending = {entry["id"] for entry in _load_deferred(repo, alias)}
            baseline: Dict[str, None] = {}
            for item in raw_items:
                try:
                    message_id = str(getattr(item[0], "message_id", "") or "")
                except Exception:                      # noqa: BLE001
                    continue
                if message_id and message_id not in pending:
                    baseline[message_id] = None
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            _save_seen(repo, alias, baseline, since=stamp, force=True)
            logger.info("bus: no seen ledger for %s: %d message(s) already on the bus "
                        "recorded as seen, none delivered", alias, len(baseline))
            outcome.skipped = "baseline"
            raw_items = []
        seen = _load_seen(repo, alias)
        since = _ledger_since(repo, alias)
        deferred = _load_deferred(repo, alias)

        me_ids = {cfg.identity.alias.strip().lower(),
                  cfg.identity.name.lstrip("@").lower()}

        def unseen_mail_for_me(message: Any) -> bool:
            """Whether a rejection is worth reporting: unseen, addressed here, not ours."""
            sender_id = str(getattr(getattr(message, "sender", None), "agent_id", "")
                            or "").strip().lower()
            to = str(getattr(getattr(message, "recipient", None), "agent_id", "")
                     or "").strip().lower()
            return (str(getattr(message, "message_id", "") or "") not in seen
                    and not _older_than(message, since) and sender_id not in me_ids
                    and (to in me_ids or to in {"", "*"}))

        items = _accepted_items(raw_items, cfg, outcome, relevant=unseen_mail_for_me)

        # Track whether every addressable message in this cycle was already seen,
        # so we can surface "already_delivered" as a distinct skip reason.
        addressable_seen = 0
        addressable_total = 0

        # 2. Pre-classify and load deferred actionable messages onto the cycle.
        # Freeze stays bus-visible (issue #59): FREEZE/RESUME envelopes apply
        # even while the agent is frozen; actionable bodies are deferred, not
        # dropped or marked seen.
        # Review B5: the CLI path never calls channel.start() — apply the
        # persisted freeze flag here (quietly; the transport owns the
        # re-announce) so the outage poller defers instead of injecting
        # actionable mail into a frozen agent (the #49.F persistence class).
        _restore_freeze_flag(channel)

        # Review B4: the admin gate is only meaningful when the caller knows the
        # AUTHOR's admin identity (the transport path passes it). Defaulting to
        # the configured admin id compared it to itself — always True — which
        # let any bus writer broadcast-freeze the fleet. No author ⇒ no admin.
        is_frozen_now = bool(getattr(channel, "_frozen", False))
        admin_id = author_admin_id

        for item in items:
            try:
                message = item[0]
            except Exception:                          # noqa: BLE001
                continue
            message_id = str(getattr(message, "message_id", "") or "")
            if not message_id:
                continue

            text = _gitbus_text(message)
            envelope, body = parse_message(text)

            sender = str(getattr(getattr(message, "sender", None), "agent_id", "")
                         or "").strip().lower()
            recipient = str(getattr(getattr(message, "recipient", None),
                                    "agent_id", "") or "").strip().lower()
            is_own_message = sender in me_ids
            is_for_me = recipient in me_ids or recipient in {"", "*"}
            # A *bus* broadcast is one whose bus recipient is the wildcard. The
            # envelope ``to`` field on a non-envelope body is unrelated; the
            # original ``check_gitbus`` only looked at the bus recipient.
            is_broadcast = recipient in {"*", ""}

            # Track "already delivered" before we skip seen ids. Only messages
            # addressed to this agent count toward the dedupe skip signal (so an
            # empty inbox does not look like "already delivered").
            if is_for_me and not is_own_message:
                addressable_total += 1
                if message_id in seen:
                    addressable_seen += 1

            if message_id in seen or _older_than(message, since):
                continue

            # Transport-compat mode preserves the legacy ``check_gitbus`` rules:
            # bus broadcasts (``*``/empty recipient) and this agent's own posts
            # were silently skipped. Machine-class envelopes were NOT special on
            # that path (legacy had no classifier) — addressed ones were
            # injected like any other message (review B7).
            if transport_compat:
                if is_broadcast or is_own_message or not recipient:
                    continue

            # Machine-class traffic (HELLO/RECEIPT/ALERT etc) is recorded as state.
            # Transport-compat stays on the LEGACY path (no classifier there —
            # such envelopes were injected like any other addressed message).
            if not transport_compat and _is_machine_event(envelope) and (is_for_me or is_broadcast):
                outcome.machine_events.append(message_id)
                outcome.delivered.append(message_id)
                seen[message_id] = None
                # Review B8: update the token-economy state surfaces
                # (_peer_last_seen / _machine_log / stats) and resolve
                # NOTE intent=CLOSED tickets — the same bookkeeping the
                # in-transport path performs.
                handler = getattr(channel, "_handle_machine_event", None)
                if handler is not None:
                    try:
                        handler(envelope, _bus_channel_event(text, sender, cfg))
                    except Exception:                  # noqa: BLE001
                        logger.warning("poll: machine-event recording failed",
                                       exc_info=True)
                continue

            # Freeze-control envelopes apply even while frozen. They are admin-only
            # at the broadcast level (existing channel invariant).
            if _is_freeze_control(envelope) and is_for_me:
                if not _addresses_me(envelope, cfg):
                    # Review P0-1 F2: the signed payload names another agent, so the
                    # unsigned header was re-addressed. Record it; never act on it,
                    # and never hand it to the adapter as an ordinary turn.
                    logger.warning("poll: %s is a %s for %r under a header addressed "
                                   "here; ignored", message_id, envelope.intent,
                                   envelope.to)
                    outcome.machine_events.append(message_id)
                    seen[message_id] = None
                    continue
                payload_to = (envelope.to or "").strip().lstrip("@")   # @* is * too
                if (payload_to in {"", "*"} or recipient == "*") and \
                        cfg.limits.require_admin_for_broadcast and \
                        not cfg.is_admin(admin_id):
                    outcome.machine_events.append(message_id)
                    seen[message_id] = None
                    continue
                if (envelope.intent or "").upper() == "FREEZE":
                    if hasattr(channel, "_set_frozen"):
                        channel._set_frozen(True, envelope.ref)
                else:
                    if hasattr(channel, "_set_frozen"):
                        channel._set_frozen(False, envelope.ref)
                outcome.delivered.append(message_id)
                seen[message_id] = None
                continue

            # Own message or not for us: skip without bookkeeping.
            if is_own_message or not is_for_me:
                if not is_for_me:
                    continue
                continue

            # Review P0-1 F3 (partial until P1-3 signs the header): a signed envelope
            # whose own to= names another agent was re-addressed; it is not ours.
            if not _addresses_me(envelope, cfg):
                logger.warning("poll: %s is addressed to %r inside its signed text; "
                               "not delivered here", message_id, envelope.to)
                seen[message_id] = None              # review R2: warn once, not every cycle
                continue

            # Actionable body addressed to us.
            if not text:
                logger.warning("poll: %s carries no text payload; skipped", message_id)
                continue
            if is_frozen_now and hasattr(channel, "_frozen"):
                # Defer; do NOT mark seen (so a future unfreeze delivers it).
                # Capture the payload too: a fetch-window rollover must not lose
                # it (codex review of PR #60).
                if not any(entry["id"] == message_id for entry in deferred):
                    deferred.append({"id": message_id, "text": text, "sender": sender,
                                     "accepted": True})
                outcome.deferred.append(message_id)
                continue

            # A transport-written mirror of a Telegram hand-off: run it only if this bot
            # missed the Telegram copy (dropped by a busy queue, a cooldown, an outage).
            payload_dict = getattr(message, "payload", None)
            tg_ref = payload_dict.get("telegram") if isinstance(payload_dict, dict) else None
            took = getattr(channel, "took_telegram_copy", None)
            if isinstance(tg_ref, dict) and callable(took) and _took(took, tg_ref, sender):
                logger.info("poll: %s mirrors Telegram message %s, already handled here",
                            message_id, tg_ref.get("message_id"))
                outcome.machine_events.append(message_id)
                seen[message_id] = None
                continue
            if isinstance(tg_ref, dict):
                logger.info("poll: %s mirrors Telegram message %s, which never reached a "
                            "turn here; running it from the bus", message_id,
                            tg_ref.get("message_id"))

            # 3. Inject via the adapter (state stays in scope for verify hook).
            # Issue #59 spec amendment (1): every scheduled-poller injection
            # MUST carry a unique planted token — the token IS the delivery-probe
            # key in every adapter store. Default: the message id; when a
            # reply-store ticket is in scope (none on the pure-bus path), reuse it.
            adapter: AgentAdapter = channel.adapter
            token = planted_token_for(message, ticket_id=None)
            outcome.tokens[message_id] = token
            incoming = _bus_incoming(text, sender, cfg, token, channel)
            try:
                async with _Admission(channel, refresh):
                    reply = await adapter.respond(incoming)
            except Exception:                          # noqa: BLE001 - retry on a later poll
                logger.exception("poll: adapter failed on %s; deferring",
                                 message_id)
                if not any(entry["id"] == message_id for entry in deferred):
                    deferred.append({"id": message_id, "text": text, "sender": sender,
                                     "accepted": True})
                outcome.deferred.append(message_id)
                continue

            outcome.adapter_replies.append((message_id, reply))
            outcome.delivered.append(message_id)
            seen[message_id] = None

            # 4. Verify the adapter actually accepted/recorded the message
            # (issue #59 deliverable 6). Probe errors degrade silently; clean
            # negatives ×N escalate. Only run for adapters that opt in.
            verified = await _verify_with_degrade(
                adapter, incoming, channel=channel, refresh=refresh,
                attempts=delivery_verify_attempts,
                delay_seconds=delivery_verify_delay_seconds,
                settle_seconds=settle_seconds,
                settle_sleep=settle_sleep,
                outcome=outcome, message_id=message_id,
                writer=writer, escalator=escalator,
                sender_alias=alias, recipient=sender,
                token=token,
            )
            if verified is True:
                outcome.delivery_verified.append(message_id)
            elif verified is False:
                outcome.delivery_unknown.append(message_id)

            # 5. Telegram echo: only attempt when the transport is up (the CLI /
            # offline path has no sender). Never retried, never queued for
            # reconnect (issue #59).
            if not is_frozen_now:
                await _echo_reply(channel, sender, reply, outcome,
                                  thread_id=tg_ref.get("thread") if isinstance(tg_ref, dict)
                                  else None, about=text,
                                  message_id=tg_ref.get("message_id")
                                  if isinstance(tg_ref, dict) else None)

        # After the loop, refresh the freeze flag in case RESUME arrived.
        is_frozen_now = bool(getattr(channel, "_frozen", False))

        # No new work in this cycle? Surface the dedupe-only skip reason for callers.
        if not outcome.delivered and not outcome.deferred and \
                addressable_total > 0 and addressable_seen == addressable_total:
            outcome.skipped = "already_delivered"

        # 6. Drain deferred messages once unfrozen. Payloads were captured at
        # defer time, so a fetch-window rollover cannot lose them (codex review
        # of PR #60); drained injections get the SAME verify + echo treatment
        # as the main loop — no seen-without-verify.
        if not is_frozen_now and deferred:
            still_deferred: List[Dict[str, str]] = []
            for entry in list(deferred):
                message_id = entry["id"]
                if message_id in seen:
                    continue
                text, sender = entry.get("text") or "", entry.get("sender") or ""
                if text and not entry.get("accepted") and not cfg.bus.allow_unsigned:
                    # Captured before signatures were checked: its origin is unknown.
                    logger.warning("poll: dropped deferred %s, captured before bus "
                                   "signatures were checked", message_id)
                    outcome.rejected.append({"id": message_id, "sender": sender,
                                             "reason": "deferred before verification"})
                    continue
                if not text:
                    # Legacy entry (pre-payload format): recover from the window
                    # when still visible; otherwise there is nothing to deliver.
                    target = None
                    for item in items:
                        try:
                            candidate = item[0]
                        except Exception:
                            continue
                        if str(getattr(candidate, "message_id", "")) == message_id:
                            target = candidate
                            break
                    if target is None:
                        logger.warning("poll: deferred %s has no captured payload "
                                       "(legacy format) and left the window; escalating",
                                       message_id)
                        # Nit from codex's delta re-check: a legacy payload-less
                        # entry that left the window must not vanish silently.
                        await _escalate_blocked(
                            outcome=outcome, writer=writer, escalator=escalator,
                            sender_alias=alias, recipient="(unknown)",
                            message_id=message_id,
                            body="deferred entry has no captured payload (legacy "
                                 "format) and left the fetch window")
                        outcome.delivery_unknown.append(message_id)
                        continue
                    text = _gitbus_text(target)
                    sender = str(getattr(getattr(target, "sender", None), "agent_id", "")
                                 or "").strip().lower()
                envelope, _body = parse_message(text)
                if _is_machine_event(envelope):
                    seen[message_id] = None
                    outcome.machine_events.append(message_id)
                    continue
                drain_token = planted_token_for(message_id, ticket_id=None)
                outcome.tokens[message_id] = drain_token
                incoming = _bus_incoming(text, sender, cfg, drain_token, channel)
                try:
                    async with _Admission(channel, refresh):
                        reply = await channel.adapter.respond(incoming)
                except Exception:                          # noqa: BLE001
                    logger.exception("poll: deferred adapter failed on %s",
                                     message_id)
                    still_deferred.append(entry)
                    continue
                outcome.adapter_replies.append((message_id, reply))
                outcome.delivered.append(message_id)
                seen[message_id] = None
                verified = await _verify_with_degrade(
                    channel.adapter, incoming, channel=channel, refresh=refresh,
                    attempts=delivery_verify_attempts,
                    delay_seconds=delivery_verify_delay_seconds,
                    settle_seconds=settle_seconds,
                    settle_sleep=settle_sleep,
                    outcome=outcome, message_id=message_id,
                    writer=writer, escalator=escalator,
                    sender_alias=alias, recipient=sender,
                    token=drain_token,
                )
                if verified is True:
                    outcome.delivery_verified.append(message_id)
                elif verified is False:
                    outcome.delivery_unknown.append(message_id)
                await _echo_reply(channel, sender, reply, outcome, about=text)
            deferred = still_deferred

        _save_seen(repo, alias, seen, since=since)
        _save_deferred(repo, alias, deferred)
        # The lanes run after the v1 ledgers are saved, so a crash in a long lane turn
        # cannot re-deliver this cycle's v1 messages.
        if getattr(cfg.bus, "swarm", None):
            await _swarm_lane_step(
                channel, cfg, repo, outcome, frozen=is_frozen_now, escalator=escalator,
                refresh=refresh,
                verify=lambda incoming, env_id, token, sender: _verify_with_degrade(
                    channel.adapter, incoming, channel=channel, refresh=refresh,
                    attempts=delivery_verify_attempts,
                    delay_seconds=delivery_verify_delay_seconds,
                    settle_seconds=settle_seconds, settle_sleep=settle_sleep,
                    outcome=outcome, message_id=env_id,
                    # A lane problem is not a v1 message: no BLOCKED in .agents/inbox.
                    writer=None, escalator=escalator,
                    sender_alias=alias, recipient=sender, token=token))
        transport_meta: Optional[Dict[str, Any]] = None
        if transport_compat:
            # Issue #123: this tick IS the long-running transport; stamp the beat
            # with its identity + endpoint health so supervision can tell a live
            # transport from a masquerading poller.
            endpoint_ok: Optional[bool] = None
            probe = getattr(getattr(channel, "adapter", None), "endpoint_ok", None)
            if callable(probe):
                try:
                    endpoint_ok = probe()
                except Exception:                      # noqa: BLE001 - liveness is best-effort
                    endpoint_ok = None
            pid = os.getpid()
            transport_meta = {
                "pid": pid,
                "host": socket.gethostname(),
                "started_at": process_start_time(pid),
                "endpoint_ok": endpoint_ok,
            }
        _write_heartbeat(repo, alias, outcome, transport=transport_meta)
        busy = outcome.delivered or outcome.deferred or outcome.machine_events \
            or outcome.escalations
        # An idle cycle every minute is noise at INFO; one that did something is not.
        logger.log(logging.INFO if busy else logging.DEBUG,
                   "gitbus: poll cycle complete (delivered=%d deferred=%d "
                   "machine_events=%d escalations=%d)",
                   len(outcome.delivered), len(outcome.deferred),
                   len(outcome.machine_events), len(outcome.escalations))
        return outcome
    finally:
        release_poll_lock(repo, alias)


def _lane_cursor_path(repo_root: Path, alias: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]", "", (alias or "agent").strip().lower()) or "agent"
    return Path(repo_root) / ".agents" / "state" / f"lanes-{safe}.json"


#: A lane item is dead-lettered after this many failed injections, once they also span
#: at least LANE_DEAD_LETTER_SECONDS. Retries back off (lane_retry_backoff).
MAX_LANE_ATTEMPTS = 3
LANE_DEAD_LETTER_SECONDS = 30 * 60.0
#: An agent pages the owner about stuck lane items at most this often; items
#: dead-lettered in between are folded into the next page.
STUCK_PAGE_INTERVAL_SECONDS = 60 * 60.0
_lane_clock = time.time


def lane_retry_backoff(failures: int) -> float:
    """Seconds to wait before retrying an item that has failed ``failures`` times."""
    return min(5 * 60.0 * (2 ** max(failures - 1, 0)), 60 * 60.0)   # 5, 10, 20 ... 60 min


async def _page_stuck(cursor: Any, batch: Any, outcome: PollOutcome, agent: str,
                      failure: str, escalator: Optional[Callable[[Dict[str, str]], Any]],
                      save: Callable[[], bool]) -> None:
    """One owner page for everything this agent has dead-lettered (rate-limited)."""
    from swarmly.swarm.inbox import escalate_stuck
    now = _lane_clock()
    if now - cursor.last_stuck_page < STUCK_PAGE_INTERVAL_SECONDS:
        logger.info("poll: %d dead-lettered lane item(s) wait for the next stuck page",
                    len(cursor.unpaged))
        return
    items = list(cursor.unpaged)
    escalation = {"intent": "STUCK", "envelope_id": items[0], "ticket_id": items[0],
                  "message_id": items[0],
                  "body": f"{agent} could not take lane item(s) {', '.join(items)}: {failure}"}
    outcome.escalations.append(escalation)
    if escalator is not None:
        try:
            escalator(escalation)
        except Exception:                              # noqa: BLE001
            logger.warning("poll: escalator raised", exc_info=True)
    task_ids = {env.task_id for env in batch.deliver if env.id in items and env.task_id}
    task_id = task_ids.pop() if len(task_ids) == 1 else None
    if await _to_thread(escalate_stuck, batch, items, failure, task_id):
        cursor.last_stuck_page = now
        cursor.unpaged = []
        save()


async def _swarm_lane_step(channel: Any, cfg: AgentConfig, repo: Path,
                           outcome: PollOutcome, *, frozen: bool,
                           verify: Callable[..., Any],
                           escalator: Optional[Callable[[Dict[str, str]], Any]] = None,
                           refresh: Optional[Callable[[], Any]] = None) -> None:
    """Deliver the a2a/2 lane envelopes addressed to this agent (phase 2 wire-in).

    Only envelopes the projection replayed are delivered: anything unsigned, forged,
    off-chain or illegal under the protocol is quarantined there, logged once here and
    recorded in ``outcome.lane_quarantined``. Each lane has a delivery cursor, saved after
    every accepted envelope, so a failed injection is retried on the next cycle and a
    crash repeats at most the envelope in flight. An item that fails
    ``MAX_LANE_ATTEMPTS`` times is dead-lettered (its lane moves on) and escalated to the
    owner as stuck. While frozen nothing is delivered and the cursors stay put. Lane ids
    appear only in the ``lane_*`` outcome fields.
    """
    agent = (cfg.bus.swarm_agent or cfg.identity.alias or "").strip().lower()
    if not getattr(cfg.adapter, "enabled", True):
        # Nothing could take the mail: leave it on the lanes rather than burn attempts.
        logger.info("poll: adapter disabled; swarm lanes left for later")
        return
    try:
        from swarmly.swarm.inbox import (LaneCursor, mark_delivered,
                                     read_lanes, render_catch_up, render_prompt)
        cursor = LaneCursor.load(_lane_cursor_path(repo, cfg.identity.alias), agent)
        batch = await _to_thread(read_lanes, cfg.bus.swarm, agent, cursor)
    except Exception as exc:                           # noqa: BLE001 - v1 delivery stands
        outcome.lane_error = f"{type(exc).__name__}: {exc}"
        logger.warning("poll: swarm lanes unreadable (%s); skipped this cycle",
                       outcome.lane_error, exc_info=True)
        return
    outcome.lane_quarantined.extend(batch.quarantined)

    def save() -> bool:
        if cursor.save():
            return True
        # Delivering on without a durable cursor would repeat every item next cycle.
        outcome.lane_error = f"could not save the lane cursor {cursor.path}"
        return False

    if cursor.catch_up_pending and not batch.next_info.get("obligations"):
        cursor.catch_up_pending = False              # nothing is owed any more
    if frozen:
        waiting = [env.id for env in batch.deliver]
        if cursor.catch_up_pending:
            waiting.insert(0, f"{agent}:catch-up")
        if waiting:
            logger.info("poll: frozen; %d lane item(s) wait for RESUME", len(waiting))
        outcome.lane_deferred.extend(waiting)
        save()
        return
    if not save():
        return
    prompts = [(None, render_catch_up(batch.next_info), "swarm")] \
        if cursor.catch_up_pending else []
    prompts += [(env, render_prompt(env, batch.next_info), env.sender)
                for env in batch.deliver]
    blocked: set = set()
    last_failure = "see the agent's poll log"
    for env, text, sender in prompts:
        env_id = env.id if env is not None else f"{agent}:catch-up"
        if env is not None and env.lane in blocked:
            outcome.lane_deferred.append(env_id)
            continue                     # keep each lane in order: retry from here next time
        previous = cursor.failures.get(env_id)
        if previous is not None and _lane_clock() < previous.get("last", 0.0) + \
                lane_retry_backoff(int(previous["count"])):
            # Each attempt can be a paid model turn: a failing item waits out its backoff
            # even when a doorbell asks for an early poll.
            if env is not None:
                blocked.add(env.lane)
            outcome.lane_deferred.append(env_id)
            continue
        token = planted_token_for(env_id, ticket_id=None)
        outcome.tokens[env_id] = token
        incoming = _bus_incoming(text, sender, cfg, token, channel)
        failure: Optional[str] = None
        reply: Optional[AdapterReply] = None
        try:
            async with _Admission(channel, refresh):
                reply = await channel.adapter.respond(incoming)
        except Exception as exc:                       # noqa: BLE001 - retried next cycle
            logger.exception("poll: adapter raised on lane item %s", env_id)
            failure = f"{type(exc).__name__}: {exc}"
        else:
            if reply is None:
                failure = "the adapter took no turn (disabled, or the prompt was refused)"
            elif not getattr(reply, "ok", True):
                failure = getattr(reply, "error", "") or "the adapter reported a failure"
        if failure is not None:
            now = _lane_clock()
            record = cursor.failures.get(env_id) or {"count": 0, "first": now}
            record["count"] += 1
            record["last"] = now
            cursor.failures[env_id] = record
            failing_for = now - record["first"]
            if record["count"] < MAX_LANE_ATTEMPTS or failing_for < LANE_DEAD_LETTER_SECONDS:
                # Several attempts can come seconds apart (each doorbell starts a poll), so
                # an item is given up on only once its failures also span a real interval.
                logger.warning("poll: lane item %s not delivered (attempt %d, failing for "
                               "%.0f min; next try in %.0f min): %s", env_id,
                               int(record["count"]), failing_for / 60,
                               lane_retry_backoff(int(record["count"])) / 60, failure)
                if env is not None:
                    blocked.add(env.lane)
                outcome.lane_deferred.append(env_id)
                if not save():
                    return
                continue
            logger.error("poll: lane item %s failed %d times over %.0f minutes; "
                         "dead-lettered. Last error: %s", env_id, int(record["count"]),
                         failing_for / 60, failure)
            outcome.lane_dead_letters.append(env_id)
            cursor.failures.pop(env_id, None)
            cursor.unpaged.append(env_id)
            last_failure = failure
            if env is None:
                cursor.catch_up_pending = False
            else:
                mark_delivered(cursor, env, batch.bad)
            if not save():
                return
            continue
        cursor.failures.pop(env_id, None)
        outcome.adapter_replies.append((env_id, reply))
        outcome.lane_delivered.append(env_id)
        if env is None:
            cursor.catch_up_pending = False
        else:
            mark_delivered(cursor, env, batch.bad)
        if not save():
            return
        if reply is not None and getattr(reply, "text", ""):
            # Lane answers go through a2a_post; a plain reply reaches no one.
            logger.info("poll: %s answered %s in text, which reaches no one: %s",
                        agent, env_id, reply.text[:500])
        if env is None:
            continue                     # nothing to escalate to: the catch-up has no sender
        verified = await verify(incoming, env_id, token, sender)
        if verified is True:
            outcome.delivery_verified.append(env_id)
        elif verified is False:
            outcome.delivery_unknown.append(env_id)
    if cursor.unpaged:
        await _page_stuck(cursor, batch, outcome, agent, last_failure, escalator, save)
    if outcome.lane_delivered and outcome.skipped == "already_delivered":
        outcome.skipped = None
    logger.info("poll: swarm lanes delivered %d to %s (%d waiting, %d dead-lettered, "
                "%d quarantined)", len(outcome.lane_delivered), agent,
                len(outcome.lane_deferred), len(outcome.lane_dead_letters),
                len(batch.quarantined))


def _bounded_settle_seconds(value: Any) -> float:
    """Clamp a configured settle window into ``[0, MAX_VERIFY_SETTLE_SECONDS]``.

    Issue #80: the window is config-declared but must never stall a poll cycle,
    so anything above the cap is capped (and anything unparseable falls back to
    the default). ``0`` disables the settle entirely.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_VERIFY_SETTLE_SECONDS
    if seconds != seconds or seconds < 0:               # NaN or negative
        return 0.0
    return min(seconds, MAX_VERIFY_SETTLE_SECONDS)


async def _settle_window(sleep_fn, seconds: float) -> None:
    """Await the injected sleep callable (default: a thread-pool ``time.sleep``).

    The callable receives the number of seconds; async callables are awaited.
    Injection keeps the settle deterministic in tests (fake clock, no waiting).
    """
    if sleep_fn is None:
        await _to_thread(time.sleep, seconds)
        return
    result = sleep_fn(seconds)
    if inspect.isawaitable(result):
        await result


def _verify_hook_opted_in(adapter: Any) -> bool:
    """Whether the adapter overrides the base no-op ``verify_delivery``.

    ``AgentAdapter`` always defines the hook (returning ``None`` = unknown), so
    ``hasattr`` alone cannot tell a real probe from the default. Only a real
    probe pays the settle window (issue #80) — an adapter that never reports a
    verdict has nothing to settle for.
    """
    hook = getattr(adapter, "verify_delivery", None)
    if hook is None:
        return False
    function = getattr(hook, "__func__", None)
    if function is None:
        return True                # instance-level/injected callable — opted in
    return function is not getattr(AgentAdapter, "verify_delivery", None)


async def _verify_with_degrade(adapter, message, *, attempts, delay_seconds,
                               outcome, message_id, writer, escalator,
                               sender_alias, recipient, token=None,
                               settle_seconds: float = 0.0,
                               settle_sleep=None, channel: Any = None,
                               refresh: Optional[Callable[[], Any]] = None) -> Optional[bool]:
    """Run the adapter verify_delivery hook with the degrade rule.

    Returns:
        * ``True``  — a clean probe confirmed the delivery.
        * ``False`` — clean negative on every attempt; escalation triggered.
        * ``None``  — adapter did not implement the hook, or only probe errors.
    """
    if not hasattr(adapter, "verify_delivery"):
        return None
    if attempts <= 0:
        attempts = 1
    if settle_seconds > 0 and _verify_hook_opted_in(adapter):
        # Issue #80: a just-delivered message is not yet visible to the session
        # (the probe raced it in production), so hold the first probe for the
        # declared settle window. Errors here must never break the probe path.
        try:
            await _settle_window(settle_sleep, settle_seconds)
        except Exception:                              # noqa: BLE001 - settle is best-effort
            logger.warning("poll: settle window failed; probing anyway",
                           exc_info=True)
    clean_negatives = 0
    for attempt in range(1, attempts + 1):
        try:
            # A probe is a turn too: take the channel's admission for it (not for the
            # waits between attempts), so it never runs beside a chat turn.
            async with _Admission(channel, refresh):
                reply = await adapter.verify_delivery(message)
        except Exception:                              # noqa: BLE001 - hard degrade rule
            logger.warning("poll: verify_delivery probe error on %s "
                           "(attempt %d/%d); degrading", message_id, attempt, attempts,
                           exc_info=True)
            return None
        if reply is None:
            # Adapter hook contract: ``None`` = not implemented / unknown —
            # degrade silently, never a clean negative (review B1).
            return None
        ok = bool(getattr(reply, "ok", False))
        if ok:
            return True
        clean_negatives += 1
        if delay_seconds and attempt < attempts:
            await _to_thread(time.sleep, delay_seconds)
    if clean_negatives >= attempts:
        # Escalate BLOCKED + bus write (the bus record is the durable signal;
        # a Telegram alert is a bonus the caller adds when the transport is up).
        await _escalate_blocked(
            outcome=outcome, writer=writer, escalator=escalator,
            sender_alias=sender_alias, recipient=recipient,
            message_id=message_id,
            body=f"delivery probe returned no signal after {attempts} attempts")
        return False
    return None


async def _escalate_blocked(*, outcome, writer, escalator, sender_alias, recipient,
                            message_id, body) -> None:
    """Emit a BLOCKED escalation for ``message_id``: state + bus envelope.

    The envelope carries the idempotence keys (envelope id, ticket id, and the
    original message id) so a reconnect-side receipt can dedupe it. Shared by
    the verify-clean-negative path and the legacy-deferred-drop path.
    """
    envelope_id = f"poll-{message_id}"
    ticket_id = message_id
    envelope_payload = build_fallback_envelope(
        sender=sender_alias, recipient=recipient,
        original_message_id=message_id,
        envelope_id=envelope_id, ticket_id=ticket_id, body=body,
        envelope_type=MessageType.BLOCKED)
    outcome.escalations.append({
        "intent": "BLOCKED",
        "envelope_id": envelope_id,
        "ticket_id": ticket_id,
        "message_id": str(message_id),
    })
    if escalator is not None:
        try:
            escalator({"intent": "BLOCKED",
                       "envelope_id": envelope_id,
                       "ticket_id": ticket_id,
                       "body": envelope_payload,
                       "message_id": message_id})
        except Exception:                              # noqa: BLE001
            logger.warning("poll: escalator raised", exc_info=True)
    if writer is not None:
        try:
            # Review P1-5: the gitbus writer commits and pushes; never on the event loop.
            await _to_thread(writer, sender_alias, recipient, envelope_payload)
            outcome.bus_writes.append({
                "from": sender_alias, "to": recipient,
                "envelope_id": envelope_id})
        except Exception as exc:                       # noqa: BLE001
            # Issue #82: a swallowed write failure must not look like a
            # persisted escalation — count it and keep the reason.
            outcome.bus_write_failures += 1
            outcome.bus_write_failure_details.append(
                f"{envelope_id}: {type(exc).__name__}: {exc}")
            logger.warning("poll: bus writer raised", exc_info=True)


async def _to_thread(fn, *args, **kwargs):
    """Run a sync callable in a worker thread (asyncio.to_thread replacement
    that works on older Python and remains testable)."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# Make asyncio.to_thread available for the actual module if absent.
import asyncio  # noqa: E402
