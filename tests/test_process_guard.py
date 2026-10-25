"""Tests proving process management never signals a PID it did not spawn (#141).

Acceptance for #141 requires a test proving the suite and process lifecycle
managers never signal an unowned PID or an external process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from swarmly.telegram.children import Child, ChildRegistry, kill_pid, reap_orphans


class TestProcessSignalingSafety(unittest.TestCase):
    """Guards ensuring process reaping and timeout killers never signal unowned PIDs."""

    def test_reap_orphans_refuses_unspawned_pid_with_mismatched_identity(self) -> None:
        """A live PID on the system that does not match the recorded start time
        must never be signaled (guards against PID reuse or signaling host processes)."""
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "children.json"
            registry = ChildRegistry(registry_path)

            # Child recorded with start time 't_child_original', spawned by dead transport 9999
            registry.save(
                [
                    Child(
                        pid=12345,
                        transport_pid=9999,
                        transport_identity="t_transport",
                        identity="t_child_original",
                        command="python -m echo",
                    )
                ]
            )

            signaled: list[int] = []

            def mock_kill(pid: int) -> None:
                signaled.append(pid)

            # System process table: parent 9999 is dead. PID 12345 is live, but its start time
            # is 't_child_reused' (a different, unowned process).
            reaped = reap_orphans(
                registry_path,
                identity=lambda pid: "t_child_reused" if pid == 12345 else None,
                alive=lambda pid: pid == 12345,
                kill=mock_kill,
            )

            self.assertEqual(
                reaped, [], "Reaped list must be empty when identity does not match"
            )
            self.assertEqual(
                signaled,
                [],
                "Must never signal an unowned PID with mismatched identity",
            )

    def test_reap_orphans_refuses_when_parent_transport_is_live(self) -> None:
        """Children belonging to a live transport must never be signaled."""
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "children.json"
            registry = ChildRegistry(registry_path)
            registry.save(
                [
                    Child(
                        pid=54321,
                        transport_pid=1111,
                        transport_identity="parent_live",
                        identity="child_live",
                        command="python -m echo",
                    )
                ]
            )

            signaled: list[int] = []

            reaped = reap_orphans(
                registry_path,
                identity=lambda pid: "parent_live" if pid == 1111 else "child_live",
                alive=lambda pid: True,
                kill=lambda pid: signaled.append(pid),
            )

            self.assertEqual(reaped, [])
            self.assertEqual(
                signaled, [], "Must never signal child of a live transport"
            )

    def test_kill_pid_rejects_non_positive_and_invalid_pids(self) -> None:
        """kill_pid must cleanly refuse zero, negative, or invalid PID inputs."""
        with patch("subprocess.run") as mock_run, patch("os.kill") as mock_kill:
            for invalid_pid in (0, -1, -999, None, "123"):
                kill_pid(invalid_pid)  # type: ignore[arg-type]
            mock_run.assert_not_called()
            mock_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
