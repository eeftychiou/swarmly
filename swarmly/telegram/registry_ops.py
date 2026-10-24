"""Changing a topic's links, work items and agents (topic registry, phase 5).

The lease holder applies the coordinator's ``LINK:`` / ``ASSIGN:`` / ``PHASE:`` lines and the
admin's ``/link``, ``/unlink`` and ``/phase`` to the topic record (``topic/<id>``), which it
alone writes. Every change also goes into the topic's ``history``. Nothing routes by these
links yet: phase 5 records them, a later flag lets GitHub notices follow them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

from swarmly.state import keys as K
from swarmly.state.links import AssignLine, LinkLine, PhaseLine, parse_ref
from swarmly.state.records import (Assignment, Binding, Change, Link, RecordError, Topic,
                                   now_iso)

if TYPE_CHECKING:  # pragma: no cover
    from swarmly.state.store import StateStore

logger = logging.getLogger("swarmly.telegram.registry")

#: A new work item starts here unless a PHASE line says otherwise.
DEFAULT_PHASE = "development"


class RegistryOps:
    def __init__(self, store: "StateStore", *, home: int, repo: Optional[str],
                 clock: Any) -> None:
        self.store = store
        self.home = int(home)
        self.repo = repo
        self.clock = clock

    # -- reading ---------------------------------------------------------------------------
    def topic(self, thread: int, title: str = "") -> Topic:
        data = self.store.read(K.topic(thread))
        try:
            if data:
                return Topic.from_dict(data)
        except RecordError:
            logger.warning("registry: topic/%s unreadable; starting it again", thread)
        stamp = now_iso(self.clock)
        return Topic(topic_id=int(thread), chat_id=self.home, title=title or str(thread),
                     status="open", opened_by="transport", opened_at=stamp,
                     last_activity=stamp)

    def find(self, topic: Topic, ref: str) -> Optional[Link]:
        """A link by its id (``L2``), its URL/path, or its title (``#256``)."""
        token = str(ref or "").strip()
        if token.upper().startswith("L") and token[1:].isdigit():
            return topic.link(token.upper())
        parsed = parse_ref(token, repo=self.repo)
        for link in topic.links:
            if parsed is not None and (link.ref == parsed.ref or
                                       (parsed.title and link.title == parsed.title
                                        and link.provider == parsed.provider)):
                return link
            if link.title and link.title == token:
                return link
        return None

    # -- changes ---------------------------------------------------------------------------
    def apply(self, topic: Topic, lines: List[object], by: str) -> List[str]:
        """Apply LINK/ASSIGN/PHASE lines to ``topic`` (in memory); returns what was done."""
        done: List[str] = []
        for line in lines:
            try:
                if isinstance(line, LinkLine):
                    done.append(self._link(topic, line, by))
                elif isinstance(line, AssignLine):
                    done.append(self._assign(topic, line, by))
                elif isinstance(line, PhaseLine):
                    done.append(self._phase(topic, line.ref, line.phase, by))
            except (RecordError, ValueError) as exc:
                done.append(f"refused: {exc}")
        return done

    def _stamp(self) -> str:
        return now_iso(self.clock)

    def _link(self, topic: Topic, line: LinkLine, by: str) -> str:
        parsed = parse_ref(line.ref, repo=self.repo)
        if parsed is None:
            raise ValueError(f"{line.ref} is not a reference this swarm knows")
        existing = self.find(topic, line.ref)
        stamp = self._stamp()
        if existing is not None:
            if existing.role != line.role:
                existing.role = line.role
                if line.role == "work":
                    existing.status = existing.status or "open"
                    existing.phase = existing.phase or DEFAULT_PHASE
                    existing.phase_at = existing.phase_at or stamp
                else:
                    existing.status = existing.phase = existing.phase_at = None
                    existing.assignees = []
                existing.updated_by, existing.updated_at = by, stamp
            if line.title:
                existing.title = line.title
            if existing.added_by == "legacy":
                existing.added_by = by                   # an explicit link now
            return f"{existing.id} {existing.title or existing.ref} ({existing.role})"
        link = Link(id=topic.next_link_id(), provider=parsed.provider, kind=parsed.kind,
                    ref=parsed.ref, role=line.role, title=line.title or parsed.title,
                    added_by=by, added_at=stamp, updated_by=by, updated_at=stamp)
        if line.role == "work":
            link.status, link.phase, link.phase_at = "open", DEFAULT_PHASE, stamp
        link.validate()
        topic.links.append(link)
        return f"{link.id} {link.title or link.ref} ({link.role})"

    def _work(self, topic: Topic, ref: str) -> Link:
        link = self.find(topic, ref)
        if link is None:
            raise ValueError(f"{ref} is not linked to this topic (LINK: it first)")
        if link.role != "work":
            raise ValueError(f"{link.id} is a {link.role} link, not a work item")
        return link

    def _assign(self, topic: Topic, line: AssignLine, by: str) -> str:
        link = self._work(topic, line.ref)
        agent = line.agent.lstrip("@")
        stamp = self._stamp()
        before = [a.agent for a in link.assignees]
        if not any(K.norm(a.agent) == K.norm(agent) and a.role == line.role
                   for a in link.assignees):
            link.assignees.append(Assignment(agent=agent, role=line.role, assigned_by=by,
                                             assigned_at=stamp))
            topic.history.append(Change(at=stamp, field="assignees", old=before,
                                        to=[a.agent for a in link.assignees], by=by,
                                        link=link.id))
        self.link_agent(topic, agent, by, role=line.role)
        link.updated_by, link.updated_at = by, stamp
        return f"{agent} on {link.id} as {line.role}"

    def _phase(self, topic: Topic, ref: str, phase: str, by: str) -> str:
        link = self._work(topic, ref)
        if link.phase == phase:
            return f"{link.id} already {phase}"
        stamp = self._stamp()
        topic.history.append(Change(at=stamp, field="phase", old=link.phase, to=phase, by=by,
                                    link=link.id))
        link.phase, link.phase_at = phase, stamp
        if phase == "done" and link.status == "open":
            topic.history.append(Change(at=stamp, field="status", old="open", to="closed",
                                        by=by, link=link.id))
            link.status = "closed"
        link.updated_by, link.updated_at = by, stamp
        return f"{link.id} {phase}"

    def link_agent(self, topic: Topic, alias: str, by: str, *, role: str = "") -> str:
        """Link an agent to the topic, with a binding from its published profile."""
        key = next((a for a in topic.agents if K.norm(a) == K.norm(alias)), None)
        profile = self.store.read(K.agent(alias, "profile")) or {}
        if key is not None:
            binding = topic.agents[key]
            if role and binding.role != role and binding.role != "coordinator":
                topic.history.append(Change(at=self._stamp(), field="role", old=binding.role,
                                            to=role, by=by, agent=key))
                binding.role = role
            return f"{key} already linked"
        effort = profile.get("default_effort") or "default"
        topic.agents[str(profile.get("alias") or alias)] = Binding(
            role=role or str(profile.get("role_preference") or "developer"),
            runtime=str(profile.get("runtime") or "unknown"),
            model=str(profile.get("default_model") or "default"),
            effort=effort, linked_by=by, linked_at=self._stamp())
        return f"{alias} linked"

    def unlink_agent(self, topic: Topic, alias: str, by: str) -> str:
        key = next((a for a in topic.agents if K.norm(a) == K.norm(alias)), None)
        if key is None:
            return f"{alias} was not linked"
        if topic.agents[key].role == "coordinator":
            raise ValueError("the coordinator follows the lease; it is not unlinked")
        del topic.agents[key]
        for link in topic.links:
            link.assignees = [a for a in link.assignees if K.norm(a.agent) != K.norm(alias)]
        return f"{alias} unlinked"

    async def save(self, thread: int, topic: Topic) -> bool:
        topic.validate()
        result = await self.store.write(K.topic(thread), topic.to_dict())
        if not result.applied:
            logger.info("registry: topic/%s not written: %s", thread, result.reason)
        return bool(result.applied)
