"""Load ``protocol.yaml`` (the rules) and a project's ``swarm.yaml`` (who is who).

``protocol.yaml`` ships inside the package and is the single source of truth for
intents, task states, evidence and the default policy. ``swarm.yaml`` belongs to a
project: the owner, the planner and standby, the roster of agents with their vendor and
public key, where the lanes live, and any policy overrides.
"""

from __future__ import annotations

import copy
import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without the swarm extra
    yaml = None  # type: ignore[assignment]

PROTOCOL_PATH = Path(__file__).with_name("protocol.yaml")
ROLES = ("owner", "planner", "worker")
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class SwarmConfigError(ValueError):
    """protocol.yaml or swarm.yaml is missing, malformed or inconsistent."""


def _load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        raise SwarmConfigError(
            'the swarm kernel needs PyYAML: pip install "swarmly[swarm]"')
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SwarmConfigError(f"{path}: not found") from None
    except yaml.YAMLError as exc:
        raise SwarmConfigError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise SwarmConfigError(f"{path}: expected a mapping at the top level")
    return data


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``base`` with ``override`` merged in. Lists and scalars replace."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class IntentRule:
    name: str
    summary: str
    roles: List[str]
    task: str = "optional"
    from_states: Optional[List[Optional[str]]] = None
    to_state: Optional[str] = None
    body: List[str] = field(default_factory=list)
    evidence: int = 0
    re: Optional[str] = None
    checks: List[str] = field(default_factory=list)

    def allows_role(self, role: str) -> bool:
        return "any" in self.roles or role in self.roles


@dataclass
class SafetyCategory:
    id: str
    summary: str
    commands: List["re.Pattern[str]"] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    touch: str = "any"  # "any": reading counts; "write": only writes count

    def matches_command(self, command: str) -> bool:
        return any(p.search(command) for p in self.commands)

    def matches_path(self, path: str) -> bool:
        norm = path.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        candidates = {norm, norm.lstrip("/")}
        for pattern in self.paths:
            for cand in candidates:
                if fnmatch.fnmatchcase(cand, pattern):
                    return True
                # '**/x' also matches a bare 'x' at the root.
                if pattern.startswith("**/") and fnmatch.fnmatchcase(cand, pattern[3:]):
                    return True
        return False


class Protocol:
    """The parsed rules from ``protocol.yaml`` merged with project policy overrides."""

    def __init__(self, data: Dict[str, Any], *, source: str = "protocol.yaml") -> None:
        self.raw = data
        self.source = source
        self.name = str(data.get("protocol", ""))
        if self.name != "a2a/2":
            raise SwarmConfigError(f"{source}: protocol must be 'a2a/2', got {self.name!r}")
        self.roles: Dict[str, str] = dict(data.get("roles") or {})
        self.states: Dict[str, str] = dict(data.get("states") or {})
        self.terminal_states = list(data.get("terminal_states") or [])
        self.evidence_kinds = list(data.get("evidence_kinds") or [])
        self.intents: Dict[str, IntentRule] = {}
        for name, spec in (data.get("intents") or {}).items():
            self.intents[name] = self._parse_intent(name, spec or {})
        self.policy: Dict[str, Any] = dict(data.get("policy") or {})
        self.safety: List[SafetyCategory] = self._parse_safety(self.policy.get("safety") or [])

    def _parse_intent(self, name: str, spec: Dict[str, Any]) -> IntentRule:
        where = f"{self.source}: intent {name}"
        roles = list(spec.get("roles") or [])
        for role in roles:
            if role != "any" and role not in ROLES:
                raise SwarmConfigError(f"{where}: unknown role {role!r}")
        task = spec.get("task", "optional")
        if task not in ("required", "forbidden", "optional"):
            raise SwarmConfigError(f"{where}: task must be required, forbidden or optional")
        from_states = spec.get("from")
        to_state = spec.get("to")
        for state in list(from_states or []) + ([to_state] if to_state else []):
            if state is not None and state != "@verdict" and state not in self.states:
                raise SwarmConfigError(f"{where}: unknown state {state!r}")
        return IntentRule(
            name=name, summary=str(spec.get("summary", "")), roles=roles, task=task,
            from_states=list(from_states) if from_states is not None else None,
            to_state=to_state, body=list(spec.get("body") or []),
            evidence=int(spec.get("evidence") or 0), re=spec.get("re"),
            checks=list(spec.get("checks") or []))

    def _parse_safety(self, items: List[Dict[str, Any]]) -> List[SafetyCategory]:
        out = []
        for item in items:
            try:
                commands = [re.compile(p) for p in item.get("commands") or []]
            except re.error as exc:
                raise SwarmConfigError(f"safety {item.get('id')}: bad regex: {exc}") from None
            touch = str(item.get("touch") or "any")
            if touch not in ("any", "write"):
                raise SwarmConfigError(f"safety {item.get('id')}: touch must be any or write")
            out.append(SafetyCategory(id=str(item["id"]), summary=str(item.get("summary", "")),
                                      commands=commands, paths=list(item.get("paths") or []),
                                      touch=touch))
        return out

    def safety_category(self, cat_id: str) -> Optional[SafetyCategory]:
        for cat in self.safety:
            if cat.id == cat_id:
                return cat
        return None

    def stuck(self, key: str) -> float:
        return float((self.policy.get("stuck") or {}).get(key, 0))

    def escalation(self, key: str) -> Any:
        return (self.policy.get("escalation") or {}).get(key)


