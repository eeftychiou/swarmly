"""Publication hygiene: tracked files carry no deployment identifiers, and the
v1 cluster config carries only keys the code reads.

The forbidden identifiers (real bot usernames and display names, chat and user
ids, host and account names, captured tokens and task ids) are stored as
truncated SHA-256 digests in ``tests/hygiene_digests.txt``. They are low-entropy
values, so the digests can be brute-forced: that file stays private, and where it
is absent (the public tree) the file scan is skipped, unless
``A2A_HYGIENE_REQUIRE_DIGESTS`` is set.

What is scanned, lower-cased, in every tracked text file:

- each ``[A-Za-z0-9_]+`` token, and every run of its ``_``-separated parts
  (so ``<handle>_h`` and ``<bot>_old`` are caught too);
- each UUID, whole.

Use placeholders instead: ``example_codex_bot``, ``-1001234567890``,
``1234567890``, ``alice``, ``12345678-1234-5678-1234-567812345678``.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests.gitenv import git_env

ROOT = Path(__file__).resolve().parent.parent

# Positive control: the canary below, never a real value.
CANARY_DIGEST = "6652827a6f9ff560"
# The real digests live in a file that is kept out of the public tree: they are low-entropy
# values, so publishing even their digests would publish the values.
DIGESTS_FILE = ROOT / "tests" / "hygiene_digests.txt"


def _load_digests() -> set:
    try:
        text = DIGESTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.split("#", 1)[0].strip() for line in text.splitlines()} - {""}


REAL_DIGESTS = _load_digests()
FORBIDDEN = REAL_DIGESTS | {CANARY_DIGEST}
# Built at runtime so this file never carries it whole and needs no exemption from its own scan.
CANARY = "_".join(("hygiene", "canary", "q7z"))

# Bus traffic is runtime data, not source; it is moved off the default branch
# separately (see the pre-publication review, P0-4).
SKIP_PREFIXES = (".agents/inbox/",)
SKIP_FILES = {"uv.lock"}

TOKEN = re.compile(r"[A-Za-z0-9_]+")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def _digest(value: str) -> str:
    return hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:16]


def _candidates(line: str):
    for token in TOKEN.findall(line):
        parts = [p for p in token.split("_") if p]
        yield token
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                yield "_".join(parts[start:end])
    yield from UUID.findall(line)


def find_identifiers(text: str):
    """Line numbers (1-based) of *text* that carry a forbidden identifier."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(_digest(c) in FORBIDDEN for c in _candidates(line)):
            hits.append(lineno)
    return hits


def _tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
                         env=git_env())
    for name in out.stdout.decode("utf-8").split("\0"):
        if name and name not in SKIP_FILES and not name.startswith(SKIP_PREFIXES):
            yield name


class TestNoDeploymentIdentifiers(unittest.TestCase):
    def test_the_scan_catches_the_canary(self):
        """DISCRIMINATING: positive control through the same scan the files get."""
        self.assertEqual(find_identifiers(f"a\nsee {CANARY} here"), [2])
        self.assertEqual(find_identifiers(f"x_{CANARY.upper()}_old"), [1])

    def test_the_scan_passes_placeholders(self):
        self.assertEqual(find_identifiers(
            "@example_codex_bot -1001234567890 1234567890 alice_h "
            "12345678-1234-5678-1234-567812345678"), [])

    def test_tracked_files_carry_no_deployment_identifiers(self):
        if DIGESTS_FILE.exists():
            # Present but unreadable or empty is never legitimate: it would pass everything.
            self.assertTrue(REAL_DIGESTS, f"{DIGESTS_FILE.name} holds no digests")
        elif os.environ.get("A2A_HYGIENE_REQUIRE_DIGESTS"):
            self.fail(f"{DIGESTS_FILE.name} is missing; the private repository must carry it")
        else:
            self.skipTest(f"no identifier list ({DIGESTS_FILE.name} is kept privately)")
        try:
            files = list(_tracked_files())
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        hits = []
        for name in files:
            try:
                text = (ROOT / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits += [f"{name}:{lineno}" for lineno in find_identifiers(text)]
        self.assertEqual(hits, [], "deployment identifiers in tracked files; use placeholders")


class TestClusterConfigMatchesSchema(unittest.TestCase):
    """config/a2a_config.json carries only keys the schema (and the code) knows."""

    def setUp(self):
        self.config = json.loads((ROOT / "config" / "a2a_config.json").read_text("utf-8"))
        self.schema = json.loads((ROOT / "schemas" / "a2a_config.schema.json").read_text("utf-8"))

    def test_top_level_keys_are_declared(self):
        self.assertLessEqual(set(self.config), set(self.schema["properties"]))

    def test_nested_keys_are_declared(self):
        props = self.schema["properties"]
        for section in ("coordination", "fast_plane"):
            self.assertLessEqual(set(self.config.get(section, {})),
                                 set(props[section]["properties"]), section)
        agent_props = set(props["authorized_agents"]["additionalProperties"]["properties"])
        for agent_id, entry in self.config["authorized_agents"].items():
            self.assertLessEqual(set(entry), agent_props, agent_id)
            self.assertIn("public_key_or_secret_name", entry, agent_id)

    def test_status_runs_on_the_shipped_config(self):
        """`scripts/a2a_bus.py status` formats every agent's family and scope."""
        result = subprocess.run([sys.executable, "scripts/a2a_bus.py", "status"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        for agent_id in self.config["authorized_agents"]:
            self.assertIn(f"@{agent_id}", result.stdout)


if __name__ == "__main__":
    unittest.main()
