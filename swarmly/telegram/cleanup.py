"""``/cleanup``: the admin's tidy-up of the group (issue #63).

After a round the home group fills with one-off forum topics -- a topic per side task, a
topic per PR -- and with machine notices in General, and neither people nor bots can find
the work that matters. The bots are group admins, so the transport can tidy it itself; only
the admin may ask, and no deletion happens without a second, explicit confirmation.

What the command does (answered by the transport, never a model turn; the coordinator
answers a bare ``/cleanup``, or ``/cleanup@<bot>`` asks one bot):

* ``/cleanup`` -- deletes every finished topic at once and says what went and what stayed.
  Finished means not protected (Swarm pulse, ``limits.cleanup_keep``), no open work (an open
  work item in the topic registry, an open owed row, a turn running) and no activity in the
  last hour. No dry run and no confirmation code (admin, 25 Oct).
* ``/cleanup preview`` -- the same list, deleting nothing (the dashboard's Cleanup button).
* ``/cleanup topic <id>`` -- one topic, now.
* ``/cleanup general`` -- the General messages this bot can still delete, in batches.

Two Telegram limits shape this. ``deleteForumTopic`` works on a topic of any age, but the
Bot API cannot *list* a group's topics, so the topics offered are the ones the transports
know (``topics-<alias>.json``). ``deleteMessage(s)`` works only on messages under 48 hours
old and the Bot API cannot read history either, so General cleanup covers only the message
ids a transport recorded while running: :class:`GeneralLedger` keeps them with their
timestamps and prunes the ones past the window. Older General messages stay the admin's to delete by hand.

After a topic is deleted this transport forgets everything it kept about it (topic memory,
the topic's CLI session, the issue/PR -> topic map, its ``/model`` and ``/reasoning``
choices, pending hand-offs, the name cache) and posts one machine
``NOTE intent=TOPIC_GONE ref=<topic>``, so the peers forget it too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarmly.telegram.adapters.base import load_state, save_state

logger = logging.getLogger("swarmly.telegram.cleanup")

#: ``deleteMessage`` works only on messages younger than this; the ledger keeps no longer.
LEDGER_TTL_SECONDS = 48 * 3600
#: One Telegram ``deleteMessages`` call takes at most this many ids.
DELETE_BATCH = 100
#: A topic active more recently than this is not finished, whatever else it shows.
FINISHED_IDLE_SECONDS = 3600.0
#: Bound on the ledger: a chat's recorded ids are trimmed to the newest of these.
LEDGER_MAX_PER_CHAT = 2000


@dataclass
class TopicInfo:
    """One forum topic, as /cleanup decides about it."""

    thread_id: int
    chat_id: Optional[int] = None
    title: Optional[str] = None
    last_activity: Optional[float] = None       # wall clock, or None when never recorded
    open_reason: Optional[str] = None           # why it is not finished (open work), if so


class GeneralLedger:
    """The General message ids one transport could still delete, with their timestamps.

    The Bot API cannot list a chat's history, so the only messages a bot can delete are the
    ones it saw or posted while running. Entries older than ``deleteMessage``'s 48 hours are
    pruned and counted: the dry run reports that count as the messages it can no longer
    reach, instead of pretending the list is the whole history.
    """

    def __init__(self, path: Optional[Path] = None, ttl: float = LEDGER_TTL_SECONDS) -> None:
        self.path = path
        self.ttl = float(ttl)
        self._ids: Dict[int, List[Tuple[int, float]]] = {}
        self._notices: Dict[int, List[Tuple[int, float]]] = {}
        self._expired: Dict[int, int] = {}
        data = load_state(path, "general message ledger") if path is not None else {}
        self._ids = self._load_entries(data.get("chats"))
        # Issue #64: the notices this transport itself posted in General, and when. Only these
        # are ever swept on a timer; everything else here is merely deletable by /cleanup.
        self._notices = self._load_entries(data.get("notices"))
        for chat, count in (data.get("expired") or {}).items():
            try:
                self._expired[int(chat)] = int(count)
            except (TypeError, ValueError):
                continue

    @staticmethod
    def _load_entries(raw: Any) -> Dict[int, List[Tuple[int, float]]]:
        """``{chat: [(message_id, at), …]}`` from the saved shape, skipping what is malformed."""
        loaded: Dict[int, List[Tuple[int, float]]] = {}
        for chat, entries in (raw or {}).items():
            try:
                key = int(chat)
            except (TypeError, ValueError):
                continue
            kept: List[Tuple[int, float]] = []
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, (list, tuple)) and len(entry) == 2 \
                        and isinstance(entry[0], int) and not isinstance(entry[0], bool) \
                        and isinstance(entry[1], (int, float)):
                    kept.append((int(entry[0]), float(entry[1])))
            if kept:
                loaded[key] = kept[-LEDGER_MAX_PER_CHAT:]
        return loaded

    # -- recording -----------------------------------------------------------------------

    def note(self, chat_id: int, ids: Sequence[Optional[int]],
             now: Optional[float] = None) -> None:
        """Record message ids seen or sent in ``chat_id``; a repeat only refreshes it."""
        moment = float(now if now is not None else time.time())
        key = int(chat_id)
        entries = self._ids.setdefault(key, [])
        known = {message_id for message_id, _ in entries}
        added = False
        for message_id in ids:
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                continue
            if message_id in known:
                continue
            entries.append((message_id, moment))
            known.add(message_id)
            added = True
        if not added and not self._prune(key, moment):
            return
        del entries[:-LEDGER_MAX_PER_CHAT]
        self.save()

    def _prune(self, chat_id: int, now: float) -> bool:
        """Drop the ids past the window; True when any went."""
        entries = self._ids.get(int(chat_id)) or []
        kept = [(message_id, at) for message_id, at in entries if now - at <= self.ttl]
        if len(kept) == len(entries):
            return False
        gone = len(entries) - len(kept)
        self._expired[int(chat_id)] = self._expired.get(int(chat_id), 0) + gone
        if kept:
            self._ids[int(chat_id)] = kept
        else:
            self._ids.pop(int(chat_id), None)
        return True

    def drop(self, chat_id: int, ids: Sequence[int]) -> None:
        """The ids are deleted: stop offering them (and stop counting them as notices)."""
        key = int(chat_id)
        gone = {int(i) for i in ids}
        entries = [entry for entry in self._ids.get(key, []) if entry[0] not in gone]
        if entries:
            self._ids[key] = entries
        else:
            self._ids.pop(key, None)
        notices = [entry for entry in self._notices.get(key, []) if entry[0] not in gone]
        if notices:
            self._notices[key] = notices
        else:
            self._notices.pop(key, None)
        self.save()

    # -- this transport's own notices (issue #64) ----------------------------------------

    def note_notice(self, chat_id: int, message_id: Optional[int],
                    now: Optional[float] = None) -> None:
        """Record one machine notice this transport itself posted in General.

        Only what the transport marked as a notice goes here -- never a human message and
        never a model reply -- and only these ids are swept once they are older than
        ``limits.ephemeral_notices_minutes``. The entry is bounded by the same 48-hour window
        as the rest of the ledger: past it Telegram refuses the delete anyway.
        """
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        moment = float(now if now is not None else time.time())
        key = int(chat_id)
        entries = self._notices.setdefault(key, [])
        if any(known == message_id for known, _ in entries):
            return
        entries.append((int(message_id), moment))
        self._prune_notices(key, moment)
        entries = self._notices.setdefault(key, [])
        del entries[:-LEDGER_MAX_PER_CHAT]
        self.save()

    def _prune_notices(self, chat_id: int, now: float) -> bool:
        """Drop the notices past the delete window; True when any went."""
        key = int(chat_id)
        entries = self._notices.get(key) or []
        kept = [(message_id, at) for message_id, at in entries if now - at <= self.ttl]
        if len(kept) == len(entries):
            return False
        if kept:
            self._notices[key] = kept
        else:
            self._notices.pop(key, None)
        return True

    def notices_due(self, chat_id: int, minutes: float,
                    now: Optional[float] = None) -> List[int]:
        """Own notices older than ``minutes``, oldest first: what the sweep deletes.

        ``minutes <= 0`` disables the sweep and offers nothing.
        """
        if minutes is None or float(minutes) <= 0:
            return []
        moment = float(now if now is not None else time.time())
        key = int(chat_id)
        if self._prune_notices(key, moment):
            self.save()
        age_limit = float(minutes) * 60.0
        entries = sorted(self._notices.get(key, []), key=lambda pair: (pair[1], pair[0]))
        return [message_id for message_id, at in entries if moment - at >= age_limit]

    def notices(self, chat_id: int, now: Optional[float] = None) -> List[int]:
        """Every own notice still inside the window, oldest first (tests and the dry run)."""
        moment = float(now if now is not None else time.time())
        key = int(chat_id)
        if self._prune_notices(key, moment):
            self.save()
        entries = sorted(self._notices.get(key, []), key=lambda pair: (pair[1], pair[0]))
        return [message_id for message_id, _ in entries]

    # -- reading -------------------------------------------------------------------------

    def recent(self, chat_id: int, now: Optional[float] = None) -> List[int]:
        """The ids still inside the window, oldest first: what ``general`` would delete."""
        moment = float(now if now is not None else time.time())
        if self._prune(int(chat_id), moment):
            self.save()
        entries = sorted(self._ids.get(int(chat_id), []), key=lambda pair: (pair[1], pair[0]))
        return [message_id for message_id, _ in entries]

    def expired(self, chat_id: int) -> int:
        """How many recorded ids aged out of the window (no longer deletable)."""
        return int(self._expired.get(int(chat_id), 0))

    def save(self) -> None:
        if self.path is None:
            return
        state = {"chats": {str(chat): [list(entry) for entry in entries]
                           for chat, entries in self._ids.items()},
                 "notices": {str(chat): [list(entry) for entry in entries]
                             for chat, entries in self._notices.items()},
                 "expired": {str(chat): count for chat, count in self._expired.items()}}
        save_state(self.path, state, "general message ledger")


class Cleanup:
    """``/cleanup``: delete every finished topic, at once (admin, 25 Oct: "just a command
    /cleanup which will delete all finished topics, no checks").

    A topic is *finished* unless it is kept: General is never a topic, the protected topics
    (Swarm pulse, ``limits.cleanup_keep``) are never deleted, and a topic is kept while it has
    open work -- the host says why (an open work item in the registry, an open owed row, a turn
    running) -- or was active in the last ``idle_seconds``. Forms:

    * ``/cleanup`` -- delete the finished topics now, and say what went and what stayed
    * ``/cleanup preview`` -- the same list, deleting nothing (the dashboard's button)
    * ``/cleanup topic <id>`` -- delete one topic now (not a protected one)
    * ``/cleanup general`` -- delete the General messages this bot can still reach

    The channel supplies the Telegram calls through a small host interface, so everything
    here is testable with fakes:

    * ``cleanup_topics() -> List[TopicInfo]`` -- the topics this transport knows, each with
      ``open_reason`` set when it has open work
    * ``cleanup_home_chat() -> Optional[int]`` -- the home group's chat id
    * ``cleanup_delete_topic(chat_id, thread_id) -> bool`` (awaited)
    * ``cleanup_delete_messages(chat_id, ids) -> int`` (awaited): how many went
    * ``cleanup_forget_topic(chat_id, thread_id)`` (awaited): forget it, and tell the peers
    """

    def __init__(self, host: Any, ledger: GeneralLedger, *, handle: str = "",
                 keep: Sequence[int] = (), clock: Callable[[], float] = time.time,
                 idle_seconds: float = FINISHED_IDLE_SECONDS) -> None:
        self.host = host
        self.ledger = ledger
        self.handle = handle
        self.keep = {int(t) for t in keep if isinstance(t, int) and not isinstance(t, bool)}
        self._clock = clock
        self.idle_seconds = float(idle_seconds)

    # -- the command ---------------------------------------------------------------------

    async def handle_args(self, argv: Sequence[str], *, is_admin: bool,
                          chat_id: Optional[int] = None) -> str:
        """Answer ``/cleanup <argv>``."""
        if not is_admin:
            logger.warning("/cleanup ignored: only the admin tidies the group")
            return f"⛔ {self._me()}: only the admin runs /cleanup."
        words = [str(word).lower() for word in argv]
        home = self.host.cleanup_home_chat()
        if home is None:
            return f"{self._me()}: /cleanup needs agent.home_channel_id."
        home = int(home)                 # always the home group, wherever it was asked
        if not words:
            finished, kept = self.plan(self.host.cleanup_topics())
            return await self._delete_topics(home, finished, kept)
        if words == ["preview"]:
            return self.preview(self.host.cleanup_topics())
        if words == ["general"]:
            return await self._delete_messages(home, self.ledger.recent(home, self._clock()))
        if len(words) == 2 and words[0] == "topic":
            return await self._delete_one(words[1], home)
        return (f"{self._me()}: /cleanup deletes every finished topic. Also: /cleanup preview, "
                "/cleanup topic <id>, /cleanup general.")

    # -- deciding ------------------------------------------------------------------------

    def plan(self, topics: Sequence[TopicInfo],
             now: Optional[float] = None) -> Tuple[List[TopicInfo], List[Tuple[TopicInfo, str]]]:
        """(finished topics, [(kept topic, why)])."""
        moment = float(now if now is not None else self._clock())
        kept_ids = self._kept()
        finished: List[TopicInfo] = []
        kept: List[Tuple[TopicInfo, str]] = []
        for topic in sorted(topics, key=lambda t: t.thread_id):
            if topic.thread_id in kept_ids:
                kept.append((topic, "protected"))
            elif topic.open_reason:
                kept.append((topic, topic.open_reason))
            elif topic.last_activity is None:
                kept.append((topic, "no activity recorded"))      # never guess: keep it
            elif moment - float(topic.last_activity) < self.idle_seconds:
                kept.append((topic, f"active {_age(moment - float(topic.last_activity))} ago"))
            else:
                finished.append(topic)
        return finished, kept

    def preview(self, topics: Sequence[TopicInfo]) -> str:
        finished, kept = self.plan(topics)
        lines = [f"🧹 {self._me()} (a preview: nothing is deleted): /cleanup would delete "
                 f"{len(finished)} topic(s)"
                 + (": " + ", ".join(self._name(t) for t in finished) if finished else ".")]
        if kept:
            lines.append("Kept: " + "; ".join(f"{self._name(t)} ({why})" for t, why in kept))
        return "\n".join(lines)

    # -- deleting ------------------------------------------------------------------------

    async def _delete_one(self, word: str, chat_id: int) -> str:
        try:
            thread_id = int(word.lstrip("#"))
        except ValueError:
            return f"{self._me()}: /cleanup topic takes a topic id (a number)."
        if thread_id in self._kept():
            return f"{self._me()}: topic {thread_id} is protected; it is never deleted."
        known = {t.thread_id: t for t in self.host.cleanup_topics()}
        topic = known.get(thread_id) or TopicInfo(thread_id=thread_id, chat_id=chat_id)
        return await self._delete_topics(chat_id, [topic], [])

    async def _delete_topics(self, chat_id: int, topics: Sequence[TopicInfo],
                             kept: Sequence[Tuple[TopicInfo, str]]) -> str:
        deleted: List[str] = []
        failed: List[str] = []
        kept = list(kept)
        for topic in topics:
            if len(topics) > 1:
                # Asked again just before each delete: work may have started meanwhile.
                fresh = {t.thread_id: t for t in self.host.cleanup_topics()}.get(topic.thread_id)
                stays = self.plan([fresh])[1] if fresh is not None else []
                if stays:
                    kept.extend(stays)
                    continue
            try:
                ok = await self.host.cleanup_delete_topic(chat_id, topic.thread_id)
            except Exception:                              # noqa: BLE001 - report, go on
                logger.exception("could not delete topic %s", topic.thread_id)
                ok = False
            if not ok:
                failed.append(self._name(topic))
                continue
            deleted.append(self._name(topic))
            try:
                await self.host.cleanup_forget_topic(chat_id, topic.thread_id)
            except Exception:                              # noqa: BLE001 - the delete stands
                logger.exception("deleted topic %s but could not forget it", topic.thread_id)
        lines = [f"🧹 {self._me()}: deleted {len(deleted)} topic(s)"
                 + (": " + ", ".join(deleted) if deleted else ".")]
        if failed:
            lines.append("Could not delete (no right to manage topics, or already gone): "
                         + ", ".join(failed))
        if kept:
            lines.append("Kept: " + "; ".join(f"{self._name(t)} ({why})" for t, why in kept))
        return "\n".join(lines)

    async def _delete_messages(self, chat_id: int, ids: Sequence[int]) -> str:
        ids = list(ids)
        if not ids:
            return (f"{self._me()}: no General message this bot can still delete (it reaches "
                    "only its last 48h).")
        deleted = 0
        try:
            for start in range(0, len(ids), DELETE_BATCH):
                batch = ids[start:start + DELETE_BATCH]
                got = await self.host.cleanup_delete_messages(chat_id, batch)
                got = int(got or 0)
                deleted += max(0, got)
                if got:
                    self.ledger.drop(chat_id, batch[:got])
                if got < len(batch):
                    break                      # the rest are gone or no longer deletable
        except Exception as exc:                             # noqa: BLE001 - report it
            logger.exception("could not delete General messages")
            return (f"{self._me()}: deleted {deleted} General message(s), then Telegram "
                    f"refused the rest: {exc}")
        return (f"{self._me()}: deleted {deleted} General message(s). Messages older than "
                "48h stay yours to remove by hand.")

    # -- helpers -------------------------------------------------------------------------

    def _kept(self) -> set:
        """``limits.cleanup_keep`` plus the topics the swarm cannot lose: the pulse topic
        (live, 23 Oct: a bulk delete would have taken "📡 Swarm pulse", which nothing kept)."""
        kept = set(self.keep)
        protected = getattr(self.host, "cleanup_protected_topics", None)
        if callable(protected):
            try:
                kept |= {int(t) for t in protected() or () if t}
            except Exception:                         # noqa: BLE001 - keep what is configured
                logger.warning("cleanup: could not read the protected topics", exc_info=True)
        return kept

    @staticmethod
    def _name(topic: TopicInfo) -> str:
        name = " ".join((topic.title or "").split())
        return f"{name} ({topic.thread_id})" if name else str(topic.thread_id)

    def _me(self) -> str:
        # The alias, never the @handle: a bot's own handle in front of its answer read as
        # addressed to itself (admin, 25 Oct).
        return self.handle.lstrip("@") if self.handle else "cleanup"


def _age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds >= 172800:                       # two days up: days read better than "52h"
        return f"{seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 60:.0f}m"
