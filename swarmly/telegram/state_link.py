"""The transport's link to the shared state store (topic registry, phase 1: shadow).

``state.backend: telegram`` in ``config.yaml`` turns it on. In ``state.mode: shadow`` (the
only mode in phase 1) the store runs beside today's logic and nothing reads it:

* each bot writes a heartbeat key (``probe/<alias>/beat``) every ``state.heartbeat_minutes``,
  carrying its revision and the state hash it sees;
* the bot that holds today's (gitbus) lease also claims and renews the store's lease, and
  publishes the pinned snapshot -- so the lease rules and the snapshot run on the live swarm
  long before phase 3 lets anything depend on them;
* ``/state@bot`` shows what the store looks like from that bot, and the hashes of every bot's
  last heartbeat, which must agree.

Every failure is logged and swallowed: in shadow, the store must never stop the transport.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from swarmly.state import keys as K
from swarmly.state.telegram_store import Pinned, Posted, TelegramIO, TelegramStore

if TYPE_CHECKING:  # pragma: no cover
    from swarmly.telegram.channel import AgentChannel, ChannelEvent
    from swarmly.telegram.wire import Envelope

logger = logging.getLogger("swarmly.telegram.state_link")

#: The store lease's time to live, as today's lease.
LEASE_TTL_SECONDS = 600.0
#: A new snapshot at most this often, unless many writes piled up since the last one: each
#: is a message and a pin in Swarm pulse.
SNAPSHOT_EVERY_SECONDS = 900.0
SNAPSHOT_AFTER_OPS = 25
#: Telegram's flood control: wait what it asks, this many times, before giving up.
RETRIES = 3


def _date(message: Any) -> float:
    date = getattr(message, "date", None)
    if hasattr(date, "timestamp"):
        return float(date.timestamp())
    try:
        return float(date)
    except (TypeError, ValueError):
        return time.time()


class ChannelIO(TelegramIO):
    """Swarm pulse, reached through the transport's bot. Store lines are sent directly, not
    through the paced reply path: they are small, rare, and their message date is needed."""

    def __init__(self, channel: "AgentChannel") -> None:
        self.channel = channel

    async def _place(self) -> Any:
        chat = int(self.channel.config.identity.home_channel_id or 0)
        return chat, await self.channel._ensure_pulse_topic()

    async def _call(self, method: Any, **kwargs: Any) -> Any:
        """One Bot API call, waiting out Telegram's flood control (``retry_after``)."""
        for attempt in range(RETRIES + 1):
            try:
                return await method(**kwargs)
            except Exception as exc:                   # noqa: BLE001 - re-raised below
                wait = getattr(exc, "retry_after", None)
                if not isinstance(wait, (int, float)) or attempt == RETRIES:
                    raise
                logger.info("state: Telegram asks to wait %ss", wait)
                await asyncio.sleep(float(wait) + 0.5)
        raise RuntimeError("unreachable")

    async def post_line(self, text: str) -> Posted:
        chat, topic = await self._place()
        sent = await self._call(self.channel.bot.send_message, chat_id=chat,
                                message_thread_id=topic, text=text,
                                disable_notification=True, disable_web_page_preview=True,
                                parse_mode=None)
        return Posted(int(sent.message_id), _date(sent))

    async def post_file(self, name: str, data: bytes, caption: str) -> Posted:
        from aiogram.types import BufferedInputFile
        chat, topic = await self._place()
        sent = await self._call(self.channel.bot.send_document, chat_id=chat,
                                message_thread_id=topic,
                                document=BufferedInputFile(data, name), caption=caption,
                                disable_notification=True, parse_mode=None)
        return Posted(int(sent.message_id), _date(sent))

    async def pin(self, message_id: int) -> None:
        chat, _ = await self._place()
        await self.channel.bot.pin_chat_message(chat_id=chat, message_id=message_id,
                                                disable_notification=True)

    async def unpin(self, message_id: int) -> None:
        chat, _ = await self._place()
        await self.channel.bot.unpin_chat_message(chat_id=chat, message_id=message_id)

    async def pinned(self) -> Optional[Pinned]:
        chat, _ = await self._place()
        got = await self.channel.bot.get_chat(chat)
        message = getattr(got, "pinned_message", None)
        if message is None:
            return None
        document = getattr(message, "document", None)
        bot = self.channel.bot

        async def read() -> bytes:
            if document is None:
                return b""
            buffer = await bot.download(document.file_id, destination=io.BytesIO())
            return buffer.getvalue()
        caption = getattr(message, "caption", None) or getattr(message, "text", None) or ""
        return Pinned(int(message.message_id), str(caption), read)


