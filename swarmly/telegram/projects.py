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
        when worktrees is true (created on first use). A failed worktree creation falls back
        to ``cwd`` and logs a warning; it never blocks the turn. ``create=False`` only names
        the folder (for /status): no fetch, no worktree.
        """
        base_cwd = Path(project.cwd).expanduser().resolve()
        if not project.worktrees:
            return base_cwd
        target = base_cwd / ".swarmly-worktrees" / f"topic-{thread_id}"
        if target.exists() or not create:
            return target
        _ensure_git_exclude(base_cwd)
        try:
            subprocess.run(
                ["git", "-C", str(base_cwd), "fetch", "origin"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(
                ["git", "-C", str(base_cwd), "worktree", "add", str(target), "origin/HEAD", "--detach"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if res.returncode != 0:
                res = subprocess.run(
                    ["git", "-C", str(base_cwd), "worktree", "add", str(target), "HEAD", "--detach"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            if res.returncode == 0 and target.exists():
                return target
            logger.warning(
                "worktree add failed in %s for topic %s: %s (falling back to %s)",
                base_cwd,
                thread_id,
                res.stderr.strip() or res.stdout.strip(),
                base_cwd,
            )
            return base_cwd
        except Exception as exc:
            logger.warning(
                "failed to create worktree for topic %s in %s: %s (falling back to %s)",
                thread_id,
                base_cwd,
                exc,
                base_cwd,
            )
            return base_cwd
