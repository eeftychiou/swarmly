"""Durable round state on the bus (issue #86).

A round -- the tracking issue, its items, who owns each one, the verdicts and the open
WAKE timers -- outlives any one bot's memory: a successor (#83) has to rebuild it from
the bus and GitHub alone, after the coordinator's process is gone.

Three decisions shape this module.

**Append-only, never one mutable file.** The bus transport retries a push that lost a
race onto the remote's tip (:mod:`swarmly.transports.git`), so two writers in the same
window both land and the later bytes win. One shared file would then lose an entire
update silently -- the reviewer's "one mutable state file loses updates on gitbus". Each
write here is its own file under ``.agents/rounds/<round>/``, named
``<term>-<seq>-<writer>-<nonce>.json``, so concurrent writers never overwrite each other
and a lost push costs one segment, not the state. Reading folds the segments in a
deterministic order, so every bot that reads the same bus computes the same state.

**Missing is not empty.** A state that cannot be read is ``absent`` or ``unreadable``,
never "no open items" -- an empty round would otherwise post a round report for work
that is still running. Callers must look at :attr:`RoundState.status`.

**A verdict is bound to a head.** A review or QA verdict carries the SHA it was given
on. It is *cleared* -- not carried, not marked stale-and-usable -- the moment the head
moves: reading a verdict for a different head returns ``None``.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from swarmly.transports.base import ROUNDS_RELPATH, is_bus_document

logger = logging.getLogger("swarmly.telegram.rounds")

#: The schema version of one segment. A reader refuses what it does not know.
VERSION = 1

#: The default ceiling on a segment's payload, which travels inside the coordinator's reply.
#: It is a setting (``limits.round_state_chars``) because the right number is how big a real
#: round is: a six-item round with heads and verdicts is ~2,000 characters, and a cap below
#: that refuses exactly the rounds this exists for (PR #89 review, B3).
MAX_BLOCK_CHARS = 8000

#: ``ROUND_STATE {...}`` on its own line, the block a coordinator reply carries.
BLOCK_MARKER = "ROUND_STATE"

#: The kinds of verdict an item carries.
VERDICT_KINDS = ("review", "qa")

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class RoundStateError(ValueError):
    """A segment, a block or a round state is not usable."""


class RoundStaleTerm(RoundStateError):
    """A write from an older coordinator term: it must not overwrite a newer one."""


class RoundUnauthorised(RoundStateError):
    """This bot may not speak for the round: no authority for it, or it is not the one."""


class RoundFenced(RoundStateError):
    """Another bot holds this round at this term: this write is not this bot's to make."""


class RoundPushError(RoundStateError):
    """The segment was written but could not be pushed to the bus."""

    def __init__(self, message: str, path: Optional[str] = None):
        super().__init__(message)
        self.path = path


@dataclass(frozen=True)
class Authority:
    """Who the swarm says the coordinator is, and where that came from.

    The first claim on a round that does not exist yet is the one write that cannot be fenced
    against an earlier holder, so it needs an authority: the alias the swarm names as the
    coordinator, with its term from the lease. An empty alias or expired/absent lease means
    "nobody knows", and that fails closed.
    """

    alias: str = ""
    source: str = "lease"
    term: int = 0


def utc_now(clock: Optional[Any] = None) -> str:
    """An ISO-8601 UTC stamp. *clock* is a wall-clock callable (tests inject one)."""
    moment = clock() if clock is not None else datetime.datetime.now(
        datetime.timezone.utc).timestamp()
    return datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def normalize_sha(value: Any, head: Optional[str] = None) -> str:
    """A full, lower-case commit SHA.

    A 7..40 character hex prefix is expanded to ``head`` when it is a prefix of it, and
    refused otherwise: an ambiguous or unknown prefix must never be treated as a match,
    or a verdict on one head would validate another.
    """
    if not isinstance(value, str):
        raise RoundStateError(f"not a sha: {value!r}")
    text = value.strip().lower()
    if not _SHA_RE.match(text):
        raise RoundStateError(f"not a sha: {value!r}")
    if len(text) == 40:
        return text
    if head and head.strip().lower().startswith(text):
        return head.strip().lower()
    raise RoundStateError(f"{value!r} is not the head (or is too short to identify it)")


# ------------------------------------------------------------------------------------
# One segment
# ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """A verdict by one bot on one head."""

    verdict: str
    sha: str
    by: str = ""
    at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "sha": self.sha, "by": self.by, "at": self.at}

    @classmethod
    def from_dict(cls, data: Any, *, head: Optional[str] = None) -> "Verdict":
        if not isinstance(data, dict):
            raise RoundStateError(f"a verdict must be an object, got {type(data).__name__}")
        verdict = data.get("verdict")
        if not isinstance(verdict, str) or not verdict.strip():
            raise RoundStateError("a verdict needs a non-empty 'verdict'")
        sha = normalize_sha(data.get("sha"), head)
        by = data.get("by") or ""
        at = data.get("at") or ""
        if not isinstance(by, str) or not isinstance(at, str):
            raise RoundStateError("a verdict's 'by' and 'at' must be strings")
        return cls(verdict=verdict.strip(), sha=sha, by=by, at=at)


