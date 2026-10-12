"""
Session handover: a topic session that is retired (too large, too many turns) never leaves
its successor blank.

Before the transport drops a topic's session, it runs one more turn in that session asking
for a handover under fixed headings (goal, decisions, state, open hand-offs, files and
commands, gotchas, next step). The handover is not posted to the group; it is kept under the
transport's state folder (``handover/<topic>-<alias>-<UTC time>.md``, never deleted) and the
new session's first prompt starts with it, ahead of the topic recap. When the old session
cannot write one, the new session is told so and rebuilds the state from the recap, the
tracking issue and the PRs.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: The last turn of a retiring session (not posted to the group).
HANDOVER_PROMPT = (
    "[Session handover; this reply is NOT posted to the group.] Your session for this topic "
    "is full and the next turn starts a new session of yourself, which will know only what "
    "you write now. Write the handover for it under exactly these headings, at most about "
    "1,500 tokens, facts only:\n"
    "1. Goal and scope: the task, its issue and PR numbers, and what done means.\n"
    "2. Decisions made and why: the rules and constraints the team agreed (for example what "
    "must never be committed, which branch to work from, the merge gate).\n"
    "3. Current state: done; in progress (branch, commit SHA, PR, who has it); blocked, and "
    "on what.\n"
    "4. Open hand-offs: who owes whom what.\n"
    "5. Files and commands that matter: key paths, how to run the tests, data locations.\n"
    "6. Gotchas and failed attempts: what did not work, and why.\n"
    "7. Next concrete step for you.")

#: Ahead of the first prompt of the new session.
TAKEOVER_LEAD = (
    "[You are taking over this topic from a previous session of yourself, which ran out of "
    "room. Its handover follows. Check the PR, issue and commit states it names with git and "
    "gh before acting on them: they may have moved since it was written.]\n\n")
TAKEOVER_END = "\n\n[End of the handover. The topic recap and the new message follow.]\n\n"

#: When the old session could not write a handover.
NO_HANDOVER_LEAD = (
    "[A previous session of yourself worked on this topic but could not write a handover. "
    "Rebuild the goal, the decisions and the current state from the recap below, the "
    "tracking issue and the PRs (check them with git and gh) before acting.]\n\n")

#: A handover reply shorter than this is not a handover.
MIN_HANDOVER_CHARS = 80


class Handovers:
    """The handovers one adapter has written and its new sessions have not yet started from,
    kept beside the adapter's sessions state file."""

    def __init__(self, alias: str) -> None:
        self.alias = alias
        self.folder: Optional[Path] = None
        self._pending: Dict[str, str] = {}     # context_id -> handover file, or "" (none)

    def attach(self, sessions_state: Optional[Path]) -> None:
        if sessions_state is None:
            return
        self.folder = Path(sessions_state).parent / "handover"
        try:
            data = json.loads(self._index().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            self._pending = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def _index(self) -> Path:
        assert self.folder is not None
        safe = re.sub(r"[^\w.-]", "_", self.alias.lower())
        return self.folder / f"pending-{safe}.json"

    def _save_index(self) -> None:
        if self.folder is None:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._index().write_text(json.dumps(self._pending, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("%s: could not save the handover index", self.alias, exc_info=True)

    def pending(self, context_id: str) -> bool:
        return context_id in self._pending

    def save(self, context_id: str, text: Optional[str]) -> Optional[Path]:
        """Keep the handover for the topic's next session; ``None`` or a stub records that
        the old session wrote none."""
        text = (text or "").strip()
        if len(text) < MIN_HANDOVER_CHARS:
            self._pending[context_id] = ""
            self._save_index()
            logger.warning("%s: no usable handover for %s; the new session rebuilds from the "
                           "recap", self.alias, context_id)
            return None
        path: Optional[Path] = None
        if self.folder is not None:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            safe = re.sub(r"[^\w.-]", "_", f"{context_id}-{self.alias.lower()}")
            path = self.folder / f"{safe}-{stamp}.md"
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                path.write_text(text + "\n", encoding="utf-8")
            except OSError:
                logger.warning("%s: could not write the handover file", self.alias,
                               exc_info=True)
                path = None
        self._pending[context_id] = str(path) if path else text
        self._save_index()
        logger.info("%s: handover for %s saved%s", self.alias, context_id,
                    f" to {path}" if path else "")
        return path

    def lead(self, context_id: str) -> str:
        """What goes ahead of the new session's first prompt ("" when nothing is pending)."""
        if context_id not in self._pending:
            return ""
        held = self._pending[context_id]
        if not held:
            return NO_HANDOVER_LEAD
        text = held
        if self.folder is not None and held.endswith(".md"):
            try:
                text = Path(held).read_text(encoding="utf-8").strip()
            except OSError:
                return NO_HANDOVER_LEAD
        return TAKEOVER_LEAD + text + TAKEOVER_END

    def done(self, context_id: str) -> None:
        """The new session started from it (its first turn succeeded); the file is kept."""
        if self._pending.pop(context_id, None) is not None:
            self._save_index()
