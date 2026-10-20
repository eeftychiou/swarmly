"""Messages taken for a turn but not yet answered, kept on disk so a restart replays them.

Live, round 4: Claudebot had a reply ready to post and a peer's hand-off queued behind it
when its transport was restarted. Both were in memory only, Telegram had already handed the
updates over, and the swarm stalled for the night with every bot waiting on another.

A message is written here the moment the channel accepts it for a turn (queued or not) and
removed when the channel is done with it: the reply was posted, or the turn chose silence, or
the message was dropped on purpose (/stop, /pause). Whatever is still here at start was
interrupted, and the channel runs it again. That is at-least-once, never at-most-once: a
crash between posting a reply and removing its entry repeats one turn, which is the cheap side
of the trade. Commands are never written here (they are answered by the transport), and
neither are a bot's own briefs, which have their own durable stores (wake timers, topics).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("swarmly.telegram.inbox")

#: An interrupted message older than this is not run again: the work has moved on.
MAX_AGE_SECONDS = 12 * 3600
#: Bounded: a transport that keeps failing must not build an unbounded file.
MAX_ENTRIES = 50

#: The ChannelEvent fields a replay needs. ``raw`` (an aiogram object) is not kept.
_FIELDS = ("text", "chat_id", "author_id", "author_username", "author_is_bot", "message_id",
           "replies_to_bot", "thread_id", "topic_name", "topic_name_current", "attachments",
           "handoff", "reassembled")


def entry_key(chat_id: Any, message_id: Any) -> str:
    return f"{chat_id}:{message_id}"


class Inbox:
    """``{chat:message -> {"at": wall time, "event": {...}}}`` in one JSON file."""

    def __init__(self, path: Optional[Path], *, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._entries: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("inbox: %s is unreadable; starting empty", self.path)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items()
                if isinstance(v, dict) and isinstance(v.get("event"), dict)}

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(self._entries, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.path)
        except (OSError, TypeError, ValueError):
            logger.warning("inbox: could not save %s", self.path, exc_info=True)

    def record(self, event: Any) -> bool:
        """Keep *event* until :meth:`done`. False when it has no message id to key on."""
        message_id = getattr(event, "message_id", None)
        if message_id is None or getattr(event, "self_task", False):
            return False
        data = {name: getattr(event, name, None) for name in _FIELDS}
        try:
            json.dumps(data)
        except (TypeError, ValueError):
            data["attachments"] = []          # a file handle or the like: the text still counts
        self._entries[entry_key(event.chat_id, message_id)] = {"at": self.clock(), "event": data}
        if len(self._entries) > MAX_ENTRIES:
            oldest = sorted(self._entries, key=lambda k: self._entries[k].get("at", 0))
            for key in oldest[:len(self._entries) - MAX_ENTRIES]:
                self._entries.pop(key, None)
        self._save()
        return True

    def done(self, chat_id: Any, message_id: Any) -> None:
        if self._entries.pop(entry_key(chat_id, message_id), None) is not None:
            self._save()

    def __contains__(self, key: Tuple[Any, Any]) -> bool:
        return entry_key(*key) in self._entries

    def interrupted(self) -> List[Dict[str, Any]]:
        """The events a previous run accepted and never finished, oldest first; stale ones
        are dropped."""
        now = self.clock()
        fresh, stale = [], []
        for key, entry in sorted(self._entries.items(), key=lambda kv: kv[1].get("at", 0)):
            age = now - float(entry.get("at", 0) or 0)
            (fresh if 0 <= age <= MAX_AGE_SECONDS else stale).append((key, entry))
        for key, _ in stale:
            self._entries.pop(key, None)
        if stale:
            logger.info("inbox: dropped %d interrupted message(s) older than %d h",
                        len(stale), MAX_AGE_SECONDS // 3600)
            self._save()
        return [entry["event"] for _, entry in fresh]


def event_from(data: Dict[str, Any], event_type: Any) -> Any:
    """Rebuild a ChannelEvent from what :meth:`Inbox.record` kept."""
    known = {f.name for f in fields(event_type)}
    return event_type(**{k: v for k, v in data.items() if k in known})