@dataclass
class RoundUpdate:
    """One append-only segment: the fields this turn learned or changed.

    Every field is optional because a turn usually changes one item, one verdict or one
    blocker. What a segment does not carry, the fold leaves as it was.
    """

    round: str
    writer: str
    term: int = 0
    seq: int = 0
    at: str = ""
    version: int = VERSION
    repo: str = ""
    items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    order: Optional[List[str]] = None
    blockers: Optional[List[str]] = None
    timers: Optional[Dict[str, Dict[str, Any]]] = None
    note: str = ""
    #: The head the segment was written against, when the writer knows it. Verdict SHAs in
    #: this segment are expanded against it.
    head: str = ""
    #: The ``seq`` the writer saw when it built this segment -- a Lamport base. Two segments
    #: with the same term and the same base are concurrent, and if they set the same field
    #: the fold reports that instead of picking a winner silently (PR #89 review, blocker 2).
    base_seq: int = 0
    #: The bot this segment claims the round for. Set by the store, never by the block.
    holder: str = ""

    # -- validation ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "version": self.version, "round": self.round, "writer": self.writer,
            "term": self.term, "seq": self.seq, "at": self.at,
        }
        if self.repo:
            data["repo"] = self.repo
        if self.items:
            data["items"] = self.items
        if self.order is not None:
            data["order"] = self.order
        if self.blockers is not None:
            data["blockers"] = self.blockers
        if self.timers is not None:
            data["timers"] = self.timers
        if self.note:
            data["note"] = self.note
        if self.head:
            data["head"] = self.head
        if self.base_seq:
            data["base_seq"] = self.base_seq
        if self.holder:
            data["holder"] = self.holder
        return data

    @classmethod
    def from_dict(cls, data: Any, *, writer: str = "", seq: Optional[int] = None,
                  term: Optional[int] = None, at: str = "",
                  head: Optional[str] = None) -> "RoundUpdate":
        """A strict parse: an unknown *field* is ignored, a wrong *type* is refused.

        ``writer``, ``seq`` and ``term`` default to the transport's own values when the
        block does not carry them -- the writer must never take them from the model's text.
        """
        if not isinstance(data, dict):
            raise RoundStateError(f"a round state must be an object, got {type(data).__name__}")
        version = data.get("version", VERSION)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise RoundStateError(f"bad version: {version!r}")
        if version > VERSION:
            raise RoundStateError(f"version {version} is newer than this bot reads ({VERSION})")
        round_key = data.get("round")
        if isinstance(round_key, int) and not isinstance(round_key, bool):
            round_key = str(round_key)
        if not isinstance(round_key, str) or not round_key.strip():
            raise RoundStateError("a round state needs 'round' (the tracking issue number)")
        repo = data.get("repo") or ""
        if not isinstance(repo, str):
            raise RoundStateError("'repo' must be a string")
        items_raw = data.get("items")
        if items_raw is None:
            items_raw = {}
        if not isinstance(items_raw, dict):
            raise RoundStateError("'items' must be an object keyed by item")
        items: Dict[str, Dict[str, Any]] = {}
        for key, patch in items_raw.items():
            if not isinstance(patch, dict):
                raise RoundStateError(f"item {key!r} must be an object")
            items[str(key)] = _item_patch(str(key), patch, head=head)
        order = data.get("order")
        if order is not None and not isinstance(order, list):
            raise RoundStateError("'order' must be a list")
        blockers = data.get("blockers")
        if blockers is not None and (not isinstance(blockers, list)
                                     or not all(isinstance(b, str) for b in blockers)):
            raise RoundStateError("'blockers' must be a list of strings")
        timers = data.get("timers")
        if timers is not None:
            if not isinstance(timers, dict):
                raise RoundStateError("'timers' must be an object")
            for key, timer in timers.items():
                if not isinstance(timer, dict):
                    raise RoundStateError(f"timer {key!r} must be an object")
        note = data.get("note") or ""
        if not isinstance(note, str):
            raise RoundStateError("'note' must be a string")
        return cls(
            round=round_key.strip(), writer=writer, term=int(term or 0),
            seq=int(seq or 0), at=at or utc_now(), version=version, repo=repo,
            items=items, order=[str(x) for x in order] if order is not None else None,
            blockers=list(blockers) if blockers is not None else None,
            timers=dict(timers) if timers is not None else None, note=note,
            head=head or "",
        )

    def sort_key(self) -> Tuple[int, int, str, str]:
        """Deterministic order: term, then seq, then time, then writer.

        Two bots that write in the same window can pick the same ``seq``; the writer name
        makes the winner identical everywhere instead of "whichever arrived last".
        """
        return (self.term, self.seq, self.at, self.writer.lower())


