"""The Telegram backend: the op log is the Swarm pulse topic, the snapshot is a pinned file.

``docs/design/topic-registry.md`` §1b, "Telegram as the store":

* **A write is one machine line** in Swarm pulse::

      [A2A v1.1] NOTE to=* intent=STATE ref=<key>

      {"k":"<key>","b":<base>,"t":<term>,"w":"<alias>","v":{...}}

  Several writes made together go in one line as a JSON list (``ref=batch``), as the design
  asks, so a beat that changes ten keys is one message. An op's position is
  ``message id * SLOTS + its index in the message`` and its time the message's Telegram date,
  so every bot folds the same log the same way. The writer is who Telegram says sent it, not who the line
  claims. A value too big for one message goes as a JSON file with the header as caption.
* **The snapshot is a pinned file.** The lease holder posts the state as a new JSON file,
  pins it and unpins the previous one; ``getChat`` returns the chat's newest *sent* pin, so a
  dashboard that is only ever edited in place never hides it. It covers only ops older than
  ``SNAPSHOT_SETTLE_SECONDS``, so no line still on its way can land behind it.
* **Starting or rejoining** reads the local cache, then the pin, then whatever lines arrive.
  A line written against a version this bot has not seen (a gap) makes it read the pin again.
  The pin holds only settled ops, so a bot that restarts can lack the newest seconds of the
  log until a later line (a gap) or the next snapshot fills them in; in shadow that is fine,
  and phase 3 has to keep it in mind (review, #263).
* **A snapshot from an older term is refused**, and a snapshot never overwrites an entry the
  reader holds with a higher (term, position): see ``StateLog.rebase``.

Telegram itself is reached through a small ``TelegramIO`` the transport provides (post a
line, post a file, pin, unpin, read the pin), so this module needs no aiogram and is tested
with a fake group.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from swarmly.state.log import Op, StateLog
from swarmly.state.store import StateStore, StoreError
from swarmly.telegram.wire import Envelope, MessageType, format_message, parse_message

logger = logging.getLogger("swarmly.state.telegram")

STATE_INTENT = "STATE"
SNAPSHOT_INTENT = "SNAPSHOT"
#: Over this, the op goes as a file (Telegram's limit is 4096; the header needs room).
MAX_LINE_CHARS = 3500
#: One message may carry up to this many ops (a batch); an op's position is
#: ``message_id * SLOTS + its index in the message``, so the order stays total.
SLOTS = 64
#: A snapshot covers only ops at least this old.
SNAPSHOT_SETTLE_SECONDS = 15.0


@dataclass
class Posted:
    message_id: int
    date: float


@dataclass
class Pinned:
    message_id: int
    caption: str
    read: Callable[[], Awaitable[bytes]]


class TelegramIO:
    """What the store needs from the transport. Every method may raise: the store fails
    closed (``StoreError``) and the caller acts on nothing until it works again."""

    async def post_line(self, text: str) -> Posted:
        raise NotImplementedError

    async def post_file(self, name: str, data: bytes, caption: str) -> Posted:
        raise NotImplementedError

    async def pin(self, message_id: int) -> None:
        raise NotImplementedError

    async def unpin(self, message_id: int) -> None:
        raise NotImplementedError

    async def pinned(self) -> Optional[Pinned]:
        raise NotImplementedError


def header(intent: str, ref: str) -> str:
    return format_message(Envelope(type=MessageType.NOTE, to="*", intent=intent, ref=ref))


class TelegramStore(StateStore):
    CONFIRM_SECONDS = 4.0

    def __init__(self, alias: str, io: TelegramIO, **kwargs: Any) -> None:
        super().__init__(alias, **kwargs)
        self.io = io
        #: ops that arrived since the last refresh, as (op, None) or (wire-less file, loader)
        self._inbox: List[Op] = []
        self._files: List[Tuple[int, float, str, Callable[[], Awaitable[bytes]]]] = []
        self.snapshot_message: Optional[int] = None
        self._snapshot_pos = 0
        #: the newest SNAPSHOT line seen, and how often the pin was re-read for it
        self._want_pin = 0
        self._pin_tries = 0

    @property
    def snapshot_pos(self) -> int:
        return self._snapshot_pos

    # -- inbound: the transport calls this for every STATE / SNAPSHOT message ------------
    def ingest(self, *, message_id: int, date: float, author: str, text: str,
               read_file: Optional[Callable[[], Awaitable[bytes]]] = None) -> bool:
        """Take one message from Swarm pulse; True if it was the store's. Never raises."""
        try:
            envelope, body = parse_message(text or "")
        except Exception:                              # noqa: BLE001 - not ours
            return False
        intent = (envelope.intent or "").upper()
        if envelope.type != MessageType.NOTE or intent not in (STATE_INTENT, SNAPSHOT_INTENT):
            return False
        if intent == SNAPSHOT_INTENT:
            # Read through the pin, not here -- but a newer snapshot than the one this bot
            # holds means its next refresh reads the pin again (that is how a bot that adopted
            # a stale pin heals at the true holder's next snapshot; review, #263).
            if int(message_id) > int(self.snapshot_message or 0):
                self.log.late = True
                self._want_pin = max(self._want_pin, int(message_id))
            return True
        self.observe_time(float(date))
        if read_file is not None:
            self._files.append((int(message_id), float(date), author, read_file))
            return True
        try:
            # Folded at once, not at the next refresh: lease readers (is_coordinator, the
            # fence, succession) must see a peer's claim when it arrives (review, phase 3).
            self.log.add_all(self._ops(json.loads(body), message_id, date, author))
        except (ValueError, KeyError, TypeError):
            logger.warning("state: unreadable STATE line %s from %s", message_id, author)
        return True

    @staticmethod
    def _ops(payload: Any, message_id: int, date: float, author: str) -> List[Op]:
        """The ops of one message: an object, or a list of them (a batch)."""
        wires = payload if isinstance(payload, list) else [payload]
        if len(wires) > SLOTS:
            # More ops than positions: refuse the whole message rather than lose some.
            logger.warning("state: message %s from %s carries %d ops (at most %d); ignored",
                           message_id, author, len(wires), SLOTS)
            return []
        return [Op.from_wire(wire, pos=int(message_id) * SLOTS + index, at=date,
                             writer=author)
                for index, wire in enumerate(wires)]

    async def _pull(self) -> List[Op]:
        ops, self._inbox = self._inbox, []
        files, self._files = self._files, []
        for message_id, date, author, read in files:
            try:
                ops.extend(self._ops(json.loads((await read()).decode("utf-8")), message_id,
                                     date, author))
            except Exception:                          # noqa: BLE001 - one bad file
                logger.warning("state: STATE file %s from %s unreadable", message_id, author,
                               exc_info=True)
        return ops

    # -- outbound --------------------------------------------------------------------------
    async def _append(self, wire: Dict[str, Any]) -> Tuple[int, float]:
        return (await self._append_many([wire]))[0]

    async def _append_many(self, wires: List[Dict[str, Any]]) -> List[Tuple[int, float]]:
        """As few messages as fit: lines of up to SLOTS ops under MAX_LINE_CHARS; a single
        op too big for a line goes as a file."""
        out: List[Tuple[int, float]] = []
        chunk: List[Dict[str, Any]] = []

        async def flush() -> None:
            if not chunk:
                return
            payload: Any = chunk[0] if len(chunk) == 1 else list(chunk)
            ref = chunk[0]["k"] if len(chunk) == 1 else "batch"
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            head = header(STATE_INTENT, ref)
            try:
                if len(head) + len(body) + 2 > MAX_LINE_CHARS:
                    name = f"state-{ref.replace('/', '_')}.json"
                    posted = await self.io.post_file(name, body.encode("utf-8"), head)
                else:
                    posted = await self.io.post_line(f"{head}\n\n{body}")
            except Exception as exc:                   # noqa: BLE001
                raise StoreError(f"the write did not reach Telegram: {exc}") from exc
            out.extend((int(posted.message_id) * SLOTS + i, float(posted.date))
                       for i in range(len(chunk)))
            chunk.clear()

        size = 0
        for wire in wires:
            length = len(json.dumps(wire, separators=(",", ":"))) + 1
            if chunk and (len(chunk) >= SLOTS or size + length > MAX_LINE_CHARS - 200):
                await flush()
                size = 0
            chunk.append(wire)
            size += length
        await flush()
        return out

    # -- the snapshot ----------------------------------------------------------------------
    async def _load_snapshot(self) -> None:
        try:
            pinned = await self.io.pinned()
        except Exception as exc:                       # noqa: BLE001
            raise StoreError(f"the pinned snapshot could not be read: {exc}") from exc
        if pinned is None:
            return
        envelope, _body = parse_message(pinned.caption or "")
        if (envelope.intent or "").upper() != SNAPSHOT_INTENT:
            logger.info("state: the newest pin (%s) is not a snapshot", pinned.message_id)
            return
        try:
            data = json.loads((await pinned.read()).decode("utf-8"))
        except Exception as exc:                       # noqa: BLE001
            raise StoreError(f"the snapshot file could not be read: {exc}") from exc
        if not self.log.rebase(data):
            logger.warning("state: the pinned snapshot %s is stale (term %s, position %s); "
                           "keeping this bot's state", pinned.message_id, data.get("term"),
                           data.get("last_pos"))
            return
        self.snapshot_message = pinned.message_id
        self._snapshot_pos = self.log.base_pos

    async def refresh(self) -> None:
        await super().refresh()
        if self._want_pin > int(self.snapshot_message or 0) and self._pin_tries < 3:
            # The SNAPSHOT line can arrive before its file is pinned: read the pin again on the
            # next refresh, a few times at most (review).
            self._pin_tries += 1
            self.log.late = True
        elif self._want_pin <= int(self.snapshot_message or 0):
            self._pin_tries = 0

    async def publish_snapshot(self, *, now: Optional[float] = None,
                               force: bool = False) -> bool:
        """Post and pin the state (the lease holder only, by the log's clock). False when
        there is nothing new."""
        moment = self.server_now() if now is None else now
        if not self.holds_lease(moment):
            return False
        settled = [o.pos for o in self.log.ops if o.at <= moment - SNAPSHOT_SETTLE_SECONDS]
        upto = settled[-1] if settled else self.log.base_pos
        if upto <= self._snapshot_pos and not force:
            return False
        snapshot = self.log.snapshot(upto=upto)
        lease = self.lease()
        if snapshot["term"] != lease.term or                 str(snapshot.get("holder") or "").lower() != self.alias:
            # The cut's lease is not this bot's current one (its claim has not settled yet):
            # publishing it would pin a term this bot does not hold in that cut.
            return False
        data = json.dumps(snapshot, separators=(",", ":")).encode()
        cut = StateLog.from_snapshot(snapshot).state_hash()   # the hash of what is in the file
        caption = format_message(Envelope(type=MessageType.NOTE, to="*",
                                          intent=SNAPSHOT_INTENT, ref=f"p{upto}"),
                                 f"term={snapshot['term']} hash={cut}")
        try:
            posted = await self.io.post_file(f"swarm-state-{upto}.json", data, caption)
            await self.io.pin(posted.message_id)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("state: the snapshot was not published: %s", exc)
            return False
        previous, self.snapshot_message = self.snapshot_message, posted.message_id
        # Fold in the ops the *previous* snapshot covered, not this one's: the last interval
        # stays in the log, so /state can still compare peers' recent beats (hash_at).
        self.log.compact(self._snapshot_pos)
        self._snapshot_pos = upto
        if previous is not None and previous != posted.message_id:
            try:
                await self.io.unpin(previous)
            except Exception:                          # noqa: BLE001 - an extra pin is harmless
                logger.info("state: the previous snapshot %s stays pinned", previous)
        return True

    # -- recovery when the pin is lost ---------------------------------------------------
    async def restore_from_cache(self) -> bool:
        """The lease holder publishes its own state when there is no snapshot pinned (what it
        holds is its folded log, which its local cache mirrors).

        The holder reads every line, so its state is the newest any bot has; the design's
        fuller recovery (every bot offers its cache, the highest wins) comes with phase 3,
        when the lease itself lives here.
        """
        try:
            pinned = await self.io.pinned()
        except Exception:                              # noqa: BLE001
            return False
        if pinned is not None:
            envelope, _ = parse_message(pinned.caption or "")
            if (envelope.intent or "").upper() == SNAPSHOT_INTENT:
                return False
        moment = self.server_now()
        settled = any(o.at <= moment - SNAPSHOT_SETTLE_SECONDS for o in self.log.ops)
        if not settled and self.log.base_pos == 0:
            return False                               # nothing settled to restore yet
        logger.warning("state: no snapshot is pinned; publishing this bot's state")
        return await self.publish_snapshot(force=True)
