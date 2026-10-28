"""The records of the topic registry (``docs/design/topic-registry.md`` §1a), as plain data.

Every record round-trips through JSON: ``to_dict`` drops empty optional fields and
``from_dict`` ignores fields it does not know, so a newer bot's record still loads on an older
one (forward compatible) and an older bot's record loads on a newer one (missing fields take
their defaults). Enumerated fields are checked on load; a bad value raises ``RecordError`` so a
malformed write is refused rather than folded into the state.

Times are ISO-8601 UTC strings, as in the design; ``now_iso`` makes one.
"""

from __future__ import annotations

import time
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, ClassVar, Dict, List, Optional, Type, TypeVar

T = TypeVar("T", bound="Record")

TOPIC_STATUS = ("open", "closed")
PROVIDERS = ("github", "gitlab", "gdrive", "cloud", "local", "topic")
LINK_KINDS = ("issue", "pr", "doc", "folder", "file", "task")
LINK_ROLES = ("tracking", "work", "reference")
LINK_STATUS = ("open", "closed", "abandoned")
PHASES = ("research", "design", "development", "review", "qa", "done", "blocked")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "default")
MODEL_SOURCES = ("profile", "topic")
CHANGE_FIELDS = ("role", "model", "effort", "status", "phase", "assignees")
ROTATION_REASONS = ("drift", "turns", "age", "admin")


class RecordError(ValueError):
    """A record that does not match the data dictionary."""


def now_iso(clock: Any = time.time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(clock())))


def _check(name: str, value: Any, allowed: tuple, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if value not in allowed:
        raise RecordError(f"{name}={value!r}; allowed: {', '.join(allowed)}")


@dataclass
class Record:
    """Shared JSON behaviour. Subclasses list their nested record fields in ``NESTED``."""

    #: field name -> (record class, "one" | "list" | "map")
    NESTED: ClassVar[Dict[str, Any]] = {}

    def validate(self) -> None:
        """Raise RecordError when a field breaks the data dictionary."""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Record):
                value = value.to_dict()
            elif isinstance(value, list):
                value = [v.to_dict() if isinstance(v, Record) else v for v in value]
            elif isinstance(value, dict):
                value = {k: v.to_dict() if isinstance(v, Record) else v
                         for k, v in value.items()}
            if value is None or value == [] or value == {}:
                if f.default is not MISSING or f.default_factory is not MISSING:  # type: ignore
                    continue                          # an empty optional field is left out
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls: Type[T], data: Any) -> T:
        if not isinstance(data, dict):
            raise RecordError(f"{cls.__name__}: expected an object, got {type(data).__name__}")
        nested = cls.NESTED or {}
        kwargs: Dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name in nested and value is not None:
                kind, shape = nested[f.name]
                if shape == "one":
                    value = kind.from_dict(value)
                elif shape == "list":
                    value = [kind.from_dict(v) for v in value or []]
                elif shape == "map":
                    value = {str(k): kind.from_dict(v) for k, v in (value or {}).items()}
            kwargs[f.name] = value
        missing = [f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING  # type: ignore
                   and f.name not in kwargs]
        if missing:
            raise RecordError(f"{cls.__name__}: missing {', '.join(missing)}")
        record = cls(**kwargs)
        record.validate()
        return record


@dataclass
class Assignment(Record):
    agent: str
    role: str                                   # the design's ``as`` (a Python keyword)
    assigned_by: str
    assigned_at: str

    def to_dict(self) -> Dict[str, Any]:
        out = super().to_dict()
        out["as"] = out.pop("role")
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "Assignment":
        if isinstance(data, dict) and "as" in data and "role" not in data:
            data = {**data, "role": data["as"]}
        return super().from_dict(data)


@dataclass
class Link(Record):
    id: str
    provider: str
    kind: str
    ref: str
    role: str
    added_by: str
    added_at: str
    updated_by: str = ""
    updated_at: str = ""
    title: str = ""
    status: Optional[str] = None
    phase: Optional[str] = None
    phase_at: Optional[str] = None
    blocked_on: Optional[str] = None
    assignees: List[Assignment] = field(default_factory=list)

    NESTED: ClassVar[Dict[str, Any]] = {"assignees": (Assignment, "list")}

    def validate(self) -> None:
        _check("provider", self.provider, PROVIDERS, optional=False)
        _check("kind", self.kind, LINK_KINDS, optional=False)
        _check("role", self.role, LINK_ROLES, optional=False)
        _check("status", self.status, LINK_STATUS)
        _check("phase", self.phase, PHASES)
        # A tracking link carries GitHub's state too (#468): whether its issue is still open
        # is what keeps a finished topic listed, so it has to be recordable. A reference link
        # is a pointer and carries neither; assignees follow the work alone.
        if self.assignees and self.role != "work":
            raise RecordError(f"link {self.id}: only a work item has assignees")
        if self.role not in ("work", "tracking") and (self.status or self.phase):
            raise RecordError(f"link {self.id}: only a work item or its tracking issue has "
                              "a status or a phase")
        if self.role == "work" and (self.status is None or self.phase is None):
            raise RecordError(f"link {self.id}: a work item needs a status and a phase (§1a)")


