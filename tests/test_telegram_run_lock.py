"""
Single-instance lock for ``cli run`` (issue #77).

``run`` had no guard of its own, so a restart task racing the live daemon could start
a second ``getUpdates`` consumer on the same bot token -- and Telegram splits the
updates between the two consumers SILENTLY. The transport now takes an exclusive lock
in the bus state dir, refuses a second start while a live holder owns it (exit 4, one
line naming the holder), takes over a stale marker on its own, and releases the lock
when it exits.

No Telegram token, no aiogram, no network: the refused path returns before any client
is built, and the takeover and release paths run through the lock core plus a real
second process that holds the marker for as long as the test needs it.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from swarmly.telegram import cli
from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.poll import (
    RUN_LOCK_EXIT_CODE,
    RUN_LOCK_WRITE_GRACE_SECONDS,
    acquire_run_lock,
    host_boot_stamp,
    pid_is_alive,
    process_start_time,
    release_run_lock,
    run_lock_path,
    run_lock_stale_reason,
    transport_state_root,
)

GROUP = -1001234567890
ADMIN_ID = 1234567890
REPO_ROOT = Path(__file__).resolve().parents[1]

# A real second process that takes the lock and blocks until its stdin closes. The
# acceptance paths need a LIVE holder (and, once killed, a genuinely dead pid), not a
# marker file pretending to be one.
_HOLDER_SOURCE = """
import json, os, sys
from pathlib import Path

from swarmly.telegram.poll import acquire_run_lock, release_run_lock, run_lock_path

root = Path(sys.argv[1])
result = acquire_run_lock(root, sys.argv[2])
print(json.dumps({"acquired": result.acquired, "pid": os.getpid(),
                  "path": str(run_lock_path(root, sys.argv[2]))}), flush=True)
sys.stdin.read()
if len(sys.argv) > 3 and sys.argv[3] == "release":
    release_run_lock(root, sys.argv[2])
