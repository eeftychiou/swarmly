"""Shared fixtures for the swarm kernel tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import cryptography  # noqa: F401
    import yaml  # noqa: F401
    HAVE_SWARM_DEPS = True
except ImportError:  # pragma: no cover
    HAVE_SWARM_DEPS = False

needs_swarm = unittest.skipUnless(HAVE_SWARM_DEPS, "needs the swarm extra")

VENDORS = {"alice": "human", "claude": "anthropic", "codex": "openai",
           "antigravity": "google", "hermes": "nous", "claude2": "anthropic"}


class SwarmCase(unittest.TestCase):
    """A swarm with five agents on a DirLaneStore, keys in a temporary key dir."""

    policy: Optional[Dict[str, Any]] = None

    def setUp(self) -> None:
        from swarmly.swarm import keys
        from swarmly.swarm.lanes import DirLaneStore
        from swarmly.swarm.protocol import swarm_from_dict

        self.tmp = tempfile.TemporaryDirectory()
        # A cleanup, not tearDown: cleanups run last-in first-out after tearDown, so a test's
        # own cleanup (closing a log file inside the directory) runs first. Windows cannot
        # delete a file that is still open.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._old_key_dir = os.environ.get("A2A_KEY_DIR")
        os.environ["A2A_KEY_DIR"] = str(self.root / "keys")
        agents = {}
        for name, vendor in VENDORS.items():
            _, pub = keys.generate(name)
            agents[name] = {"vendor": vendor, "key": pub}
        self.config: Dict[str, Any] = {
            "project": "demo", "owner": "alice", "planner": "claude", "standby": "hermes",
            "agents": agents, "bus": {"backend": "dir", "path": str(self.root / "lanes")},
        }
        if self.policy:
            self.config["policy"] = self.policy
        self.swarm = swarm_from_dict(self.config)
        self.store = DirLaneStore(self.root / "lanes")
        self._govs: Dict[str, Any] = {}

    def tearDown(self) -> None:
        if self._old_key_dir is None:
            os.environ.pop("A2A_KEY_DIR", None)
        else:
            os.environ["A2A_KEY_DIR"] = self._old_key_dir

    def gov(self, agent: str) -> Any:
        from swarmly.swarm.governor import Governor
        if agent not in self._govs:
            self._govs[agent] = Governor(self.swarm, self.store, agent)
        return self._govs[agent]

    def post(self, agent: str, intent: str, **fields: Any) -> Any:
        return self.gov(agent).post(intent, **fields)

    def rejected(self, agent: str, intent: str, **fields: Any) -> Any:
        from swarmly.swarm.governor import Rejected
        with self.assertRaises(Rejected) as ctx:
            self.gov(agent).post(intent, **fields)
        return ctx.exception

    def view(self) -> Any:
        return self.gov("claude").view()

    def write_swarm_yaml(self) -> Path:
        import yaml as _yaml
        path = self.root / "swarm.yaml"
        path.write_text(_yaml.safe_dump(self.config), encoding="utf-8")
        return path


EV = [{"kind": "ci", "ref": "https://ci.example/run/1"}]
