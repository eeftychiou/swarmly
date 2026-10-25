"""The live swarm dashboard as a local web page (issue #240, slice 1).

The pinned Telegram card (#117, #241) answers "what is the swarm doing" in one message that
is edited in place. This module answers the same question in a page: the transport that holds
the lease serves it on the loopback, and the page polls one small JSON endpoint every few
seconds. No model turn is involved on either side.

Three rules decide the shape, the same three the card keeps.

**A status, not a leak.** Every field here is picked by *name* out of the records the
transport already holds, so a snapshot or usage row carrying a chat id, a thread id, a token
or a path shows none of them. A discussion appears as its ``tg-…`` token, never an id. The
page has no external references at all -- no CDN, no font host, no analytics -- so it works on
a LAN with no route out, and nothing in it can call home.

**Unknown stays unknown.** A number the runtime never reported is ``null`` in the JSON and
``?`` on the page; it is never summed as a zero. A total says how many rows reported one.

**One write, and only while this bot holds the lease.** The server answers ``GET``/``HEAD``
for the page and its state, and one ``POST`` -- ``/api/action``, the control centre's write
path (#342) -- which is refused unless it carries a verified admin Mini App ``initData``, the
loopback included. Everything else is a ``404`` or a ``405``. It binds ``127.0.0.1``, so
nothing off the host can reach it; slice 2 (a Telegram Mini App over HTTPS) is where an
address becomes a decision, and that decision is the admin's.

The page carries the swarm (one card per bot), the lease and the round, the open debts, and
-- since #332 -- the topic registry: one card per open work topic, read from the same
``topic/<id>`` records ``/topics`` reads, so a ``PHASE:`` change shows on the next poll.

The page itself is one string with its styles and script inline -- self-contained, phone-width
first, light and dark by ``prefers-color-scheme`` -- ready to be the Mini App's page unchanged.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlsplit

from swarmly.state import keys as K
from swarmly.state.records import Profile, RecordError, Topic, as_dict
from swarmly.telegram.drift import RESET_AT as DRIFT_RESET_AT
from swarmly.telegram.snapshot import (BUSY, DOWN, FROZEN, IDLE, PAUSED, STUCK, Snapshot)

#: One word per state, and the colour class the page paints it with (#241's vocabulary).
BADGES: Dict[str, str] = {
    BUSY: "working", IDLE: "idle", PAUSED: "paused", FROZEN: "frozen",
    STUCK: "stuck", DOWN: "down",
}

#: The paths the server knows. Anything else is a 404, and no other method is allowed.
PAGE_PATH = "/"
STATE_PATH = "/state.json"

#: The one path that *writes* (#342): ``POST`` with ``{action, target?, value?, code?}``, sent
#: through the same dispatch the Telegram buttons use. Every write needs a verified admin
#: ``initData``, the loopback included: the plain local page stays read-only.
ACTION_PATH = "/api/action"

#: The most a write's body may carry: an action, a target, a value and a confirm code. A body
#: larger than this is refused unread, so a page cannot be made to buffer something huge.
MAX_BODY_BYTES = 8192
#: ``/memory``'s own view (#344): this bot's lessons, for the admin's eyes and nobody else's.
#: It asks for the verified login *always*, the loopback included -- a bot's working notes are
#: not a status field, and the plain local page stays a page about the swarm.
MEMORY_PATH = "/memory.json"

#: Where the server binds. Loopback only: the page is reached through a tunnel, not a bind
#: (#240 slice 2). The bind stays local so nothing on the network can talk to it directly.
HOST = "127.0.0.1"

logger = logging.getLogger(__name__)

#: The ``Host`` values that are always allowed. A request naming anything else is refused
#: unless the operator configured that name -- see :func:`allowed_hosts` and the DNS-rebinding
#: note in :class:`DashboardServer`. Only the IPv4 loopback is listed because that is the only
#: address the server binds; an IPv6 entry here could never match a bracketed ``Host`` and
#: would be a rule that only looks like one (review, #258).
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

#: The header a Mini App's page sends its ``initData`` in, and how long a known-good one lasts.
INIT_HEADER = "X-Telegram-Init-Data"
INIT_MAX_AGE = 300.0


# ------------------------------------------------------------------------------------
# Reading the records
# ------------------------------------------------------------------------------------


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _sum_or_none(values: Sequence[Optional[float]]) -> Optional[float]:
    """The sum of what was reported, or ``None`` when nothing was: never a made-up zero."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _spend(usage: Optional[Mapping[str, Any]], now: float) -> Dict[str, Any]:
    """One bot's spend today: numbers where reported, ``None`` where the runtime was silent."""
    usage = usage or {}
    quota_left = usage.get("quota_left")
    resets = usage.get("quota_resets_at")
    return {
        "turns": None if usage.get("turns") is None else int(_num(usage.get("turns"))),
        "tokens_in": (None if usage.get("tokens_in") is None
                      else int(_num(usage.get("tokens_in")))),
        "tokens_out": (None if usage.get("tokens_out") is None
                       else int(_num(usage.get("tokens_out")))),
        "cost": None if usage.get("cost_usd") is None else round(_num(usage.get("cost_usd")), 4),
        "quota_left": None if quota_left is None else int(_num(quota_left)),
        "quota_resets_in": (max(0.0, _num(resets) - now)
                            if quota_left is not None and resets else None),
    }