@dataclass
class Change(Record):
    at: str
    field: str
    to: Any
    by: str
    # ``from`` is a keyword: kept as ``old`` in Python, ``from`` in JSON.
    old: Any = None
    agent: str = ""
    link: str = ""

    def validate(self) -> None:
        _check("field", self.field, CHANGE_FIELDS, optional=False)

    def to_dict(self) -> Dict[str, Any]:
        out = super().to_dict()
        if "old" in out:
            out["from"] = out.pop("old")
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "Change":
        if isinstance(data, dict) and "from" in data:
            data = {k: v for k, v in data.items() if k != "from"} | {"old": data["from"]}
        return super().from_dict(data)


@dataclass
class Binding(Record):
    """An agent in a topic: configuration and identity, never live availability."""

    role: str
    runtime: str
    model: str
    effort: str
    linked_by: str
    linked_at: str
    model_source: str = "profile"

    def validate(self) -> None:
        _check("effort", self.effort, EFFORTS, optional=False)
        _check("model_source", self.model_source, MODEL_SOURCES, optional=False)


@dataclass
class Topic(Record):
    """``topic/<id>``: written only by the lease holder."""

    topic_id: int
    chat_id: int
    title: str
    status: str
    opened_by: str
    opened_at: str
    last_activity: str = ""
    closed_at: Optional[str] = None
    links: List[Link] = field(default_factory=list)
    agents: Dict[str, Binding] = field(default_factory=dict)
    history: List[Change] = field(default_factory=list)
    #: ``/cleanup keep`` in the topic: /cleanup never deletes it (admin, 26 Oct: kept topics
    #: are decided in the group, not as ids in config.yaml).
    keep: bool = False

    NESTED: ClassVar[Dict[str, Any]] = {"links": (Link, "list"), "agents": (Binding, "map"),
              "history": (Change, "list")}

    def validate(self) -> None:
        _check("status", self.status, TOPIC_STATUS, optional=False)
        seen = set()
        for link in self.links:
            if link.id in seen:
                raise RecordError(f"topic {self.topic_id}: link id {link.id} twice")
            seen.add(link.id)

    def link(self, link_id: str) -> Optional[Link]:
        return next((link for link in self.links if link.id == link_id), None)

    def next_link_id(self) -> str:
        used = {int(link.id[1:]) for link in self.links
                if link.id.startswith("L") and link.id[1:].isdigit()}
        return f"L{max(used, default=0) + 1}"


@dataclass
class Rotation(Record):
    at: str
    reason: str

    def validate(self) -> None:
        _check("reason", self.reason, ROTATION_REASONS, optional=False)


@dataclass
class AgentInTopic(Record):
    """``topic/<id>/agent/<alias>``: that binding's session, written only by that agent."""

    session_id: Optional[str] = None
    started_at: Optional[str] = None
    turns: int = 0
    rotations: int = 0
    handover: Optional[str] = None
    last_rotation: Optional[Rotation] = None
    #: link id -> the agent's last turn on that work item
    last_activity: Dict[str, str] = field(default_factory=dict)

    NESTED: ClassVar[Dict[str, Any]] = {"last_rotation": (Rotation, "one")}


@dataclass
class Profile(Record):
    """``agent/<alias>/profile``: published by the agent from its config.yaml."""

    alias: str
    handle: str
    runtime: str
    default_model: str
    default_effort: str = "default"
    capabilities: List[str] = field(default_factory=list)
    role_preference: Optional[str] = None
    max_turns: Optional[int] = None
    max_age_hours: Optional[float] = None

    def validate(self) -> None:
        _check("default_effort", self.default_effort, EFFORTS, optional=False)


def as_dict(record: Any) -> Dict[str, Any]:
    """A record or a plain dict, as a plain dict."""
    if isinstance(record, Record):
        return record.to_dict()
    if isinstance(record, dict):
        return dict(record)
    return asdict(record)
