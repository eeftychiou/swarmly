"""
Console-encoding guards for the scripts (review F1 on PR #74).

``scripts/a2a_bus.py inbox`` prints check-mark status icons; a legacy console
(cp1252) cannot encode them, so the script used to die with a UnicodeEncodeError
the moment a message was listed. The fix degrades the unencodable character
instead of raising - this test forces the encoding so it holds on every OS.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "a2a_bus.py"

ENVELOPE = """---
protocol: a2a/v1.0
message_id: msg-20260919T000000Z-test
correlation_id: 
timestamp_utc: '2026-09-19T00:00:00+00:00'
sender:
  agent_id: alice
  model_family: custom
  framework: custom
recipient:
  agent_id: bob
intent: PR_REVIEW_REQUEST
priority: ROUTINE
timeout_utc: 
signature: 'hmac-sha256:deadbeef'
payload_json: '{"text": "hi"}'
---

body
"""


class BusScriptConsoleTests(unittest.TestCase):
    def test_inbox_survives_a_legacy_console_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus = Path(tmp)
            subprocess.run(["git", "init", "-q", "."], cwd=bus, check=True)
            inbox = bus / ".agents" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "msg-test.md").write_text(ENVELOPE, encoding="utf-8")

            env = dict(os.environ, PYTHONIOENCODING="cp1252")
            proc = subprocess.run([sys.executable, str(SCRIPT), "inbox"],
                                  cwd=bus, capture_output=True, text=True,
                                  env=env, timeout=60)

            self.assertEqual(proc.returncode, 0,
                             msg=f"inbox crashed on a cp1252 console: {proc.stderr}")
            self.assertIn("A2A INBOX", proc.stdout)


if __name__ == "__main__":
    unittest.main()