"""


def _child_env() -> dict:
    """Env for child processes: Telegram credentials scrubbed, worktree importable."""
    env = {k: v for k, v in os.environ.items()
           if "TELEGRAM" not in k and "BOT_TOKEN" not in k}
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


class _Holder:
    """A live second process holding the ``run`` lock; ``stop()`` ends it."""

    def __init__(self, root: Path, alias: str = "Hermes", *, release_on_exit: bool = False):
        argv = [sys.executable, "-c", _HOLDER_SOURCE, str(root), alias]
        if release_on_exit:
            argv.append("release")
        self.proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), env=_child_env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True)
        try:
            self.acquired = json.loads(self.proc.stdout.readline())
        except Exception:
            self.kill()
            raise

    @property
    def pid(self) -> int:
        return int(self.acquired["pid"])

    def stop(self) -> None:
        """Let the holder exit (releasing the lock first when it was asked to)."""
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        self.proc.stdout.close()

    def kill(self) -> None:
        """Kill the holder outright: its marker stays, its pid goes dead."""
        self.proc.kill()
        self.proc.wait(timeout=30)
        self.proc.stdin.close()
        self.proc.stdout.close()


def _config(root: Path, **overrides) -> AgentConfig:
    data = {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
        "peers": {"Codex": "CodexBot"},
        "adapter": {"type": "echo"},
        "bus": {"poll_minutes": 10, "repo": str(root)},
        "limits": {"min_seconds_between_messages": 0},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return AgentConfig.from_dict(data)


class TestPidLiveness(unittest.TestCase):
    """The liveness probe the whole lock rests on."""

    def test_own_pid_is_alive_and_a_reaped_child_is_not(self):
        self.assertTrue(pid_is_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"], env=_child_env())
        child.wait(timeout=30)
        self.assertFalse(pid_is_alive(child.pid))
        self.assertFalse(pid_is_alive(0))
        self.assertFalse(pid_is_alive(99999999))


class TestTransportStateRoot(unittest.TestCase):
    """Where the lock lives: the bus checkout, never the process CWD."""

    def test_bus_repo_wins(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(transport_state_root(_config(Path(tmp))), Path(tmp))

    def test_fallback_gitbus_root_is_the_legacy_alias(self):
        with TemporaryDirectory() as tmp:
            config = AgentConfig.from_dict({
                "agent": {"name": "HermesBot", "alias": "Hermes",
                          "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
                "peers": {"Codex": "CodexBot"},
                "adapter": {"type": "echo"},
                "fallback": {"gitbus_root": str(tmp)},
                "limits": {"min_seconds_between_messages": 0},
            })
            self.assertEqual(transport_state_root(config), Path(tmp))

    def test_default_is_the_checkout_not_the_process_cwd(self):
        config = AgentConfig.from_dict({
            "agent": {"name": "HermesBot", "alias": "Hermes",
                      "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
            "peers": {"Codex": "CodexBot"},
            "adapter": {"type": "echo"},
            "limits": {"min_seconds_between_messages": 0},
        })
        with patch.dict(os.environ):
            os.environ.pop("SWARMLY_STATE_ROOT", None)  # the suite's own root, set aside
            self.assertEqual(transport_state_root(config).resolve(), REPO_ROOT)
            os.environ["SWARMLY_STATE_ROOT"] = str(Path("/srv/state"))
            self.assertEqual(transport_state_root(config), Path("/srv/state"))
            config.bus.repo = "/srv/bus"                # a bus repo still wins
            self.assertEqual(transport_state_root(config), Path("/srv/bus"))


# --------------------------------------------------------------------------------------
# Acceptance 1: a live holder refuses the second start, and is left untouched
# --------------------------------------------------------------------------------------
class TestLiveHolderRefusesASecondStart(unittest.TestCase):
    def test_second_start_is_refused_while_the_holder_runs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root)
            try:
                marker = run_lock_path(root, "Hermes")
                before = marker.read_bytes()
                result = acquire_run_lock(root, "Hermes")
                self.assertFalse(result.acquired)
                self.assertEqual(result.holder_pid, holder.pid)
                self.assertIsNotNone(result.holder_started_at)
                # A is unaffected: still alive, and its marker is byte-for-byte what it
                # wrote -- the refused start neither deleted nor rewrote it.
                self.assertTrue(pid_is_alive(holder.pid))
                self.assertEqual(marker.read_bytes(), before)
                self.assertEqual(run_lock_stale_reason(json.loads(before)), "")
            finally:
                holder.stop()

    def test_release_never_touches_a_marker_owned_by_another_pid(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root)
            try:
                release_run_lock(root, "Hermes")          # we do not hold it
                self.assertTrue(run_lock_path(root, "Hermes").exists())
                self.assertTrue(pid_is_alive(holder.pid))
            finally:
                holder.stop()


# --------------------------------------------------------------------------------------
# Acceptance 2: stale markers (dead pid, recycled pid, another boot) are taken over
# --------------------------------------------------------------------------------------
class TestStaleMarkersAreTakenOver(unittest.TestCase):
    def _marker(self, root: Path, **fields) -> Path:
        path = run_lock_path(root, "Hermes")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fields), encoding="utf-8")
        return path

    def test_dead_pid_marker_is_taken_over(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root)
            holder.kill()                        # a killed transport leaves its marker
            self.assertFalse(pid_is_alive(holder.pid))
            result = acquire_run_lock(root, "Hermes")
            try:
                self.assertTrue(result.acquired)
                self.assertIn("gone", result.note)
                record = json.loads(run_lock_path(root, "Hermes").read_text(encoding="utf-8"))
                self.assertEqual(record["pid"], os.getpid())       # names the new pid
            finally:
                release_run_lock(root, "Hermes")

    def test_recycled_pid_marker_is_taken_over(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = process_start_time(os.getpid()) or time.time()
            self._marker(root, pid=os.getpid(), started_at=time.time() - 86_400,
                         pid_start=started - 86_400, boot=host_boot_stamp())
            result = acquire_run_lock(root, "Hermes")
            try:
                self.assertTrue(result.acquired)
                self.assertIn("recycled", result.note)
                record = json.loads(run_lock_path(root, "Hermes").read_text(encoding="utf-8"))
                self.assertEqual(record["pid"], os.getpid())
            finally:
                release_run_lock(root, "Hermes")

    def test_marker_from_an_earlier_boot_is_taken_over(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._marker(root, pid=os.getpid(), started_at=time.time() - 86_400,
                         pid_start=process_start_time(os.getpid()),
                         boot=(host_boot_stamp() or time.time()) - 86_400)
            result = acquire_run_lock(root, "Hermes")
            try:
                self.assertTrue(result.acquired)
                self.assertIn("rebooted", result.note)
            finally:
                release_run_lock(root, "Hermes")

    def test_marker_with_malformed_fields_never_raises_and_stays_held(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._marker(root, pid=os.getpid(), started_at="whenever",
                         pid_start="not-a-time", boot="not-a-stamp")
            result = acquire_run_lock(root, "Hermes")     # our own pid, live
            self.assertFalse(result.acquired)
            self.assertEqual(result.holder_pid, os.getpid())
            self.assertIsNone(result.holder_started_at)   # nothing usable to name

    def test_unreadable_marker_is_held_while_fresh_and_reclaimed_once_settled(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = run_lock_path(root, "Hermes")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("{still being written", encoding="utf-8")
            fresh = acquire_run_lock(root, "Hermes")
            self.assertFalse(fresh.acquired)         # another start may be mid-write
            self.assertIsNone(fresh.holder_pid)      # nothing nameable yet
            settled = time.time() - RUN_LOCK_WRITE_GRACE_SECONDS - 5
            os.utime(marker, (settled, settled))     # a corrupt marker from long ago
            result = acquire_run_lock(root, "Hermes")
            try:
                self.assertTrue(result.acquired)
                self.assertIn("unreadable", result.note)
            finally:
                release_run_lock(root, "Hermes")


# --------------------------------------------------------------------------------------
# Acceptance 3: a clean exit releases the lock, and ``run`` releases what it took
# --------------------------------------------------------------------------------------
class TestReleaseOnExit(unittest.TestCase):
    def test_holder_that_exits_normally_lets_the_next_start_in(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root, release_on_exit=True)
            self.assertFalse(acquire_run_lock(root, "Hermes").acquired)   # live: refused
            holder.stop()                                                 # exits cleanly
            self.assertFalse(run_lock_path(root, "Hermes").exists())
            result = acquire_run_lock(root, "Hermes")
            try:
                self.assertTrue(result.acquired)      # the next start is not blocked
            finally:
                release_run_lock(root, "Hermes")
            self.assertFalse(run_lock_path(root, "Hermes").exists())

    def test_run_takes_over_a_stale_marker_and_releases_it_on_exit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = run_lock_path(root, "Hermes")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"pid": 99999999, "started_at": 0.0}),
                              encoding="utf-8")
            seen = {}

            async def _note_holder(channel):
                seen.update(json.loads(marker.read_text(encoding="utf-8")))

            async def _noop(channel):
                return None

            with patch.object(AgentChannel, "start", _note_holder), \
                    patch.object(AgentChannel, "stop", _noop):
                rc = asyncio.run(cli._run(_config(root)))
            self.assertEqual(rc, 0)                         # proceeded, not refused
            self.assertEqual(seen.get("pid"), os.getpid())  # held while it ran
            self.assertFalse(marker.exists())               # released on exit

    def test_run_releases_the_lock_when_the_channel_fails_to_start(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = run_lock_path(root, "Hermes")

            async def _boom(channel):
                raise RuntimeError("telegram unreachable")

            with patch.object(AgentChannel, "start", _boom):
                with self.assertRaises(RuntimeError):
                    asyncio.run(cli._run(_config(root)))
            self.assertFalse(marker.exists())               # released on the way out


# --------------------------------------------------------------------------------------
# The CLI itself: exit 4 and exactly one line naming the live holder
# --------------------------------------------------------------------------------------
class TestRunCLIRefusal(unittest.TestCase):
    def _config_file(self, root: Path) -> Path:
        path = root / "config.yaml"
        path.write_text(json.dumps({
            "agent": {"name": "HermesBot", "alias": "Hermes",
                      "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
            "peers": {"Codex": "CodexBot"},
            "adapter": {"type": "echo"},
            "bus": {"poll_minutes": 10, "repo": str(root)},
            "limits": {"min_seconds_between_messages": 0},
        }), encoding="utf-8")
        return path

    def test_run_exits_three_with_one_line_naming_the_live_holder(self):
        # The config carries NO token, so a ``run`` that reached the Telegram client
        # would fail loudly instead of exiting 3 -- the refusal is proven to happen
        # before anything is constructed, let alone contacted.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root)
            try:
                config_path = self._config_file(root)
                marker = run_lock_path(root, "Hermes")
                before = marker.read_bytes()
                proc = subprocess.run(
                    [sys.executable, "-m", "swarmly.telegram.cli",
                     "--config", str(config_path), "run"],
                    capture_output=True, text=True, cwd=str(REPO_ROOT),
                    env=_child_env(), timeout=120)
                self.assertEqual(proc.returncode, RUN_LOCK_EXIT_CODE, proc.stderr)
                lines = [line for line in proc.stdout.splitlines() if line.strip()]
                self.assertEqual(len(lines), 1, proc.stdout)      # ONE clear line
                self.assertIn("already running", lines[0])
                self.assertIn(str(holder.pid), lines[0])           # names the pid
                started = datetime.datetime.fromtimestamp(
                    json.loads(before)["started_at"]).astimezone().isoformat(
                        timespec="seconds")
                self.assertIn(started, lines[0])                   # names the start time
                # A is unaffected: still alive, marker byte-for-byte untouched.
                self.assertTrue(pid_is_alive(holder.pid))
                self.assertEqual(marker.read_bytes(), before)
            finally:
                holder.stop()


if __name__ == "__main__":
    unittest.main()


# Issue #106: ``AgentConfig.from_dict`` merges the developer's real ``.env`` into the
# process-global ``os.environ``; the guard keeps this module's tests from leaking it.
from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)
