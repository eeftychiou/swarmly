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
        #: Phase 5: "registry" lets GitHub notices follow the topics' explicit links.
        self.links_mode = str(getattr(settings, "links", "legacy") or "legacy").lower() \
            if self.backend == "telegram" else "legacy"
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
                async with self.topic_lock:
                    ops = self._ops()
                    await store.refresh()
                    topic = ops.topic(thread_id, name)
                    done = ops.apply(topic, lines, K.norm(self.channel.config.identity.alias))
                    saved = await ops.save(thread_id, topic)
                if saved:
                    logger.info("registry: topic %s: %s", thread_id, "; ".join(done))
            except Exception:                          # noqa: BLE001 - shadow: never fatal
                logger.warning("registry: the reply's lines were not recorded", exc_info=True)
        task = asyncio.get_running_loop().create_task(record())
        self._tasks = getattr(self, "_tasks", set())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            return "The state store is not running here (state.backend), so nothing is kept."
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
            # "- name verb ...", never "name: ...": an alias opening a line followed by a colon
            # reads as an address, and the transport would @mention every bot (live, 24 Oct).
            if mine is None:
                verdicts.append(f"- {who} last beat before this bot's snapshot (not comparable)")
            else:
                verdicts.append(f"- {who} {'agrees' if mine == theirs else 'DIFFERS'} at "
                                f"p{value.get('pos')} ({theirs})")
        return verdicts