def bot_cards(*, now: float, snapshots: Sequence[Snapshot] = (),
              usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
              roles: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """One card per bot, in alias order: what it is, what it is doing, what it spent.

    A record that stopped being renewed is ``down`` whatever it last said of itself (#124):
    a dead bot publishes nothing, so the age of its record is the only thing that can say it.
    """
    moment = float(now)
    usage = usage or {}
    roles = roles or {}
    newest: Dict[str, Snapshot] = {}
    for row in snapshots:
        key = str(row.alias).strip().lower()
        if key not in newest or row.at >= newest[key].at:
            newest[key] = row
    cards: List[Dict[str, Any]] = []
    for key in sorted(newest):
        row = newest[key]
        state = DOWN if (row.state == DOWN or row.is_stale(moment)) else row.state
        since = _num(row.since)
        working = state == BUSY or state == STUCK
        card: Dict[str, Any] = {
            "alias": str(row.alias),
            "role": (roles.get(row.alias) or roles.get(key) or "").strip() or None,
            "state": state,
            "badge": BADGES.get(state, "?"),
            # When the *state* began. For a bot that stopped beating that is the record's
            # age, because its own "since" is as old as the last thing it managed to say.
            "since": (max(0.0, row.age(moment)) if state == DOWN
                      else (max(0.0, moment - since) if since and since <= moment else None)),
            "model": str(row.model or "").strip() or None,
            "effort": str(row.effort or "").strip() or None,
            "rev": str(row.rev or "").strip() or None,
            "topic": str(row.topic or "").strip() or None,
            "turn_seconds": (max(0.0, _num(row.turn_elapsed)) if working else None),
            "turn_timeout": (_num(row.turn_timeout) if working and row.turn_timeout else None),
            "pulse_age": row.age(moment),
            "queue": int(_num(row.queue)),
            "wakes": len(row.wakes or ()),
        }
        card.update(_spend(usage.get(row.alias), moment))
        cards.append(card)
    return cards


def swarm_total(cards: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The swarm's day: the sum of what the cards reported, and how many cards reported it."""
    def column(name: str) -> List[Optional[float]]:
        return [card.get(name) for card in cards]

    turns = column("turns")
    return {
        "bots": len(cards),
        "reported_turns": sum(1 for value in turns if value is not None),
        "turns": None if not any(value is not None for value in turns)
        else int(sum(_num(value) for value in turns if value is not None)),
        "tokens_in": _sum_or_none([value for value in column("tokens_in")]),
        "tokens_out": _sum_or_none([value for value in column("tokens_out")]),
        "cost": _sum_or_none([value for value in column("cost")]),
    }


def owed_rows(rows: Sequence[Any] = (), *, now: float) -> List[Dict[str, Any]]:
    """The open expectations, read by name: who owes what, to whom, for how long (#162)."""
    moment = float(now)
    out: List[Dict[str, Any]] = []
    for row in rows:
        opened = _num(getattr(row, "created_at", 0.0))
        deadline = _num(getattr(row, "deadline", 0.0))
        out.append({
            "item": str(getattr(row, "item", "") or "") or None,
            "kind": str(getattr(row, "kind", "") or "") or None,
            "debtor": str(getattr(row, "debtor", "") or "") or None,
            "owes": str(getattr(row, "owes", "") or "") or None,
            "waiting_on": str(getattr(row, "waiting_on", "") or "") or None,
            "head": (str(getattr(row, "head", "") or "") or "")[:7] or None,
            "topic": str(getattr(row, "topic", "") or "") or None,
            "flag": str(getattr(row, "flag", "") or "") or None,
            "stalls": int(_num(getattr(row, "stalls", 0))),
            # How long the debt has stood, and how long is left before its deadline is late.
            "open_seconds": max(0.0, moment - opened) if opened else None,
            "due_in": (deadline - moment) if deadline else None,
        })
    return out


# ------------------------------------------------------------------------------------
# The read commands' views (#344)
# ------------------------------------------------------------------------------------
#
# Every read command the bots answer in Telegram gets a view here, built from the records the
# transport already holds -- no model turn, no adapter call. Each one is picked by name, like
# the rest of the page: a record carrying a chat id, a thread id, a token or a path shows none
# of them.

#: What a view says when the thing it would read is not running: words, never an empty list
#: that would read as "there is nothing there".
NOT_READY = "this bot is not reading that right now"


def roster_view(*, cards: Sequence[Mapping[str, Any]] = (),
                peers: Sequence[Mapping[str, Any]] = (),
                coordinator: Optional[str] = None) -> Dict[str, Any]:
    """``/status`` and ``/swarm``: who is in the swarm, what each one runs, who leads.

    The bot cards are the same rows the Swarm section draws; *peers* adds what this bot knows
    about the others from its own presence table (never a live probe), and *coordinator* is the
    lease's own answer.
    """
    rows = [{
        "alias": str(card.get("alias") or ""),
        "role": card.get("role"),
        "state": card.get("state"),
        "badge": card.get("badge"),
        "model": card.get("model"),
        "effort": card.get("effort"),
        "rev": card.get("rev"),
        "topic": card.get("topic"),
        "queue": card.get("queue"),
    } for card in cards]
    seen = []
    for peer in peers:
        data = dict(peer or {})
        alias = str(data.get("alias") or "").strip()
        if not alias or any(row["alias"].lower() == alias.lower() for row in rows):
            continue
        seen.append({
            "alias": alias,
            "role": str(data.get("role") or "").strip() or None,
            # A peer's own liveness claim is not a state: its pulse is (the cards have it).
            "term": int(_num(data.get("term"))) or None,
            "rev": str(data.get("rev") or "").strip() or None,
            "frozen": bool(data.get("frozen")),
        })
    return {"ready": True, "coordinator": coordinator or None,
            "bots": rows, "peers": seen}


def drift_view(*, alias: str = "", decided: Optional[Mapping[str, Any]] = None,
               sessions: Optional[Mapping[str, Any]] = None,
               live: Sequence[Any] = (), now: float = 0.0,
               limit: int = 10) -> Dict[str, Any]:
    """``/drift``: the score, where it stands, and the latest violations behind it.

    Only the kind, its weight and its age are published: the ledger's ``evidence`` is the
    bot's own words about what it did and can name a topic, a path or an id, so it stays home.
    """
    moment = float(now)
    scores: Dict[str, Any] = {}
    for place, value in dict(sessions or {}).items():
        pair = value if isinstance(value, (list, tuple)) else (value, 0)
        scores[str(place)] = {"score": int(_num(pair[0])),
                              "hard": int(_num(pair[1] if len(pair) > 1 else 0))}
    rows = []
    for row in list(live)[-int(limit):]:
        at = _num(getattr(row, "at", 0.0))
        rows.append({"kind": str(getattr(row, "kind", "") or "") or None,
                     "severity": int(_num(getattr(row, "severity", 0))),
                     "age": (max(0.0, moment - at) if at else None)})
    decided = dict(decided or {})
    return {
        "ready": True,
        "alias": str(alias or "") or None,
        "score": int(_num(decided.get("score"))),
        "hard": int(_num(decided.get("hard"))),
        "remind": bool(decided.get("remind")),
        "reset": bool(decided.get("reset")),
        "resets_today": int(_num(decided.get("resets_today"))),
        "sessions": scores,
        "recent": rows,
    }


def state_view(*, backend: str = "", mode: str = "", enabled: Any = None,
               ready: Any = None, routing: str = "", owed: str = "") -> Dict[str, Any]:
    """``/state``: how the shared state store is set up, and whether it is readable.

    ``ready`` is ``None`` when this bot is not running the store at all -- an absent store is
    not a broken one, and the page says which.
    """
    name = str(backend or "").strip().lower() or "off"
    return {
        "ready": bool(ready) if enabled else None,
        "backend": name,
        "mode": str(mode or "").strip().lower() or None,
        "enabled": bool(enabled),
        "routing": str(routing or "").strip().lower() or None,
        "owed": str(owed or "").strip().lower() or None,
        "note": ("" if not enabled else ("" if ready else NOT_READY)),
    }


def settings_view(*, cards: Sequence[Mapping[str, Any]] = (),
                  topics: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """``/model``, ``/reasoning`` and ``/role``: what each bot runs, and what a topic changes.

    A bot's row is the setting it reports in its own pulse; a topic's row is its binding, which
    wins over the bot's default while that topic's turns run.
    """
    bots = [{"alias": str(card.get("alias") or ""),
             "role": card.get("role"),
             "model": card.get("model"),
             "effort": card.get("effort")}
            for card in cards]
    overrides = []
    for topic in topics:
        for binding in topic.get("bindings") or ():
            overrides.append({
                "topic": topic.get("topic"),
                "agent": binding.get("agent"),
                "role": binding.get("role"),
                "model": binding.get("model"),
                "effort": binding.get("effort"),
            })
    return {"ready": True, "bots": bots, "topics": overrides}


def memory_view(*, text: Optional[str] = None, alias: str = "") -> Dict[str, Any]:
    """``/memory``: this bot's own lessons, for the admin's eyes only.

    It reaches the wire through its own path, behind the same verified login as a write
    (#342): a bot's notes are its private working memory, not a status field. The file's home
    is never published -- only whether there is anything to read.
    """
    body = str(text or "")
    return {"ready": text is not None, "alias": str(alias or "") or None,
            "bytes": len(body.encode("utf-8")), "text": body}


# ------------------------------------------------------------------------------------
# The registry's topics (#332)
# ------------------------------------------------------------------------------------

#: A work item whose phase has not moved for this long reads as stalled. It is the transport's
#: own nudge threshold (``limits.stall_minutes``, thirty minutes by default), so the page and
#: the stall ladder never disagree about when a thing has gone quiet.
STALL_SECONDS = 30 * 60.0

#: The providers whose ``ref`` is safe to publish: a URL or a short ``#12``. A ``local`` ref
#: is a filesystem path and a ``topic`` ref is ``T-<thread>-<n>`` -- a thread id -- so for
#: those two only the link's title goes on the wire.
PUBLIC_PROVIDERS = ("github", "gitlab", "gdrive", "cloud")

#: The phases in which a work item is finished, so no phase change is ever due.
DONE_PHASES = ("done",)


def _iso_seconds(text: Any) -> Optional[float]:
    """An ISO-8601 UTC stamp as epoch seconds, or ``None`` when it is not one."""
    token = str(text or "").strip()
    if not token:
        return None
    try:
        return float(calendar.timegm(time.strptime(token, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def _since(stamp: Any, moment: float) -> Optional[float]:
    at = _iso_seconds(stamp)
    return None if at is None else max(0.0, moment - at)


def link_cards(links: Sequence[Any] = (), *, now: float,
               stall_seconds: float = STALL_SECONDS) -> List[Dict[str, Any]]:
    """One topic's links, read by name: the ref, its status, its phase, who is on it.

    A work item is **stalled** when its phase has not changed for *stall_seconds* and it is
    neither closed nor done -- the same threshold the transport's nudge ladder uses. A link
    with no ``phase_at`` is not stalled: an unknown age is not a stale one.
    """
    moment = float(now)
    out: List[Dict[str, Any]] = []
    for raw in links:
        data = as_dict(raw)
        provider = str(data.get("provider") or "").strip().lower()
        ref = str(data.get("ref") or "").strip()
        name = str(data.get("title") or "").strip()
        card: Dict[str, Any] = {
            "id": str(data.get("id") or "").strip() or None,
            "role": str(data.get("role") or "").strip() or None,
            "kind": str(data.get("kind") or "").strip() or None,
            # A local link's ref is a path: it stays out of the JSON, and the title speaks.
            "ref": (ref or None) if provider in PUBLIC_PROVIDERS else None,
            "name": name or None,
        }
        if card["role"] == "work":
            phase = str(data.get("phase") or "").strip() or None
            status = str(data.get("status") or "").strip() or None
            age = _since(data.get("phase_at"), moment)
            card.update({
                "status": status,
                "phase": phase,
                "phase_age": age,
                "stalled": bool(status != "closed" and phase not in DONE_PHASES
                                and age is not None and age >= float(stall_seconds)),
                "assignees": [{
                    "agent": str(as_dict(a).get("agent") or "").strip() or None,
                    "as": str(as_dict(a).get("as") or as_dict(a).get("role")
                              or "").strip() or None,
                } for a in data.get("assignees") or ()],
            })
        out.append(card)
    return out


def _rotation_due(session: Mapping[str, Any], profile: Mapping[str, Any],
                  drift: Optional[Mapping[str, Any]], age: Optional[float]) -> Optional[str]:
    """Why this session should be handed over, or ``None``: the design's three reasons.

    Drift first, because it is the reason that must not wait; then the profile's own limits
    (``max_turns``, ``max_age_hours``). A limit the profile does not set never trips.
    """
    if drift is not None:
        score = int(_num(drift.get("score")))
        hard = int(_num(drift.get("hard")))
        if score >= DRIFT_RESET_AT or hard >= 2:
            return "drift"
    turns = session.get("turns")
    limit = profile.get("max_turns")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 \
            and isinstance(turns, int) and turns >= limit:
        return "turns"
    limit_age = profile.get("max_age_hours")
    if isinstance(limit_age, (int, float)) and not isinstance(limit_age, bool) \
            and float(limit_age) > 0 and age is not None \
            and age >= float(limit_age) * 3600.0:
        return "age"
    return None


def binding_cards(agents: Mapping[str, Any] = (), *, thread: Any,
                  sessions: Optional[Mapping[Any, Mapping[str, Any]]] = None,
                  profiles: Optional[Mapping[str, Mapping[str, Any]]] = None,
                  drift: Optional[Mapping[Any, Mapping[str, Any]]] = None,
                  now: float) -> List[Dict[str, Any]]:
    """A topic's bindings: the agent, its role, and its session's state.

    The session is written by that agent's own transport, so one may be missing; then the
    turn count is ``null`` (unknown) rather than a zero nobody reported. Nothing here is
    looked up live: availability stays on the bot cards, which read the pulse.
    """
    moment = float(now)
    sessions = sessions or {}
    profiles = profiles or {}
    drift = drift or {}
    out: List[Dict[str, Any]] = []
    for alias in sorted(agents or ()):
        binding = dict(agents[alias] or {})
        key = K.norm(alias)
        session = dict(sessions.get((thread, key)) or {})
        known = bool(session)
        started = _iso_seconds(session.get("started_at"))
        age = None if started is None else max(0.0, moment - started)
        profile = dict(profiles.get(key) or {})
        score = drift.get((thread, key))
        turns = session.get("turns")
        out.append({
            "agent": str(alias),
            "role": str(binding.get("role") or "").strip() or None,
            "runtime": str(binding.get("runtime") or "").strip() or None,
            "model": str(binding.get("model") or "").strip() or None,
            "effort": str(binding.get("effort") or "").strip() or None,
            # Turns since the session started -- which is since its last rotation.
            "turns": (int(_num(turns)) if known else None),
            "rotations": (int(_num(session.get("rotations"))) if known else None),
            "session_age": age,
            "handover": (bool(session.get("handover")) if known else None),
            "drift": (None if score is None else int(_num(score.get("score")))),
            "hard": (None if score is None else int(_num(score.get("hard")))),
            "rotation_due": (_rotation_due(session, profile, score, age) if known else None),
        })
    return out


def topic_cards(*, now: float, topics: Sequence[Any] = (),
                sessions: Optional[Mapping[Any, Mapping[str, Any]]] = None,
                profiles: Optional[Mapping[str, Mapping[str, Any]]] = None,
                drift: Optional[Mapping[Any, Mapping[str, Any]]] = None,
                stall_seconds: float = STALL_SECONDS) -> List[Dict[str, Any]]:
    """One card per **open** topic, most recently active first.

    The topic's own identity (its thread id, its chat id) is used to reach its session
    records and goes no further: what reaches the wire is the topic's name, its links' refs
    and the agents' aliases.
    """
    moment = float(now)
    cards: List[Dict[str, Any]] = []
    for raw in topics:
        data = as_dict(raw)
        if str(data.get("status") or "open").strip().lower() != "open":
            continue                                    # closed topics carry no live work
        thread = data.get("topic_id")
        links = link_cards(data.get("links") or (), now=moment, stall_seconds=stall_seconds)
        bindings = binding_cards(data.get("agents") or {}, thread=thread, sessions=sessions,
                                 profiles=profiles, drift=drift, now=moment)
        cards.append({
            "topic": str(data.get("title") or "").strip() or None,
            "status": "open",
            "updated": _since(data.get("last_activity"), moment),
            "stalled": any(link.get("stalled") for link in links),
            "links": links,
            "bindings": bindings,
        })
    # Most recently active first; a topic that never said goes last, and never becomes a zero.
    cards.sort(key=lambda card: card["updated"] if card["updated"] is not None
               else float("inf"))
    return cards


def registry_panel(*, now: float, store: Any = None,
                   drift: Optional[Mapping[Any, Mapping[str, Any]]] = None,
                   stall_seconds: float = STALL_SECONDS) -> Dict[str, Any]:
    """The Topics panel from the store's registry -- the same records ``/topics`` reads.

    A store that is not started, or not yet ``ready``, is reported as such: an empty panel
    would say "no topics" about a registry nobody has read, which is the one thing a status
    page must never do. Nothing here costs a model turn, and no gitbus is read.
    """
    if store is None or not getattr(store, "ready", False):
        return {"ready": False, "topics": []}
    topics: List[Dict[str, Any]] = []
    sessions: Dict[Any, Dict[str, Any]] = {}
    for key in store.keys("topic/"):
        place = K.topic_of(key)
        if place is None:
            continue
        thread, alias = place
        data = store.read(key) or {}
        if alias is not None:
            sessions[(thread, K.norm(alias))] = data
            continue
        try:
            topics.append(Topic.from_dict(data).to_dict())
        except RecordError:
            # A record this bot cannot read is left out and logged: a page that raises is
            # worse than one that is thin.
            logger.warning("web dashboard: topic/%s is unreadable; leaving it off the page",
                           thread)
    profiles: Dict[str, Dict[str, Any]] = {}
    for key in store.keys("agent/"):
        parts = key.split("/")
        if len(parts) == 3 and parts[2] == "profile":
            try:
                profiles[K.norm(parts[1])] = Profile.from_dict(store.read(key) or {}).to_dict()
            except RecordError:
                logger.warning("web dashboard: %s is unreadable; no limits from it", key)
    return {"ready": True,
            "topics": topic_cards(now=now, topics=topics, sessions=sessions,
                                  profiles=profiles, drift=drift,
                                  stall_seconds=stall_seconds)}


def watch_state(*, now: float, snapshots: Sequence[Snapshot] = (),
                usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
                roles: Optional[Mapping[str, str]] = None, coordinator: str = "",
                lease: Optional[Any] = None, round_state: Optional[Any] = None,
                owed: Sequence[Any] = (), registry: Optional[Mapping[str, Any]] = None,
                peers: Sequence[Any] = (), drift: Optional[Mapping[str, Any]] = None,
                store: Optional[Mapping[str, Any]] = None
                ) -> Dict[str, Any]:
    """The JSON the page polls: the same records the pinned card is built from, in full.

    The card drops detail to fit 4,096 characters; a page has room, so nothing is cut here --
    but nothing is looked up either: this is a view over records the transport already took.
    """
    moment = float(now)
    cards = bot_cards(now=moment, snapshots=snapshots, usage=usage, roles=roles)
    # The lease is the authority (#83): a name the caller remembers can be stale, the folded
    # lease record cannot be older than the term it carries.
    holder = (str(getattr(lease, "holder", "") or "").strip()
              or str(coordinator or "").strip() or None)
    expires = _num(getattr(lease, "expires_at", 0.0))
    items = _round_items(round_state)
    done = sum(1 for item in items if item["state"] in ("merged", "closed", "done"))
    return {
        "at": moment,
        "refreshed": time.strftime("%H:%M:%SZ", time.gmtime(moment)),
        "coordinator": holder,
        "lease": {
            "holder": holder,
            "term": int(_num(getattr(lease, "term", 0))) or None,
            "expires_in": (max(0.0, expires - moment) if expires else None),
            "status": str(getattr(lease, "status", "") or "") or None,
        },
        "round": ({"name": str(getattr(round_state, "round", "") or "") or None,
                   "status": str(getattr(round_state, "status", "") or "") or None,
                   "done": done, "total": len(items),
                   "items": items}
                  if round_state is not None else None),
        "bots": cards,
        "total": swarm_total(cards),
        "owed": owed_rows(owed, now=moment),
        # A caller that did not collect the registry says so with ``None``: the page hides the
        # panel rather than claiming a registry nobody read.
        "registry": (None if registry is None else
                     {"ready": bool(registry.get("ready")),
                      "topics": list(registry.get("topics") or ())}),
        # The read commands' own views (#344). Each is ``None`` when the caller did not collect
        # it -- a view nobody read is not an empty one.
        "roster": roster_view(cards=cards, peers=peers or (), coordinator=holder),
        "drift": (None if drift is None else dict(drift)),
        "store": (None if store is None else dict(store)),
        "settings": (None if registry is None and not cards else
                     settings_view(cards=cards,
                                   topics=(registry or {}).get("topics") or ())),
    }


def _round_items(round_state: Optional[Any]) -> List[Dict[str, Any]]:
    """The round's items, by name: the checklist progress is all the strip needs (#240)."""
    items = getattr(round_state, "items", None)
    if not isinstance(items, dict):
        return []
    out: List[Dict[str, Any]] = []
    for key, raw in items.items():
        if not isinstance(raw, dict):
            continue
        out.append({
            "item": str(key),
            "state": str(raw.get("state") or "").strip().lower(),
            "owner": str(raw.get("owner") or "") or None,
            "pr": raw.get("pr") if isinstance(raw.get("pr"), int) else None,
        })
    return out


# ------------------------------------------------------------------------------------
# Who may read it (slice 2: the Telegram Mini App)
# ------------------------------------------------------------------------------------


def allowed_hosts(*, public_host: str = "") -> List[str]:
    """The ``Host`` values the server answers for: the loopback, plus the configured one.

    Binding the loopback is not enough on its own: a page served from a name that *resolves*
    to ``127.0.0.1`` is same-origin with this one, so its script could read ``/state.json``.
    Refusing every ``Host`` but these is what stops it (review, #256 — DNS rebinding).
    """
    hosts = list(LOOPBACK_HOSTS)
    public = str(public_host or "").strip().lower()
    if public:
        hosts.append(public)
        public_host_only = public.split(":", 1)[0]
        hosts.append(public_host_only)
    return hosts


def verify_init_data(raw: str, *, bot_token: str, admin_id: Any = 0,
                     now: Optional[float] = None, max_age: float = INIT_MAX_AGE
                     ) -> tuple[bool, str]:
    """``(ok, why not)`` for a Telegram Mini App's ``initData`` string.

    The check is Telegram's own (Web App docs): the payload is ``key=value`` pairs, and the
    ``hash`` field is ``HMAC-SHA256`` of the rest -- sorted, newline-joined -- under a secret
    that is itself ``HMAC-SHA256`` of the bot token keyed by ``"WebAppData"``. On top of that
    this refuses a signature older than *max_age* (a replayed one), one dated in the future,
    and any user that is not the ``admin_user_id``: the page is the admin's, so a member who
    somehow holds a valid payload still reads nothing.
    """
    moment = time.time() if now is None else float(now)
    text = str(raw or "").strip()
    if not text:
        return False, "no init data"
    if not bot_token:
        return False, "no bot token to check it against"
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=False)
    fields = dict(pairs)
    given = fields.pop("hash", "")
    if not given:
        return False, "no hash in the payload"
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        return False, "the hash does not match the payload"
    try:
        signed_at = float(fields.get("auth_date") or "")
    except (TypeError, ValueError):
        return False, "no auth_date"
    if max_age > 0 and moment - signed_at > max_age:
        return False, "the payload is older than %ds" % int(max_age)
    if signed_at - moment > 60:
        return False, "the payload is dated in the future"
    try:
        user = json.loads(fields.get("user") or "{}")
    except ValueError:
        return False, "the user field is unreadable"
    wanted = int(admin_id or 0)
    if not wanted:
        return False, "no admin_user_id to check against"
    if int(user.get("id") or 0) != wanted:
        return False, "not the admin"
    return True, "ok"


# ------------------------------------------------------------------------------------
# The page
# ------------------------------------------------------------------------------------


def page_html(*, title: str = "Swarm", every: float = 3.0) -> str:
    """The whole page: styles, markup and script inline, nothing fetched from anywhere else."""
    return (_PAGE
            .replace("__TITLE__", _escape(title))
            .replace("__EVERY_MS__", str(int(max(1.0, float(every)) * 1000)))
            .replace("__STATE_PATH__", STATE_PATH)
            .replace("__MEMORY_PATH__", MEMORY_PATH))


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>__TITLE__</title>
<style>
/* The design system (#343). Every colour is a token here, and the dark block defines every
   one of those names again, so no part of the page is left unstyled in either scheme. Spacing
   and type are tokens too, but they never change with the scheme. Contrast is WCAG AA or
   better against the surface each token is used on. */
:root {
  --bg: #f4f6f9; --card: #ffffff; --ink: #10151c; --dim: #4b5563; --line: #d7dde6;
  --accent: #1f5fd1; --ok: #14683f; --warn: #7a4f00; --bad: #b3261e; --frozen: #4c3aa8;
  --shadow: 0 1px 2px rgba(16, 21, 28, .07);
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 24px;
  --radius: 12px; --pill: 999px; --wrap: 1180px;
  --font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-small: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-tiny: 12px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-title: 600 19px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-head: 600 13px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
/* Two palettes, and three ways to be in one of them: the phone's own preference, what the
   Mini App's chat says (the data-theme attribute), and the chat's own colours (#347 review).
   An attribute is a *whole* palette, not a hint: an explicit one wins over the system's, so
   no token is ever left over from the other scheme. */
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --card: #161b22; --ink: #eef2f7; --dim: #a7b2c0; --line: #29313c;
    --accent: #8fbaff; --ok: #5ad597; --warn: #f0b429; --bad: #ff9a94; --frozen: #b8a9ff;
    --shadow: 0 1px 2px rgba(0, 0, 0, .5);
  }
}
:root[data-theme="dark"] {
  --bg: #0d1117; --card: #161b22; --ink: #eef2f7; --dim: #a7b2c0; --line: #29313c;
  --accent: #8fbaff; --ok: #5ad597; --warn: #f0b429; --bad: #ff9a94; --frozen: #b8a9ff;
  --shadow: 0 1px 2px rgba(0, 0, 0, .5);
}
:root[data-theme="light"] {
  --bg: #f4f6f9; --card: #ffffff; --ink: #10151c; --dim: #4b5563; --line: #d7dde6;
  --accent: #1f5fd1; --ok: #14683f; --warn: #7a4f00; --bad: #b3261e; --frozen: #4c3aa8;
  --shadow: 0 1px 2px rgba(16, 21, 28, .07);
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; padding: var(--space-3); background: var(--bg); color: var(--ink);
  font: var(--font); overflow-wrap: anywhere; }
.wrap { max-width: var(--wrap); margin: 0 auto; }

/* The header: the round, who coordinates, the lease, how the bots stand, what it costs. */
header.top { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: var(--space-3); margin-bottom: var(--space-3); }
.title { display: flex; align-items: baseline; gap: var(--space-2); flex-wrap: wrap; }
h1 { font: var(--font-title); margin: 0; }
.sub { color: var(--dim); font: var(--font-small); }
nav.tabs { display: flex; flex-wrap: wrap; gap: var(--space-1); margin-top: var(--space-3); }
nav.tabs a { text-decoration: none; color: var(--ink); border: 1px solid var(--line);
  border-radius: var(--pill); padding: 3px 10px; font: var(--font-small); }
nav.tabs a:hover, nav.tabs a:focus-visible { border-color: var(--accent); color: var(--accent); }
.health { display: flex; flex-wrap: wrap; gap: var(--space-1) var(--space-2);
  margin-top: var(--space-3); }
.pill { font: var(--font-small); color: var(--dim); border: 1px solid var(--line);
  border-radius: var(--pill); padding: 2px 9px; }
.pill b { color: var(--ink); font-weight: 600; }
.pill.bad { color: var(--bad); border-color: currentColor; }

main { display: grid; gap: var(--space-3); }
section.panel { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: var(--space-3); scroll-margin-top: var(--space-3); }
h2 { font: var(--font-head); text-transform: uppercase; letter-spacing: .05em; color: var(--dim);
  margin: 0 0 var(--space-2); }
.note { font: var(--font-small); color: var(--dim); margin: 0; }
.card { border: 1px solid var(--line); border-radius: var(--radius); padding: var(--space-2)
  var(--space-3); margin-bottom: var(--space-2); }
.card:last-child { margin-bottom: 0; }
.card .top { display: flex; align-items: baseline; gap: var(--space-2); flex-wrap: wrap; }
.alias { font-weight: 600; }
.badge { font: var(--font-tiny); font-weight: 600; padding: 1px 8px; border-radius: var(--pill);
  border: 1px solid currentColor; }
.badge.working { color: var(--accent); } .badge.idle { color: var(--ok); }
.badge.paused { color: var(--dim); } .badge.frozen { color: var(--frozen); }
.badge.stuck, .badge.due { color: var(--warn); } .badge.down { color: var(--bad); }
.card .doing { margin-top: var(--space-1); font: var(--font); }
.card .meta, .line, .binding { margin-top: var(--space-1); color: var(--dim);
  font: var(--font-small); display: flex; flex-wrap: wrap; gap: 2px var(--space-2); }
.line { margin-top: 0; padding: var(--space-1) 0; border-top: 1px solid var(--line); }
.line:first-of-type { border-top: 0; }
.ref { font-weight: 600; color: var(--ink); }
table { width: 100%; border-collapse: collapse; font: var(--font-small); }
th, td { text-align: right; padding: 5px 4px; border-top: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
thead th { border-top: 0; color: var(--dim); font: var(--font-tiny); text-transform: uppercase;
  letter-spacing: .04em; }
tfoot td { font-weight: 600; color: var(--ink); }
pre.memory { margin: var(--space-1) 0; padding: var(--space-2); background: var(--bg);
  border: 1px solid var(--line); border-radius: var(--radius); font: var(--font-small);
  white-space: pre-wrap; overflow-wrap: anywhere; }
footer { color: var(--dim); font: var(--font-tiny); text-align: center;
  padding: var(--space-4) 0 var(--space-2); }
@media (min-width: 760px) {
  body { padding: var(--space-5); }
  /* Two columns from tablet up: the swarm beside everything else. */
  main { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: start; }
  #swarm { grid-row: span 2; }
  #bots { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: var(--space-2); }
  #bots .card { margin-bottom: 0; }
}
</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div class="title">
    <h1>Swarm</h1>
    <span class="sub" id="stamp">loading&hellip;</span>
  </div>
  <nav class="tabs" id="tabs">
    <a href="#swarm">Swarm</a>
    <a href="#round">Round &amp; owed</a>
    <a href="#topics">Topics</a>
    <a href="#usage">Usage</a>
    <a href="#settings">Settings</a>
    <a href="#drift">Drift</a>
    <a href="#state">State</a>
    <a href="#memory">Memory</a>
    <a href="#controls">Controls</a>
  </nav>
  <div class="health" id="health"></div>
</header>

<main>
  <section class="panel" id="swarm">
    <h2>Swarm</h2>
    <div id="bots"></div>
  </section>

  <section class="panel" id="round">
    <h2>Round &amp; owed</h2>
    <div class="health" id="round-strip"></div>
    <div id="owed-rows"></div>
  </section>

  <section class="panel" id="topics">
    <h2>Topics</h2>
    <div id="topic-cards"></div>
  </section>

  <section class="panel" id="usage">
    <h2>Usage</h2>
    <div id="usage"></div>
  </section>

  <section class="panel" id="settings">
    <h2>Settings</h2>
    <div id="settings"></div>
  </section>

  <section class="panel" id="drift">
    <h2>Drift</h2>
    <div id="drift"></div>
  </section>

  <section class="panel" id="state">
    <h2>State</h2>
    <div id="state"></div>
  </section>

  <section class="panel" id="memory">
    <h2>Memory</h2>
    <div id="memory"></div>
  </section>

  <section class="panel" id="controls">
    <h2>Controls</h2>
    <p class="note" id="controls-note">Every control needs the admin's verified Mini App login
      and a confirm step. This page's own controls arrive with the next slice.</p>
  </section>
</main>

<footer>read-only &middot; refreshes every <span id="every"></span>s</footer>
</div>
<script>
const STATE = "__STATE_PATH__";
const MEMORY = "__MEMORY_PATH__";
const EVERY = __EVERY_MS__;
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const num = (value) => (value === null || value === undefined) ? "?" : String(value);
const compact = (value) => {
  if (value === null || value === undefined) return "?";
  if (value >= 1e6) return (value / 1e6).toFixed(1) + "M";
  if (value >= 1e3) return (value / 1e3).toFixed(1) + "k";
  return String(Math.round(value));
};
const ago = (seconds) => {
  if (seconds === null || seconds === undefined) return "?";
  const s = Math.max(0, seconds);
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 172800) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
};
const money = (value) => (value === null || value === undefined)
  ? "?" : "$" + Number(value).toFixed(2);

// One chip: a quiet label and its value in the ink colour.
function pill(label, value, cls) {
  const node = el("span", "pill" + (cls ? " " + cls : ""));
  if (label) node.append(el("span", "", label + " "));
  node.append(el("b", "", value));
  return node;
}

function health(state) {
  const box = document.getElementById("health");
  box.replaceChildren();
  box.append(pill("round", state.round
    ? (state.round.name || "?") + " " + state.round.done + "/" + state.round.total
    : "none"));
  box.append(pill("coordinator", state.coordinator || "none"));
  box.append(state.lease && state.lease.term
    ? pill("lease", "term " + state.lease.term +
        (state.lease.expires_in === null || state.lease.expires_in === undefined
          ? "" : ", " + ago(state.lease.expires_in) + " left"))
    : pill("lease", "no term"));
  const counts = {};
  (state.bots || []).forEach((bot) => {
    counts[bot.badge] = (counts[bot.badge] || 0) + 1;
  });
  const stand = ["working", "idle", "paused", "frozen", "stuck", "down"]
    .filter((name) => counts[name]).map((name) => name + " " + counts[name]);
  box.append(pill("bots", stand.length ? stand.join(" · ") : "none",
    counts.down ? "bad" : ""));
  const total = state.total || {};
  box.append(pill("today", num(total.turns) + " turns · " +
    compact(total.tokens_in) + "/" + compact(total.tokens_out) + " tok · " +
    money(total.cost)));
}

function card(bot) {
  const box = el("article", "card");
  const top = el("div", "top");
  top.append(el("span", "alias", bot.alias));
  top.append(el("span", "badge " + bot.badge, bot.badge));
  if (bot.role) top.append(el("span", "sub", bot.role));
  box.append(top);
  if (bot.state === "working" || bot.state === "stuck") {
    const turn = bot.turn_seconds === null ? "?" : ago(bot.turn_seconds);
    const cap = bot.turn_timeout === null ? "" : " / " + ago(bot.turn_timeout);
    box.append(el("div", "doing",
      "working on " + (bot.topic || "?") + " for " + turn + cap));
  } else {
    box.append(el("div", "doing", bot.badge + " for " + ago(bot.since)));
  }
  const meta = el("div", "meta");
  const model = bot.model ? (bot.model + (bot.effort ? "/" + bot.effort : "")) : "?";
  meta.append(el("span", "", "runtime " + model));
  if (bot.rev) meta.append(el("span", "", "rev " + bot.rev));
  meta.append(el("span", "", "pulse " + ago(bot.pulse_age) + " ago"));
  meta.append(el("span", "", "today " + num(bot.turns) + " turns"));
  meta.append(el("span", "", compact(bot.tokens_in) + "/" + compact(bot.tokens_out) + " tok"));
  meta.append(el("span", "", money(bot.cost)));
  if (bot.queue) meta.append(el("span", "", "queue " + bot.queue));
  box.append(meta);
  return box;
}

function topicCard(topic) {
  const box = el("article", "card");
  const top = el("div", "top");
  top.append(el("span", "alias", topic.topic || "?"));
  top.append(el("span", "badge " + (topic.stalled ? "stuck" : "idle"),
    topic.stalled ? "stalled" : "open"));
  if (topic.updated !== null && topic.updated !== undefined)
    top.append(el("span", "sub", "active " + ago(topic.updated) + " ago"));
  box.append(top);
  (topic.links || []).forEach((link) => {
    const line = el("div", "line");
    line.append(el("span", "ref", link.ref || link.name || link.id || "?"));
    // The title is shown beside a ref, but never twice: a link titled "#342" and reffed
    // "#342" is one thing, not two.
    if (link.name && link.name !== link.ref && link.name !== link.id)
      line.append(el("span", "sub", link.name));
    if (link.kind) line.append(el("span", "sub", link.kind));
    // A work item's status is on the card beside its phase: closed work must not read open.
    if (link.status) line.append(el("span", "badge " + (link.status === "open"
      ? "idle" : "paused"), link.status));
    if (link.phase) line.append(el("span", "badge " + (link.stalled ? "stuck" : "idle"),
      link.phase));
    if (link.stalled) line.append(el("span", "sub",
      "no phase change for " + ago(link.phase_age)));
    const who = (link.assignees || []).filter((a) => a.agent)
      .map((a) => a.agent + (a.as ? " " + a.as : ""));
    if (who.length) line.append(el("span", "sub", who.join(", ")));
    box.append(line);
  });
  (topic.bindings || []).forEach((binding) => {
    const line = el("div", "binding");
    line.append(el("span", "", binding.agent + (binding.role ? " " + binding.role : "")));
    line.append(el("span", "", "turns " + num(binding.turns)));
    if (binding.drift !== null && binding.drift !== undefined)
      line.append(el("span", "", "drift " + binding.drift));
    if (binding.rotation_due)
      line.append(el("span", "badge due", "rotation due: " + binding.rotation_due));
    box.append(line);
  });
  return box;
}

// What the swarm spent today, a row per bot and a total: the /usage answer, on the page.
function usage(state) {
  const box = document.getElementById("usage");
  box.replaceChildren();
  const bots = state.bots || [];
  if (!bots.length) {
    box.append(el("p", "note", "no bot has reported yet"));
    return;
  }
  const table = el("table", "");
  const head = el("thead", "");
  const headRow = el("tr", "");
  ["Bot", "Turns", "Tokens in", "Tokens out", "Cost"].forEach((name) => {
    headRow.append(el("th", "", name));
  });
  head.append(headRow);
  table.append(head);
  const body = el("tbody", "");
  bots.forEach((bot) => {
    const row = el("tr", "");
    row.append(el("td", "", bot.alias));
    row.append(el("td", "", num(bot.turns)));
    row.append(el("td", "", compact(bot.tokens_in)));
    row.append(el("td", "", compact(bot.tokens_out)));
    row.append(el("td", "", money(bot.cost)));
    body.append(row);
  });
  table.append(body);
  const total = state.total || {};
  const foot = el("tfoot", "");
  const footRow = el("tr", "");
  footRow.append(el("td", "", "swarm"));
  footRow.append(el("td", "", num(total.turns)));
  footRow.append(el("td", "", compact(total.tokens_in)));
  footRow.append(el("td", "", compact(total.tokens_out)));
  footRow.append(el("td", "", money(total.cost)));
  foot.append(footRow);
  table.append(foot);
  box.append(table);
}

function roundBoard(state) {
  const row = document.getElementById("round-strip");
  row.replaceChildren();
  if (state.round) {
    row.append(pill("round", (state.round.name || "?") + ": " +
      state.round.done + "/" + state.round.total + " done"));
    (state.round.items || []).forEach((item) => {
      row.append(pill(item.item, (item.state || "?") +
        (item.owner ? " · " + item.owner : "")));
    });
  } else {
    row.append(el("span", "note", "no round in flight"));
  }
  const rows = document.getElementById("owed-rows");
  rows.replaceChildren();
  if (!(state.owed || []).length) {
    rows.append(el("p", "note", "nothing owed"));
    return;
  }
  state.owed.forEach((debt) => {
    const line = el("div", "line");
    line.textContent = (debt.debtor || "?") + " owes " + (debt.owes || "?") +
      (debt.item ? " on " + debt.item : "") +
      (debt.waiting_on ? " (waiting: " + debt.waiting_on + ")" : "") +
      " · open " + ago(debt.open_seconds) +
      (debt.stalls ? " · stall " + debt.stalls : "");
    rows.append(line);
  });
}

// /model, /reasoning and /role: what each bot runs, and what a topic overrides (slice 3).
function settings(state) {
  const box = document.getElementById("settings");
  box.replaceChildren();
  const view = state.settings;
  if (!view || !view.ready) {
    box.append(el("p", "note", "settings are not being read on this bot"));
    return;
  }
  (view.bots || []).forEach((bot) => {
    const line = el("div", "line");
    line.append(el("span", "ref", bot.alias || "?"));
    if (bot.role) line.append(el("span", "sub", bot.role));
    line.append(el("span", "sub", "model " + (bot.model || "?")));
    line.append(el("span", "sub", "effort " + (bot.effort || "?")));
    box.append(line);
  });
  (view.topics || []).forEach((row) => {
    const line = el("div", "line");
    line.append(el("span", "ref", row.topic || "?"));
    line.append(el("span", "sub", (row.agent || "?") + (row.role ? " " + row.role : "")));
    line.append(el("span", "sub", "model " + (row.model || "?")));
    line.append(el("span", "sub", "effort " + (row.effort || "?")));
    line.append(el("span", "sub", "in this topic"));
    box.append(line);
  });
  if (!(view.bots || []).length && !(view.topics || []).length)
    box.append(el("p", "note", "no bot has reported a setting yet"));
}

// /drift: the score, and the newest rule breaks behind it -- kinds and weights, never evidence.
function drift(state) {
  const box = document.getElementById("drift");
  box.replaceChildren();
  const view = state.drift;
  if (!view || !view.ready) {
    box.append(el("p", "note", (view && view.text) || "the drift ledger is not read here"));
    return;
  }
  const head = el("div", "health");
  head.append(pill("score", num(view.score)));
  head.append(pill("hard", num(view.hard)));
  head.append(pill("resets today", num(view.resets_today)));
  head.append(pill("standing", view.reset ? "a reset is due"
    : (view.remind ? "a reminder rides the next turn" : "nothing due")));
  box.append(head);
  if (!(view.recent || []).length) {
    box.append(el("p", "note", "no live violations"));
    return;
  }
  view.recent.forEach((row) => {
    const line = el("div", "line");
    line.append(el("span", "ref", row.kind || "?"));
    line.append(el("span", "sub", "+" + num(row.severity)));
    line.append(el("span", "sub", ago(row.age) + " ago"));
    box.append(line);
  });
}

// /state: which backend, which mode, and whether it is readable.
function storeState(state) {
  const box = document.getElementById("state");
  box.replaceChildren();
  const view = state.store;
  if (!view) {
    box.append(el("p", "note", "the state store is not read here"));
    return;
  }
  const head = el("div", "health");
  head.append(pill("backend", view.backend || "off"));
  head.append(pill("mode", view.mode || "?"));
  head.append(pill("routing", view.routing || "?"));
  head.append(pill("owed", view.owed || "?"));
  head.append(pill("store", view.enabled ? (view.ready ? "ready" : "not ready") : "off",
    view.enabled && !view.ready ? "bad" : ""));
  box.append(head);
  if (view.note) box.append(el("p", "note", view.note));
}

// /memory: this bot's own notes. It is a separate, admin-only request -- opened without the
// Mini App's login it says so instead of showing anything.
async function memory() {
  const box = document.getElementById("memory");
  box.replaceChildren();
  if (!INIT) {
    box.append(el("p", "note", "the admin's login is needed to read a bot's memory"));
    return;
  }
  try {
    const response = await fetch(MEMORY, {cache: "no-store",
                                          headers: {"X-Telegram-Init-Data": INIT}});
    if (!response.ok) throw new Error("status " + response.status);
    const view = await response.json();
    if (!view.ready) {
      box.append(el("p", "note", "this bot keeps no memory yet"));
      return;
    }
    const pre = el("pre", "memory", view.text);
    box.append(pre);
    box.append(el("p", "note", num(view.bytes) + " bytes"));
  } catch (err) {
    box.append(el("p", "note", "memory unavailable: " + err.message));
  }
}

function render(state) {
  document.getElementById("stamp").textContent =
    "refreshed " + state.refreshed +
    (state.coordinator ? " · coordinator " + state.coordinator : " · no coordinator");
  health(state);

  const bots = document.getElementById("bots");
  bots.replaceChildren();
  (state.bots || []).forEach((bot) => bots.append(card(bot)));

  roundBoard(state);

  const cards = document.getElementById("topic-cards");
  cards.replaceChildren();
  if (!state.registry) {
    cards.append(el("p", "note", "the registry is not read on this bot"));
  } else if (!state.registry.ready) {
    cards.append(el("p", "note", "registry not ready"));
  } else if (!(state.registry.topics || []).length) {
    cards.append(el("p", "note", "no open topics yet"));
  } else {
    state.registry.topics.forEach((topic) => cards.append(topicCard(topic)));
  }

  usage(state);
  settings(state);
  drift(state);
  storeState(state);
}

// Opened as a Telegram Mini App, the page arrives with initData in its URL hash. It is sent
// back on every poll so the server can check it; opened locally there is none, and none is
// asked for. Nothing here loads Telegram's script: the page stays self-contained (#242).
const INIT = (() => {
  const match = location.hash.match(/tgWebAppData=([^&]*)/);
  return match ? decodeURIComponent(match[1]) : "";
})();

// A Mini App also hands its chat's theme in the hash: which scheme it is, and some of its
// colours. Nothing is loaded to read them.
//
// The scheme becomes the data-theme attribute, which is a *whole* palette in the stylesheet --
// the six tokens the chat does not send (line, ok, warn, bad, frozen, shadow) come from that
// palette, so a status badge never ends up in the other scheme's colours (review, #347). The
// colours the chat does send are set on top, as inline overrides.
const THEME_PARAMS = (() => {
  const match = location.hash.match(/tgWebAppThemeParams=([^&]*)/);
  if (!match) return {};
  try {
    return JSON.parse(decodeURIComponent(match[1])) || {};
  } catch (err) {
    return {};
  }
})();

// #rrggbb -> how light it is, so a chat that sends colours but no scheme still gets the right
// palette. A colour that is not 6-digit hex says nothing, and the system's preference stands.
function luma(color) {
  const hex = String(color || "").trim().replace("#", "");
  if (hex.length !== 6) return null;
  const parts = [0, 2, 4].map((at) => parseInt(hex.slice(at, at + 2), 16) / 255);
  if (parts.some((part) => Number.isNaN(part))) return null;
  return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2];
}

const SCHEME = (() => {
  const named = (location.hash.match(/tgWebAppColorScheme=(dark|light)/) || [])[1];
  if (named) return named;
  const light = luma(THEME_PARAMS.bg_color);
  return light === null ? "" : (light < 0.45 ? "dark" : "light");
})();
document.documentElement.dataset.theme = SCHEME;
[["bg_color", "--bg"], ["secondary_bg_color", "--card"], ["text_color", "--ink"],
 ["hint_color", "--dim"], ["button_color", "--accent"]].forEach(([key, token]) => {
  if (THEME_PARAMS[key]) document.documentElement.style.setProperty(token, THEME_PARAMS[key]);
});

async function tick() {
  try {
    const headers = INIT ? {"X-Telegram-Init-Data": INIT} : {};
    const response = await fetch(STATE, {cache: "no-store", headers});
    if (!response.ok) throw new Error("status " + response.status);
    render(await response.json());
  } catch (err) {
    document.getElementById("stamp").textContent = "dashboard unavailable: " + err.message;
  }
}

document.getElementById("every").textContent = String(EVERY / 1000);
tick();
setInterval(tick, EVERY);
memory();
setInterval(memory, EVERY);
</script>
</body>
</html>
"""


# ------------------------------------------------------------------------------------
# The server
# ------------------------------------------------------------------------------------


def action_answer(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """A write's answer, read by name: what ran, how it went, the words, the confirm code.

    Nothing else reaches the wire. The *text* is the policy's own answer -- the same words the
    Telegram button gives -- so a page shows exactly what a presser would have read, and no
    field here is ever an id or a path.
    """
    ok = bool(raw.get("ok"))
    out: Dict[str, Any] = {
        "ok": ok,
        "verdict": str(raw.get("verdict") or ("ok" if ok else "refused"))[:24],
        "text": str(raw.get("text") or "")[:2000],
    }
    for name in ("refused", "action", "target", "confirm"):
        value = str(raw.get(name) or "").strip()
        if value:
            out[name] = value[:64]
    return out


class DashboardServer:
    """A read-only HTTP server for the page, on the loopback, for as long as it is started.

    ``provider`` is called once per request and returns the JSON-able state; anything it
    raises is a 503 with a JSON body, never a traceback on the wire. The server owns one
    daemon thread, so a stuck request cannot hold the transport's shutdown.

    ``action`` is the one write path (#342). It is a plain callable -- the transport hands the
    request to its own dispatch -- so this module stays free of asyncio, and a server without
    one is read-only exactly as before.
    """

    def __init__(self, provider: Callable[[], Mapping[str, Any]], *, port: int = 0,
                 host: str = HOST, title: str = "Swarm", every: float = 3.0,
                 public_host: str = "", bot_token: str = "", admin_id: Any = 0,
                 init_max_age: float = INIT_MAX_AGE,
                 action: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None,
                 memory: Optional[Callable[[], Optional[str]]] = None) -> None:
        self.provider = provider
        self.host = host
        self.requested_port = int(port)
        self.title = title
        self.every = every
        #: The write path (#342): ``{action, target, value, code}`` in, ``{ok, text, …}`` out.
        #: ``None`` means no controls are wired, and a POST stays a 405.
        self.action = action
        #: The name the page is reached under behind a tunnel (#240 slice 2). Empty means the
        #: server only answers for the loopback, and every request is local.
        self.public_host = str(public_host or "").strip().lower()
        #: The Mini App's ``initData`` is checked against these. Empty token or admin means
        #: "no public host", which never asks for one -- and, since #342, no write either.
        self.bot_token = str(bot_token or "")
        self.admin_id = admin_id
        self.init_max_age = float(init_max_age)
        #: ``/memory``'s text (#344), served only to a verified admin login. ``None`` means this
        #: page has no memory to show, and that path stays a 404.
        self.memory = memory
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- reading it ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """Whether the server is up. The thread's own liveness counts: a serve thread that
        died would otherwise be reported as "serving" forever (review, #256)."""
        return bool(self._httpd is not None and self._thread is not None
                    and self._thread.is_alive())

    @property
    def requires_init(self) -> bool:
        """A page reachable under a public name is the admin's alone: it asks for ``initData``."""
        return bool(self.public_host)

    def needs_init(self, host: str) -> bool:
        """Whether a request from *host* must carry ``initData``.

        A loopback request never does: the operator on the host itself is not a stranger, and
        turning the Mini App on must not take away the plain local page (review, #258). The
        ``Host`` check still applies to it, so this is not a hole in the rebinding rule.
        """
        if not self.requires_init:
            return False
        name = str(host or "").strip().lower()
        name = name.rsplit(":", 1)[0] if name.count(":") == 1 else name
        return name not in LOOPBACK_HOSTS

    @property
    def hosts(self) -> List[str]:
        return allowed_hosts(public_host=self.public_host)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1]) if self._httpd is not None else 0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{PAGE_PATH}"

    def state(self) -> Dict[str, Any]:
        """The provider's answer, as a plain dict: what the JSON endpoint serves."""
        return dict(self.provider() or {})

    # -- running it ------------------------------------------------------------------
    def start(self) -> str:
        """Bind the port and serve in a background thread. Returns the page's URL."""
        if self._httpd is not None:
            return self.url
        httpd = ThreadingHTTPServer((self.host, self.requested_port), self._handler())
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="swarm-web-dashboard",
                                        daemon=True)
        self._thread.start()
        return self.url

    def stop(self, *, timeout: float = 0.5) -> None:
        """Close the port. Bounded, because this can run on the event loop (#256 review).

        ``shutdown()`` waits for the serve loop to notice, and the join waits for the thread;
        both are capped so a failover or a shutdown cannot stall the transport behind a
        request that is mid-flight. A thread that outlives the timeout is a daemon and dies
        with the process.
        """
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def __enter__(self) -> "DashboardServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- the protocol ----------------------------------------------------------------
    def _handler(self) -> type:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "swarmly-dashboard"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:   # never on stderr
                return

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # The page is self-contained, so nothing but itself may be loaded.
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; connect-src 'self'")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _not_found(self) -> None:
                self._send(404, b'{"error":"not found"}', "application/json")

            def _host_ok(self) -> bool:
                """Whether the request's ``Host`` is one this server answers for (#256 review).

                A page on a name that resolves to ``127.0.0.1`` is *same-origin* with this one,
                so it could read ``/state.json``; the bind address cannot tell them apart, and
                only the ``Host`` header can. Anything else is a 404 -- the same answer an
                unknown path gets, so the page's existence is not confirmed either.
                """
                given = str(self.headers.get("Host", "") or "").strip().lower()
                name = given.rsplit(":", 1)[0] if given.count(":") == 1 else given
                if server.requires_init and given in server.hosts:
                    return True
                return name in server.hosts or given in server.hosts

            def _admin_ok(self) -> bool:
                """Whether this request carries a valid admin ``initData``, wherever it came from.

                The state has a loopback exception (#258: the operator on the host is not a
                stranger); the memory does not (#344) -- so the check is repeated here rather
                than reusing the one that may have answered "yes" for a local request.
                """
                raw = str(self.headers.get(INIT_HEADER, "") or "")
                ok, why = verify_init_data(raw, bot_token=server.bot_token,
                                           admin_id=server.admin_id,
                                           now=time.time(), max_age=server.init_max_age)
                if not ok:
                    logger.info("web dashboard: refused a memory read (%s)", why)
                return ok

            def _init_ok(self, query: str) -> bool:
                """Whether this request carries a valid Mini App ``initData`` for the admin."""
                if not server.needs_init(self.headers.get("Host", "")):
                    return True
                raw = str(self.headers.get(INIT_HEADER, "") or "")
                if not raw:
                    for key, value in parse_qsl(query, keep_blank_values=True):
                        if key == "init":
                            raw = value
                ok, why = verify_init_data(raw, bot_token=server.bot_token,
                                           admin_id=server.admin_id,
                                           now=time.time(), max_age=server.init_max_age)
                if not ok:
                    logger.info("web dashboard: refused a request from %s (%s)",
                                self.headers.get("Host", "?"), why)
                return ok

            def do_GET(self) -> None:                       # noqa: N802 - http.server's name
                path, _, query = self.path.partition("?")
                if not self._host_ok():
                    self._not_found()
                    return
                if path in (PAGE_PATH, "/index.html"):
                    body = page_html(title=server.title, every=server.every)
                    self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path == MEMORY_PATH:
                    # Always behind the login, loopback included (#344): a bot's own notes are
                    # its private working memory, and the path only answers when the transport
                    # wired one at all.
                    if server.memory is None:
                        self._not_found()
                        return
                    if not self._admin_ok():
                        self._send(403, json.dumps(
                            {"ready": False, "refused": "auth",
                             "text": "the admin's login is needed to read this"}).encode("utf-8"),
                            "application/json")
                        return
                    try:
                        text = server.memory()
                    except Exception:                       # noqa: BLE001 - never on the wire
                        self._send(503, b'{"ready":false,"text":"unavailable"}',
                                   "application/json")
                        return
                    body = json.dumps(memory_view(text=text, alias=server.title),
                                      sort_keys=True, default=str)
                    self._send(200, body.encode("utf-8"), "application/json")
                    return
                if path == STATE_PATH:
                    if not self._init_ok(query):
                        self._send(403, b'{"error":"not the admin"}', "application/json")
                        return
                    try:
                        payload = server.state()
                    except Exception:                       # noqa: BLE001 - never on the wire
                        self._send(503, b'{"error":"unavailable"}', "application/json")
                        return
                    body = json.dumps(payload, sort_keys=True, default=str)
                    self._send(200, body.encode("utf-8"), "application/json")
                    return
                self._not_found()

            def do_HEAD(self) -> None:                      # noqa: N802
                self.do_GET()

            # -- the write path (#342) ---------------------------------------------------
            def _origin_ok(self) -> bool:
                """Whether a write comes from this page and nowhere else.

                A browser sends ``Origin`` on every cross-site ``POST``; a name that is not one
                this server answers for is another site's form or fetch, and it is refused
                before anything else happens. A request with no ``Origin`` (a script, a test)
                still faces the ``initData`` check below, which is the real door.
                """
                given = str(self.headers.get("Origin", "") or "").strip().lower()
                if not given:
                    return True
                name = urlsplit(given).netloc.lower()
                bare = name.rsplit(":", 1)[0] if name.count(":") == 1 else name
                return name in server.hosts or bare in server.hosts

            def _write(self, code: int, payload: Mapping[str, Any]) -> None:
                self._send(code, json.dumps(action_answer(payload), sort_keys=True)
                           .encode("utf-8"), "application/json")

            def do_POST(self) -> None:                      # noqa: N802
                path = self.path.partition("?")[0]
                if not self._host_ok():
                    self._not_found()
                    return
                if path != ACTION_PATH:
                    self._read_only()                       # every other path stays read-only
                    return
                if server.action is None:
                    self._read_only()                       # no controls wired: still a 405
                    return
                if not self._origin_ok():
                    self._write(403, {"ok": False, "refused": "origin",
                                      "text": "that origin may not write here"})
                    return
                # Every write needs a verified admin Mini App login -- the loopback included
                # (#342). The plain local page stays read-only, and a write is never weaker
                # than the state read the same page already has to pass.
                if server.init_max_age <= 0:
                    # A write fails closed: the age check is what makes a captured payload
                    # useless, and config refuses a window of 0 anyway (review, #347).
                    logger.info("web dashboard: refused a write (no init_max_age window)")
                    self._write(403, {"ok": False, "refused": "auth",
                                      "text": "this page has no initData window to check"})
                    return
                ok, why = verify_init_data(str(self.headers.get(INIT_HEADER, "") or ""),
                                          bot_token=server.bot_token,
                                          admin_id=server.admin_id,
                                          now=time.time(), max_age=server.init_max_age)
                if not ok:
                    logger.info("web dashboard: refused a write (%s)", why)
                    self._write(403, {"ok": False, "refused": "auth", "text": why})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = -1
                if length < 0 or length > MAX_BODY_BYTES:
                    self._write(400, {"ok": False, "refused": "body",
                                      "text": "a write is a small JSON object"})
                    return
                try:
                    asked = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._write(400, {"ok": False, "refused": "body",
                                      "text": "the body is not JSON"})
                    return
                if not isinstance(asked, dict):
                    self._write(400, {"ok": False, "refused": "body",
                                      "text": "the body is not a JSON object"})
                    return
                try:
                    answer = server.action(asked)
                except Exception:                       # noqa: BLE001 - never on the wire
                    logger.warning("web dashboard: the action failed", exc_info=True)
                    self._write(503, {"ok": False, "refused": "failed",
                                      "text": "the action failed; the log has the reason"})
                    return
                self._write(200, dict(answer or {}))

            def _read_only(self) -> None:
                self._send(405, b'{"error":"read-only"}', "application/json")

            do_PUT = do_PATCH = do_DELETE = _read_only      # noqa: N815

        return Handler
