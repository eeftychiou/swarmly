"""What each discussion is about, kept by the transport across turns and restarts.

A runtime that starts every turn fresh (``WANTS_TOPIC_RECAP``) used to see only the last few
messages of a topic, so on a long task it lost the task itself and how far the work had come.
The transport now keeps, per discussion:

- the opening message (the task), pinned: it never scrolls out;
- a progress list: messages that name an issue or PR, a GitHub link, a hand-off to a peer,
  a REPORT_BACK, or a merge, approval or blocker;
- the latest messages.

A fresh runtime gets all three before the new message. A runtime that resumes its own
session per topic already remembers its turns, so it gets only what others posted since its
last reply (and the task, the first time it speaks there). Only the swarm's own messages are
recorded; the caller filters strangers out.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from swarmly.telegram.adapters.base import load_state, save_state
from swarmly.telegram.topic_open import issue_refs

Key = Tuple[int, Optional[int]]
Entry = Tuple[Optional[int], str, str]            # (telegram message id, who, text)

#: What is kept, and what is shown. Messages are stored whole -- a brief cut on the way in
#: cannot be recovered, and the task is the one thing a fresh turn must not lose -- and the
#: length cuts are applied only when the recap is rendered. A fresh runtime pays for the
#: whole recap on every turn (nothing is cached), so what it is *shown* stays near 2k
#: characters; a session runtime gets the task once, when its session starts, then only the
#: messages it has not seen.
PROGRESS_ITEMS = 20
LOG_ITEMS = 30
TOPICS_KEPT = 80
SAVE_DELAY = 2.0
#: A topic's first message has an id just above the topic's own (its creation service
#: message); anything later is labelled "the earliest message seen here", not "the task".
OPENING_GAP = 3
FRESH_TASK, FRESH_PROGRESS, FRESH_RECENT, FRESH_CHARS = 700, 5, 3, 160
SESSION_TASK, SINCE_SHOWN, SINCE_CHARS = 1000, 12, 300

_PROGRESS = re.compile(
    r"(?i)(github\.com/\S+/(?:pull|issues|commit)/|\bREPORT_BACK\b|\bmerged\b|\bapproved\b|"
    r"\bchanges requested\b|\bblocked\b|\bblocker\b|\bCI (?:is )?(?:green|red|failing)\b)")
_MENTION = re.compile(r"(?<![\w@])@(\w{3,})")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


class TopicMemory:
    """Per-discussion task, progress and latest messages, saved to one state file."""

    def __init__(self, path: Optional[Path] = None, peer_handles: Tuple[str, ...] = ()):
        self.path = path
        self.peer_handles = {h.lstrip("@").lower() for h in peer_handles if h}
        self._topics: Dict[Key, Dict[str, Any]] = {}
        self._save_handle: Optional[asyncio.TimerHandle] = None
        for item in load_state(path, "topic memory").get("topics", []):
            try:
                key = (int(item["chat"]), None if item.get("thread") is None
                       else int(item["thread"]))
                log = deque((e for e in map(_entry, item.get("log", [])) if e),
                            maxlen=LOG_ITEMS)
                count = item.get("count")
                seen = item.get("seen")
                self._topics[key] = {
                    "opening": _entry(item.get("opening")),
                    "progress": [e for e in map(_entry, item.get("progress", [])) if e],
                    "log": log,
                    "count": count if isinstance(count, int) and count >= len(log)
                    else len(log),
                    "seen": seen if isinstance(seen, int) else None,
                }
            except (KeyError, TypeError, ValueError):
                continue

    # -- recording -----------------------------------------------------------------------

    def record(self, chat_id: int, thread_id: Optional[int], who: str, text: str,
               message_id: Optional[int]) -> None:
        text = " ".join((text or "").split())
        if not text:
            return
        key = (int(chat_id), thread_id)
        topic = self._topics.pop(key, None) or {"opening": None, "progress": [],
                                                "log": deque(maxlen=LOG_ITEMS),
                                                "count": 0, "seen": None}
        self._topics[key] = topic                     # most recently active last
        who = who or "someone"
        if message_id is not None and any(e[0] == message_id for e in topic["log"]):
            return                                    # already seen (bus copy, retry)
        if topic["opening"] is None and thread_id is not None:
            topic["opening"] = (message_id, who, text)
        elif self._is_progress(text):
            progress: List[Entry] = topic["progress"]
            progress.append((message_id, who, text))
            del progress[:-PROGRESS_ITEMS]
        topic["log"].append((message_id, who, text))
        topic["count"] += 1
        while len(self._topics) > TOPICS_KEPT:
            self._topics.pop(next(iter(self._topics)))
        self._schedule_save()

    def _schedule_save(self) -> None:
        """Save soon, once for a burst of messages; at once when no event loop runs."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save()
            return
        if self._save_handle is None:
            self._save_handle = loop.call_later(SAVE_DELAY, self.save)

    def _is_progress(self, text: str) -> bool:
        if issue_refs(text) or _PROGRESS.search(text):
            return True
        return any(m.lower() in self.peer_handles for m in _MENTION.findall(text))

    def save(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        if self.path is None:
            return
        save_state(self.path, {"topics": [
            {"chat": key[0], "thread": key[1],
             "opening": list(t["opening"]) if t["opening"] else None,
             "progress": [list(e) for e in t["progress"]],
             "log": [list(e) for e in t["log"]],
             "count": t["count"], "seen": t["seen"]}
            for key, t in self._topics.items()]}, "topic memory")

    # -- reading -------------------------------------------------------------------------

    def forget(self, chat_id: int, thread_id: Optional[int]) -> bool:
        """Drop a discussion's task, progress and messages: the topic is gone (issue #63).

        A new topic gets a new thread id, so nothing else can be meant by the same key; a
        kept recap would only be shown to a topic that no longer exists.
        """
        key = (int(chat_id), thread_id)
        if self._topics.pop(key, None) is None:
            return False
        self.save()
        return True

    def opening(self, chat_id: int, thread_id: Optional[int]) -> Optional[Entry]:
        topic = self._topics.get((int(chat_id), thread_id))
        return topic["opening"] if topic else None

    def recap(self, chat_id: int, thread_id: Optional[int], *, current_id: Optional[int],
              fresh: bool, own_alias: str, mark_seen: bool = True) -> str:
        """The context block that goes before the new message; "" when there is none."""
        topic = self._topics.get((int(chat_id), thread_id))
        if not topic:
            return ""
        opening: Optional[Entry] = topic["opening"]
        first = topic["count"] - len(topic["log"])          # the log's first entry, overall
        numbered = [(first + i, e) for i, e in enumerate(topic["log"])
                    if current_id is None or e[0] != current_id]
        log = [e for _, e in numbered]
        seen_before = topic["seen"]
        if not fresh and mark_seen:
            # Everything up to now goes into this prompt (or was in the session before):
            # the next catch-up starts after it, whenever this bot's reply gets logged.
            topic["seen"] = topic["count"]
            self._schedule_save()
        is_opening = opening is not None and current_id is not None \
            and opening[0] == current_id
        parts: List[str] = []
        if fresh:
            shown_task = opening if opening and not is_opening else None
            if shown_task:
                parts.append(_task_label(shown_task, thread_id)
                             + _quote(_cut(shown_task[2], FRESH_TASK)))
            recent = [e for e in log if not shown_task or e[0] != shown_task[0]]
            recent = recent[-FRESH_RECENT:]
            recent_ids = {e[0] for e in recent if e[0] is not None}
            progress = [e for e in topic["progress"]
                        if (current_id is None or e[0] != current_id)
                        and (e[0] is None or e[0] not in recent_ids)][-FRESH_PROGRESS:]
            if progress:
                parts.append("Progress so far, oldest first:\n"
                             + "\n".join(f"- {who}: {_cut(text, FRESH_CHARS)}"
                                         for _, who, text in progress))
            if recent:
                parts.append("Latest messages, oldest first:\n"
                             + "\n".join(f"- {who}: {_cut(text, FRESH_CHARS)}"
                                         for _, who, text in recent))
            head = "About this discussion (context only; answer the new message below)."
        else:
            own = [i for i, e in enumerate(log) if e[1] == own_alias]
            if seen_before is not None:
                since = [e for n, e in numbered if n >= seen_before and e[1] != own_alias]
            elif own:
                since = log[own[-1] + 1:]
            else:
                if opening and not is_opening:
                    parts.append(_task_label(opening, thread_id)
                                 + _quote(_cut(opening[2], SESSION_TASK)))
                since = [e for e in log if not opening or e[0] != opening[0]]
            since = since[-SINCE_SHOWN:]
            if since:
                parts.append("Posted here since your last reply, oldest first:\n"
                             + "\n".join(f"- {who}: {_cut(text, SINCE_CHARS)}"
                                         for _, who, text in since))
            head = "Catching up on this discussion (context only; answer the new message below)."
        if not parts:
            return ""
        return head + "\n\n" + "\n\n".join(parts) + "\n\nNew message:\n"


def _task_label(opening: Entry, thread_id: Optional[int]) -> str:
    message_id = opening[0]
    if message_id is not None and thread_id is not None \
            and 0 < message_id - thread_id <= OPENING_GAP:
        return f"The task, as first posted by {opening[1]}:\n"
    return f"The earliest message seen here, from {opening[1]}:\n"


def _quote(text: str) -> str:
    return "> " + text


def _entry(value: Any) -> Optional[Entry]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    message_id, who, text = value
    if message_id is not None and (not isinstance(message_id, int)
                                   or isinstance(message_id, bool)):
        return None
    if not isinstance(who, str) or not isinstance(text, str):
        return None
    return (message_id, who, text)
