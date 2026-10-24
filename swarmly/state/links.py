"""Work items and links (topic registry, phase 5): what a reference points to, and the lines
and commands that change a topic's links (``docs/design/topic-registry.md`` §1a, Link).

A link is a pointer to wherever the work lives -- GitHub, GitLab, Google Drive or another
cloud, a local path, or nothing at all (``provider: topic``: the topic is its own work item).
:func:`parse_ref` turns what a person or the coordinator writes into the link's fields.

The coordinator changes a topic's links with lines in its replies, recorded by the transport:

* ``LINK: <ref> [as work|reference|tracking] [— title]``
* ``ASSIGN: @bot <ref|L1> as <role>``
* ``PHASE: <ref|L1> <phase>``

and the admin does the same with ``/link @bot``, ``/unlink @bot`` and ``/phase <ref> <phase>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from swarmly.state.records import LINK_ROLES, PHASES

_GH = re.compile(r"^https?://github\.com/([^/\s]+/[^/\s]+)/(issues|pull)/(\d+)\b", re.I)
_GL = re.compile(r"^https?://[^/\s]*gitlab[^/\s]*/(.+?)/-/(issues|merge_requests)/(\d+)\b", re.I)
_DRIVE = re.compile(r"^https?://(drive|docs)\.google\.com/", re.I)
_URL = re.compile(r"^https?://\S+$", re.I)
_SHORT = re.compile(r"^(?:([\w.-]+/[\w.-]+))?#(\d+)$")
_TASK = re.compile(r"^T-\d+-\d+$")
_LOCAL = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\./|\.\./|~/)")


@dataclass(frozen=True)
class Ref:
    provider: str
    kind: str
    ref: str
    title: str = ""


def parse_ref(text: str, *, repo: Optional[str] = None) -> Optional[Ref]:
    """What ``text`` points to, or None. ``repo`` (owner/name) resolves a bare ``#12``."""
    token = str(text or "").strip().strip("<>").rstrip(".,;)")
    if not token:
        return None
    found = _GH.match(token)
    if found:
        kind = "pr" if found.group(2).lower() == "pull" else "issue"
        number = found.group(3)
        return Ref("github", kind, f"https://github.com/{found.group(1)}/"
                                   f"{'pull' if kind == 'pr' else 'issues'}/{number}",
                   f"#{number}")
    found = _GL.match(token)
    if found:
        kind = "pr" if found.group(2).lower() == "merge_requests" else "issue"
        return Ref("gitlab", kind, token.split("#")[0], f"!{found.group(3)}"
                   if kind == "pr" else f"#{found.group(3)}")
    found = _SHORT.match(token)
    if found:
        owner = found.group(1) or repo
        if not owner:
            return None
        return Ref("github", "issue", f"https://github.com/{owner}/issues/{found.group(2)}",
                   f"#{found.group(2)}")
    if _DRIVE.match(token):
        kind = "folder" if "/folders/" in token else "doc"
        return Ref("gdrive", kind, token)
    if _URL.match(token):
        return Ref("cloud", "doc", token)
    if _TASK.match(token):
        return Ref("topic", "task", token)
    if _LOCAL.match(token):
        kind = "folder" if token.endswith(("/", "\\")) else "file"
        return Ref("local", kind, token)
    return None


# -- lines in the coordinator's replies --------------------------------------------------

@dataclass(frozen=True)
class LinkLine:
    ref: str
    role: str = "work"
    title: str = ""


@dataclass(frozen=True)
class AssignLine:
    agent: str
    ref: str
    role: str


@dataclass(frozen=True)
class PhaseLine:
    ref: str
    phase: str


_LINK_LINE = re.compile(r"^\s*LINK:\s*(\S+)(?:\s+as\s+(\w+))?(?:\s*[—–-]+\s*(.+))?\s*$",
                        re.I | re.M)
_ASSIGN_LINE = re.compile(r"^\s*ASSIGN:\s*@?([\w.-]+)\s+(\S+)\s+as\s+([\w-]+)\s*$", re.I | re.M)
_PHASE_LINE = re.compile(r"^\s*PHASE:\s*(\S+)\s+(\w+)\s*$", re.I | re.M)


def parse_lines(text: str) -> List[object]:
    """The LINK/ASSIGN/PHASE lines of a reply, in order. Malformed ones are skipped."""
    found: List[Tuple[int, object]] = []
    for match in _LINK_LINE.finditer(text or ""):
        role = (match.group(2) or "work").lower()
        if role in LINK_ROLES:
            found.append((match.start(), LinkLine(match.group(1), role,
                                                  (match.group(3) or "").strip())))
    for match in _ASSIGN_LINE.finditer(text or ""):
        found.append((match.start(), AssignLine(match.group(1), match.group(2),
                                                match.group(3).lower())))
    for match in _PHASE_LINE.finditer(text or ""):
        phase = match.group(2).lower()
        if phase in PHASES:
            found.append((match.start(), PhaseLine(match.group(1), phase)))
    return [line for _, line in sorted(found, key=lambda item: item[0])]
