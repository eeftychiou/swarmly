"""Telegram forum topics as swarm discussions.

In a group with Topics turned on, every message carries the topic it was posted in
(``message_thread_id``). Each topic is one discussion, with one ``context_id`` that every
agent works out the same way without talking to the others, from the topic id alone:

* a topic in the home group is ``tg-<topic id>`` (for example ``tg-42``);
* a topic in any other group is ``tg-<chat tag>-<topic id>``, the tag being an HMAC of the
  group id under ``SWARMLY_REDACTION_SECRET`` -- the same value on every agent, so the id is
  agreed without a chat id (or a guessable key) reaching the bus. Without that secret such a
  topic has no context id: it fails closed rather than leak or be confirmable;
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


def chat_tag(chat_id: int, key: Optional[str] = None) -> str:
    """HMAC-SHA-256 of a chat id, 16 hex digits. Fails closed without a key.

    A context id is shared vocabulary: every agent must name a topic the same way, so the
    key has to be shared, and it has to be high-entropy -- a group id is guessable, which
    would let anyone holding a tag confirm candidate chat ids offline (review of PR #23).
    The only such value is ``SWARMLY_REDACTION_SECRET``; without it there is no tag.
    """
    material = (key if key is not None else
                os.environ.get("SWARMLY_REDACTION_SECRET", "").strip())
    if not material:
        raise ValueError("a chat tag needs a shared secret (SWARMLY_REDACTION_SECRET)")
    message = f"chat:{int(chat_id)}".encode("utf-8")
    return hmac.new(material.encode("utf-8"), message, hashlib.sha256).hexdigest()[:16]


#: Warned once per process: a topic outside the home group needs a secret to get a tag.
_NO_SECRET_WARNED = False


def context_block_reason(chat_id: int, thread_id: Optional[int],
                         home_chat_id: Optional[int]) -> Optional[str]:
    """Why a turn in this topic must be refused, or ``None`` when it is fine.

    A topic outside the home group needs a shared secret to get a context id. Without one the
    choice is between leaking a chat id, naming a guessable one, and pretending to be the
    General session -- none acceptable, so the turn is refused instead (issue #22 review).
    """
    if thread_id is None or home_chat_id is None:
        return None
    if int(chat_id) == int(home_chat_id):
        return None
    if os.environ.get("SWARMLY_REDACTION_SECRET", "").strip():
        return None
    return ("this topic is outside the home group and SWARMLY_REDACTION_SECRET is not set: "
            "set the same secret on every agent, or work in the home group. Refusing the "
            "turn rather than sharing the General session or naming a chat id.")


def context_for(chat_id: int, thread_id: Optional[int],
                home_chat_id: Optional[int] = None) -> Optional[str]:
    """The discussion a topic stands for (``None`` for the General topic).

    A topic in the home group is ``tg-<thread>`` -- no chat id in it at all. A topic in
    another group is ``tg-<chat tag>-<thread>``, and the tag is an HMAC **only** under a
    configured ``SWARMLY_REDACTION_SECRET``: every agent in the swarm shares that secret, so
    every agent names the topic the same way, and nobody can confirm a guessed chat id
    offline. Without a secret this **fails closed** -- no context id, so the topic keeps
    running turn by turn instead of claiming a shared identity (issue #22 review). An id
    written before the change stays readable as an opaque label; nothing parses one back.
    """
    if thread_id is None:
        return None
    if home_chat_id is None or int(chat_id) == int(home_chat_id):
        return f"tg-{int(thread_id)}"
    secret = os.environ.get("SWARMLY_REDACTION_SECRET", "").strip()
    if not secret:
        global _NO_SECRET_WARNED
        if not _NO_SECRET_WARNED:
            _NO_SECRET_WARNED = True
            logger.warning(
                "topic %s is outside the home group and SWARMLY_REDACTION_SECRET is unset: "
                "no context id, so this topic has no shared discussion identity. Set the "
                "secret (the same value on every agent) to name such topics.", thread_id)
        return None
    return f"tg-{chat_tag(chat_id, key=secret)}-{int(thread_id)}"


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