@dataclass
class Agent:
    name: str
    vendor: str
    role: str
    key: Optional[str] = None  # base64 Ed25519 public key
    telegram: Optional[str] = None  # @username the doorbell mentions


@dataclass
class Swarm:
    """A project's ``swarm.yaml`` together with the protocol it runs."""

    project: str
    owner: str
    planner: str
    standby: Optional[str]
    agents: Dict[str, Agent]
    protocol: Protocol
    bus: Dict[str, Any]
    path: Optional[Path] = None
    doorbell: Dict[str, Any] = field(default_factory=dict)

    def agent(self, name: str) -> Agent:
        try:
            return self.agents[name]
        except KeyError:
            raise SwarmConfigError(f"{name!r} is not in the swarm roster") from None

    def role_of(self, name: str) -> str:
        return self.agent(name).role

    def public_key(self, name: str) -> Optional[str]:
        return self.agents[name].key if name in self.agents else None


def load_protocol(overrides: Optional[Dict[str, Any]] = None) -> Protocol:
    data = _load_yaml(PROTOCOL_PATH)
    if overrides:
        data = dict(data)
        base = data.get("policy") or {}
        overrides = dict(overrides)
        extra_safety = overrides.pop("safety", None)
        data["policy"] = deep_merge(base, overrides)
        if extra_safety is not None:
            # Review P1-9: a swarm.yaml can add safety categories but never drop or redefine
            # the built-in ones (``safety: []`` used to wipe them all).
            if not isinstance(extra_safety, list):
                raise SwarmConfigError("policy.safety must be a list of categories")
            built_in = list(base.get("safety") or [])
            ids = {str(item.get("id")) for item in built_in if isinstance(item, dict)}
            for item in extra_safety:
                item_id = item.get("id") if isinstance(item, dict) else None
                if not isinstance(item_id, str) or not item_id.strip():
                    raise SwarmConfigError("policy.safety: each category needs a text id")
                if item_id in ids:
                    raise SwarmConfigError(f"policy.safety: {item_id!r} is a built-in "
                                           "category or a duplicate; add a new id")
                ids.add(item_id)
            data["policy"]["safety"] = built_in + copy.deepcopy(extra_safety)
    return Protocol(data, source=str(PROTOCOL_PATH))


def swarm_from_dict(data: Dict[str, Any], *, path: Optional[Path] = None) -> Swarm:
    where = str(path) if path else "swarm.yaml"
    for key in ("project", "owner", "planner", "agents"):
        if not data.get(key):
            raise SwarmConfigError(f"{where}: missing {key!r}")
    owner, planner = str(data["owner"]), str(data["planner"])
    standby = data.get("standby")
    agents: Dict[str, Agent] = {}
    for name, spec in (data.get("agents") or {}).items():
        name = str(name)
        if not AGENT_RE.match(name):
            raise SwarmConfigError(f"{where}: agent name {name!r} must match {AGENT_RE.pattern}")
        spec = spec or {}
        if name == owner:
            role = "owner"
        elif name == planner:
            role = "planner"
        else:
            role = "worker"
        vendor = spec.get("vendor")
        if not vendor:
            raise SwarmConfigError(f"{where}: agent {name!r} needs a vendor")
        agents[name] = Agent(name=name, vendor=str(vendor), role=role, key=spec.get("key"),
                             telegram=spec.get("telegram"))
    for who, label in ((owner, "owner"), (planner, "planner")):
        if who not in agents:
            raise SwarmConfigError(f"{where}: {label} {who!r} is not in agents")
    if owner == planner:
        raise SwarmConfigError(f"{where}: the owner cannot also be the planner")
    if standby is not None and str(standby) not in agents:
        raise SwarmConfigError(f"{where}: standby {standby!r} is not in agents")
    protocol = load_protocol(data.get("policy") or None)
    bus = dict(data.get("bus") or {})
    return Swarm(project=str(data["project"]), owner=owner, planner=planner,
                 standby=str(standby) if standby else None, agents=agents,
                 protocol=protocol, bus=bus, path=path,
                 doorbell=dict(data.get("doorbell") or {}))


def find_swarm_file(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("A2A_SWARM")
    if env:
        return Path(env).expanduser()
    here = Path.cwd()
    for folder in [here, *here.parents]:
        cand = folder / "swarm.yaml"
        if cand.is_file():
            return cand
    raise SwarmConfigError("no swarm.yaml found: pass --swarm or set A2A_SWARM")


def load_swarm(path: Optional[str] = None) -> Swarm:
    file = find_swarm_file(path)
    return swarm_from_dict(_load_yaml(file), path=file)
