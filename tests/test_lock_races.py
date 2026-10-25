"""
The poll and ``run`` locks cannot be won by two contenders (review P1-6).

The poll lock used to be created with ``O_EXCL`` and written afterwards, so a contender
that read the still-empty marker took it for stale, deleted it and won as well. Both
locks now take an OS file lock first, and only its holder reads or replaces the marker.
Each test runs a real second process that holds the lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from swarmly.telegram import oslock
from swarmly.telegram.poll import (
    acquire_poll_lock,
    acquire_run_lock,
    poll_lock_path,
    release_poll_lock,
    release_run_lock,
    run_lock_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_HOLDER_SOURCE = """
import json, sys
from pathlib import Path

from swarmly.telegram import poll

root, alias, kind = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if kind == "poll":
    acquired = poll.acquire_poll_lock(root, alias)
else:
    acquired = poll.acquire_run_lock(root, alias).acquired
print(json.dumps({"acquired": acquired}), flush=True)
sys.stdin.read()
"""


class _Holder:
    """A second process holding the poll or run lock until ``stop()``."""

    def __init__(self, root: Path, kind: str):
        env = {k: v for k, v in os.environ.items()
               if "TELEGRAM" not in k and "BOT_TOKEN" not in k}
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_SOURCE, str(root), "Hermes", kind],
            cwd=str(REPO_ROOT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True)
        self.acquired = json.loads(self.proc.stdout.readline())["acquired"]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        self.proc.stdout.close()


class TestPollLockRace(unittest.TestCase):
    def test_a_half_written_marker_is_not_taken_for_stale(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root, "poll")
            try:
                self.assertTrue(holder.acquired)
                # What a contender saw between the old O_EXCL create and its write.
                poll_lock_path(root, "Hermes").write_text("", encoding="utf-8")
                self.assertFalse(acquire_poll_lock(root, "Hermes"))
            finally:
                holder.stop()

    def test_the_lock_is_free_once_its_holder_exits(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root, "poll")
            holder.stop()
            # The holder never released; the kernel dropped its OS lock, and the
            # marker it left (an exited pid, just written) is the only thing in the way.
            poll_lock_path(root, "Hermes").unlink()
            self.assertTrue(acquire_poll_lock(root, "Hermes"))
            release_poll_lock(root, "Hermes")

    def test_release_leaves_another_holder_alone(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root, "poll")
            try:
                release_poll_lock(root, "Hermes")
                self.assertTrue(poll_lock_path(root, "Hermes").exists())
                self.assertFalse(acquire_poll_lock(root, "Hermes"))
            finally:
                holder.stop()


class TestRunLockRace(unittest.TestCase):
    def test_a_contender_that_read_a_stale_marker_does_not_win(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = _Holder(root, "run")
            try:
                self.assertTrue(holder.acquired)
                # A contender that judged the marker stale just before the live holder
                # replaced it: the old code then deleted the live marker and won too.
                with patch("swarmly.telegram.poll._read_run_lock_marker",
                           return_value=({}, "pid 1 is gone")):
                    result = acquire_run_lock(root, "Hermes")
                self.assertFalse(result.acquired)
                self.assertTrue(run_lock_path(root, "Hermes").exists())
            finally:
                holder.stop()

    def test_release_frees_the_os_lock(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(acquire_run_lock(root, "Hermes").acquired)
            gate = oslock.gate_path(run_lock_path(root, "Hermes"))
            self.assertTrue(oslock.holds(gate))
            release_run_lock(root, "Hermes")
            self.assertFalse(oslock.holds(gate))
            self.assertTrue(acquire_run_lock(root, "Hermes").acquired)
            release_run_lock(root, "Hermes")


if __name__ == "__main__":
    unittest.main()
