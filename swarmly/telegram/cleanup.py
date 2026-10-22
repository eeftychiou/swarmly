"""``/cleanup``: the admin's tidy-up of the group (issue #63).

After a round the home group fills with one-off forum topics -- a topic per side task, a
topic per PR -- and with machine notices in General, and neither people nor bots can find
the work that matters. The bots are group admins, so the transport can tidy it itself; only
the admin may ask, and no deletion happens without a second, explicit confirmation.

What the command can do (``/cleanup@<bot> …``, answered by the transport, never a model
turn):

* no arguments -- a dry run: every topic this transport knows, with its id, name, last
  activity, linked issue/PR and that item's state, plus the General messages it could still
  delete. Nothing changes.
* ``topics done`` -- the topics whose linked issue or PR is closed or merged and that have
  been idle for at least ``limits.cleanup_idle_hours`` (default 24).
* ``topic <id>`` -- that one topic.
* ``topics all`` -- every topic except General and the ids in ``limits.cleanup_keep``.
* ``general`` -- the General messages this bot can still delete, oldest first, in batches.

Every deleting form first replies with what it will remove and a one-use code; the deletion
runs only on ``confirm <code>`` within :data:`CONFIRM_SECONDS`, because it cannot be undone.

Two Telegram limits shape this. ``deleteForumTopic`` works on a topic of any age, but the
Bot API cannot *list* a group's topics, so the topics offered are the ones the transports
know (``topics-<alias>.json``). ``deleteMessage(s)`` works only on messages under 48 hours
old and the Bot API cannot read history either, so General cleanup covers only the message
ids a transport recorded while running: :class:`GeneralLedger` keeps them with their
timestamps, prunes the ones past the window, and counts those, so the dry run can say how
many it can no longer reach. Older General messages stay the admin's to delete by hand.

After a topic is deleted this transport forgets everything it kept about it (topic memory,
the topic's CLI session, the issue/PR -> topic map, its ``/model`` and ``/reasoning``
choices, pending hand-offs, the name cache) and posts one machine
``NOTE intent=TOPIC_GONE ref=<topic>``, so the peers forget it too.
"""

from __future__ import annotations

import asyncio
import logging
import secrets as _secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarmly.telegram.adapters.base import load_state, save_state

logger = logging.getLogger("swarmly.telegram.cleanup")

#: ``deleteMessage`` works only on messages younger than this; the ledger keeps no longer.
LEDGER_TTL_SECONDS = 48 * 3600
#: One Telegram ``deleteMessages`` call takes at most this many ids.
DELETE_BATCH = 100
#: How long a confirmation code stands.
CONFIRM_SECONDS = 300.0
#: Bound on the ledger: a chat's recorded ids are trimmed to the newest of these.
LEDGER_MAX_PER_CHAT = 2000
#: A topic with no recorded activity is offered by ``topics done`` only if its item is closed
#: *and* this transport has known it this long (nothing ever touched it since it was named).
UNKNOWN_ACTIVITY_IDLE_SECONDS = 24 * 3600


@dataclass
class TopicInfo:
    """One forum topic, as the dry run shows it and the selectors choose it."""

    thread_id: int
    chat_id: Optional[int] = None
    title: Optional[str] = None
    last_activity: Optional[float] = None       # wall clock, or None when never recorded
    refs: Tuple[str, ...] = ()                  # "#12", "#34" -- issues and PRs it points at
    states: Dict[str, Optional[str]] = field(default_factory=dict)   # ref -> open/closed/merged

    @property
    def done(self) -> bool:
        """Every item this topic links is closed or merged (no linked item: not done).

        Read the *refs*, not ``states``: a ref GitHub could not be asked about can be missing
        from ``states`` entirely (or its state unknown), and a topic is offered for deletion
        only when every item it names is provably closed or merged.
        """
        return bool(self.refs) and all(
            (self.states.get(ref) or "").lower() in ("closed", "merged") for ref in self.refs)

    def idle_for(self, now: float) -> float:
        """Seconds since this topic was last active; ``UNKNOWN_ACTIVITY_IDLE_SECONDS`` when
        nothing was ever recorded (a topic named once and never posted in)."""
        if self.last_activity is None:
            return float(UNKNOWN_ACTIVITY_IDLE_SECONDS)
        return max(0.0, now - float(self.last_activity))


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


