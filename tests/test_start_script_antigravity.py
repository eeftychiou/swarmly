"""
The start script and its documentation must not drift apart.

``scripts/start_antigravity.ps1`` carries a ``$TaskTable`` that is the source of
truth for how this host is supervised, and ``docs/telegram/SETUP-ANTIGRAVITY.md``
section 6a documents the same three tasks for a human reader. Two hand-kept copies
of the same contract is exactly the arrangement that rots quietly: someone tunes a
cadence in one place, everything keeps working, and the doc becomes a lie that is
only discovered during a failure -- which is precisely when a wrong cadence in a
runbook costs the most.

This test pins them together. It reads both files as text (no PowerShell, no
Windows: it must run in the ubuntu-latest CI job like every other test here) and
fails if the task names, wrappers, cadences or execution caps disagree.

Prove it can fail by mutating one cadence in either file and re-running.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "start_antigravity.ps1"
SETUP_DOC = REPO / "docs" / "telegram" / "SETUP-ANTIGRAVITY.md"
GETTING_STARTED = REPO / "docs" / "GETTING-STARTED.md"

#: The tasks, in the order the script starts them. Order is part of the contract:
#: the watchdog must not start before the poller that feeds it a heartbeat.
EXPECTED_ORDER = ["a2a-agy-transport", "a2a-agy-bus-poll", "a2a-agy-heartbeat"]

#: The long-running transport must never be capped by Task Scheduler; the two
#: one-shot wrappers get a finite cap. Getting this backwards either kills the
#: transport mid-reply or lets a wedged cycle run forever.
TRANSPORT_TIME_LIMIT = "PT0S"


def _read(path: Path) -> str:
    if not path.exists():
        raise AssertionError(f"expected file is missing: {path}")
    return path.read_text(encoding="utf-8")


def parse_script_table() -> "list[dict]":
    """Extract the ``$TaskTable`` rows from the PowerShell script."""
    text = _read(SCRIPT)
    start = text.find("$TaskTable = @(")
    if start == -1:
        raise AssertionError("scripts/start_antigravity.ps1 has no $TaskTable block")
    end = text.find("\n)", start)
    if end == -1:
        raise AssertionError("$TaskTable block is not closed")

    rows = []
    for line in text[start:end].splitlines():
        match = re.search(
            r"Name\s*=\s*'([^']+)'\s*;\s*"
            r"Wrapper\s*=\s*'([^']+)'\s*;\s*"
            r"Minutes\s*=\s*(\d+)\s*;\s*"
            r"TimeLimit\s*=\s*'([^']+)'",
            line,
        )
        if match:
            rows.append(
                {
                    "name": match.group(1),
                    "wrapper": match.group(2),
                    "minutes": int(match.group(3)),
                    "time_limit": match.group(4),
                }
            )
    if not rows:
        raise AssertionError("$TaskTable block contained no parsable rows")
    return rows


def parse_doc_table() -> "list[dict]":
    """Extract the task table from SETUP-ANTIGRAVITY.md section 6a.

    Rows look like::

        | `a2a-agy-transport` | 30 min | `scripts\\run_antigravity_telegram.ps1` | restart-on-failure ... |
    """
    text = _read(SETUP_DOC)
    rows = []
    for line in text.splitlines():
        match = re.match(
            r"\|\s*`(a2a-agy-[a-z-]+)`\s*\|\s*(\d+)\s*min\s*\|\s*"
            r"`scripts\\([^`]+)`\s*\|",
            line.strip(),
        )
        if match:
            rows.append(
                {
                    "name": match.group(1),
                    "minutes": int(match.group(2)),
                    "wrapper": match.group(3).strip(),
                }
            )
    if not rows:
        raise AssertionError(
            "docs/telegram/SETUP-ANTIGRAVITY.md has no recognisable task table"
        )
    return rows


class TaskTableAgreementTests(unittest.TestCase):
    def setUp(self):
        self.script_rows = parse_script_table()
        self.doc_rows = parse_doc_table()

    def test_the_two_tables_cover_the_same_tasks(self):
        self.assertEqual(
            [row["name"] for row in self.script_rows],
            [row["name"] for row in self.doc_rows],
            msg="the script and SETUP-ANTIGRAVITY.md 6a list different tasks "
                "(or the same tasks in a different order)",
        )
        self.assertEqual([row["name"] for row in self.script_rows], EXPECTED_ORDER)

    def test_cadences_agree(self):
        for script_row, doc_row in zip(self.script_rows, self.doc_rows):
            self.assertEqual(
                script_row["minutes"],
                doc_row["minutes"],
                msg=f"{script_row['name']}: script says {script_row['minutes']} min, "
                    f"doc says {doc_row['minutes']} min",
            )

    def test_wrappers_agree(self):
        for script_row, doc_row in zip(self.script_rows, self.doc_rows):
            self.assertEqual(
                script_row["wrapper"],
                doc_row["wrapper"],
                msg=f"{script_row['name']}: script names {script_row['wrapper']}, "
                    f"doc names {doc_row['wrapper']}",
            )

    def test_wrappers_exist(self):
        for row in self.script_rows:
            wrapper = REPO / "scripts" / row["wrapper"]
            self.assertTrue(
                wrapper.is_file(),
                msg=f"{row['name']} names a wrapper that does not exist: {wrapper}",
            )

    def test_only_the_transport_is_uncapped(self):
        for row in self.script_rows:
            if row["name"] == "a2a-agy-transport":
                self.assertEqual(
                    row["time_limit"],
                    TRANSPORT_TIME_LIMIT,
                    msg="the transport's task must have no execution cap (PT0S): it "
                        "is a foreground long-running process",
                )
            else:
                self.assertNotEqual(
                    row["time_limit"],
                    TRANSPORT_TIME_LIMIT,
                    msg=f"{row['name']} is a one-shot wrapper and must be capped, "
                        f"else a wedged cycle occupies the task forever",
                )

    def test_the_shim_is_used(self):
        """Every action must go through run_hidden.vbs, or a console window flashes."""
        text = _read(SCRIPT)
        self.assertIn("$VbsShim = 'run_hidden.vbs'", text)
        self.assertIn("<Command>wscript.exe</Command>", text)
        self.assertTrue((REPO / "scripts" / "run_hidden.vbs").is_file())


class DocDiscoverabilityTests(unittest.TestCase):
    """The point of the task was discoverability: no reading of the codebase."""

    def test_setup_doc_names_the_script(self):
        text = _read(SETUP_DOC)
        for verb in ("start_antigravity.ps1", "-Status", "-Stop"):
            self.assertIn(
                verb,
                text,
                msg=f"SETUP-ANTIGRAVITY.md does not mention {verb!r}; a host operator "
                    f"has to reconstruct the commands from the source",
            )

    def test_getting_started_points_windows_hosts_at_the_script(self):
        text = _read(GETTING_STARTED)
        self.assertIn(
            "start_antigravity.ps1",
            text,
            msg="docs/GETTING-STARTED.md 'Windows notes' must point at the "
                "one-command start script, not only at SETUP-CODEX.md",
        )

    def test_execution_caps_are_the_ones_the_doc_states(self):
        """The doc table has no cap column, so the caps live in its prose.

        An earlier version of this test only asserted that certain strings existed
        inside the script, which passed no matter what the documentation said -- it
        could not fail on a doc/script divergence. It now reads the prose.
        """
        doc = _read(SETUP_DOC)
        script_caps = {row["name"]: row["time_limit"] for row in parse_script_table()}

        self.assertEqual(script_caps["a2a-agy-transport"], TRANSPORT_TIME_LIMIT)
        self.assertIn(
            "PT0S",
            doc,
            msg="the doc no longer states the transport's execution cap, so a reader "
                "cannot tell why the long-running task is uncapped",
        )
        self.assertIn("PT10M", doc, msg="the doc no longer states the one-shot caps")
        for name, cap in script_caps.items():
            self.assertIn(
                cap,
                doc,
                msg=f"{name}'s cap {cap} is set by the script but never stated in the doc",
            )

    def test_entry_point_defaults_are_the_documented_ones(self):
        """The defaults the doc tells a reader to rely on must be the real ones."""
        script = _read(SCRIPT)
        doc = _read(SETUP_DOC)

        # config default (both the param default and the doc's example call)
        self.assertIn('[string]$Config = "config.yaml"', script)
        self.assertIn("config.yaml", doc)

        # the venv python the wrappers pin, and the shim every action uses
        self.assertIn(r'.venv\Scripts\python.exe', script)
        self.assertIn("run_hidden.vbs", script)
        self.assertIn("run_hidden.vbs", doc)

        # the three wrappers the doc's table names must be the ones the script drives
        for row in parse_script_table():
            self.assertIn(row["wrapper"], doc)


if __name__ == "__main__":
    unittest.main()
