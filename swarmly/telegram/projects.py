"""
Per-topic project routing and workspace management.

Maps Telegram forum topics to project repositories and checkouts.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from swarmly.telegram.config import ProjectSettings

logger = logging.getLogger("swarmly.telegram.projects")

#: Where per-topic working copies live, under the checkout they are made from.
WORKTREES_DIRNAME = ".swarmly-worktrees"


def topic_worktree_path(base_cwd: Union[str, Path], name: str) -> Path:
    """One topic's working copy: ``<base_cwd>/.swarmly-worktrees/<name>``."""
    return Path(base_cwd).expanduser().resolve() / WORKTREES_DIRNAME / name


def _ensure_git_exclude(base_cwd: Path) -> None:
    """Ensure ``.swarmly-worktrees/`` is listed in the repository's local exclude file,
    never in tracked ``.gitignore``."""
    try:
        res = subprocess.run(
            ["git", "-C", str(base_cwd), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            exclude_file = Path(res.stdout.strip())
            if not exclude_file.is_absolute():
                exclude_file = base_cwd / exclude_file
        else:
            exclude_file = base_cwd / ".git" / "info" / "exclude"

        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        lines = [line.strip() for line in existing.splitlines()]
        if ".swarmly-worktrees/" not in lines and ".swarmly-worktrees" not in lines:
            with open(exclude_file, "a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(".swarmly-worktrees/\n")
    except Exception as exc:
        logger.warning("could not add .swarmly-worktrees/ to git exclude in %s: %s", base_cwd, exc)


def ensure_topic_worktree(base_cwd: Union[str, Path], name: str, *,
                          create: bool = True) -> Optional[Path]:
    """One topic's own working copy of ``base_cwd`` (#486).

    ``<base_cwd>/.swarmly-worktrees/<name>``, created on first use: a fetch, then
    a detached worktree of ``origin/HEAD`` (falling back to ``HEAD``), with
    ``.swarmly-worktrees/`` added to ``.git/info/exclude`` rather than the tracked
    ``.gitignore``. ``create=False`` only names the folder (for /status and /where): no
    fetch, no worktree.

    Returns ``None`` when the copy cannot be made. The caller then knows ``base_cwd`` is
    shared: a turn there must run **alone** (the shared lock), never beside another turn.
    """
    base = Path(base_cwd).expanduser().resolve()
    target = topic_worktree_path(base, name)
    if target.exists() or not create:
        return target
    _ensure_git_exclude(base)
    try:
        subprocess.run(
            ["git", "-C", str(base), "fetch", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            ["git", "-C", str(base), "worktree", "add", str(target), "origin/HEAD", "--detach"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if res.returncode != 0:
            res = subprocess.run(
                ["git", "-C", str(base), "worktree", "add", str(target), "HEAD", "--detach"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        if res.returncode == 0 and target.exists():
            return target
        logger.warning(
            "worktree add failed in %s for %s: %s (a turn there must run alone in %s)",
            base,
            name,
            res.stderr.strip() or res.stdout.strip(),
            base,
        )
    except Exception as exc:                                  # noqa: BLE001 - no copy, not no turn
        logger.warning(
            "failed to create worktree for %s in %s: %s (a turn there must run alone "
            "in %s)",
            name,
            base,
            exc,
            base,
        )
    return None


class ProjectRouter:
    """Routes messages in Telegram topics to project repositories and workspaces."""

    def __init__(
        self,
        projects: Union[Sequence[ProjectSettings], Dict[str, ProjectSettings]],
        home_chat_id: Optional[int] = None,
    ):
        self._projects: List[ProjectSettings] = (
            list(projects.values()) if isinstance(projects, dict) else list(projects)
        )
        self._home_chat_id: Optional[int] = home_chat_id

    def project_for(self, chat_id: int, thread_id: Optional[int]) -> Optional[ProjectSettings]:
        """Return the ProjectSettings bound to (chat_id, thread_id), or None for General / unbound."""
        if thread_id is None:
            return None
        for project in self._projects:
            for entry_chat_id, entry_thread_id in project.topics:
                if entry_thread_id == thread_id:
                    if entry_chat_id is None:
                        if self._home_chat_id is None or chat_id == self._home_chat_id:
                            return project
                    elif entry_chat_id == chat_id:
                        return project
        return None

    def topics_of(self, key: str) -> List[Tuple[int, int]]:
        """Return resolved (chat_id, thread_id) tuples for project ``key``."""
        for project in self._projects:
            if project.key == key:
                out: List[Tuple[int, int]] = []
                for entry_chat_id, entry_thread_id in project.topics:
                    cid = entry_chat_id if entry_chat_id is not None else (self._home_chat_id or 0)
                    out.append((cid, entry_thread_id))
                return out
        return []

    def workdir_for(self, project: ProjectSettings, thread_id: int, *,
                    create: bool = True) -> Path:
        """Return the working directory for this project and thread.
        Returns ``cwd`` when worktrees is false, or ``<cwd>/.swarmly-worktrees/topic-<thread_id>``
        when worktrees is true (created on first use). A failed creation falls back to
        ``cwd`` -- the caller must then hold the shared lock so no two turns run there at
        once (#486); :func:`ensure_topic_worktree` says which case it was. ``create=False``
        only names the folder (for /status): no fetch, no worktree.
        """
        base_cwd = Path(project.cwd).expanduser().resolve()
        if not project.worktrees:
            return base_cwd
        target = ensure_topic_worktree(base_cwd, f"topic-{thread_id}", create=create)
        return base_cwd if target is None else target
