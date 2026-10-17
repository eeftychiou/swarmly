"""One environment for the git subprocesses the suite starts (issue #7, brainstorm 2.4).

A test runner is not a neutral place.  The transport that runs an agent puts a role guard
in front of ``git``: ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` point
``core.hooksPath`` at hooks that refuse a push to main, ``SWARMLY_ROLE`` and
``SWARMLY_REAL_GIT``/``SWARMLY_REAL_GH`` say whose guard it is, and the guard directory sits
first on ``PATH``.  The same runner carries the agent's commit identity in
``GIT_AUTHOR_*``/``GIT_COMMITTER_*``.  A test that starts ``git`` inside that environment
then tests the guard instead of the code -- about 27 false failures on a guarded host.

Every test that spawns git therefore builds the subprocess environment with
``git_env()``: the inherited guard and identity are stripped, a fixed test identity is set,
and anything the test needs on top is passed as keyword arguments.  The process environment
itself is stripped once, at import of the ``tests`` package (``scrub_process``), which also
covers code under test that inherits ``os.environ``.

    from tests.gitenv import git_env, git

    subprocess.run(["git", "commit", "-m", "x"], cwd=repo, env=git_env())
    git(repo, "commit", "-qm", "x")          # same thing, captured text output

``tests/test_git_env_guard.py`` fails the suite when a test starts git without it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

__all__ = ["TEST_IDENTITY", "STRIPPED", "STRIPPED_PREFIXES", "strip", "scrub_process",
           "git_env", "git", "git_out"]

#: The identity test git commands commit with, so no runner's name lands in a test repo.
TEST_IDENTITY = {
    "GIT_AUTHOR_NAME": "Swarmly Tests",
    "GIT_AUTHOR_EMAIL": "tests@swarmly.invalid",
    "GIT_COMMITTER_NAME": "Swarmly Tests",
    "GIT_COMMITTER_EMAIL": "tests@swarmly.invalid",
}

#: Variables the guard and the transport set, which a test subprocess must not inherit.
STRIPPED = (
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "SWARMLY_ROLE",
    "SWARMLY_REAL_GIT",
    "SWARMLY_REAL_GH",
    # A checkout taken over by another repository is equally foreign to a test.
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

#: Per-index companions of ``GIT_CONFIG_COUNT``, and the identity variables it replaces.
STRIPPED_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_AUTHOR_", "GIT_COMMITTER_")


def _is_guard_dir(entry: str) -> bool:
    """Whether a ``PATH`` entry is a transport guard directory.

    The transport names them ``guard`` or, per agent, ``guard-<alias>`` (the live one here is
    ``guard-deepseek``), so an equality test on ``guard`` leaves the real guard first on
    ``PATH`` -- which the reviewer caught on PR #14. A guard directory is recognised by its
    name *and* by the shim it holds (``gh``/``git``), never by the name alone: a directory a
    test happens to call ``guard-...`` must not be dropped from ``PATH``.
    """
    if not entry:
        return False
    folder = Path(entry)
    name = folder.name.lower()
    if name != "guard" and not name.startswith("guard-"):
        return False
    return any((folder / shim).exists() for shim in ("gh", "git", "gh.exe", "git.exe"))


def _clean_path(value: str) -> str:
    return os.pathsep.join(part for part in value.split(os.pathsep) if not _is_guard_dir(part))


def strip(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """``env`` (by default ``os.environ``) without the guard and without a foreign identity.

    No identity is set: code under test that commits passes its own (``git -c user.name=``,
    ``GIT_AUTHOR_*`` in the environment it builds), and the scrub must not shadow it.
    """
    out = dict(os.environ if env is None else env)
    for key in list(out):
        if key in STRIPPED or key.startswith(STRIPPED_PREFIXES):
            del out[key]
    if "PATH" in out:
        out["PATH"] = _clean_path(out["PATH"])
    return out


def scrub_process() -> None:
    """Strip ``os.environ`` itself, in place.

    Called once when the test package is imported, so code under test that reads
    ``os.environ`` (rather than a subprocess environment) sees the same clean world.
    """
    stripped = strip()
    for key in list(os.environ):
        if key not in stripped:
            del os.environ[key]
    os.environ.update(stripped)


def git_env(env: Optional[Mapping[str, str]] = None, **overrides: str) -> Dict[str, str]:
    """The environment for a git subprocess: stripped, with the test identity, plus
    ``overrides``.

    ``overrides`` win, so a test may hand git its own identity, ``GIT_CONFIG_*`` values or
    ``SWARMLY_ROLE`` deliberately -- that is how the guard's own tests build their world.
    """
    out = strip(env)
    out.update(TEST_IDENTITY)
    out.update(overrides)
    return out


def git(repo, *args: str, check: bool = True,
        env: Optional[Mapping[str, str]] = None, **kwargs) -> subprocess.CompletedProcess:
    """Run ``git -C repo args`` in a scrubbed environment (text output, captured)."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          env=git_env(env), **kwargs)


def git_out(repo, *args: str, **kwargs) -> str:
    """``git(repo, *args)`` with the trailing newline removed, as most tests want it."""
    return git(repo, *args, **kwargs).stdout.strip()


def argv_of(command: Sequence[str]) -> bool:
    """Whether ``command`` starts a ``git`` process (used by the CI guard's scanner)."""
    return bool(command) and Path(str(command[0])).name in ("git", "git.exe")
