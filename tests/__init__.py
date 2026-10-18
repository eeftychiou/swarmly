"""
Swarmly Test Suite.
"""

import os

from . import gitenv

# The CLIs write a rotating debug log; keep test runs from filling ~/.a2a/logs.
# Tests that check the log file set their own A2A_LOG_DIR.
os.environ.setdefault("A2A_LOG_DIR", "off")

# A runner may be a host where a transport guards git (GIT_CONFIG_* hooks, SWARMLY_ROLE,
# the guard directory on PATH) under an agent's commit identity.  The suite tests the code,
# not that guard, so the inherited environment is scrubbed once here -- for pytest and for
# ``unittest discover`` alike -- and tests build their git subprocess environment with
# ``tests.gitenv.git_env()``.  See tests/gitenv.py and tests/test_git_env_guard.py.
gitenv.scrub_process()

# A redaction tag is derived from a key the transport generates and keeps beside its state
# (~/.swarmly/redaction.secret). A test must not write into the runner's home, so the suite
# points that file at a throwaway directory once, before any test builds a Redactor
# (issue #22 review: the unset-secret test used to touch the real home).
if not os.environ.get("SWARMLY_REDACTION_SECRET_FILE"):
    import tempfile

    _redaction_dir = tempfile.mkdtemp(prefix="swarmly-redaction-")
    os.environ["SWARMLY_REDACTION_SECRET_FILE"] = os.path.join(_redaction_dir,
                                                               "redaction.secret")
