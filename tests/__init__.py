"""
Swarmly Test Suite.
"""

import os

# The CLIs write a rotating debug log; keep test runs from filling ~/.a2a/logs.
# Tests that check the log file set their own A2A_LOG_DIR.
os.environ.setdefault("A2A_LOG_DIR", "off")
