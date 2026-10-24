"""Store keys and their single writer (``docs/design/topic-registry.md`` §1b, rule 1).

Every key has exactly one kind of writer, so two bots never write the same value and a merge
is a union. The fold applies these rules on *read*, not only on write: a line written by a bot
that does not own the key is ignored by every reader, so a buggy or old bot cannot change a
value it does not own, whatever it posts.

=========================  =====================================================
key                        only writer
=========================  =====================================================
``lease``                  any bot may claim; the fold picks one winner per term
``topic/<id>``             the lease holder of the current term
``owed``                   the lease holder of the current term
``topic/<id>/agent/<a>``   agent ``<a>``'s own transport
``agent/<a>/<anything>``   agent ``<a>``'s own transport
``probe/<a>/<anything>``   agent ``<a>`` (shadow-mode heartbeats, store tests)
=========================  =====================================================
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

LEASE = "lease"
OWED = "owed"

_TOPIC = re.compile(r"^topic/(-?\d+)$")
_TOPIC_AGENT = re.compile(r"^topic/(-?\d+)/agent/([A-Za-z0-9_.-]+)$")
_AGENT = re.compile(r"^agent/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_PROBE = re.compile(r"^probe/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")

#: What ``owner`` returns for a key the lease holder writes.
HOLDER = "@lease-holder"
#: ... and for the lease itself.
ANYONE = "@anyone"


class KeyError_(ValueError):
    """A key that matches no pattern."""


def norm(alias: str) -> str:
    return str(alias or "").strip().lower()


def topic(topic_id: int) -> str:
    return f"topic/{int(topic_id)}"


def topic_agent(topic_id: int, alias: str) -> str:
    return f"topic/{int(topic_id)}/agent/{norm(alias)}"


def agent(alias: str, part: str) -> str:
    return f"agent/{norm(alias)}/{part}"


def probe(alias: str, part: str) -> str:
    return f"probe/{norm(alias)}/{part}"


def owner(key: str) -> str:
    """``HOLDER``, ``ANYONE`` or the (lower-case) alias that alone may write ``key``."""
    if key == LEASE:
        return ANYONE
    if key == OWED or _TOPIC.match(key):
        return HOLDER
    for pattern, group in ((_TOPIC_AGENT, 2), (_AGENT, 1), (_PROBE, 1)):
        found = pattern.match(key)
        if found:
            return norm(found.group(group))
    raise KeyError_(f"unknown store key {key!r}")


def valid(key: str) -> bool:
    try:
        owner(key)
    except KeyError_:
        return False
    return True


def may_write(key: str, writer: str, holder: Optional[str]) -> bool:
    """Whether ``writer`` may write ``key`` while ``holder`` holds the lease (None: nobody)."""
    who = owner(key)
    if who == ANYONE:
        return True
    if who == HOLDER:
        return holder is not None and norm(writer) == norm(holder)
    return norm(writer) == who


def topic_of(key: str) -> Optional[Tuple[int, Optional[str]]]:
    """(topic id, agent or None) for a topic key, else None."""
    found = _TOPIC.match(key)
    if found:
        return int(found.group(1)), None
    found = _TOPIC_AGENT.match(key)
    if found:
        return int(found.group(1)), found.group(2)
    return None
