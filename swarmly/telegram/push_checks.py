"""Checks the pre-push hook runs on every push (admin, 24 Oct: the rulebook in transport space).

Three rules from the role prompts that a push can prove or disprove:

* **Never commit secrets.** The pushed commits' added lines are scanned for token shapes
  (Telegram bot tokens, GitHub tokens, API keys, private keys).
* **Never force-push or reset another agent's branch.** A push that would drop commits from a
  branch (not a fast-forward) is refused when that branch's tip was written by someone else.
* **The coordinator does not write product code.** A coordinator push carries merges only: to
  ``main``/``master``, and the pushed tip must be a merge commit.

Stdlib only: ``guard.py`` runs it with ``python -I -S``. Each refusal names the rule, and the
kind is what the drift ledger records.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

ZERO = "0" * 40
PROTECTED = ("main", "master")

#: (label, pattern) for secrets that must never be pushed.
SECRETS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("a Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"
                                  r"|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("an API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

Git = Callable[..., str]


def default_git(real_git: str) -> Git:
    def run(*args: str) -> str:
        try:
            done = subprocess.run([real_git, *args], capture_output=True, text=True,
                                  timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout if done.returncode == 0 else ""
    return run


def secrets_in(added: str) -> List[str]:
    """The kinds of secret found in these added lines (a diff's ``+`` lines)."""
    found = []
    for label, pattern in SECRETS:
        if pattern.search(added):
            found.append(label)
    return found


def _added_lines(git: Git, local: str, remote: str) -> str:
    base = remote if remote != ZERO else (git("merge-base", local, "origin/main").strip()
                                          or git("merge-base", local, "origin/master").strip())
    span = f"{base}..{local}" if base else local
    diff = git("log", "-p", "--format=", "--no-color", span)
    return "\n".join(line[1:] for line in diff.splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


def check(lines: Sequence[str], role: str, me: str, git: Git) -> Optional[Tuple[str, str]]:
    """``(drift kind, why)`` for the first rule these pre-push lines break, or None.

    ``lines`` are git's ``<local ref> <local sha> <remote ref> <remote sha>``; ``me`` is the
    pusher's git author name; ``git`` runs a git command and returns its stdout."""
    role = (role or "").strip().lower()
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        _local_ref, local, remote_ref, remote = parts[:4]
        if local == ZERO or local == remote:
            continue                                         # a delete or a no-op push
        branch = remote_ref.removeprefix("refs/heads/")
        found = secrets_in(_added_lines(git, local, remote))
        if found:
            return ("secret_in_push", f"the push to {branch} contains {', '.join(found)}; "
                    "remove it from the commits (and rotate it) before pushing")
        if remote != ZERO and not _is_ancestor(git, remote, local):
            author = git("log", "-1", "--format=%an", remote).strip()
            if author and me and author.lower() != me.lower():
                return ("foreign_force_push", f"{branch} was last written by {author}; a push "
                        "that drops their commits is not yours to make")
        if role == "coordinator":
            if branch.startswith("agents-"):
                continue                                     # the bus branch is state, not code
            if branch not in PROTECTED:
                return ("coordinator_code_push", f"the coordinator does not push branches "
                        f"({branch}); a developer writes the change")
            parents = git("rev-list", "--parents", "-n", "1", local).split()
            if len(parents) < 3:
                return ("coordinator_code_push", f"the coordinator pushes merges only; the tip "
                        f"of this push to {branch} is not a merge commit")
    return None


def _is_ancestor(git: Git, older: str, newer: str) -> bool:
    """``git merge-base --is-ancestor`` prints nothing either way: compare merge-base."""
    base = git("merge-base", older, newer).strip()
    return bool(base) and base == older
