"""Telegram forum topics as swarm discussions.

In a group with Topics turned on, every message carries the topic it was posted in
(``message_thread_id``). Each topic is one discussion, with one ``context_id`` that every
agent works out the same way without talking to the others, from the topic id alone:

* a topic in the home group is ``tg-<topic id>`` (for example ``tg-42``);
* a topic in any other group is ``tg<group id>-<topic id>``, so two groups never share one;
* the General topic (no ``message_thread_id``) is no discussion in particular: ``None``.

The topic's name never decides its context id: a rename cannot move a topic to another
discussion, and no name can claim another topic's id. Names are only shown to the agent,
next to the id. Telegram tells a bot a topic's name only in the service messages that
create or rename it (and, as the original name, in the ``reply_to_message`` of messages
posted at the topic's top level), so names are kept in ``topics-<alias>.json`` next to the
transport's other state, and survive restarts.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("swarmly.telegram.topics")

MAX_NAME = 128          # Telegram's limit for a topic name


def chat_tag(chat_id: int, secret: Optional[str] = None) -> str:
    """A stable tag for a chat that does not carry the id: HMAC-SHA-256, 16 hex digits.

    Always keyed. An unkeyed digest would let anyone who can guess an id confirm the guess
    by hashing it, and the first cut's 32-bit tag was collision-prone too (review of PR #23).
    The key is ``SWARMLY_REDACTION_SECRET`` when the operator sets it, otherwise a local
    secret generated once and kept beside the transport's state
    (:func:`swarmly.telegram.redact.local_secret`) -- never nothing.
    """
    from swarmly.telegram.redact import local_secret
    key = (secret or local_secret()).encode("utf-8")
    message = f"chat:{int(chat_id)}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]


def context_for(chat_id: int, thread_id: Optional[int],
                home_chat_id: Optional[int] = None) -> Optional[str]:
    """The discussion a topic stands for (``None`` for the General topic).

    A topic in the home group is ``tg-<thread>``; a topic in another group is
    ``tg-<chat tag>-<thread>``. The tag replaced the raw chat id in #22: a context id travels
    to the bus and into every prompt, so it must not name a chat. Nothing parses a context id
    back into a chat id -- topic lookups key on ``(chat, thread)`` -- so an id written before
    the change stays readable as an opaque label.
    """
    if thread_id is None:
        return None
    if home_chat_id is None or int(chat_id) == int(home_chat_id):
        return f"tg-{int(thread_id)}"
    return f"tg-{chat_tag(chat_id)}-{int(thread_id)}"


def topic_name(context_id: str, title: str) -> str:
    """The name the swarm gives a topic it opens for ``context_id``."""
    name = f"{context_id} · {' '.join((title or '').split()) or context_id}"
    return name if len(name) <= MAX_NAME else name[:MAX_NAME - 1] + "…"


def _name(source: Any, field: str) -> Optional[str]:
    name = getattr(getattr(source, field, None), "name", None)
    return name.strip() if isinstance(name, str) and name.strip() else None


def name_from_message(message: Any) -> Tuple[Optional[str], bool]:
    """``(name, current)``: a topic name an aiogram message carries, if any.

    ``current`` is True for the service message that created or renamed the topic. A
    message at a topic's top level carries the *creation* message as ``reply_to_message``,
    whose name may be out of date (``current`` False: use it only when nothing better is
    known).
    """
    own = _name(message, "forum_topic_edited") or _name(message, "forum_topic_created")
    if own:
        return own, True
    return _name(getattr(message, "reply_to_message", None), "forum_topic_created"), False


class TopicMap:
    """Topic names this transport has seen, per chat, persisted as JSON."""

    def __init__(self, path: Optional[Path] = None,
                 home_chat_id: Optional[int] = None) -> None:
        self.path = path
        self.home_chat_id = home_chat_id
        self._names: Dict[str, str] = {}
        if path is not None:
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("names") or {}, dict):
                raise ValueError("not a topic map")
            self._names = {str(k): str(v) for k, v in (data.get("names") or {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # Keep the unreadable file for a look later; names are relearned as they come.
            aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(path, aside)
            except OSError:
                aside = path
            logger.warning("could not read %s (%s); kept it as %s, topic names start empty",
                           path, exc, aside.name)

    @staticmethod
    def _key(chat_id: int, thread_id: int) -> str:
        return f"{chat_id}:{thread_id}"

    def name(self, chat_id: int, thread_id: Optional[int]) -> Optional[str]:
        if thread_id is None:
            return None
        return self._names.get(self._key(chat_id, thread_id))

    def remember(self, chat_id: int, thread_id: Optional[int], name: Optional[str],
                 current: bool = True) -> None:
        """Record a topic's name; a name that is not ``current`` only fills a gap."""
        if thread_id is None or not name:
            return
        name = name[:MAX_NAME]
        key = self._key(chat_id, thread_id)
        if self._names.get(key) == name or (not current and key in self._names):
            return
        self._names[key] = name
        logger.info("topic %s is %r (discussion %s)", key, name, self.context(chat_id, thread_id))
        self._save()

    def context(self, chat_id: int, thread_id: Optional[int]) -> Optional[str]:
        return context_for(chat_id, thread_id, self.home_chat_id)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"names": self._names}, ensure_ascii=False, indent=1,
                              sort_keys=True)
            fd, tmp = tempfile.mkstemp(prefix=".topics-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError as exc:            # a lost name only costs the name in the prompt
            logger.warning("could not save topic names to %s: %s", self.path, exc)