class StateLink:
    """One bot's store, in shadow."""

    def __init__(self, channel: "AgentChannel", *, clock: Any = time.time) -> None:
        self.channel = channel
        self.clock = clock
        settings = getattr(channel.config, "state", None)
        self.backend = str(getattr(settings, "backend", "off") or "off").lower()
        self.mode = str(getattr(settings, "mode", "shadow") or "shadow").lower()
        self.beat_seconds = 60.0 * float(getattr(settings, "heartbeat_minutes", 10) or 10)
        self.store: Optional[TelegramStore] = None
        self.error = ""
        self._last_beat = float("-inf")
        self._last_snapshot = float("-inf")
        self._starting: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self.backend == "telegram"

    def start(self) -> None:
        """Begin reading the store, in the background."""
        if not self.enabled or self.store is not None:
            return
        from swarmly.telegram.channel import _state_file
        self.store = TelegramStore(self.channel.config.identity.alias, ChannelIO(self.channel),
                                   clock=self.clock, cache=_state_file(self.channel.config,
                                                                       "state"))
        self._starting = asyncio.get_running_loop().create_task(self._start())

    async def _start(self) -> None:
        assert self.store is not None
        try:
            await self.store.start()
            self.error = ""
            logger.info("state: ready at position %s, hash %s", self.store.log.last_pos,
                        self.store.state_hash())
        except Exception as exc:                       # noqa: BLE001 - shadow: never fatal
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("state: the store did not start: %s", self.error)

    # -- inbound ---------------------------------------------------------------------------
    def on_machine(self, envelope: "Envelope", event: "ChannelEvent") -> bool:
        """A STATE or SNAPSHOT message from a peer; True if the store took it."""
        if self.store is None:
            return False
        raw = event.raw
        document = getattr(raw, "document", None)
        read = None
        if document is not None:
            bot = self.channel.bot

            async def read() -> bytes:
                buffer = await bot.download(document.file_id, destination=io.BytesIO())
                return buffer.getvalue()
        return self.store.ingest(message_id=int(event.message_id or 0), date=_date(raw),
                                 author=self.channel._alias_for(event),
                                 text=event.text or "", read_file=read)

    # -- the beat --------------------------------------------------------------------------
    async def beat(self, *, now: Optional[float] = None) -> str:
        """Called from the heartbeat. Returns what it did, for the log and a test."""
        store = self.store
        if store is None:
            return "off"
        if not store.ready:
            if self._starting is None or self._starting.done():
                self._starting = asyncio.get_running_loop().create_task(self._start())
            return "starting"
        moment = self.clock() if now is None else now
        done = []
        try:
            await store.refresh()
            if self.channel.is_coordinator():
                lease = store.lease()
                mine = K.norm(lease.holder or "") == store.alias
                if not mine or lease.expires_at - moment < LEASE_TTL_SECONDS / 2:
                    result = await store.claim_lease(LEASE_TTL_SECONDS, confirm=False)
                    done.append(f"lease {'claimed' if result.applied else 'refused'} "
                                f"(term {store.lease().term})")
                fresh = sum(1 for o in store.log.ops if o.pos > store.snapshot_pos)
                due = (moment - self._last_snapshot >= SNAPSHOT_EVERY_SECONDS
                       or fresh >= SNAPSHOT_AFTER_OPS)
                if due and await store.publish_snapshot(now=moment):
                    self._last_snapshot = moment
                    done.append(f"snapshot to p{store.snapshot_pos}")
                elif store.snapshot_message is None and await store.restore_from_cache():
                    self._last_snapshot = moment
                    done.append("snapshot restored")
            if moment - self._last_beat >= self.beat_seconds:
                rev = str(getattr(self.channel, "_running_revision", "") or "")
                await store.write(K.probe(store.alias, "beat"),
                                  {"at": round(moment), "rev": rev[:12],
                                   "pos": store.log.last_pos, "hash": store.state_hash()})
                self._last_beat = moment
                done.append("beat")
            self.error = ""
        except Exception as exc:                       # noqa: BLE001 - shadow: never fatal
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("state: beat failed: %s", self.error)
            return "failed"
        return ", ".join(done) or "nothing to do"

    # -- /state ----------------------------------------------------------------------------
    def describe(self) -> str:
        if not self.enabled:
            return "State store: off (state.backend is not set to telegram)."
        store = self.store
        if store is None:
            return "State store: not started."
        lease = store.lease()
        lines = [f"State store ({self.backend}, {self.mode}): "
                 + ("ready" if store.ready else "not ready")
                 + (f" -- last error: {self.error}" if self.error else ""),
                 f"position {store.log.last_pos}, {len(store.keys())} key(s), "
                 f"hash {store.state_hash()}",
                 f"lease: {lease.holder or 'nobody'} (term {lease.term})",
                 f"snapshot: message {store.snapshot_message or 'none'}"]
        lines.extend(self.agreement())
        if store.log.rejected:
            lines.append(f"{len(store.log.rejected)} write(s) refused since the snapshot")
        return "\n".join(lines)

    def agreement(self) -> list:
        """For each bot's last beat: does this bot's state at that position hash the same?"""
        store = self.store
        if store is None:
            return []
        verdicts = []
        for key in store.keys("probe/"):
            if not key.endswith("/beat"):
                continue
            value = store.read(key) or {}
            who = key.split("/")[1]
            try:
                mine = store.log.hash_at(int(value.get("pos")))
            except (TypeError, ValueError):
                mine = None
            theirs = value.get("hash")
            if mine is None:
                verdicts.append(f"{who}: beat before this bot's snapshot, not comparable")
            else:
                verdicts.append(f"{who}: {'agrees' if mine == theirs else 'DIFFERS'} at "
                                f"p{value.get('pos')} ({theirs})")
        return verdicts