@dataclass
class _Pending:
    """One deleting form waiting for ``confirm <code>``."""

    code: str
    what: str                     # "topics" or "general"
    chat_id: int
    thread_ids: List[int] = field(default_factory=list)
    message_ids: List[int] = field(default_factory=list)
    expires: float = 0.0


class Cleanup:
    """``/cleanup``: the dry run, the four deleting forms, and their confirmation.

    The channel supplies the Telegram calls and the forget-after-delete through a small host
    interface, so everything here is testable with fakes:

    * ``cleanup_topics() -> List[TopicInfo]`` -- the topics this transport knows
    * ``cleanup_home_chat() -> Optional[int]`` -- the home group's chat id
    * ``cleanup_delete_topic(chat_id, thread_id) -> bool`` (awaited)
    * ``cleanup_delete_messages(chat_id, ids) -> int`` (awaited): how many went
    * ``cleanup_forget_topic(chat_id, thread_id)`` (awaited): forget it, and tell the peers
    """

    def __init__(self, host: Any, ledger: GeneralLedger, *, handle: str = "",
                 idle_hours: float = 24.0, keep: Sequence[int] = (),
                 clock: Callable[[], float] = time.time,
                 code: Optional[Callable[[], str]] = None,
                 confirm_seconds: float = CONFIRM_SECONDS) -> None:
        self.host = host
        self.ledger = ledger
        self.handle = handle
        self.idle_hours = float(idle_hours)
        self.keep = {int(t) for t in keep if isinstance(t, int) and not isinstance(t, bool)}
        self._clock = clock
        self._code = code or (lambda: _secrets.token_hex(3))
        self.confirm_seconds = float(confirm_seconds)
        self._pending: Dict[str, _Pending] = {}
        #: The selectors the last dry run offered, for the tests and the logs.
        self.last_topics: List[TopicInfo] = []

    # -- the command ---------------------------------------------------------------------

    async def handle_args(self, argv: Sequence[str], *, is_admin: bool,
                          chat_id: Optional[int] = None) -> str:
        """Answer ``/cleanup <argv>``: the dry run, a refusal, a confirmation, a usage line."""
        if not is_admin:
            logger.warning("/cleanup ignored: only the admin tidies the group")
            return (f"⛔ {self._me()}: only the admin runs /cleanup. "
                    f"Ask them, or ask them to pass it on.")
        words = [str(word).lower() for word in argv]
        home = chat_id if chat_id is not None else self.host.cleanup_home_chat()
        if home is None:
            return f"{self._me()}: /cleanup needs agent.home_channel_id."
        home = int(home)

        if not words:
            return self.dry_run(home, topics=await self._topics_off_loop())
        if words[0] == "confirm":
            return await self._confirm(words[1:], home)
        if words == ["general"]:
            return await self._ask_general(home)
        if words == ["topics", "done"]:
            return await self._ask_done(home)
        if words == ["topics", "all"]:
            return await self._ask_all(home)
        if len(words) == 2 and words[0] == "topic":
            return await self._ask_one(words[1], home)
        return (f"{self._me()}: /cleanup takes one of: (nothing) for a dry run, "
                "topics done, topics all, topic <id>, general, confirm <code>.")

    # -- the dry run ---------------------------------------------------------------------

    def dry_run(self, chat_id: int, now: Optional[float] = None,
                topics: Optional[List[TopicInfo]] = None) -> str:
        """Every known topic with its state, and the General messages it could delete."""
        moment = float(now if now is not None else self._clock())
        topics = self._topics() if topics is None else topics
        self.last_topics = topics
        lines = [f"🧹 {self._me()} cleanup (dry run: nothing is deleted)", ""]
        if topics:
            lines.append(f"Topics ({len(topics)}):")
            for topic in topics:
                lines.append("· " + self._describe(topic, moment))
        else:
            lines.append("Topics: none known to this transport.")
        recent = self.ledger.recent(chat_id, moment)
        expired = self.ledger.expired(chat_id)
        general = (f"General: {len(recent)} message(s) this bot can still delete, "
                   "oldest first")
        if expired:
            general += (f"; {expired} recorded longer than 48h ago it can no longer delete "
                        "(older messages stay yours to remove by hand)")
        else:
            general += ("; anything older than 48h it cannot reach -- the Bot API cannot list "
                        "history, so those are yours to remove by hand")
        lines += ["", general]
        done = [topic for topic in topics if self._is_done(topic, moment)]
        keep = sorted(self.keep)
        lines += ["", "Then:"]
        lines.append(f"· /cleanup{self._at()} topics done -- {len(done)} topic(s) whose item is "
                     f"closed or merged and idle at least {self.idle_hours:g}h")
        lines.append(f"· /cleanup{self._at()} topics all -- every topic except General"
                     + (f" and {', '.join(str(t) for t in keep)}" if keep else ""))
        if recent:
            lines.append(f"· /cleanup{self._at()} general -- the {len(recent)} message(s) above")
        lines.append("Each one replies with what it will remove and a code; nothing is deleted "
                     f"until /cleanup{self._at()} confirm <code> within "
                     f"{self.confirm_seconds / 60:g} minutes.")
        return "\n".join(lines)

    # -- the deleting forms: ask, then confirm -------------------------------------------

    async def _ask_done(self, chat_id: int) -> str:
        moment = self._clock()
        targets = [topic for topic in await self._topics_off_loop()
                   if self._is_done(topic, moment)]
        if not targets:
            return (f"{self._me()}: no topic has a closed or merged item and has been idle "
                    f"{self.idle_hours:g}h. Nothing to delete.")
        return self._ask("topics", chat_id, thread_ids=[t.thread_id for t in targets],
                         listing=[self._describe(t, moment) for t in targets])

    async def _ask_all(self, chat_id: int) -> str:
        moment = self._clock()
        targets = [topic for topic in await self._topics_off_loop()
                   if topic.thread_id not in self.keep]
        if not targets:
            return f"{self._me()}: no topic to delete (only General and the kept ones)."
        return self._ask("topics", chat_id, thread_ids=[t.thread_id for t in targets],
                         listing=[self._describe(t, moment) for t in targets])

    async def _ask_one(self, word: str, chat_id: int) -> str:
        try:
            thread_id = int(word.lstrip("#"))
        except ValueError:
            return f"{self._me()}: /cleanup topic takes a topic id (a number)."
        topic = next((t for t in await self._topics_off_loop() if t.thread_id == thread_id),
                     None)
        return self._ask("topics", chat_id, thread_ids=[thread_id],
                         listing=[self._describe(topic, self._clock()) if topic
                                  else f"{thread_id} · unknown to this transport"])

    async def _ask_general(self, chat_id: int) -> str:
        ids = self.ledger.recent(chat_id, self._clock())
        if not ids:
            return (f"{self._me()}: no General message it could still delete (the Bot API "
                    "reaches only what this transport recorded in the last 48h).")
        listing = [f"{len(ids)} message(s), oldest first: "
                   + ", ".join(str(i) for i in ids[:20])
                   + (" …" if len(ids) > 20 else "")]
        return self._ask("general", chat_id, message_ids=ids, listing=listing)

    def _ask(self, what: str, chat_id: int, *, thread_ids: Optional[List[int]] = None,
             message_ids: Optional[List[int]] = None, listing: Sequence[str] = ()) -> str:
        code = self._code()
        expires = self._clock() + self.confirm_seconds
        self._pending = {code: _Pending(code=code, what=what, chat_id=chat_id,
                                        thread_ids=list(thread_ids or []),
                                        message_ids=list(message_ids or []),
                                        expires=expires)}
        noun = "topic(s)" if what == "topics" else "message(s)"
        count = len(thread_ids or []) if what == "topics" else len(message_ids or [])
        head = (f"🧹 {self._me()} will delete {count} {noun}. This cannot be undone.")
        body = "\n".join(listing[:40])
        if len(listing) > 40:
            body += f"\n(+{len(listing) - 40} more)"
        return (f"{head}\n\n{body}\n\nTo delete them, reply "
                f"/cleanup{self._at()} confirm {code} within "
                f"{self.confirm_seconds / 60:g} minutes. Anything else cancels nothing; "
                "the code simply expires.")

    async def _confirm(self, words: Sequence[str], chat_id: int) -> str:
        if len(words) != 1:
            return f"{self._me()}: /cleanup confirm takes the code it gave you."
        pending = self._pending.pop(str(words[0]).lower(), None)
        if pending is None:
            return f"{self._me()}: no cleanup is waiting on that code (it expired or was used)."
        if pending.expires < self._clock():
            return (f"{self._me()}: that code expired before you confirmed it; run the cleanup "
                    "again for a fresh one.")
        if pending.chat_id != int(chat_id):
            return f"{self._me()}: that code was issued in another chat."
        if pending.what == "general":
            return await self._delete_messages(pending)
        return await self._delete_topics(pending)

    async def _delete_topics(self, pending: _Pending) -> str:
        deleted: List[str] = []
        failed: List[str] = []
        for thread_id in pending.thread_ids:
            try:
                ok = await self.host.cleanup_delete_topic(pending.chat_id, thread_id)
            except Exception:                              # noqa: BLE001 - report, go on
                logger.exception("could not delete topic %s", thread_id)
                ok = False
            if not ok:
                failed.append(f"#{thread_id}")
                continue
            deleted.append(f"#{thread_id}")
            try:
                await self.host.cleanup_forget_topic(pending.chat_id, thread_id)
            except Exception:                              # noqa: BLE001 - the delete stands
                logger.exception("deleted topic %s but could not forget it", thread_id)
        parts = []
        if deleted:
            parts.append(f"{self._me()}: deleted {len(deleted)} topic(s): "
                         + ", ".join(deleted) + ". Each bot has forgotten them "
                         "(a machine TOPIC_GONE note told the peers).")
        if failed:
            parts.append(f"{len(failed)} could not be deleted (no admin right to manage "
                         "topics, or already gone): " + ", ".join(failed))
        return "\n".join(parts) if parts else f"{self._me()}: nothing was deleted."

    async def _delete_messages(self, pending: _Pending) -> str:
        ids = list(pending.message_ids)
        deleted = 0
        try:
            for start in range(0, len(ids), DELETE_BATCH):
                batch = ids[start:start + DELETE_BATCH]
                got = await self.host.cleanup_delete_messages(pending.chat_id, batch)
                got = int(got or 0)
                deleted += max(0, got)
                if got:
                    self.ledger.drop(pending.chat_id, batch[:got])
                if got < len(batch):
                    break                      # the rest are gone or no longer deletable
        except Exception as exc:                             # noqa: BLE001 - report it
            logger.exception("could not delete General messages")
            return (f"{self._me()}: deleted {deleted} General message(s), then Telegram "
                    f"refused the rest: {exc}")
        return (f"{self._me()}: deleted {deleted} General message(s). Messages older than "
                "48h stay yours to remove by hand.")

    # -- helpers -------------------------------------------------------------------------

    async def _topics_off_loop(self) -> List[TopicInfo]:
        """The topics, gathered in a worker thread: their states are GitHub lookups, and the bot
        must keep handling messages while they run (live: a dry run held it for 52 s)."""
        return await asyncio.to_thread(self._topics)

    def _topics(self) -> List[TopicInfo]:
        topics = list(self.host.cleanup_topics() or [])
        return sorted(topics, key=lambda t: t.thread_id)

    def _is_done(self, topic: TopicInfo, now: float) -> bool:
        if topic.thread_id in self.keep:
            return False
        return topic.done and topic.idle_for(now) >= self.idle_hours * 3600

    def _describe(self, topic: TopicInfo, now: float) -> str:
        name = " ".join((topic.title or "").split()) or "unnamed"
        parts = [str(topic.thread_id), name, f"idle {_age(topic.idle_for(now))}"]
        if topic.refs:
            parts.append(", ".join(
                f"{ref} {(topic.states.get(ref) or 'unknown')}" for ref in topic.refs))
        else:
            parts.append("no linked item")
        return " · ".join(parts)

    def _me(self) -> str:
        return self.handle or "cleanup"

    def _at(self) -> str:
        return self.handle if self.handle else ""


def _age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds >= 172800:                       # two days up: days read better than "52h"
        return f"{seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 60:.0f}m"
