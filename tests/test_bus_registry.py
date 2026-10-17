"""A corrupt registry fails loudly instead of becoming an empty one (issue #8, 2.7).

``DurableBus._load_config`` used to swallow every error with a bare ``except: pass`` and
carry on with ``authorized_agents = {}``, so a broker whose registry was unreadable, not
JSON, or the wrong shape verified nothing and signed mail failed silently. The three cases
below are the ones the issue names: corrupt JSON, the wrong top-level type, and a missing
file (which stays the documented first-run state).
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.bus import DurableBus, RegistryError


class _BusCase(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "config" / "a2a_config.json"
        self.registry.parent.mkdir(parents=True)

    def load(self):
        return DurableBus(root_dir=self.root)


class TestCorruptRegistryFailsLoudly(_BusCase):

    def test_invalid_json_names_the_path_and_the_cause(self):
        self.registry.write_text('{"authorized_agents": {', encoding="utf-8")
        with self.assertRaises(RegistryError) as caught:
            self.load()
        message = str(caught.exception)
        self.assertIn(str(self.registry), message)
        self.assertIn("not valid JSON", message)

    def test_an_empty_file_is_corrupt_too(self):
        self.registry.write_text("", encoding="utf-8")
        with self.assertRaises(RegistryError) as caught:
            self.load()
        self.assertIn(str(self.registry), str(caught.exception))

    def test_a_list_is_not_a_registry(self):
        self.registry.write_text(json.dumps([{"agent": "hermes"}]), encoding="utf-8")
        with self.assertRaises(RegistryError) as caught:
            self.load()
        self.assertIn("must be a JSON object", str(caught.exception))
        self.assertIn("list", str(caught.exception))

    def test_a_string_is_not_a_registry(self):
        self.registry.write_text(json.dumps("a2a_config"), encoding="utf-8")
        with self.assertRaises(RegistryError) as caught:
            self.load()
        self.assertIn("must be a JSON object", str(caught.exception))

    def test_the_secondary_location_is_checked_too(self):
        """``init`` writes ``.agents/A2A_CONFIG.json`` on some checkouts: same rule."""
        legacy = self.root / ".agents" / "A2A_CONFIG.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("{oops", encoding="utf-8")
        with self.assertRaises(RegistryError) as caught:
            self.load()
        self.assertIn(str(legacy), str(caught.exception))

    @unittest.skipIf(os.name == "nt", "POSIX permissions are what makes the file unreadable")
    @unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0, "root reads a 000 file")
    def test_an_unreadable_file_names_the_path(self):
        self.registry.write_text(json.dumps({"authorized_agents": {}}), encoding="utf-8")
        self.registry.chmod(0o000)
        self.addCleanup(self.registry.chmod, 0o600)
        with self.assertRaises(RegistryError) as caught:
            self.load()
        self.assertIn(str(self.registry), str(caught.exception))
        self.assertIn("cannot be read", str(caught.exception))

    def test_a_registry_named_on_purpose_must_exist(self):
        """The old behaviour, unchanged: a named registry is never silently another one."""
        with self.assertRaises(FileNotFoundError):
            DurableBus(root_dir=self.root, config_path=self.root / "no-such.json")


class TestTheQuietCases(_BusCase):

    def test_a_missing_registry_stays_the_first_run_state(self):
        bus = self.load()                    # no file at all: today's empty registry
        self.assertEqual(bus.config["authorized_agents"], {})
        self.assertEqual(bus.config["revocation_list"], [])
        self.assertEqual(bus.config["coordination"]["primary_master"], "antigravity")

    def test_a_valid_registry_still_loads(self):
        self.registry.write_text(json.dumps({
            "authorized_agents": {"hermes": {"public_key_or_secret_name": "HERMES_KEY"}},
            "revocation_list": ["mallory"],
        }), encoding="utf-8")
        bus = self.load()
        self.assertEqual(bus.key_type_for("hermes"), "hmac")
        self.assertEqual(bus.verification_key("hermes"), None)   # the env var is unset


if __name__ == "__main__":
    unittest.main()
