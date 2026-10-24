"""
Agent roles: what each agent in the swarm is for (coordinator, developer, reviewer, ...).

A role is a Markdown file with YAML front matter, the same shape as a Claude Code subagent
definition (``.claude/agents/<name>.md``): ``name`` and a one-line ``description`` of when
the role is the right one, then the role's standing instructions as the body. Swarmly ships
coordinator, developer, reviewer, qa-tester and researcher; a project overrides or adds roles
in ``.agents/roles/<name>.md`` of its bus checkout (``bus.repo``).

``agent.role`` in config.yaml picks this agent's role; ``peers`` entries may carry each peer's
role, so every agent knows whom to hand work to. The role's instructions travel with the
channel rules into every turn (the system prompt where the CLI has one).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

BUILTIN_ROLES = ("coordinator", "developer", "reviewer", "qa-tester", "researcher")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
#: Hard cap on a role body: it rides in every turn's prompt.
MAX_INSTRUCTIONS_CHARS = 6000


class RoleError(ValueError):
    """A role that cannot be found or read; config validation reports it."""


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    instructions: str
    source: str                        # "builtin" or the file it was read from

    def prompt(self, peer_roles: Optional[Dict[str, str]] = None) -> str:
        """The role as prompt text: who this agent is and how it works.

        No "who does what" here (admin, 24 Oct): roles are assignments the swarm makes at
        runtime by availability, not identities fixed to bots, and this text is baked into a
        session once. Who can act right now is the transport's per-turn note, never a rule.
        ``peer_roles`` is accepted for callers that still pass it."""
        del peer_roles
        lines = [f"YOUR ROLE IN THE SWARM: {self.name} — {self.description}", "",
                 self.instructions.strip()]
        return "\n".join(lines)


def team_line(peer_roles: Optional[Dict[str, str]]) -> str:
    roles = {alias: role for alias, role in (peer_roles or {}).items() if role}
    if not roles:
        return ""
    return "TEAM ROLES (hand work to the peer whose role fits): " + "; ".join(
        f"{alias}: {role}" for alias, role in roles.items())


def parse_role(text: str, source: str) -> Role:
    """A role file's front matter and body."""
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.S)
    if not match:
        raise RoleError(f"{source}: a role file starts with '---' front matter "
                        "(name, description) followed by the instructions")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise RoleError(f"{source}: front matter is not valid YAML ({exc})") from exc
    if not isinstance(meta, dict):
        raise RoleError(f"{source}: front matter must be a mapping")
    name = str(meta.get("name") or "").strip()
    description = " ".join(str(meta.get("description") or "").split())
    body = match.group(2).strip()
    if not _NAME.match(name):
        raise RoleError(f"{source}: name {name!r} must be lowercase letters, digits, '-' or '_'")
    if not description:
        raise RoleError(f"{source}: description is required (one line: what the role is for)")
    if not body:
        raise RoleError(f"{source}: the role has no instructions after the front matter")
    if len(body) > MAX_INSTRUCTIONS_CHARS:
        raise RoleError(f"{source}: instructions are {len(body)} characters; keep them under "
                        f"{MAX_INSTRUCTIONS_CHARS} (they ride in every turn)")
    return Role(name=name, description=description, instructions=body, source=source)


def role_dirs(*roots: Optional[str]) -> List[Path]:
    """``<root>/.agents/roles`` for each configured root, in order, without duplicates."""
    out: List[Path] = []
    for root in roots:
        if not root:
            continue
        path = Path(root).expanduser() / ".agents" / "roles"
        if path not in out:
            out.append(path)
    return out


def _read(path: Path) -> str:
    """A role file's text; a byte-order mark (Windows Notepad writes one) is fine."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoleError(f"{path}: cannot read the role file ({exc})") from exc


def load_role(spec: str, search: Iterable[Path] = (), base: Optional[Path] = None) -> Role:
    """The role named *spec*: a path to a ``.md`` file (relative to *base*, the config's
    folder), else ``<dir>/<spec>.md`` in the first search directory that has it, else the
    built-in role of that name."""
    spec = spec.strip()
    search = list(search)
    if not spec:
        raise RoleError("agent.role is empty")
    if spec.endswith(".md") or "/" in spec or "\\" in spec:
        path = Path(spec).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        if not path.is_file():
            raise RoleError(f"role file {path} not found")
        return parse_role(_read(path), str(path))
    name = spec.lower()
    for directory in search:
        candidate = directory / f"{name}.md"
        if candidate.is_file():
            role = parse_role(_read(candidate), str(candidate))
            if role.name != name:
                raise RoleError(f"{candidate}: its name is {role.name!r}, not {name!r}")
            return role
    if name in BUILTIN_ROLES:
        text = resources.files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
        return parse_role(text, "builtin")
    raise RoleError(f"unknown role {spec!r}; known roles: "
                    f"{', '.join(sorted(known_roles(search)))}")


def known_roles(search: Iterable[Path] = ()) -> set[str]:
    """The built-in role names plus every ``<name>.md`` in the search directories."""
    return set(BUILTIN_ROLES) | {p.stem.lower() for d in search if d.is_dir()
                                 for p in d.glob("*.md")}
