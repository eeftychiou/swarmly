"""Keep an agent's git checkout current from outside its sandbox.

Codex's ``workspace-write`` sandbox keeps ``.git`` read-only (on Windows, through deny
entries for its sandbox identities), and git's credential helper cannot start inside it, so
``git fetch`` and ``git pull`` fail from inside a turn and the agent works on a stale tree
(live test: QA ran against a commit two merges old). The transport runs as the real user, so
it fetches before each turn and fast-forwards a clean checkout, then tells the agent where
it stands.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Seconds a fetch may take before the turn goes ahead without it.
FETCH_TIMEOUT = 60


def _git(path: str, *args: str, timeout: float = 15) -> Tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    output = (done.stdout or "").strip() or (done.stderr or "").strip()
    return done.returncode, output


def _first_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "unknown error")[:160]


def _contained(path: str, default: str) -> bool:
    """Whether ``default`` already holds every change on HEAD: merged in, rebased (each
    patch already there, ``git cherry``) or squash-merged (merging HEAD would change
    nothing; live test: both stale branches had been squash-merged)."""
    code, _ = _git(path, "merge-base", "--is-ancestor", "HEAD", default)
    if code == 0:
        return True
    code, out = _git(path, "cherry", default, "HEAD")
    if code == 0 and out and all(line.startswith("-") for line in out.splitlines()):
        return True
    code, merged = _git(path, "merge-tree", "--write-tree", default, "HEAD")
    _, tree = _git(path, "rev-parse", f"{default}^{{tree}}")
    return code == 0 and bool(tree) and merged.splitlines()[:1] == [tree]


def sync_checkout(path: str) -> Optional[str]:
    """Fetch ``origin`` in ``path`` and fast-forward its branch when the tree is clean.
    Returns the line for the agent's prompt, or None when ``path`` is not a git checkout."""
    if not path or not Path(path).is_dir():
        return None
    code, out = _git(path, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out != "true":
        return None
    facts: List[str] = []
    code, out = _git(path, "fetch", "--quiet", "--prune", "origin", timeout=FETCH_TIMEOUT)
    fetched = code == 0
    if not fetched:
        logger.warning("git sync: fetch in %s failed: %s", path, _first_line(out))
        facts.append(f"the fetch failed ({_first_line(out)})")
    _, branch = _git(path, "symbolic-ref", "--short", "-q", "HEAD")
    code, upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream = upstream if code == 0 and upstream else ""
    _, dirty = _git(path, "status", "--porcelain", "--untracked-files=no")
    _, before = _git(path, "rev-parse", "--short", "HEAD")
    if branch and upstream and fetched:
        if dirty:
            facts.append(f"not fast-forwarded to {upstream}: the tree has local changes")
        else:
            code, out = _git(path, "merge", "--ff-only", "--quiet", upstream)
            if code != 0:
                facts.append(f"not fast-forwarded to {upstream} ({_first_line(out)})")
    _, head = _git(path, "rev-parse", "--short", "HEAD")
    if head and before and head != before:
        facts.append(f"fast-forwarded from {before}")
    if upstream:
        code, counts = _git(path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if code == 0 and len(counts.split()) == 2:
            ahead, behind = counts.split()
            if behind != "0":
                facts.append(f"{behind} commit(s) behind {upstream}")
            if ahead != "0":
                facts.append(f"{ahead} commit(s) ahead of {upstream}")
    code, default = _git(path, "rev-parse", "--abbrev-ref", "origin/HEAD")
    default = default if code == 0 and default.startswith("origin/") else "origin/main"
    code, default_sha = _git(path, "rev-parse", "--short", default)
    if code == 0 and default_sha and branch and branch != default.split("/", 1)[1]:
        # Live test: two agents sat on feature branches merged long ago and worked there.
        # Said as fact, not as "merged": a branch made from an older main looks the same.
        _, behind_default = _git(path, "rev-list", "--count", f"HEAD..{default}")
        if behind_default not in ("", "0") and _contained(path, default):
            facts.append(f"every change on this branch is already in {default}, which is "
                         f"{behind_default} commit(s) ahead; start new work from {default}")
    where = f"{branch or 'a detached HEAD'} at {head or 'unknown'}"
    line = (f"Git, checked by the transport just now: {where}"
            + (f" ({'; '.join(facts)})" if facts else "")
            + (f"; {default} is at {default_sha}" if code == 0 and default_sha else "")
            + ". The transport fetches before every turn; you don't need to.")
    logger.info("git sync: %s", line.split(". The transport")[0])
    return line
