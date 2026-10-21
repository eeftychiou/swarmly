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
#: How many times one interrupted message may be started again (issue #84). One resume, then
#: the record is dropped and the stall reported: a resume that is itself cut off must never
#: become a restart loop.
MAX_REPLAYS = 1
#: An interrupted turn older than this is **reported**, never resumed (issue #103): restarting
#: work an hour after it was cut off means acting on a topic that has moved on -- the peer who
#: asked has re-sent it, or done it itself, or the item has landed. Well inside MAX_AGE_SECONDS,
#: which is when an entry is dropped without a word.
RESUME_MAX_AGE_SECONDS = 3600.0

#: The ChannelEvent fields a replay needs. ``raw`` (an aiogram object) is not kept.
_FIELDS = ("text", "chat_id", "author_id", "author_username", "author_is_bot", "message_id",
           "replies_to_bot", "thread_id", "topic_name", "topic_name_current", "attachments",
           "handoff", "reassembled")


def entry_key(chat_id: Any, message_id: Any) -> str:
    return f"{chat_id}:{message_id}"


class Inbox:
    """``{chat:message -> {"at": wall time, "event": {...}}}`` in one JSON file."""

    def __init__(self, path: Optional[Path], *, clock: Callable[[], float] = time.time,
                 resume_max_age: float = RESUME_MAX_AGE_SECONDS):
        self.path = path
        self.clock = clock
        self.resume_max_age = float(resume_max_age)
        self._entries: Dict[str, Dict[str, Any]] = self._load()
        #: A claim whose resume is still running. A second ``start()`` in the same process
        #: (a re-entered start, a test that starts twice) must not spend a second try (#103).
        self._claiming = False

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

    def claim_replay(self, *, limit: int = 1
                      ) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Dict[str, Any]]]:
        """Split what a restart interrupted into "run it again" and "give up" (issue #84).

        The first restart after a turn was cut off runs it again; if *that* try is cut off too,
        the record is dropped rather than replayed a third time, and the caller is told which
        item to give up on. A turn that ended normally removed its entry (:meth:`done`), so
        nothing is left to count.

        The try is counted **before** the replay starts, not after: a process killed halfway
        through its second attempt must not be able to claim a third.
        """
        if self._claiming:
            return [], []               # a resume from an earlier claim is still running
        now = self.clock()
        runnable: List[Tuple[int, Dict[str, Any]]] = []
        dropped: List[Dict[str, Any]] = []
        stale: List[str] = []
        for key, entry in sorted(self._entries.items(), key=lambda kv: kv[1].get("at", 0)):
            age = now - float(entry.get("at", 0) or 0)
            if not (0 <= age <= MAX_AGE_SECONDS):
                stale.append(key)
                continue
            attempts = int(entry.get("attempts", 0) or 0)
            if attempts >= MAX_REPLAYS:
                dropped.append(dict(entry, reason="twice"))
                self._entries.pop(key, None)
                continue
            if age > self.resume_max_age:
                # Too old to be worth restarting, but somebody has to hear about it (#103).
                dropped.append(dict(entry, reason="stale"))
                self._entries.pop(key, None)
                continue
            if len(runnable) >= max(1, limit):
                continue                # next tick: untouched, and not yet counted against it
            entry["attempts"] = attempts + 1
            runnable.append((entry["attempts"], entry))
        if runnable:
            self._claiming = True
        for key in stale:
            self._entries.pop(key, None)
        if stale:
            logger.info("inbox: dropped %d interrupted message(s) older than %d h",
                        len(stale), MAX_AGE_SECONDS // 3600)
        if dropped or runnable:
            self._save()
        return runnable, dropped

    def interrupted(self) -> List[Dict[str, Any]]:
        """What a restart would resume, oldest first -- **read-only** (issue #103).

        It counts nothing and drops nothing, so asking twice spends nothing: only
        :meth:`claim_replay` may spend a try, and only the start path calls it.
        """
        now = self.clock()
        fresh = []
        for _key, entry in sorted(self._entries.items(), key=lambda kv: kv[1].get("at", 0)):
            age = now - float(entry.get("at", 0) or 0)
            if 0 <= age <= MAX_AGE_SECONDS:
                fresh.append(entry["event"])
        return fresh

    def released(self) -> None:
        """The claimed resume finished: the next claim may run (#103, one at a time)."""
        self._claiming = False

    def claiming(self) -> bool:
        return self._claiming

    def attempts(self, chat_id: Any, message_id: Any) -> int:
        """How many times this message has been started (0 until a restart resumes it)."""
        entry = self._entries.get(entry_key(chat_id, message_id)) or {}
        return int(entry.get("attempts", 0) or 0)


def event_from(data: Dict[str, Any], event_type: Any) -> Any:
    """Rebuild a ChannelEvent from what :meth:`Inbox.record` kept."""
    known = {f.name for f in fields(event_type)}
    return event_type(**{k: v for k, v in data.items() if k in known})
