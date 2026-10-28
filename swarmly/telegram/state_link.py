"""The transport's link to the shared state store (topic registry).

``state.backend: telegram`` in ``config.yaml`` turns it on, and the store is then the swarm's
record -- the lease, the owed table, the topics and their links (#472 removed the flags that
used to keep a shadow side of each):

* each bot writes a heartbeat key (``probe/<alias>/beat``) every ``state.heartbeat_minutes``,
  carrying its revision and the state hash it sees;
* the lease holder renews the store's lease and publishes the pinned snapshot;
* ``/state@bot`` shows what the store looks like from that bot, and the hashes of every bot's
  last heartbeat, which must agree.

A failure is logged and swallowed: the store must never stop the transport. A bot with no store
at all keeps the gitbus paths, which is what ``state.backend: off`` means.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

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


def _note_pulse(channel: Any, chat: int, message_id: int, topic: Any) -> None:
    """Hand a Swarm pulse post to the channel's sweep (a channel double may not have one)."""
    note = getattr(channel, "note_pulse_post", None)
    if callable(note):
        note(chat, message_id, topic)


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

    async def _pulse_place(self) -> Any:
        """Where the store posts: Swarm pulse only. With no pulse topic known yet (deleted,
        and not reopened) nothing is posted -- never General (live, 26 Oct)."""
        chat, topic = await self._place()
        if topic is None:
            raise RuntimeError("no Swarm pulse topic yet; the coordinator opens one")
        return chat, topic

    async def _post(self, method: Any, chat: int, topic: int, **kwargs: Any) -> Any:
        try:
            return await self._call(method, chat_id=chat, message_thread_id=topic, **kwargs)
        except Exception as exc:                       # noqa: BLE001 - re-raised
            if "thread not found" in str(exc).lower():
                gone = getattr(self.channel, "pulse_topic_gone", None)
                if callable(gone):
                    gone(chat, topic)
            raise

    async def post_line(self, text: str) -> Posted:
        chat, topic = await self._pulse_place()
        sent = await self._post(self.channel.bot.send_message, chat, topic, text=text,
                                disable_notification=True, disable_web_page_preview=True,
                                parse_mode=None)
        _note_pulse(self.channel, chat, int(sent.message_id), topic)
        return Posted(int(sent.message_id), _date(sent))

    async def post_file(self, name: str, data: bytes, caption: str) -> Posted:
        from aiogram.types import BufferedInputFile
        chat, topic = await self._pulse_place()
        sent = await self._post(self.channel.bot.send_document, chat, topic,
                                document=BufferedInputFile(data, name), caption=caption,
                                disable_notification=True, parse_mode=None)
        # Kept while it is the pinned snapshot; swept once a newer one replaces it.
        _note_pulse(self.channel, chat, int(sent.message_id), topic)
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
        #: #472 (phase 9c): the store's lease, its owed table, the topics' links and the
        #: registry's routing are the only behaviour -- there is no shadow or legacy side
        #: left to name, so these are facts about the backend, not settings.
        self.lease_mode = "store" if self.backend == "telegram" else "off"
        self.owed_mode = "store" if self.backend == "telegram" else "off"
        self._last_github = float("-inf")
        #: When each work link (by its URL) was last asked about: the check's cap goes to the
        #: links asked about longest ago, so no link waits behind 30 that never close (review).
        self._github_checked: Dict[str, float] = {}
        #: Topics deleted here: no notice is routed to them, whatever their record says, until
        #: the lease holder's write marks the record closed for everyone (review).
        self._gone: Set[int] = set()
        #: Topics Telegram refused a post to as closed: no notice is routed to them until a
        #: message is seen there again (a closed topic can be reopened).
        self._closed: Set[int] = set()
        self.store: Optional[TelegramStore] = None
        self.error = ""
        self._last_beat = float("-inf")
        #: When this bot last rebuilt itself from the pin because a peer disagreed (#477).
        self._last_resync = float("-inf")
        self._last_snapshot = float("-inf")
        self._starting: Optional[asyncio.Task] = None
        from swarmly.telegram.registry_shadow import RegistryShadow
        self.registry = RegistryShadow(channel, clock=clock)
        #: Every read-modify-write of a topic record in this process holds this: the beat's
        #: registry pass and a recorded LINK line or /link would otherwise both build on one
        #: read and the later write would drop the other's change (review, phase 5).
        self.topic_lock = asyncio.Lock()

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
    #: How often a diverged bot may rebuild from the pin (#477): a beat is a minute apart, and
    #: the repair is a snapshot read plus a poll -- often enough to heal, never a stampede.
    RESYNC_SECONDS = 600.0

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
            holder_beat = False
            if self.lease_mode == "store" and store.holds_lease():
                holder_beat = True                      # check_lease renews the lease itself
            if holder_beat:
                fresh = sum(1 for o in store.log.ops if o.pos > store.snapshot_pos)
                due = (moment - self._last_snapshot >= SNAPSHOT_EVERY_SECONDS
                       or fresh >= SNAPSHOT_AFTER_OPS)
                if due and await store.publish_snapshot():
                    self._last_snapshot = moment
                    done.append(f"snapshot to p{store.snapshot_pos}")
                elif store.snapshot_message is None \
                        and await store.restore_from_cache():
                    self._last_snapshot = moment
                    done.append("snapshot restored")
            if moment - self._last_beat >= self.beat_seconds:
                # The registry at the state cadence, not every heartbeat; its own failures
                # never stop the probe beat or /state's agreement check (review).
                try:
                    async with self.topic_lock:
                        written = await self.registry.sync(store)
                    if written:
                        done.append(f"registry: {len(written)} key(s)")
                except Exception as exc:               # noqa: BLE001 - shadow: never fatal
                    logger.warning("state: registry sync failed: %s", exc, exc_info=True)
                    done.append("registry failed")
                running = getattr(self, "_github_task", None)
                if store.holds_lease() and (running is None or running.done()) and \
                        moment - self._last_github >= self.GITHUB_EVERY_SECONDS:
                    # In the background: GitHub calls must never hold up the heartbeat (the
                    # silent-turn check runs right after this beat).
                    self._last_github = moment
                    self._github_task = asyncio.get_running_loop().create_task(
                        self._reconcile_logged(store))
                    done.append("github check started")
                try:
                    if await self.sync_stats(store):
                        done.append("stats")
                except Exception as exc:               # noqa: BLE001 - shadow: never fatal
                    logger.warning("state: stats sync failed: %s", exc, exc_info=True)
                rev = str(getattr(self.channel, "_running_revision", "") or "")
                await store.write(K.probe(store.alias, "beat"),
                                  {"at": round(moment), "rev": rev[:12],
                                   "pos": store.log.last_pos, "hash": store.state_hash()})
                self._last_beat = moment
                done.append("beat")
                healed = await self._resync_if_diverged(store, moment)
                if healed:
                    done.append(healed)
            # The table itself: published every heartbeat, as gitbus pushed each row at once
            # -- a debt must not wait out the state cadence to be seen.
            await self._owed_beat(store, moment, done)
            self.error = ""
        except Exception as exc:                       # noqa: BLE001 - shadow: never fatal
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("state: beat failed: %s", self.error)
            return "failed"
        return ", ".join(done) or "nothing to do"

    async def _resync_if_diverged(self, store: Any, moment: float) -> str:
        """Rebuild from the pin when another bot's beat says our state is not theirs (#477).

        A bot that missed a STATE line -- down while it was posted -- keeps a fold that is
        wrong for ever: a write it rejected stays rejected, and nothing in the log says so. The
        only repair is the snapshot, which is the holder's fold up to a position. Divergence
        *above* the pin cannot be repaired that way, so it is reported, not hidden.
        """
        try:
            verdicts = self.agreement()
        except Exception:                              # noqa: BLE001 - a check, not a beat
            return ""
        differs = [line for line in verdicts if " DIFFERS " in line]
        if not differs:
            return ""
        if moment - self._last_resync < self.RESYNC_SECONDS:
            return "diverged (a resync is cooling down)"
        self._last_resync = moment
        before = store.log.last_pos
        changed = await store.resync()
        logger.warning("state: state hash diverged from a peer (%s); rebuilt from the pin "
                       "at p%s (was p%s)", "; ".join(differs), store.log.last_pos, before)
        if store.log.base_pos < before:
            return ("diverged: rebuilt from the pin at p%s (a divergence above it is not "
                    "repairable here)" % store.log.base_pos)
        return f"diverged: rebuilt from the pin at p{store.log.base_pos}" if changed             else "diverged: the pin did not change this bot's state"

    # -- links and work items (phase 5) -------------------------------------------------------
    def _ops(self) -> Any:
        from swarmly.telegram.registry_ops import RegistryOps
        return RegistryOps(self.store, home=int(self.channel.config.identity.home_channel_id
                                                or 0),
                           repo=self.registry._repo(), clock=self.clock)

    def note_reply(self, chat_id: int, thread_id: Optional[int], text: str,
                   named: Optional[list] = None) -> None:
        """The coordinator's reply in a topic: record its LINK/ASSIGN/PHASE lines (the lease
        holder only -- it alone writes topic records). The peers it names (``named``: their
        Telegram handles) are linked too: the coordinator's mention is how an agent joins a
        topic."""
        from swarmly.state.links import parse_lines
        lines = parse_lines(text) if text else []
        handles = list(named or [])
        self._record_in_topic(chat_id, thread_id, lines, handles,
                              K.norm(self.channel.config.identity.alias))

    def note_admin_mentions(self, chat_id: int, thread_id: Optional[int],
                            named: list, author_id: Optional[int]) -> None:
        """The admin named peers (their handles) in a topic: the holder links them (the
        admin's mention links an agent, as the coordinator's does)."""
        if not named:
            return
        by = f"admin:{author_id}" if author_id is not None else "admin"
        self._record_in_topic(chat_id, thread_id, [], list(named), by)

    @staticmethod
    def _bare(handle: Any) -> str:
        return str(handle or "").strip().lstrip("@").lower()

    def agent_for_handle(self, handle: str) -> Optional[str]:
        """The alias an agent publishes in its profile, found by its Telegram handle; else
        this bot's peers alias for it. Each bot names its peers its own way, so a link is
        keyed by the agent's own alias (the profile's), never a peer's spelling (review)."""
        bare = self._bare(handle)
        store = self.store
        if store is not None and bare:
            for key in store.keys("agent/"):
                if key.endswith("/profile"):
                    profile = store.read(key) or {}
                    if self._bare(profile.get("handle")) == bare and profile.get("alias"):
                        return str(profile["alias"])
        return self.channel.config.peer_alias_for(bare) if bare else None

    def _handles_of(self, alias: str) -> Set[str]:
        """The names an agent goes by, for comparing: its alias and its handle."""
        out = {K.norm(alias)}
        store = self.store
        profile = (store.read(K.agent(alias, "profile")) if store is not None else None) or {}
        if profile.get("handle"):
            out.add(self._bare(profile.get("handle")))
        return out

    def _record_in_topic(self, chat_id: int, thread_id: Optional[int], lines: list,
                         handles: list, by: str) -> None:
        store = self.store
        if store is None or not store.ready or thread_id is None or not (lines or handles):
            return
        if int(chat_id) != int(self.channel.config.identity.home_channel_id or 0):
            return
        if not store.holds_lease():
            return
        agents: List[str] = []
        if handles:
            data = store.read(K.topic(thread_id))
            have: Set[str] = set()
            for alias in (data or {}).get("agents") or {}:
                have |= self._handles_of(alias)
            for handle in handles:
                alias = self.agent_for_handle(handle)
                if alias and K.norm(alias) not in have and self._bare(handle) not in have \
                        and alias not in agents:
                    agents.append(alias)
            if not agents and not lines:
                return                                 # all linked already: no write
        name = self.channel.topics.name(chat_id, thread_id) or ""

        async def record() -> None:
            try:
                async with self.topic_lock:
                    ops = self._ops()
                    await store.refresh()
                    topic = ops.topic(thread_id, name)
                    done = ops.apply(topic, lines, by)
                    done += [ops.link_agent(topic, alias, by) for alias in agents]
                    saved = await ops.save(thread_id, topic)
                if saved:
                    logger.info("registry: topic %s: %s", thread_id, "; ".join(done))
            except Exception:                          # noqa: BLE001 - shadow: never fatal
                logger.warning("registry: the reply's lines were not recorded", exc_info=True)
        task = asyncio.get_running_loop().create_task(record())
        self._tasks = getattr(self, "_tasks", set())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def route_facts(self, chat_id: int, thread_id: Optional[int]) -> Optional[Any]:
        """The registry's facts for routing a message in this topic, or None when routing
        stays with the mention rules: the store is not ready, there is no coordinator (lease
        lapsed), the topic's record is closed, or it is Swarm pulse. A topic with no record
        yet routes as a new one: the coordinator is linked.

        Agents are named by alias *and* handle in ``linked``, so a peer's own spelling of an
        alias never decides a match (review)."""
        from swarmly.telegram.routing import Facts
        store = self.store
        if store is None or not store.ready:
            return None
        if thread_id is None or int(chat_id) != int(
                self.channel.config.identity.home_channel_id or 0):
            return None
        pulse = getattr(self.channel, "_pulse_topic", None)
        if callable(pulse) and pulse() == thread_id:
            return None                        # transport-only: nothing there is routed
        holder = store.holder()
        data = store.read(K.topic(thread_id))
        if not holder or (data and data.get("status") == "closed"):
            return None
        # A topic with no record yet is a new one, and the coordinator's (live, 25 Oct: the
        # admin's first message in a fresh topic reached nobody). The holder's beat writes
        # its record; until then only the coordinator is linked.
        data = data or {"agents": {holder: {}}}
        unlinked = self._unlinked_since_last_turn(data)
        linked: Set[str] = set()
        for alias in data.get("agents") or {}:
            linked |= self._handles_of(alias)
        holder_handle = self._bare((store.read(K.agent(holder, "profile")) or {}).get("handle"))
        identity = self.channel.config.identity
        return Facts(me=K.norm(identity.alias), coordinator=K.norm(holder),
                     linked=frozenset(linked), coordinator_handle=holder_handle or None,
                     me_handle=self._bare(identity.name) or None)

    @staticmethod
    def _unlinked_since_last_turn(data: dict) -> Set[str]:
        """Agents whose latest role change in the topic's history is an /unlink."""
        latest: Dict[str, Any] = {}
        for change in data.get("history") or []:
            if change.get("field") == "role" and change.get("agent"):
                latest[K.norm(change["agent"])] = change.get("to")
        return {alias for alias, to in latest.items() if to == "unlinked"}

    #: How often the lease holder asks GitHub where its linked items stand.
    GITHUB_EVERY_SECONDS = 900.0

    def _topic_records(self) -> list:
        """(topic id, record) for every topic in the store."""
        out = []
        store = self.store
        if store is None:
            return out
        for key in store.keys("topic/"):
            place = K.topic_of(key)
            if place is not None and place[1] is None:
                out.append((place[0], store.read(key) or {}))
        return out

    def topic_for_refs(self, refs: list, repo: Optional[str] = None) -> Optional[tuple]:
        """The open topic whose explicit links name one of ``refs`` (``#12``) in ``repo``
        (default: this swarm's project repo); legacy guesses do not count. A work item first,
        then a tracking link, then a reference; within each, the link added most recently
        (the admin's ``/topic #N`` re-adds it), then the newer topic. Matched on the link's URL,
        not its title (a titled link keeps routing, and #40 in another repo is not this repo's
        #40). A topic deleted here, or found closed, is never the answer."""
        import re
        if self.store is None or not refs:
            return None
        repo = (repo or self.registry._repo() or "").lower()
        wanted = {str(ref).lstrip("#") for ref in refs}
        gone = self._gone | self._closed
        rank = {"work": 0, "tracking": 1, "reference": 2}
        best = None
        for topic_id, data in self._topic_records():
            if data.get("status") == "closed" or int(topic_id) in gone:
                continue
            for link in data.get("links") or []:
                if link.get("added_by") == "legacy" or link.get("provider") != "github":
                    continue
                found = re.match(r"https://github\.com/([^/]+/[^/]+)/(?:issues|pull)/(\d+)",
                                 str(link.get("ref") or ""))
                if not found or found.group(1).lower() != repo:
                    continue
                if found.group(2) in wanted and link.get("status") != "closed":
                    score = rank.get(link.get("role"), 3)
                    recent = (str(link.get("added_at") or ""), int(topic_id))
                    if best is None or score < best[0] or \
                            (score == best[0] and recent > best[1]):
                        best = (score, recent, topic_id)
        if best is None:
            return None
        return int(self.channel.config.identity.home_channel_id or 0), int(best[2])

    def topic_gone(self, thread_id: int, *, deleted: bool) -> None:
        """A topic was deleted or closed: this bot routes nothing to it. A deleted one
        (``deleted``) is gone for good, and the lease holder closes its record, so no bot's
        notice follows its links there. A closed one can be reopened: it is skipped until a
        message is seen there again (:meth:`topic_seen`), and its record and its items' GitHub
        check stay as they are."""
        thread_id = int(thread_id)
        if not deleted:
            self._closed.add(thread_id)
            return
        self._gone.add(thread_id)
        self._closed.discard(thread_id)
        store = self.store
        if store is None or not store.ready or not store.holds_lease():
            return
        if (store.read(K.topic(thread_id)) or {}).get("status") != "open":
            return

        async def record() -> None:
            from swarmly.state.records import Change, Topic, now_iso
            try:
                async with self.topic_lock:
                    await store.refresh()
                    current = store.read(K.topic(thread_id))
                    if not current or current.get("status") != "open":
                        return
                    topic = Topic.from_dict(current)
                    stamp = now_iso(self.clock)
                    topic.history.append(Change(at=stamp, field="status", old="open",
                                                to="closed", by="transport"))
                    topic.status, topic.closed_at = "closed", stamp
                    if await self._ops().save(thread_id, topic):
                        logger.info("registry: topic %s is gone; its record is closed",
                                    thread_id)
            except Exception:                          # noqa: BLE001 - shadow: never fatal
                logger.warning("registry: a gone topic's record was not closed",
                               exc_info=True)
        self._spawn(record())

    def topic_seen(self, thread_id: int) -> None:
        """A message was seen in a topic: it is open again, if a post had found it closed."""
        self._closed.discard(int(thread_id))

    def note_pin(self, chat_id: int, thread_id: Optional[int], refs: list,
                 author_id: Optional[int]) -> None:
        """The admin's ``/topic #N`` (the lease holder): an explicit work link to each item in
        this topic, set by the admin, so registry routing sends its notices here too."""
        store = self.store
        if store is None or not store.ready or thread_id is None or not refs \
                or not store.holds_lease():
            return
        if int(chat_id) != int(self.channel.config.identity.home_channel_id or 0):
            return
        from swarmly.state.links import parse_lines
        lines = parse_lines("\n".join(f"LINK: {ref} as work" for ref in refs))
        by = f"admin:{author_id}" if author_id is not None else "admin"
        name = self.channel.topics.name(chat_id, thread_id) or ""

        async def record() -> None:
            from swarmly.state.records import now_iso
            try:
                async with self.topic_lock:
                    ops = self._ops()
                    await store.refresh()
                    topic = ops.topic(thread_id, name)
                    done = ops.apply(topic, lines, by)
                    stamp = now_iso(self.clock)
                    for ref in refs:
                        link = ops.find(topic, ref)
                        if link is not None:            # the admin's own link: it wins routing
                            link.added_by, link.added_at = by, stamp
                            link.updated_by, link.updated_at = by, stamp
                    saved = await ops.save(thread_id, topic)
                if saved:
                    logger.info("registry: /topic in %s: %s", thread_id, "; ".join(done))
            except Exception:                          # noqa: BLE001 - shadow: never fatal
                logger.warning("registry: /topic was not recorded", exc_info=True)
        self._spawn(record())

    def _spawn(self, work: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(work)
        except RuntimeError:                           # no loop: a test calling it directly
            work.close()
            logger.debug("registry: no event loop, so a topic record change was not made")
            return
        self._tasks = getattr(self, "_tasks", set())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def registry_facts(self, chat_id: int, thread_id: Optional[int]) -> str:
        """What the registry records for a topic, as a few lines for a new session's first
        prompt; the facts win over a handover's own claims. "" when nothing is recorded."""
        store = self.store
        if store is None or not store.ready or thread_id is None:
            return ""
        data = store.read(K.topic(thread_id)) or {}
        items = [self.registry._link_text(link) for link in data.get("links") or []
                 if link.get("added_by") != "legacy"]
        if not items:
            return ""
        return ("[Recorded by the transport for this topic -- these facts win over anything a "
                "handover says: " + "; ".join(items[:12]) + ".]")

    def registry_note(self, chat_id: int, thread_id: Optional[int]) -> str:
        """The coordinator's per-turn hint (the store lease holder, in a topic): how to record
        this topic's work, and what is recorded already. Empty for everyone else."""
        store = self.store
        if store is None or not store.ready or thread_id is None or not store.holds_lease():
            return ""
        if int(chat_id) != int(self.channel.config.identity.home_channel_id or 0):
            return ""
        data = store.read(K.topic(thread_id)) or {}
        items = [self.registry._link_text(link) for link in data.get("links") or []
                 if link.get("added_by") != "legacy"]
        known = ("Recorded here: " + "; ".join(items[:8]) + ".") if items else \
            "Nothing is recorded for this topic yet."
        return ("\n\n[Topic registry, from the transport: record this topic's work with lines "
                "of their own -- `LINK: <#N|url> [as work|tracking|reference] [— title]`, "
                "`ASSIGN: @<bot> <#N|L1> as <role>`, `PHASE: <#N|L1> <research|design|"
                "development|review|qa|done|blocked>`. " + known + "]")

    #: At most this many GitHub calls per check.
    GITHUB_MAX_ITEMS = 30

    async def _reconcile_logged(self, store: Any) -> None:
        try:
            moved = await self.reconcile_github(store)
            if moved:
                logger.info("state: github moved %d work item(s)", moved)
        except Exception:                              # noqa: BLE001 - shadow: never fatal
            logger.warning("state: github reconcile failed", exc_info=True)

    async def reconcile_github(self, store: Any) -> int:
        """Move linked GitHub work items by where GitHub says they stand (the lease holder):
        merged or a closed issue -> done and closed; a closed, unmerged PR -> closed; an open
        PR still before review -> review. Returns how many items moved.

        At most :attr:`GITHUB_MAX_ITEMS` calls a check, spent on the links asked about longest
        ago (never asked first): items that stay open for weeks do not keep the rest from ever
        being checked (review). A topic record that cannot be read is skipped, not fatal."""
        import re
        from swarmly.github.client import GitHubClient
        from swarmly.state.records import Change, RecordError, Topic, now_iso
        github = getattr(self.channel.config, "github", None)
        client = GitHubClient(token_env=getattr(github, "token_env", None) or "GH_TOKEN")
        moved = 0
        # One call per item, whichever topics link it: (repo, number) and its (topic, link)s.
        wanted: Dict[str, Tuple[str, int, List[Tuple[int, Any]]]] = {}
        for topic_id, data in self._topic_records():
            if data.get("status") == "closed" or int(topic_id) in self._gone:
                continue
            for link in data.get("links") or []:
                if link.get("provider") != "github" or link.get("role") != "work" \
                        or link.get("status") != "open":
                    continue
                ref = str(link.get("ref") or "")
                found = re.match(r"https://github\.com/([^/]+/[^/]+)/(?:issues|pull)/(\d+)",
                                 ref)
                if found:
                    wanted.setdefault(ref, (found.group(1), int(found.group(2)), []))[2].append(
                        (int(topic_id), link.get("id")))
        order = sorted(wanted, key=lambda ref: (self._github_checked.get(ref, float("-inf")),
                                                ref))
        by_topic: Dict[int, Dict[Any, Any]] = {}
        for ref in order[:self.GITHUB_MAX_ITEMS]:
            repo, number, places = wanted[ref]
            self._github_checked[ref] = self.clock()
            answer = await asyncio.to_thread(client.ref_kind_state, repo, number)
            for topic_id, link_id in places:
                by_topic.setdefault(topic_id, {})[link_id] = answer
        for topic_id, states in sorted(by_topic.items()):
            async with self.topic_lock:
                await store.refresh()
                current = store.read(K.topic(topic_id))
                if not current:
                    continue
                try:
                    topic = Topic.from_dict(current)
                except RecordError as exc:
                    logger.warning("state: github check skipped topic %s: %s", topic_id, exc)
                    continue
                stamp = now_iso(self.clock)
                changed = relabels = 0
                for link in topic.links:
                    answer = states.get(link.id)
                    if answer is None or link.status != "open":
                        continue
                    state, is_pr = answer
                    relabelled = bool(is_pr and link.kind != "pr")
                    if relabelled:
                        link.kind = "pr"                # linked by its short ref: it is a PR
                    phase, status = link.phase, link.status
                    if state == "merged" or (state == "closed" and not is_pr):
                        phase, status = "done", "closed"
                    elif state == "closed":
                        status = "closed"               # a PR closed without a merge
                    elif state == "open" and is_pr and \
                            link.phase in ("research", "design", "development"):
                        phase = "review"
                    if (phase, status) == (link.phase, link.status):
                        if relabelled:
                            link.updated_by, link.updated_at = "github", stamp
                            relabels += 1
                        continue
                    if phase != link.phase:
                        topic.history.append(Change(at=stamp, field="phase", old=link.phase,
                                                    to=phase, by="github", link=link.id))
                    if status != link.status:
                        topic.history.append(Change(at=stamp, field="status", old=link.status,
                                                    to=status, by="github", link=link.id))
                    if phase != link.phase:
                        link.phase_at = stamp
                    link.phase, link.status = phase, status
                    link.updated_by, link.updated_at = "github", stamp
                    changed += 1
                if changed or relabels:
                    result = await store.write(K.topic(topic_id),
                                               Topic.from_dict(topic.to_dict()).to_dict())
                    if result.applied:
                        moved += changed
        return moved

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
            # /link #N still pins the item to this topic, as /topic does (channel.py).
            return ("The state store is not running here (state.backend), so the topic "
                    "registry keeps nothing.")
        if not holder:
            return "Only the bot holding the store's lease writes topic records; ask it."
        if event.thread_id is None:
            return f"/{command} works inside a topic."
        args = (event.text or "").split()[1:]            # the bot named in /link @bot stays
        async with self.topic_lock:
            return await self._registry_change(event, command, args, store)

    async def _registry_change(self, event: Any, command: str, args: list, store: Any) -> str:
        await store.refresh()
        ops = self._ops()
        topic = ops.topic(event.thread_id,
                          self.channel.topics.name(event.chat_id, event.thread_id) or "")
        by = f"admin:{event.author_id}" if event.author_id is not None else "admin"
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

    # -- topics /cleanup keeps ----------------------------------------------------------
    def kept_topics(self) -> Set[int]:
        """The topics marked ``keep`` in the registry (``/cleanup keep``)."""
        out: Set[int] = set()
        for topic_id, record in self._topic_records():
            if isinstance(record, dict) and record.get("keep"):
                out.add(int(topic_id))
        return out

    async def set_topic_keep(self, chat_id: int, thread_id: int, keep: bool, by: str) -> str:
        """Mark (or unmark) a topic as one /cleanup never deletes. The lease holder writes."""
        store = self.store
        if store is None or not store.ready:
            return "the state store is not running here, so nothing can be kept"
        if not store.holds_lease():
            return "only the coordinator (the lease holder) writes topic records"
        async with self.topic_lock:
            ops = self._ops()
            await store.refresh()
            topic = ops.topic(thread_id, self.channel.topics.name(chat_id, thread_id) or "")
            if bool(topic.keep) == bool(keep):
                return "already kept" if keep else "was not kept"
            # No history entry: an older bot validates history fields and would read the
            # whole record as broken. The log line keeps who did it.
            topic.keep = bool(keep)
            logger.info("registry: topic %s keep=%s by %s", thread_id, keep, by)
            if not await ops.save(thread_id, topic):
                return "the store did not take the change; try again in a moment"
        return "kept: /cleanup never deletes this topic" if keep else \
            "no longer kept: /cleanup may delete it once it is finished"

    # -- the pinned dashboard card ------------------------------------------------------
    def dashboard_cards(self) -> Optional[dict]:
        """alias -> the dashboard message each bot last created (``agent/<a>/dashboard``),
        or None while the store is not ready (the card waits rather than pin another)."""
        store = self.store
        if store is None or not store.ready:
            return None
        out = {}
        for key in store.keys("agent/"):
            if key.endswith("/dashboard"):
                value = store.read(key)
                mid = value.get("message_id") if isinstance(value, dict) else None
                if isinstance(mid, int):
                    out[key.split("/")[1]] = mid
        return out

    async def record_dashboard_card(self, message_id: int) -> None:
        store = self.store
        if store is None or not store.ready:
            return
        await store.write(K.agent(store.alias, "dashboard"), {"message_id": int(message_id)})

    # -- agent stats and drift (phase 8) -------------------------------------------------------
    def agent_stats(self) -> dict:
        """This bot's drift, as the store keeps it: score per live session, resets in the last
        day, the latest violations. Its own key, so the coordinator and the dashboard can read
        every bot's (docs/design/topic-registry.md, "Agent stats and drift")."""
        ledger = getattr(self.channel, "drift", None)
        if ledger is None:
            return {}
        ledger.fold_inbox()
        decision = ledger.decide()
        recent = [{"kind": r.kind, "severity": r.severity, "topic": r.topic,
                   "at": round(float(r.at))} for r in ledger.rows if not r.reset][-20:]
        return {"sessions": {t: list(v) for t, v in ledger.sessions().items()},
                "resets_24h": int(decision.resets_today), "violations": recent}

    async def sync_stats(self, store: Any) -> bool:
        """Write ``agent/<alias>/stats`` when it changed. True if written."""
        value = self.agent_stats()             # on the loop: the ledger has one writer
        if not value:
            return False
        key = K.agent(store.alias, "stats")
        if store.read(key) == value or (store.read(key) is None and not value["violations"]):
            return False
        return bool((await store.write(key, value)).applied)

    # -- the owed table (phase 3: shadow, then the store) -------------------------------------
    #: Closed rows stay in the store this long, so a reader sees how a debt ended -- and longer
    #: while any bot's key still holds the same row open (closed beats open only while the
    #: closed copy is there to be read; review).
    OWED_KEEP_CLOSED_SECONDS = 24 * 3600.0

    def _own_owed_rows(self, now: float, published: Optional[list] = None,
                       open_elsewhere: Optional[set] = None) -> list:
        """The rows this bot publishes under ``agent/<alias>/owed``.

        Every row it *holds* -- its state directory, whoever wrote the row, so a close
        or a stall it records on a peer's row reaches everyone -- folded with what it published
        before, so a lost state directory never empties the table (review). Closed rows go
        after a day, unless another key still holds them open. #472: the store is the only
        table, so there is no mirror branch left."""
        from swarmly.telegram.owed import Row, fold
        store = self.channel.owed_store()
        before = []
        for data in published or []:
            try:
                before.append(Row.from_dict(data))
            except Exception:                      # noqa: BLE001 - one bad row
                logger.warning("state: an owed row this bot published is unreadable")
        held = store.held()
        ids = {row.id for row in held}
        # The directory is the truth for the ids it holds (a re-opened id replaces its
        # closed copy); what was published before only fills in ids it lost (review).
        rows = [row for row in fold(held + [r for r in before if r.id not in ids])
                if row.is_open
                or now - float(row.updated_at) <= self.OWED_KEEP_CLOSED_SECONDS
                or row.id in (open_elsewhere or set())]
        return sorted((row.to_dict() for row in rows), key=lambda row: str(row.get("kind", ""))
                      + str(row.get("item", "")) + str(row.get("debtor", ""))
                      + str(row.get("head", "")) + str(row.get("key_extra", "")))

    async def _owed_beat(self, store: Any, moment: float, done: list) -> None:
        try:
            if await self.sync_owed(store, moment):
                done.append("owed")
        except Exception as exc:                       # noqa: BLE001 - never fatal
            logger.warning("state: owed sync failed: %s", exc, exc_info=True)
            done.append("owed failed")

    @staticmethod
    def _owed_dicts(store: Any) -> Dict[str, list]:
        """key -> its rows as plain dicts, for every ``agent/<a>/owed`` key. On the loop. A
        value of the wrong shape is skipped, so one bad key never hides the table (review)."""
        out: Dict[str, list] = {}
        for key in list(store.keys("agent/")):
            if not key.endswith("/owed"):
                continue
            value = store.read(key)
            rows = value.get("rows") if isinstance(value, dict) else None
            if not isinstance(rows, list):
                if value is not None:
                    logger.warning("state: %s is not an owed table; skipping it", key)
                continue
            out[key] = [dict(data) for data in rows if isinstance(data, dict)]
        return out

    @staticmethod
    def _rows_of(tables: Dict[str, list], keep: Any) -> list:
        """Fresh Row objects for the tables ``keep(key)`` selects (a reader may change them)."""
        from swarmly.telegram.owed import Row
        rows = []
        for key, items in tables.items():
            if not keep(key):
                continue
            for data in items:
                try:
                    rows.append(Row.from_dict(data))
                except Exception:                      # noqa: BLE001 - one bad row
                    logger.warning("state: an owed row in %s is unreadable", key)
        return rows

    def _owed_tables(self) -> Dict[str, list]:
        """The store's owed tables: read fresh on the event loop; a reader on a worker thread
        (``asyncio.to_thread``) gets the copy the last read on the loop took, since the store's
        log is only ever changed there (review: iterating it from a thread raced its updates).
        Raises when the store is not ready, so the reader reports "cannot read" instead of
        "nothing is owed"."""
        store = self.store
        if store is None or not store.ready:
            raise RuntimeError("the state store is not ready")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            snapshot = getattr(self, "_owed_snapshot", None)
            if snapshot is None:
                raise RuntimeError("the store's owed rows are not read yet") from None
            return snapshot
        self._owed_snapshot = self._owed_dicts(store)
        return self._owed_snapshot

    def shared_owed_rows(self) -> list:
        """The other bots' rows, as the store holds them (``state.owed: store``)."""
        mine = K.agent(self.channel.config.identity.alias, "owed")
        return self._rows_of(self._owed_tables(), lambda key: key != mine)

    def published_owed_rows(self) -> list:
        """The rows this bot itself published (read for ids its directory lost)."""
        mine = K.agent(self.channel.config.identity.alias, "owed")
        return self._rows_of(self._owed_tables(), lambda key: key == mine)

    async def sync_owed(self, store: Any, now: float) -> bool:
        """Write ``agent/<alias>/owed`` when this bot's rows changed. True if written."""
        key = K.agent(store.alias, "owed")
        own = store.read(key)
        # A malformed own key is replaced, never a reason to stop publishing (#298 review).
        published = [r for r in (own.get("rows") if isinstance(own, dict) else None) or []
                     if isinstance(r, dict)]
        self._owed_snapshot = self._owed_dicts(store)
        open_elsewhere = {row.id for row in self._rows_of(self._owed_snapshot,
                                                          lambda other: other != key)
                          if row.is_open}
        rows = await asyncio.to_thread(self._own_owed_rows, now, published, open_elsewhere)
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
        """The open rows the store holds: the only table there is since #472."""
        try:
            rows = [row for row in self.channel.owed_store().load() if row.is_open]
        except Exception:                              # noqa: BLE001 - a summary only
            return ""
        return f"owed: the store holds the table ({len(rows)} open row(s))"

    # -- /state ----------------------------------------------------------------------------
    def describe(self) -> str:
        if not self.enabled:
            return "State store: off (state.backend is not set to telegram)."
        store = self.store
        if store is None:
            return "State store: not started."
        lease = store.lease()
        # #475: no mode in the header -- `state.mode` has one value and nothing runs beside the
        # store any more -- and the running rev, so a deploy can be verified from /state (a bot
        # cannot read its own online notice from its workspace).
        lines = [f"State store ({self.backend}): "
                 + ("ready" if store.ready else "not ready")
                 + (f" -- last error: {self.error}" if self.error else ""),
                 f"rev: {self.running_revision() or 'unknown'}",
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

    def running_revision(self) -> str:
        """The revision this transport runs -- the same one its online notice carries (#475).

        Read from the channel, which resolves it once at start; a channel without one answers
        "" and ``/state`` says "unknown" rather than inventing a rev.
        """
        return str(getattr(self.channel, "_running_revision", "") or "")

    def _lease_compare(self) -> str:
        """Which lease this is: the store's -- #472 removed the gitbus side."""
        return " -- the coordinator lease"

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
            # "- name verb ...", never "name: ...": an alias opening a line followed by a colon
            # reads as an address, and the transport would @mention every bot (live, 24 Oct).
            if mine is None:
                verdicts.append(f"- {who} last beat before this bot's snapshot (not comparable)")
            else:
                verdicts.append(f"- {who} {'agrees' if mine == theirs else 'DIFFERS'} at "
                                f"p{value.get('pos')} ({theirs})")
        return verdicts