def _item_patch(key: str, patch: Dict[str, Any], *, head: Optional[str]) -> Dict[str, Any]:
    """One item's patch, validated. Verdicts are normalized against *head*."""
    clean: Dict[str, Any] = {}
    for name in ("issue", "pr"):
        if name in patch:
            value = patch[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise RoundStateError(f"item {key!r}: '{name}' must be a positive number")
            clean[name] = value
    for name in ("owner", "waits_on", "ci", "note", "state"):
        if name in patch:
            value = patch[name]
            if not isinstance(value, str):
                raise RoundStateError(f"item {key!r}: '{name}' must be a string")
            clean[name] = value
    if "head" in patch and patch["head"]:
        clean["head"] = normalize_sha(patch["head"], head)
    for kind in VERDICT_KINDS:
        if kind in patch:
            value = patch[kind]
            clean[kind] = None if value is None else Verdict.from_dict(
                value, head=head).to_dict()
    # Anything else is ignored on purpose: a colleague's newer block must not be refused
    # here for a field a newer reader will understand.
    return clean


# ------------------------------------------------------------------------------------
# The block in a reply
# ------------------------------------------------------------------------------------


def extract_block(text: str, *, max_chars: int = MAX_BLOCK_CHARS
                  ) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Split a coordinator reply into ``(text, payload, reason)``.

    The block is removed from the text in every case -- an unusable block is never posted
    into the channel as JSON. ``payload`` is ``None`` when the block is absent, too large,
    or not a JSON object; ``reason`` then says which, and the caller keeps the previous
    state and marks it stale.
    """
    if not text or BLOCK_MARKER not in text:
        return text, None, None
    rest = text
    blocks: List[str] = []
    reason: Optional[str] = None
    while True:
        found = _next_block(rest, max_chars=max_chars)
        if found is None:
            break
        before, raw, after, why = found
        blocks.append(raw if raw is not None else "")
        if why and reason is None:
            reason = why
        # Join the text around the removed block without leaving blank lines behind.
        rest = (before.rstrip() + "\n" + after.lstrip("\n")) if before.strip() \
            else after.lstrip("\n")
    if not blocks:
        return text, None, None
    # Every block is removed, always: one must never reach the channel as JSON.
    stripped = rest.strip()
    if len(blocks) > 1:
        return stripped, None, (f"{len(blocks)} state blocks in one reply; "
                                "exactly one is allowed")
    if reason:
        return stripped, None, reason
    raw = blocks[0]
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return stripped, None, f"the block is not valid JSON ({exc})"
    if not isinstance(payload, dict):
        return stripped, None, "the block is not a JSON object"
    return stripped, payload, None


def _next_block(text: str, *, max_chars: int = MAX_BLOCK_CHARS
                ) -> Optional[Tuple[str, Optional[str], str, Optional[str]]]:
    """``(before, raw, after, reason)`` for the next block in *text*, or None.

    A block is stripped even when it cannot be used, so the caller never has to decide
    between posting JSON and losing text.
    """
    match = re.search(r"(?m)^[ \t]*" + BLOCK_MARKER + r"[ \t]*", text)
    if match is None:
        return None
    start = match.start()
    brace = text.find("{", start)
    if brace == -1:
        return text[:start], None, text[match.end():], "the block carried no object"
    end = _matching_brace(text, brace)
    if end == -1:
        return text[:start], None, "", "the block's object was not closed"
    raw = text[brace:end + 1]
    after = text[end + 1:]
    if len(raw) > max_chars:
        return text[:start], None, after, (
            f"state too large ({len(raw)} > {max_chars} chars)")
    return text[:start], raw, after, None


def _matching_brace(text: str, start: int) -> int:
    """The index of the ``}`` closing the ``{`` at *start*, or -1.

    String literals and escapes are tracked, so a brace inside a note does not end the
    object early.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


# ------------------------------------------------------------------------------------
# The folded state
# ------------------------------------------------------------------------------------


@dataclass
class RoundState:
    """What the bus says about one round. Read ``status`` before trusting the rest."""

    round: str
    #: ``ok``: folded from at least one segment. ``absent``: nothing on the bus yet.
    #: ``unreadable``: the bus could not be read -- never treat this as an empty round.
    status: str = "absent"
    reason: str = ""
    version: int = VERSION
    repo: str = ""
    term: int = 0
    seq: int = 0
    writer: str = ""
    updated_at: str = ""
    items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    timers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    note: str = ""
    #: How many segments the fold used; 0 for absent.
    segments: int = 0
    #: Where the segments came from: ``ref`` (the bus, durable) or ``local`` (this checkout
    #: only -- a push that has not landed yet, or a bus ref that could not be read).
    source: str = ""
    #: Segments that are on this checkout but not on the bus ref yet.
    pending: int = 0
    #: Fields two concurrent writers set from the same base, as ``item.field``.
    conflicts: List[str] = field(default_factory=list)
    #: The bot that holds this round (the coordinator that first wrote it, at ``term``).
    holder: str = ""
    #: Why this state is not to be trusted, when it is not.
    stale: str = ""

    @property
    def usable(self) -> bool:
        """Whether the state can be acted on: read from the bus, and free of conflicts."""
        return self.status == "ok" and not self.stale and self.source == "ref"

    def item(self, key: str) -> Dict[str, Any]:
        return dict(self.items.get(str(key)) or {})

    def verdict(self, key: str, kind: str, head: Optional[str] = None) -> Optional[Verdict]:
        """The verdict for item *key* of *kind*, or ``None``.

        With *head* given, a verdict on any other head is ``None``: a new head clears the
        verdict instead of carrying an approval onto code nobody reviewed.
        """
        entry = self.item(key).get(kind)
        if not isinstance(entry, dict):
            return None
        try:
            verdict = Verdict.from_dict(entry, head=head)
        except RoundStateError:
            return None
        if head is not None and verdict.sha.lower() != head.strip().lower():
            return None
        return verdict

    def open_keys(self) -> List[str]:
        """Item keys that are not merged, in the round's order then by key."""
        merged = set()
        for key, item in self.items.items():
            if str(item.get("state") or "").lower() in ("merged", "closed", "done"):
                merged.add(key)
        ordered = [key for key in self.order if key in self.items and key not in merged]
        return ordered + sorted(key for key in self.items if key not in merged
                                and key not in ordered)

    def summary(self, max_chars: int = 350) -> str:
        """One short line for ``/round``: what the round waits on, and how old it is."""
        if self.status != "ok":
            return (f"round {self.round}: {self.status}"
                    + (f" ({self.reason})" if self.reason else "")
                    + " -- the state is not usable; do not act on it")
        waiting = [key for key, item in self.items.items() if item.get("waits_on")]
        line = (f"round {self.round} term={self.term} seq={self.seq} "
                f"items={len(self.items)} open={len(self.open_keys())} "
                f"updated={self.updated_at} by={self.writer}")
        if waiting:
            line += " waiting=" + ",".join(f"#{key}:{self.items[key].get('waits_on')}"
                                           for key in sorted(waiting))
        if self.blockers:
            line += " blockers=" + "; ".join(self.blockers)
        if self.stale:
            line += f" STALE ({self.stale})"
        return line if len(line) <= max_chars else line[:max_chars - 1] + "…"


def fold(updates: Iterable[RoundUpdate], *, round_key: str = "") -> RoundState:
    """Combine segments into one state, oldest first, deterministically."""
    ordered = sorted(updates, key=lambda update: update.sort_key())
    state = RoundState(round=round_key or (ordered[0].round if ordered else ""),
                       status="absent")
    # (item, field) -> (term, base_seq, writer, value): who last set it, and from where.
    seen: Dict[Tuple[str, str], Tuple[int, int, str, Any]] = {}
    #: Field -> the base_seq its last conflict was seen at. A later segment (base_seq above
    #: that) settles the field: conflicts are reports about concurrent writes, not a
    #: permanent mark on the round (PR #89 review, Codex blocker 1).
    contested: Dict[str, int] = {}
    holders: Dict[int, set] = {}
    #: term -> holder -> the base_seq of the segment that first claimed it there.
    claims: Dict[int, Dict[str, int]] = {}
    #: term -> the highest base_seq any segment at that term was built on.
    max_base: Dict[int, int] = {}
    for update in ordered:
        state.status = "ok"
        state.segments += 1
        state.version = max(state.version, update.version)
        state.term = max(state.term, update.term)
        state.seq = max(state.seq, update.seq)
        state.writer = update.writer or state.writer
        state.updated_at = update.at or state.updated_at
        state.repo = update.repo or state.repo
        for key, patch in update.items.items():
            current = dict(state.items.get(key) or {})
            for name, value in patch.items():
                field = f"{key}.{name}"
                prior = seen.get((key, name))
                if (prior is not None and prior[0] == update.term
                        and prior[1] == update.base_seq
                        and prior[2].lower() != (update.writer or "").lower()
                        and prior[3] != value):
                    # Two writers, the same base, the same field: neither saw the other. Keep
                    # the deterministic winner and say so, instead of losing one silently.
                    contested[field] = update.base_seq
                elif field in contested and update.base_seq > contested[field]:
                    # This writer saw the contested value and wrote over it: settled.
                    del contested[field]
                seen[(key, name)] = (update.term, update.base_seq, update.writer or "", value)
            current.update(patch)
            state.items[key] = current
        if update.order is not None:
            state.order = list(update.order)
        if update.blockers is not None:
            state.blockers = list(update.blockers)
        if update.timers is not None:
            state.timers.update(update.timers)
        if update.note:
            state.note = update.note
        max_base[update.term] = max(max_base.get(update.term, 0), update.base_seq)
        if update.holder:
            holders.setdefault(update.term, set()).add(update.holder)
            claims.setdefault(update.term, {}).setdefault(update.holder, update.base_seq)
    if holders:
        # The claim is resolved by the fold, not by who pushed last: the highest term wins, and
        # among simultaneous claims at that term the lowest alias does. Two claims at one term
        # are reported -- and settled by the next write from the holder at a later base, so a
        # claim race does not leave the round permanently unusable.
        top = max(holders)
        names = sorted(name for name in holders[top] if name)
        state.holder = names[0] if names else ""
        claim_base = max((claims.get(top) or {}).values(), default=0)
        if len(names) > 1 and max_base.get(top, 0) <= claim_base:
            # Two claims at one term and nobody has written since: a live conflict. A later
            # write at that term (base above the claims) settles it.
            contested["holder"] = claim_base
    state.conflicts = sorted(contested)
    if state.status == "ok":
        for key in state.items:
            if key not in state.order:
                state.order.append(key)
    return state


# ------------------------------------------------------------------------------------
# The store
# ------------------------------------------------------------------------------------


class RoundStore:
    """Reads and appends round segments on a bus transport (gitbus by default)."""

    def __init__(self, transport: Any = None, *, root: Optional[Path] = None,
                 relpath: str = ROUNDS_RELPATH, clock: Optional[Any] = None,
                 alias: str = "", authority: Optional[Authority] = None):
        self.transport = transport
        self.root = Path(root or getattr(transport, "root", None) or ".")
        self.relpath = relpath
        self.clock = clock
        self.alias = alias
        self.authority = authority
        self._refreshed = False

    # -- paths -----------------------------------------------------------------------
    @staticmethod
    def safe_key(round_key: str) -> str:
        """A directory name for a round key (``owner/repo#88`` -> ``owner_repo#88``)."""
        text = str(round_key or "").strip()
        if not text:
            raise RoundStateError("a round key is required")
        safe = re.sub(r"[^A-Za-z0-9._#-]+", "_", text).strip(".")
        if not safe or ".." in safe:
            raise RoundStateError(f"unusable round key: {round_key!r}")
        return safe

    def directory(self, round_key: str) -> Path:
        return self.root / self.relpath / self.safe_key(round_key)

    def _last_segment_path(self, round_key: str) -> str:
        """The newest segment on this checkout, for an update that writes none (#185 review).

        ``append`` promises a path, and a caller that gets one back may push it. When nothing
        was written the honest answer is the segment the state already came from; only a round
        with no segments at all (a first write that changed nothing, which cannot happen
        against an absent state) falls back to its own directory.
        """
        folder = self.directory(round_key)
        existing = sorted(folder.glob("*.json"))
        return str(existing[-1]) if existing else str(folder)

    # -- reading ---------------------------------------------------------------------
    def _local_segments(self, round_key: str) -> List[Tuple[str, str]]:
        folder = self.directory(round_key)
        if not folder.is_dir():
            return []
        return [(path.name, path.read_text(encoding="utf-8"))
                for path in sorted(folder.glob("*.json"))]

    def _refresh_once(self) -> None:
        """Fetch the bus ref once per store, for whichever read comes first."""
        transport = self.transport
        if (not self._refreshed and transport is not None
                and getattr(transport, "ref", None) and hasattr(transport, "refresh")):
            self._refreshed = True
            transport.refresh()

    def _raw_segments(self, round_key: str) -> Tuple[List[Tuple[str, str]], str, int]:
        """``(segments, source, pending)`` for one round.

        *source* is ``ref`` when the segments came from the bus ref (the durable answer), or
        ``local`` when only this checkout has them -- a push that has not landed, or a ref
        that could not be read. A ref that cannot be read is never reported as an empty
        round, and local bytes are never presented as bus state (PR #89 review, blocker 1).
        """
        directory = f"{self.relpath}/{self.safe_key(round_key)}"
        transport = self.transport
        local = self._local_segments(round_key)
        if transport is not None and getattr(transport, "ref", None) and hasattr(
                transport, "list_at_ref"):
            self._refresh_once()
            names = transport.list_at_ref(directory)
            if names is None:
                if local:
                    return local, "local", len(local)
                raise RoundStateError(
                    f"the bus ref could not be read for {directory}; "
                    "this is not an empty round")
            at_ref: List[Tuple[str, str]] = []
            for name in names:
                text = transport.read_at_ref(f"{directory}/{name}")
                if text is None:
                    raise RoundStateError(f"segment {name} could not be read at the bus ref")
                at_ref.append((name, text))
            if at_ref:
                at_ref_names = {name for name, _text in at_ref}
                extra = [(name, text) for name, text in local if name not in at_ref_names]
                # A segment whose push failed must not disappear from its own writer's view:
                # it is unioned in and counted as pending, so the next seq cannot reuse it.
                return at_ref + extra, "ref", len(extra)
            if local:
                # The ref is readable and holds nothing yet: these are this turn's own
                # segments, whose push is still in flight. Labelled `local`, never `ref`.
                return local, "local", len(local)
            return [], "ref", 0
        if local:
            # With no ref configured the checkout *is* the plane this store answers for (a
            # local bus, a test root), so there is nothing for it to be pending against.
            has_ref = bool(getattr(transport, "ref", None))
            return local, ("local" if has_ref else "ref"), (len(local) if has_ref else 0)
        return [], "ref", 0

    def _parse(self, round_key: str, raw: List[Tuple[str, str]]) -> List[RoundUpdate]:
        updates: List[RoundUpdate] = []
        for name, text in raw:
            if not name.endswith(".json"):
                continue
            try:
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise RoundStateError(
                        f"a segment must be a JSON object, got {type(data).__name__}")
                update = RoundUpdate.from_dict(
                    data, writer=str(data.get("writer") or ""),
                    seq=int(data.get("seq") or 0), term=int(data.get("term") or 0),
                    at=str(data.get("at") or ""), head=str(data.get("head") or "") or None)
                update.base_seq = int(data.get("base_seq") or 0)
                update.holder = str(data.get("holder") or "")
            except (ValueError, TypeError) as exc:
                # One unreadable segment must not hide the rest: the round keeps the state
                # it can still prove, and the loss is visible as `stale` on the fold.
                logger.warning("rounds: segment %s in %s is not usable: %s",
                               name, round_key, exc)
                updates.append(_damaged(name, str(exc)))
                continue
            updates.append(update)
        return updates

    def segments(self, round_key: str) -> List[RoundUpdate]:
        """Every readable segment, oldest first. Raises :class:`RoundStateError`."""
        try:
            raw, _source, _pending = self._raw_segments(round_key)
        except RoundStateError:
            raise
        except Exception as exc:                            # noqa: BLE001 - reported, not raw
            raise RoundStateError(f"the round's segments could not be read: {exc}") from exc
        return self._parse(round_key, raw)

    def load(self, round_key: str) -> RoundState:
        """The round's state, with ``status`` and ``source`` telling absence from a bad read."""
        try:
            raw, source, pending = self._raw_segments(round_key)
        except RoundStateError as exc:
            return RoundState(round=str(round_key), status="unreadable", reason=str(exc))
        updates = self._parse(round_key, raw)
        readable = [update for update in updates if not update.note.startswith(_DAMAGED_PREFIX)]
        if updates and not readable:
            # Every segment was unreadable: an empty round is not the same fact (PR #89, B2).
            return RoundState(round=str(round_key), status="unreadable", source=source,
                              reason=f"all {len(updates)} segment(s) could not be read")
        state = fold(updates, round_key=str(round_key))
        state.source = source
        state.pending = pending
        notes: List[str] = []
        damaged = [update for update in updates if update.note.startswith(_DAMAGED_PREFIX)]
        if damaged:
            notes.append(damaged[-1].note[len(_DAMAGED_PREFIX):])
        if pending and state.status == "ok":
            notes.append(f"{pending} segment(s) are on this checkout only, not on the bus ref")
        if state.conflicts:
            notes.append("two writers set the same field from the same base: "
                         + ", ".join(sorted(state.conflicts)))
        state.stale = "; ".join(notes)
        return state

    def rounds(self) -> List[str]:
        """Every round key this bot can see: on the bus ref, or in its own checkout."""
        self._refresh_once()          # `/round` must not list a ref from before the fetch
        names = set()
        base = self.root / self.relpath
        if base.is_dir():
            names.update(path.name for path in base.iterdir() if path.is_dir())
        transport = self.transport
        if (transport is not None and getattr(transport, "ref", None)
                and hasattr(transport, "list_at_ref")):
            listed = transport.list_at_ref(self.relpath)
            if listed:
                names.update(entry.split("/", 1)[0] for entry in listed if "/" in entry)
        return sorted(names)

    # -- writing ---------------------------------------------------------------------
    def next_seq(self, round_key: str) -> int:
        return self.load(round_key).seq + 1

    def append(self, update: RoundUpdate, *, holder: str = "",
               authority: Optional[Authority] = None) -> str:
        """Write one segment, then publish it. Returns the segment's bus path.

        An update that writes nothing -- the same state again, or a verdict the round already
        holds -- returns the path of the newest segment instead (the round's directory only
        when it has none), so a caller always gets something it could read or push.

        Three rules, one purpose: no bot writes a round it cannot prove it holds.

        * **Fail closed.** If the bus cannot be read, the term and the seq this write would
          take cannot be verified, so the write is refused (PR #89 review, blocker 3).
        * **No going back a term.** A term lower than the bus already holds is refused
          (:class:`RoundStaleTerm`): a returning coordinator must not overwrite its
          successor's state.
        * **Fenced by the holder.** Once a round has a holder at a term, another bot cannot
          write it at that term or below (:class:`RoundFenced`). Until #83 supplies a lease,
          ``term`` comes from the bus, never from the model's block.
        * **A first claim needs an authority.** The write that creates a round cannot be
          fenced against a holder, because there is none yet: it needs *authority* -- the
          alias the swarm names as its coordinator. A stale coordinator whose swarm says
          somebody else is refused (:class:`RoundUnauthorised`), and so is a bot that cannot
          name one at all.
        """
        current = self.load(update.round)
        if current.status == "unreadable":
            raise RoundStateError(
                f"the state of {update.round} on the bus could not be read "
                f"({current.reason}); refusing to write a term or a seq that cannot be "
                "verified")

        update.holder = holder or update.holder or self.alias
        auth = authority
        if auth is None and (not self.alias or self.alias.strip().lower() == update.holder.strip().lower()):
            auth = self.authority

        if current.status == "absent":
            if auth is None or not auth.alias.strip():
                raise RoundUnauthorised(
                    f"{update.round} has no state on the bus and no coordinator authority is "
                    "known; refusing to make its first claim")
            if auth.alias.strip().lower() != update.holder.strip().lower():
                raise RoundUnauthorised(
                    f"the coordinator on the swarm is {auth.alias} "
                    f"({auth.source}); {update.holder} may not make the first claim on "
                    f"{update.round}")
            if auth.term:
                update.term = auth.term

        has_ref = bool(getattr(self.transport, "ref", None))
        if has_ref and current.status == "ok" and current.source != "ref":
            raise RoundStateError(
                f"the state of {update.round} is on this checkout only, not on the bus "
                f"({current.stale or 'not pushed'}); refusing to write against a state "
                "the bus does not have")

        trusted = current.term if current.status == "ok" else 0
        if auth is not None and auth.term:
            if auth.term < trusted:
                raise RoundStaleTerm(
                    f"term {auth.term} is behind {trusted} for round {update.round}; "
                    "this bot is not the coordinator of this round any more")
            update.term = auth.term
        elif update.term and update.term < trusted:
            raise RoundStaleTerm(
                f"term {update.term} is behind {trusted} for round {update.round}; "
                "this bot is not the coordinator of this round any more")
        if not update.term:
            update.term = trusted

        if current.status == "ok":
            repeats = _repeat_verdicts(update, current)
            for field_name in repeats:
                key, kind = field_name.rsplit(".", 1)
                held = (current.items.get(key) or {}).get(kind) or {}
                logger.info("rounds: %s's %s verdict on #%s at %s is already recorded; the "
                            "repeat is suppressed", held.get("by") or "a bot", kind,
                            str(key).lstrip("#"), held.get("sha") or "?")
                update.items[key].pop(kind, None)
                if not update.items[key]:
                    update.items.pop(key, None)
            if repeats and not update.items and update.order is None \
                    and update.blockers is None and update.timers is None \
                    and not update.note and not update.repo:
                # Nothing but the repeat: no new segment for a fact the round already holds.
                return self._last_segment_path(update.round)

        if current.status == "ok" and _unchanged(update, current):
            # Nothing new: a turn that emits the same state again must not add a commit to
            # the bus's history (issue #86 review B7). Checked before the fence, so a no-op
            # from another bot returns the existing segment instead of raising.
            return self._last_segment_path(update.round)

        if auth is not None and auth.alias.strip():
            if auth.alias.strip().lower() != update.holder.strip().lower():
                raise RoundUnauthorised(
                    f"the coordinator on the swarm is {auth.alias} "
                    f"({auth.source}); {update.holder} cannot write round {update.round}")
        if (current.status == "ok" and current.holder and update.term <= current.term
                and current.holder.strip().lower() != update.holder.strip().lower()):
            raise RoundFenced(
                f"round {update.round} is held by {current.holder} at term {current.term}; "
                f"{update.holder} cannot write it at term {update.term}")
        update.base_seq = current.seq
        if update.seq <= 0:
            update.seq = current.seq + 1
        if not update.at:
            update.at = utc_now(self.clock)
        directory = self.directory(update.round)
        directory.mkdir(parents=True, exist_ok=True)
        writer = re.sub(r"[^A-Za-z0-9]+", "", (update.writer or self.alias or "bot"))[:16] or "bot"
        name = f"{update.term:06d}-{update.seq:06d}-{writer}-{uuid.uuid4().hex[:8]}.json"
        path = directory / name
        relpath = f"{self.relpath}/{directory.name}/{name}"
        if not is_bus_document(relpath):                    # a guard, not a formality
            raise RoundStateError(f"refusing to write outside {self.relpath}: {relpath!r}")
        path.write_text(json.dumps(update.to_dict(), sort_keys=True, indent=1,
                                   ensure_ascii=False), encoding="utf-8")
        transport = self.transport
        if transport is not None and hasattr(transport, "commit_and_push"):
            try:
                transport.commit_and_push(
                    f"rounds: {update.round} term {update.term} seq {update.seq} "
                    f"by {update.writer or self.alias}", paths=[relpath])
            except Exception as exc:                        # noqa: BLE001 - the file is safe
                raise RoundPushError(str(exc), path=str(path)) from exc
        # Writing is not holding. Whoever the fold names -- highest term, then lowest alias --
        # owns the round, so a bot that wrote a claim it did not win is fenced here rather
        # than left believing it writes for the round (PR #89 review, Codex blocker 2).
        settled = self.load(update.round)
        if (settled.status == "ok" and settled.holder
                and settled.holder.strip().lower() != update.holder.strip().lower()):
            raise RoundFenced(
                f"round {update.round} is held by {settled.holder}; "
                f"{update.holder} wrote a claim it does not hold")
        return str(path)


def _repeat_verdicts(update: RoundUpdate, state: RoundState) -> List[str]:
    """The verdict fields in *update* the state already holds, as ``"<item>.<kind>"`` (#185).

    A verdict is one fact -- this bot said this, about this head. The same fact again says
    nothing new, and re-stating it is how one verdict became three in the 22 Oct exports
    (Reasonix twice on a head, Antigravity twice on a QA). Compared on the fact, not on the
    dict: a newer ``at`` alone is still a repeat, and a different bot's verdict is that bot's
    own -- the item's slot is per kind, and one review does not answer for another reviewer.
    """
    repeats: List[str] = []
    for key, patch in update.items.items():
        current = state.items.get(key) or {}
        for kind in VERDICT_KINDS:
            if kind not in patch:
                continue
            incoming, held = patch.get(kind), current.get(kind)
            if not isinstance(incoming, dict) or not isinstance(held, dict):
                continue
            if (_fact(incoming.get("sha")) == _fact(held.get("sha"))
                    and _fact(incoming.get("verdict")) == _fact(held.get("verdict"))
                    and _fact(incoming.get("by")) == _fact(held.get("by"))):
                repeats.append(f"{key}.{kind}")
    return repeats


def _fact(value: Any) -> str:
    """One part of a verdict, compared the lenient way its writer will compare it."""
    return str(value or "").strip().lower()


def _unchanged(update: RoundUpdate, state: RoundState) -> bool:
    """Whether *update* would leave the folded state exactly as it is."""
    if update.term != state.term:
        return False
    for key, patch in update.items.items():
        current = dict(state.items.get(key) or {})
        if {**current, **patch} != current:
            return False
    if update.order is not None and list(update.order) != list(state.order):
        return False
    if update.blockers is not None and list(update.blockers) != list(state.blockers):
        return False
    if update.timers is not None and any(state.timers.get(name) != timer
                                        for name, timer in update.timers.items()):
        return False
    if update.note and update.note != state.note:
        return False
    # #185 review: `repo` is state too -- `fold` takes it (`state.repo = update.repo or ...`),
    # so an update carrying only a repo change is a change, not a no-op.
    if update.repo and update.repo != state.repo:
        return False
    return True


_DAMAGED_PREFIX = "segment not readable: "


def _damaged(name: str, reason: str) -> RoundUpdate:
    """A segment that could not be parsed, kept so the fold can report it as stale."""
    return RoundUpdate(round="", writer="", note=_DAMAGED_PREFIX + f"{name}: {reason}")
