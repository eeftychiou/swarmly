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
        #: Phase 3: "shadow" mirrors today's lease into the store and compares; "store" makes
        #: the store's lease the coordinator lease (check_lease claims it; nothing mirrors).
        self.lease_mode = str(getattr(settings, "lease", "shadow") or "shadow").lower() \
            if self.backend == "telegram" else "off"
        self.store: Optional[TelegramStore] = None
        self.error = ""
        self._last_beat = float("-inf")
        self._last_snapshot = float("-inf")
        self._starting: Optional[asyncio.Task] = None
        from swarmly.telegram.registry_shadow import RegistryShadow
        self.registry = RegistryShadow(channel, clock=clock)

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
            holder_beat, refused = False, False
            if self.lease_mode == "shadow" and self.channel.is_coordinator():
                # Shadow: today's lease decides; the store's mirrors it.
                holder_beat = True
                lease = store.lease()
                mine = K.norm(lease.holder or "") == store.alias
                server = store.server_now()             # leases run on the log's clock
                gitbus_term = int(getattr(self._cached_gitbus(), "term", 0) or 0)
                if not mine or lease.expires_at - server < LEASE_TTL_SECONDS / 2 \
                        or gitbus_term > lease.term:
                    # The mirror follows gitbus's term, so a later switch to the store lease
                    # carries on above the terms the round files already fence on.
                    result = await store.claim_lease(LEASE_TTL_SECONDS, confirm=False,
                                                     at_least=gitbus_term)
                    refused = not result.applied
                    done.append(f"lease {'refused' if refused else 'claimed'} "
                                f"(term {store.lease().term})")
            elif self.lease_mode == "store" and store.holds_lease():
                holder_beat = True                      # check_lease renews the lease itself
            if holder_beat:
                fresh = sum(1 for o in store.log.ops if o.pos > store.snapshot_pos)
                due = (moment - self._last_snapshot >= SNAPSHOT_EVERY_SECONDS
                       or fresh >= SNAPSHOT_AFTER_OPS)
                # A beat whose lease write was refused publishes nothing (review, #263).
                if due and not refused and await store.publish_snapshot():
                    self._last_snapshot = moment
                    done.append(f"snapshot to p{store.snapshot_pos}")
                elif not refused and store.snapshot_message is None \
                        and await store.restore_from_cache():
                    self._last_snapshot = moment
                    done.append("snapshot restored")
            if moment - self._last_beat >= self.beat_seconds:
                # The registry at the state cadence, not every heartbeat; its own failures
                # never stop the probe beat or /state's agreement check (review).
                try:
                    written = await self.registry.sync(store)
                    if written:
                        done.append(f"registry: {len(written)} key(s)")
                except Exception as exc:               # noqa: BLE001 - shadow: never fatal
                    logger.warning("state: registry sync failed: %s", exc, exc_info=True)
                    done.append("registry failed")
                try:
                    if await self.sync_owed(store, moment):
                        done.append("owed")
                except Exception as exc:               # noqa: BLE001 - shadow: never fatal
                    logger.warning("state: owed sync failed: %s", exc, exc_info=True)
                    done.append("owed failed")
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

    # -- links and work items (phase 5) -------------------------------------------------------
    def _ops(self) -> Any:
        from swarmly.telegram.registry_ops import RegistryOps
        return RegistryOps(self.store, home=int(self.channel.config.identity.home_channel_id
                                                or 0),
                           repo=self.registry._repo(), clock=self.clock)

    def note_reply(self, chat_id: int, thread_id: Optional[int], text: str) -> None:
        """The coordinator's reply in a topic: record its LINK/ASSIGN/PHASE lines (the lease
        holder only -- it alone writes topic records)."""
        from swarmly.state.links import parse_lines
        store = self.store
        if store is None or not store.ready or thread_id is None or not text:
            return
        if int(chat_id) != int(self.channel.config.identity.home_channel_id or 0):
            return
        lines = parse_lines(text)
        if not lines or not store.holds_lease():
            return
        name = self.channel.topics.name(chat_id, thread_id) or ""

        async def record() -> None:
            try:
                ops = self._ops()
                topic = ops.topic(thread_id, name)
                done = ops.apply(topic, lines, K.norm(self.channel.config.identity.alias))
                if await ops.save(thread_id, topic):
                    logger.info("registry: topic %s: %s", thread_id, "; ".join(done))
            except Exception:                          # noqa: BLE001 - shadow: never fatal
                logger.warning("registry: the reply's lines were not recorded", exc_info=True)
        task = asyncio.get_running_loop().create_task(record())
        self._tasks = getattr(self, "_tasks", set())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    REGISTRY_COMMANDS = ("link", "unlink", "phase")

    async def registry_command(self, event: Any) -> Optional[str]:
        """``/link @bot``, ``/unlink @bot``, ``/phase <ref|L1> <phase>`` from the admin, in a
        topic. None: not one of them; "": another bot answers; else the reply to post. The
        lease holder answers a bare one; a targeted one is answered by its target."""
        from swarmly.telegram.commands import parse_command
        parsed = parse_command(event.text or "")
        if not parsed or parsed[0] not in self.REGISTRY_COMMANDS:
            return None
        command, target = parsed
        me = str(self.channel.config.identity.name or "").lstrip("@").lower()
        store = self.store
        holder = store is not None and store.ready and store.holds_lease()
        if target is not None and target.lstrip("@").lower() != me:
            return ""
        if target is None and not holder:
            return ""
        if event.author_is_bot or not self.channel.config.is_admin(event.author_id):
            return "Only the admin changes a topic's links."
        if store is None or not store.ready:
            return "The state store is not running here (state.backend), so nothing is kept."
        if not holder:
            return "Only the bot holding the store's lease writes topic records; ask it."
        if event.thread_id is None:
            return f"/{command} works inside a topic."
        args = (event.text or "").split()[1:]            # the bot named in /link @bot stays
        ops = self._ops()
        topic = ops.topic(event.thread_id,
                          self.channel.topics.name(event.chat_id, event.thread_id) or "")
        by = "admin"
        from swarmly.state.links import PhaseLine, parse_lines
        try:
            if command == "link" and args:
                if args[0].startswith("@"):
                    said = ops.link_agent(topic, args[0].lstrip("@"), by)
                else:
                    said = "; ".join(ops.apply(topic, parse_lines("LINK: " + " ".join(args)),
                                               by)) or "nothing to link"
            elif command == "unlink" and args:
                said = ops.unlink_agent(topic, args[0].lstrip("@"), by)
            elif command == "phase" and len(args) == 2:
                said = "; ".join(ops.apply(topic, [PhaseLine(args[0], args[1].lower())], by))
            else:
                return ("Usage: /link @bot | /link <ref> [as work|reference|tracking], "
                        "/unlink @bot, /phase <ref|L1> <phase>")
        except ValueError as exc:
            return f"Not changed: {exc}"
        if said.startswith("refused"):
            return f"Not changed: {said}"
        if await ops.save(event.thread_id, topic):
            return f"Recorded: {said}"
        return "The store did not take the change; try again in a moment."

    # -- the owed table (phase 3, part 2: shadow) --------------------------------------------
    #: Closed rows stay in the store this long, so a reader sees how a debt ended.
    OWED_KEEP_CLOSED_SECONDS = 24 * 3600.0

    def _own_owed_rows(self, now: float) -> list:
        """This bot's own rows (the ones it wrote): open ones, and closed ones of the last day.
        Each bot writes only its own rows, so its store key has one writer, as on gitbus."""
        store = self.channel.owed_store()
        me = K.norm(self.channel.config.identity.alias)
        rows = [row for row in store.load() if K.norm(row.writer) == me
                and (row.is_open or now - float(row.updated_at) <= self.OWED_KEEP_CLOSED_SECONDS)]
        return sorted((row.to_dict() for row in rows), key=lambda row: str(row.get("kind", ""))
                      + str(row.get("item", "")) + str(row.get("debtor", "")))

    async def sync_owed(self, store: Any, now: float) -> bool:
        """Write ``agent/<alias>/owed`` when this bot's rows changed. True if written."""
        rows = await asyncio.to_thread(self._own_owed_rows, now)
        key = K.agent(store.alias, "owed")
        value = {"rows": rows}
        if store.read(key) == value or (not rows and store.read(key) is None):
            return False
        result = await store.write(key, value)
        return bool(result.applied)

    def owed_from_store(self) -> list:
        """Every bot's rows from the store, folded as the gitbus table is (owed.fold)."""
        from swarmly.telegram.owed import Row, fold
        store = self.store
        if store is None:
            return []
        rows = []
        for key in store.keys("agent/"):
            if key.endswith("/owed"):
                for data in (store.read(key) or {}).get("rows") or []:
                    try:
                        rows.append(Row.from_dict(data))
                    except Exception:                  # noqa: BLE001 - one bad row
                        logger.warning("state: an owed row in %s is unreadable", key)
        return fold(rows)

    def _owed_compare(self) -> str:
        """Shadow: the open rows the store holds, against the ones this bot reads today."""
        try:
            mine = {row.id for row in self.channel.owed_store().load() if row.is_open}
        except Exception:                              # noqa: BLE001 - a comparison only
            return ""
        theirs = {row.id for row in self.owed_from_store() if row.is_open}
        if not mine and not theirs:
            return "owed: no open rows (store and gitbus agree)"
        missing = len(mine - theirs)
        return (f"owed: {len(theirs)} open row(s) in the store, {len(mine)} here"
                + (f" ({missing} not in the store yet)" if missing else " (agree)"))

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
                 f"lease: {lease.holder or 'nobody'} (term {lease.term}){self._lease_compare()}",
                 f"snapshot: message {store.snapshot_message or 'none'}"]
        lines.extend(self.agreement())
        owed = self._owed_compare()
        if owed:
            lines.append(owed)
        if store.log.rejected:
            lines.append(f"{len(store.log.rejected)} write(s) refused since the snapshot")
        return "\n".join(lines)

    def _cached_gitbus(self) -> Any:
        """Today's lease as the transport last read it -- never a git call from here (this
        runs on the event loop)."""
        cached = self.channel.__dict__.get("_cached_lease_state")
        return cached[1] if cached else None

    def _lease_compare(self) -> str:
        """Shadow (phase 3): does the store's lease name the same holder as today's?"""
        if self.lease_mode == "store":
            return " -- the coordinator lease (state.lease: store)"
        gitbus = self._cached_gitbus()
        if gitbus is None:
            return ""
        theirs = str(getattr(gitbus, "holder", "") or "")
        mine = str(self.store.lease().holder or "") if self.store is not None else ""
        same = K.norm(theirs) == K.norm(mine)
        return f"; gitbus lease: {theirs or 'nobody'} ({'agrees' if same else 'DIFFERS'})"

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
