"""--cli claude finds and keeps a bot configured as claude_thread (phase 7 deploy, 25 Oct)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmly.telegram import join


class JoinKeepsTheThreadTypeTests(unittest.TestCase):
    def test_existing_homes_finds_the_thread_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "claude_obot"
            home.mkdir()
            (home / "config.yaml").write_text("agent:\n  name: x\nadapter:\n  type: claude_thread\n",
                                              encoding="utf-8")
            other = Path(tmp) / "codex_bot"
            other.mkdir()
            (other / "config.yaml").write_text("adapter:\n  type: codex\n", encoding="utf-8")
            with mock.patch.object(join, "HOME", Path(tmp)):
                self.assertEqual(join._existing_homes("claude"), [home])
                self.assertEqual(join._existing_homes("codex"), [other])


if __name__ == "__main__":
    unittest.main()
