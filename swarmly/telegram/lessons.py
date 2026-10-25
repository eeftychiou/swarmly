"""``memory.md``: each bot's lessons, kept by the transport (round 5.5, #226).

Roles are dynamic and a round runs for weeks, so a bot that learns something — "the lease
read must be bounded", "CI on Windows runs the same suite", "Reasonix reviews the diff first" —
learns it once and then loses it to the next fresh session. This keeps it: a small file per
bot, written by the bot, read back into every turn.

Where it lives:

* **The file** is ``memory-<alias>.md`` in the transport's state folder, beside
  ``topics-<alias>.json`` — the bot's own, never the repository's, never committed. It is
  private to that bot: no peer reads it, no bus carries it.
* **The read** is a short block in the per-turn prompt, not the system prompt: the system
  prompt is cached and fixed, and this file changes.
* **The write** is asked for, not assumed. Every ``limits.memory_every_turns`` turns (10 by
  default, and the count survives a restart) the transport adds one instruction to that turn
  and the bot answers with a ``MEMORY:`` block. The block is taken out of the reply — like a
  ``WAIT`` line or a ``ROUND_STATE`` block, it is never posted — and saved.
* **The cap** is ``limits.memory_max_bytes`` (2048 by default), counted in UTF-8 bytes. A
  block past it is **refused**: the old memory stays, and the bot is told the size and asked
  to compress. It is never truncated silently, because a half-cut lesson reads as a whole one.
* **The outcome** (saved, refused and why) is told to the bot in its *next* turn's prompt,
  never posted: it is the bot's feedback, not the people's (admin, 25 Oct: "Memory refused"
  lines in a work topic). A refusal asks for the update again on that next turn; a refusal
  of that re-asked block waits for the normal count, so a bot that cannot compress is not
  asked on every turn. Both are spent only by a turn that ran: a turn stopped, paused or
  failed before the model answered leaves them for the next one.

Lessons, not logs: one line each, what happened and what to do next time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from swarmly.telegram.adapters.base import load_state, save_state

#: The line that opens a memory block in a reply. The block runs to the end of the reply, or
#: to :data:`END_MARKER` when the bot writes one — either way it is stripped before posting.
MEMORY_MARKER = "MEMORY:"
END_MARKER = "END MEMORY"

#: Defaults, overridden by ``limits.memory_every_turns`` / ``limits.memory_max_bytes``.
EVERY_TURNS = 10
MAX_BYTES = 2048

#: What the prompt block is headed with, so it reads as the bot's own notes and not as orders
#: from the channel.
BLOCK_HEAD = "Your lessons so far (your own `memory.md`, kept across turns and restarts):"

#: The one instruction a turn carries when an update is due. It says the shape, the cap in
#: spirit and the closing marker, because a model that guesses at the shape gets it wrong.
UPDATE_INSTRUCTION = (
    "Update your memory now. Keep the lessons that held, add what you learned this time, drop "
    "what proved wrong — lessons, not a log: one line each, what happened and what to do next "
    "time. Put it at the end of your reply as a block that starts with a line `MEMORY:` and "
    f"ends with a line `{END_MARKER}`; the transport takes the block out before posting, so "
    "nobody sees it. Keep it small — it is read into every turn — and compress older lessons "
    "rather than growing it."
)

_OPEN = re.compile(r"(?m)^[ \t]*" + re.escape(MEMORY_MARKER) + r"[ \t]*$", re.IGNORECASE)
_CLOSE = re.compile(r"(?m)^[ \t]*" + re.escape(END_MARKER) + r"[ \t]*$", re.IGNORECASE)


def _safe(alias: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", str(alias or "").strip().lower()).strip("._") or "agent"


def memory_path(config: Any) -> Optional[Path]:
    """``memory-<alias>.md`` in the transport's state folder; None when it has no home."""
    if getattr(config, "source_path", None) is None:
        return None
    try:
        from swarmly.telegram.poll import state_dir
        folder = state_dir(config)
    except Exception:                                # noqa: BLE001 - kept in memory only
        return None
    return folder / f"memory-{_safe(config.identity.alias)}.md"


