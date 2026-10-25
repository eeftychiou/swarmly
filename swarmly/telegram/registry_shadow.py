"""The topic registry, filled in shadow from today's logic (topic registry, phase 2).

Nothing reads the registry yet: this proves the data. Each beat of the state store
(``state_link.py``) calls :meth:`RegistryShadow.sync`:

* **The lease holder** writes ``topic/<id>`` for every forum topic its transport knows: the
  title, the coordinator's binding (runtime, model, effort, where they come from), and the
  links today's ref-learning found -- marked ``added_by: legacy`` so they are easy to tell
  from the explicit links of phase 5.
* **Every bot** writes its own ``topic/<id>/agent/<alias>`` (the CLI session its adapter keeps
  for that topic, the turns since that session began, how often it was replaced) and its
  ``agent/<alias>/profile`` from ``config.yaml``.

A key is written only when its content changed, and ``last_activity`` only moves in steps of
``ACTIVITY_STEP_SECONDS``, so a quiet swarm writes nothing at all. ``/topics@bot`` shows the
registry as this bot sees it.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from swarmly.state import keys as K
from swarmly.state.records import (AgentInTopic, Binding, Link, Profile, RecordError,
                                   Topic, now_iso)

if TYPE_CHECKING:  # pragma: no cover
    from swarmly.state.store import StateStore
    from swarmly.telegram.channel import AgentChannel

logger = logging.getLogger("swarmly.telegram.registry")

#: ``last_activity`` is a coarse field: a topic busy all afternoon is one write an hour.
ACTIVITY_STEP_SECONDS = 3600.0
#: Topics quiet for longer than this are not written (they are history, not the registry).
ACTIVE_DAYS = 14
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def session_of(adapter: Any, context_id: Optional[str]) -> Optional[str]:
    """The CLI session ``adapter`` keeps for a topic, whichever runtime it is."""
    found = session_and_kind(adapter, context_id)
    return found[0] if found else None


def session_and_kind(adapter: Any, context_id: Optional[str]) -> Optional[tuple]:
    """(session id, derived) -- derived ids (claude_thread) exist for every topic, whether the
    bot ever ran a turn there or not."""
    sid = _session(adapter, context_id)
    if sid is None:
        return None
    return sid, bool(getattr(adapter, "KEEPS_TOPIC_SESSION", False)
                     and hasattr(adapter, "derived_session_id")
                     and not callable(getattr(adapter, "thread_for", None))
                     and not callable(getattr(adapter, "conversation_for", None)))


def _session(adapter: Any, context_id: Optional[str]) -> Optional[str]:
    if not context_id or adapter is None:
        return None
    current = getattr(adapter, "topic_session", None)
    if callable(current) and getattr(adapter, "KEEPS_TOPIC_SESSION", False):
        try:
            return str(current(context_id))           # claude_thread: with its generation
        except Exception:                              # noqa: BLE001
            pass
    for name in ("thread_for", "conversation_for"):
        method = getattr(adapter, name, None)
        if callable(method):
            try:
                found = method(context_id)
            except Exception:                          # noqa: BLE001
                found = None
            if found:
                return str(found)
    reader = getattr(adapter, "session_for", None)
    if callable(reader) and getattr(adapter, "TYPE", "") == "reasonix":
        found = reader(context_id)
        return str(found) if found else None
    if getattr(adapter, "KEEPS_TOPIC_SESSION", False) and hasattr(adapter, "derived_session_id"):
        return adapter.derived_session_id(adapter.alias, adapter.settings.cwd, context_id)
    return None


class RegistryShadow:
    def __init__(self, channel: "AgentChannel", *, clock: Any = time.time) -> None:
        self.channel = channel
        self.clock = clock
        #: context id -> turns run since the last write that recorded them. The total lives
        #: in the store (so a restart keeps it); this is only what is not written yet.
        self.turns: Dict[str, int] = {}

    # -- small readers ---------------------------------------------------------------------
    @property
    def alias(self) -> str:
        return K.norm(self.channel.config.identity.alias)

    def _home(self) -> int:
        return int(self.channel.config.identity.home_channel_id or 0)

    def _repo(self) -> Optional[str]:
        for project in (getattr(self.channel.config, "projects", None) or {}).values():
            repo = getattr(project, "repo", None)
            if repo:
                return str(repo)
        return None

    def _topics(self) -> List[tuple]:
        """(thread id, name, context id, last seen) of the forum topics this bot knows."""
        topics = self.channel.topics
        out = []
        horizon = self.clock() - ACTIVE_DAYS * 86400
        for chat, thread, name in topics.entries():
            if chat != self._home() or thread is None:
                continue
            seen = topics.last_seen(chat, thread)
            if seen is None or seen < horizon:
                continue                                # never active, or quiet too long
            out.append((thread, name, topics.context(chat, thread), seen))
        return out

    def _choice(self, table: str, context: Optional[str]) -> Optional[str]:
        return (getattr(self.channel, table, {}) or {}).get(context or "general")

    def binding(self, context: Optional[str], role: str) -> Binding:
        adapter = self.channel.config.adapter
        model = self._choice("_models", context)
        effort = self._choice("_efforts", context)
        source = "topic" if (model or effort) else "profile"
        effort = effort or getattr(adapter, "effort", None) or "default"
        return Binding(role=role, runtime=str(adapter.type),
                       model=str(model or getattr(adapter, "model", None) or "default"),
                       effort=effort if effort in EFFORTS else "default",
                       linked_by="transport", linked_at=now_iso(self.clock),
                       model_source=source)

    # -- the beat --------------------------------------------------------------------------
    async def sync(self, store: "StateStore") -> List[str]:
        """Write what changed, in one batch (§1b); returns the keys written."""
        await store.refresh()
        changes: Dict[str, Dict[str, Any]] = {}
        self._changed(store, K.agent(self.alias, "profile"), self.profile().to_dict(), changes)
        counted: Dict[str, tuple] = {}
        for thread, _name, context, _seen in self._topics():
            key = K.topic_agent(thread, self.alias)
            self._changed(store, key, self._agent_in_topic(store, thread, context), changes)
            pending = self.turns.get(context or str(thread), 0)
            if pending:
                counted[key] = (context or str(thread), pending)
        if store.holds_lease():
            for thread, name, context, seen in self._topics():
                self._changed(store, K.topic(thread),
                              self._topic(store, thread, name, context, seen), changes)
        if not changes:
            return []
        written = []
        for result in await store.write_many(list(changes.items())):
            if result.applied:
                written.append(result.key)
                if result.key in counted:              # those turns are in the store now
                    name, pending = counted[result.key]
                    self.turns[name] = max(0, self.turns.get(name, 0) - pending)
            else:
                logger.info("registry: %s not written: %s", result.key, result.reason)
        return written

    @staticmethod
    def _changed(store: "StateStore", key: str, value: Optional[Dict[str, Any]],
                 changes: Dict[str, Dict[str, Any]]) -> None:
        if value is not None and store.read(key) != value:
            changes[key] = value

    def profile(self) -> Profile:
        config = self.channel.config
        adapter = config.adapter
        effort = getattr(adapter, "effort", None) or "default"
        limit = getattr(adapter, "session_turn_limit", None)
        return Profile(alias=config.identity.alias,
                       handle="@" + str(config.identity.name or "").lstrip("@"),
                       runtime=str(adapter.type),
                       default_model=str(getattr(adapter, "model", None) or "default"),
                       default_effort=effort if effort in EFFORTS else "default",
                       capabilities=[], role_preference=getattr(config.identity, "role", None),
                       max_turns=int(limit) if isinstance(limit, int) and limit > 0 else None)

    def _agent_in_topic(self, store: "StateStore", thread: int,
                        context: Optional[str]) -> Optional[Dict[str, Any]]:
        """The agent's record for a topic, compared with what the store holds -- so a restart
        keeps the turn count and still sees a session that changed while it was down."""
        found = session_and_kind(getattr(self.channel, "adapter", None), context)
        session, derived = found if found else (None, False)
        pending = self.turns.get(context or str(thread), 0)
        current = store.read(K.topic_agent(thread, self.alias))
        try:
            record = AgentInTopic.from_dict(current) if current else AgentInTopic()
        except RecordError:
            record = AgentInTopic()
        if current is None and pending == 0 and (session is None or derived):
            # Nothing to say yet: no turn here, and (for a derived id) no session either --
            # claude_thread has an id for every topic, used or not (review).
            return None
        if record.session_id is not None and session is not None \
                and session != record.session_id:
            record.rotations += 1                       # a new session replaced the old one
            record.started_at = now_iso(self.clock)
            record.turns = 0                            # this beat's turns ran on the new one
        elif record.session_id is None and session is not None:
            record.started_at = record.started_at or now_iso(self.clock)
        record.session_id = session if session is not None else record.session_id
        record.turns += pending
        return record.to_dict()

    def _topic(self, store: "StateStore", thread: int, name: Optional[str],
               context: Optional[str], seen: Optional[float]) -> Optional[Dict[str, Any]]:
        current = store.read(K.topic(thread))
        try:
            topic = Topic.from_dict(current) if current else None
        except RecordError:
            topic = None
        stamp = now_iso(lambda: (seen or self.clock()) // ACTIVITY_STEP_SECONDS
                        * ACTIVITY_STEP_SECONDS)
        if topic is None:
            topic = Topic(topic_id=thread, chat_id=self._home(), title=str(name or thread),
                          status="open", opened_by="legacy", opened_at=stamp)
        topic.title = str(name or topic.title)
        topic.last_activity = stamp
        holder = self.channel.config.identity.alias
        binding = self.binding(context, "coordinator")
        old = topic.agents.get(holder)
        if old is not None:
            binding.linked_at = old.linked_at           # linking is an event, not a beat
        topic.agents = {alias: b for alias, b in topic.agents.items()
                        if b.role != "coordinator" or alias == holder}
        topic.agents[holder] = binding
        self._legacy_links(topic, thread)
        return topic.to_dict()

    def _legacy_links(self, topic: Topic, thread: int) -> None:
        repo = self._repo()
        if repo is None:
            return
        known = {link.ref for link in topic.links} | \
            {link.title for link in topic.links if link.provider == "github" and link.title}
        for ref, place in sorted((getattr(self.channel, "_ref_topics", {}) or {}).items()):
            if place[0] != self._home() or place[1] != thread:
                continue
            number = str(ref).lstrip("#")
            if not number.isdigit():
                continue
            url = f"https://github.com/{repo}/issues/{number}"
            if url in known or f"#{number}" in known:
                continue
            # A guess from a mention is a pointer, not a work item: phase 5's explicit links
            # carry the status, phase and assignees a work item needs.
            topic.links.append(Link(id=topic.next_link_id(), provider="github", kind="issue",
                                    ref=url, role="reference", title=f"#{number}",
                                    added_by="legacy", added_at=topic.last_activity,
                                    updated_by="legacy", updated_at=topic.last_activity))

    def turn_done(self, context: Optional[str], thread: Optional[int]) -> None:
        """A turn ended in a topic: count it (written at the next beat)."""
        if thread is None:
            return
        key = context or str(thread)
        self.turns[key] = self.turns.get(key, 0) + 1

    # -- /topics ---------------------------------------------------------------------------
    @staticmethod
    def _link_text(link: Dict[str, Any]) -> str:
        """``L2 #256 review [DeepSeek developer]`` for a work item, the title otherwise."""
        name = f"{link.get('id', '?')} {link.get('title') or link.get('ref', '?')}"
        if link.get("role") != "work":
            return name if link.get("added_by") != "legacy" else str(link.get("title") or name)
        who = ", ".join(f"{a.get('agent')} {a.get('as')}" for a in link.get("assignees") or [])
        return f"{name} {link.get('phase')}" + (f" [{who}]" if who else "")
    def describe(self, store: Optional["StateStore"]) -> str:
        if store is None or not store.ready:
            return "Topic registry: the state store is not running (state.backend)."
        lines = []
        for key in store.keys("topic/"):
            place = K.topic_of(key)
            if place is None or place[1] is not None:
                continue
            data = store.read(key) or {}
            agents = []
            bindings = {K.norm(a): b for a, b in (data.get("agents") or {}).items()}
            sessions = {k.rsplit("/", 1)[1] for k in store.keys(f"topic/{place[0]}/agent/")}
            for alias in sorted(set(bindings) | sessions):
                binding = bindings.get(alias) or {}
                session = store.read(K.topic_agent(place[0], alias)) or {}
                sid = str(session.get("session_id") or "-")[:10]
                what = (f"{binding.get('role')} {binding.get('model')}/{binding.get('effort')}"
                        if binding else "not linked")
                agents.append(f"{alias} {what} s={sid} t={session.get('turns', 0)}")
            links = ", ".join(self._link_text(link) for link in data.get("links") or [])
            lines.append(f"• {data.get('title')} ({place[0]})"
                         + (f" — {links}" if links else "")
                         + ("".join(f"\n    {a}" for a in agents)))
        if not lines:
            return "Topic registry: empty so far (the lease holder fills it on its beat)."
        return "Topic registry (shadow):\n" + "\n".join(lines)