def turns_path(config: Any) -> Optional[Path]:
    """Where the turn counter between updates is kept, beside the memory itself."""
    if getattr(config, "source_path", None) is None:
        return None
    try:
        from swarmly.telegram.poll import state_dir
        folder = state_dir(config)
    except Exception:                                # noqa: BLE001 - kept in memory only
        return None
    return folder / f"memory-turns-{_safe(config.identity.alias)}.json"


def extract_block(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Split a reply into ``(body, block, reason)``.

    Every block is removed from the reply in **every** case — a memory block is never posted,
    including one that is malformed, too large or a second one — so the caller never has to
    choose between posting the notes and losing the reply. ``block`` is the first block with
    lessons in it, None when the reply carried none.
    """
    text = text or ""
    if _OPEN.search(text) is None:
        return text, None, None
    kept: List[str] = []
    blocks: List[str] = []
    pos = 0
    while True:
        opened = _OPEN.search(text, pos)
        if opened is None:
            kept.append(text[pos:])
            break
        kept.append(text[pos:opened.start()])
        closing = _CLOSE.search(text, opened.end())
        if closing is None:                           # no END MEMORY: it runs to the end
            blocks.append(text[opened.end():])
            break
        blocks.append(text[opened.end():closing.start()])
        pos = closing.end()
    body = "\n".join(part.strip("\n") for part in kept if part.strip()).strip()
    found = [block.strip("\n") for block in blocks if block.strip()]
    reason = ("more than one memory block; the first with lessons in it is used"
              if len(blocks) > 1 else None)
    if not found:
        return body, None, "the memory block was empty"
    return body, found[0], reason


def size_of(text: str) -> int:
    """The size the cap is counted in: UTF-8 bytes, not characters."""
    return len((text or "").encode("utf-8"))


def prompt_block(memory: str, *, max_bytes: int = MAX_BYTES) -> str:
    """The "Your lessons so far" block for a turn, or "" when there is nothing to show.

    A memory already past the cap is shown **cut to the cap** with a line saying so: the bot
    is the one who has to compress it, and it cannot compress what it cannot see.
    """
    memory = (memory or "").strip()
    if not memory:
        return ""
    shown, cut = memory, False
    if size_of(shown) > max_bytes:
        encoded = shown.encode("utf-8")[:max_bytes]
        shown = encoded.decode("utf-8", errors="ignore").rstrip()
        cut = True
    lines = [BLOCK_HEAD, shown]
    if cut:
        lines.append(f"[cut at {max_bytes} bytes to fit the cap — compress it on your next "
                     f"memory update]")
    return "\n".join(lines)


class Lessons:
    """One bot's memory file, the turn counter between updates, and the cap."""

    def __init__(self, config: Any):
        limits = getattr(config, "limits", None)
        # The cap is exactly what config says: a floor here would silently refuse a smaller
        # one an admin asked for, and the refusal is the point (#226).
        self.max_bytes = max(1, int(getattr(limits, "memory_max_bytes", MAX_BYTES) or MAX_BYTES))
        self.every_turns = max(1, int(getattr(limits, "memory_every_turns", EVERY_TURNS)
                                     or EVERY_TURNS))
        self.path = memory_path(config)
        self.counter_path = turns_path(config)
        self.memory = self._load()
        state = load_state(self.counter_path, "memory turn counter") if self.counter_path else {}
        state = state if isinstance(state, dict) else {}
        self.since_asked = int(state.get("since_asked", 0) or 0)
        self.turns = int(state.get("turns", 0) or 0)
        #: What became of the last memory block, for the next turn's prompt; "" once told.
        self.said = str(state.get("said") or "")
        #: Ask for the update again on the next turn that runs (after a refusal).
        self.again = bool(state.get("again"))
        #: Bumped by :meth:`tell`: a turn built before a newer outcome must not spend it.
        self.seq = 0

    # -- the file ----------------------------------------------------------------------
    def _load(self) -> str:
        if self.path is None or not self.path.exists():
            return ""
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def text(self) -> str:
        return self.memory

    def block(self) -> str:
        return prompt_block(self.memory, max_bytes=self.max_bytes)

    def save(self, memory: str) -> Tuple[bool, str]:
        """Store a memory block, or refuse it: ``(saved, message for the bot)``.

        Over the cap the **old memory stays** — a refusal that keeps what it had is the only
        honest one — and the bot is told the size it wrote and the cap it must fit.
        """
        memory = (memory or "").strip()
        if not memory:
            return False, "the memory block was empty, so your memory is unchanged"
        size = size_of(memory)
        if size > self.max_bytes:
            return False, (
                f"your memory is unchanged: that block is {size} bytes and the cap is "
                f"{self.max_bytes}. Compress it on your next update — keep the lessons that "
                f"still hold and drop the rest.")
        if self.path is None:
            self.memory = memory                     # no state folder: kept for this run
            return True, "memory updated for this run only (no state folder to keep it in)"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(memory + "\n", encoding="utf-8")
        except OSError as exc:
            return False, f"your memory could not be saved: {exc}"
        self.memory = memory
        return True, f"memory updated ({size} bytes)"

    def clear(self) -> str:
        """Empty the file. Returns the line to answer `/memory clear` with."""
        self.memory = ""
        if self.path is not None:
            try:
                self.path.write_text("", encoding="utf-8")
            except OSError as exc:
                return f"memory not cleared: {exc}"
        return "memory cleared: the file is empty now"

    # -- what became of a block -----------------------------------------------------------
    def tell(self, note: str, *, refused: bool, reasked: bool = False) -> None:
        """Keep the outcome of a memory block for the bot's next turn, where it is told.

        A refusal asks for the update again on that turn, unless the refused block answered a
        prompt that already carried the re-ask (*reasked*, known per turn): that bot goes back
        to the normal count.
        """
        self.said = (note or "").strip()
        self.again = refused and not reasked
        self.seq += 1
        self._save_counter()

    def note(self) -> str:
        """The outcome kept by :meth:`tell`, for a prompt; it stays until a turn ran."""
        return self.said

    def turn_ran(self, seq: Optional[int] = None) -> None:
        """A turn whose prompt was built at *seq* reached the model and answered: the
        outcome it carried is told, and a re-ask it carried is spent. A newer outcome (a
        parallel turn's block, told since) stays for the next turn."""
        if seq is not None and seq != self.seq:
            return
        if not (self.said or self.again):
            return
        self.said = ""
        self.again = False
        self._save_counter()

    # -- the turn counter ---------------------------------------------------------------
    def note_turn(self) -> bool:
        """Count this turn; True when an update is due: every N turns, or right after a
        refused block (once)."""
        self.turns += 1
        self.since_asked += 1
        due = self.again or self.since_asked >= self.every_turns
        if due:
            # Reset on the *ask*, not on the write: a bot that ignores the instruction must
            # not be asked on every turn from then on. A re-ask is spent by turn_ran().
            self.since_asked = 0
        self._save_counter()
        return due

    def _save_counter(self) -> None:
        if self.counter_path is None:
            return
        save_state(self.counter_path,
                   {"turns": self.turns, "since_asked": self.since_asked, "said": self.said,
                    "again": self.again},
                   "memory turn counter")


def describe(memory: str, *, max_bytes: int) -> str:
    """What ``/memory`` answers with: the file, or that it is empty, and how full it is."""
    memory = (memory or "").strip()
    if not memory:
        return "memory is empty. Its lessons are written on a memory update turn."
    return f"memory ({size_of(memory)}/{max_bytes} bytes):\n{memory}"
