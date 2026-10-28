"""The topic filters the dashboard's Topics panel and ``/topics`` share (#438).

One registry, three questions: what kind of item a topic carries (**type**: an issue, a PR,
something else, or nothing), whether it is **open or closed** (**state**: the item's GitHub
state, the topic record's own status), and how far it has got (**stage**: the item's phase).
They combine, and both readers use *these* functions: the panel filters cards it already has,
``/topics`` filters the records it is about to print, and neither can drift from the other.

The default view is the admin's: **open topics that still have something to do**. An item in
``done`` is hidden and a topic whose every item is done drops out — that is what makes a long
registry readable. A topic with no items at all stays: nothing says it is finished, and a
topic the admin just opened must not vanish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: What a topic can carry: a GitHub issue, a GitHub pull request, anything else a link can
#: point at (Drive, cloud, a local path), or nothing at all.
TYPES: Tuple[str, ...] = ("issue", "pr", "other", "none")
#: Open or closed. For an item that is GitHub's own state; for a topic, its record's status.
STATES: Tuple[str, ...] = ("open", "closed")
#: A work item's phase, in the order the round moves.
STAGES: Tuple[str, ...] = ("research", "design", "development", "review", "qa", "done",
                           "blocked")
#: The stages that mean an item is finished, so the default view hides it.
DONE_STAGES: Tuple[str, ...] = ("done",)

#: ``type=issue,pr`` and friends: the one-word names a filter is written with.
_KEYS = {"type": "types", "state": "states", "stage": "stages"}


def _words(value: Any) -> Tuple[str, ...]:
    """A filter value as its words: ``"issue,pr"`` and ``["issue", "pr"]`` alike."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts: Iterable[Any] = value.replace(",", " ").split()
    elif isinstance(value, Mapping) or not isinstance(value, Iterable):
        parts = [value]
    else:
        parts = value
    out: List[str] = []
    for part in parts:
        word = str(part or "").strip().strip(",").lower()
        if word and word not in out:
            out.append(word)
    return tuple(out)


def item_type(link: Mapping[str, Any]) -> str:
    """``issue``, ``pr``, ``other`` or ``none`` for one link (``none`` when there is no link).

    Read from the link's own ``kind`` and ``ref`` -- a GitHub pull request is ``/pull/12`` or
    ``kind: pr`` -- and from its ``provider``: anything that is not GitHub or GitLab (Drive,
    cloud, a local path, a topic) is ``other``, because the panel and ``/topics`` both read it
    by name and neither may claim to know more.
    """
    if link is None:
        return "none"
    data = dict(link or {})
    known = str(data.get("type") or "").strip().lower()
    if known in TYPES:
        return known
    kind = str(data.get("kind") or "").strip().lower().replace("_", "-")
    provider = str(data.get("provider") or "").strip().lower()
    ref = str(data.get("ref") or "").strip().lower()
    if kind in ("pr", "pull", "pull-request") or "/pull/" in ref:
        return "pr"
    if kind == "issue" or "/issues/" in ref:
        return "issue"
    if provider or kind or ref:
        # A GitHub or GitLab link whose kind says neither issue nor PR, a Drive/cloud/local
        # one, a topic reference: "other" is exactly this -- a linked thing we do not classify
        # further, which is also why the panel prints its title and not its ref.
        return "other"
    return "none"


def item_state(link: Mapping[str, Any]) -> str:
    """``open`` or ``closed`` for one item: GitHub's state, or the record's, else unknown."""
    data = dict(link or {})
    state = str(data.get("status") or data.get("state") or "").strip().lower()
    return "closed" if state in ("closed", "done", "merged") else "open"


def item_stage(link: Mapping[str, Any]) -> str:
    """The item's phase, lowercased; empty when it has none."""
    return str(dict(link or {}).get("phase") or "").strip().lower()


@dataclass(frozen=True)
class Filters:
    """One reading of the topic filters: the three dimensions, combinable, and ``all``.

    An empty dimension constrains nothing -- except the default view, which applies only when
    *no* dimension is set: an admin who asked for ``stage=done`` asked for the finished ones,
    and must not then be told there are none.
    """

    types: Tuple[str, ...] = ()
    states: Tuple[str, ...] = ()
    stages: Tuple[str, ...] = ()
    #: ``/topics all`` (and the panel's "include closed"): list closed *records* too.
    show_closed: bool = False
    unknown: Tuple[str, ...] = field(default=())

    @property
    def asked(self) -> bool:
        """Whether the admin asked for something, i.e. the default view is not in force."""
        return bool(self.types or self.states or self.stages or self.show_closed)

    @property
    def topic_states(self) -> Tuple[str, ...]:
        """Which topic records to show: the asked state, else open (plus closed for ``all``)."""
        if self.states:
            return self.states
        return ("open", "closed") if self.show_closed else ("open",)

    @property
    def hides_done(self) -> bool:
        """The default view's other half: an item in ``done`` is not what the admin asked for."""
        return not self.asked

    def item_ok(self, link: Mapping[str, Any]) -> bool:
        """Whether one item passes: its type, its state and its phase, where they are asked."""
        if self.types and item_type(link) not in self.types:
            return False
        if self.states and item_state(link) not in self.states:
            return False
        stage = item_stage(link)
        if self.stages and stage not in self.stages:
            return False
        if self.hides_done and stage in DONE_STAGES:
            return False
        return True

    def topic_ok(self, status: str, links: Sequence[Mapping[str, Any]] = ()) -> bool:
        """Whether a topic record passes: its own status, then what its items say.

        A topic with no items stays unless a *type* or *stage* was asked for: "none" is the
        type of an empty topic, so ``type=none`` is how an admin asks for exactly those.

        A **tracking** link keeps the topic listed even when every work item in it is done
        (#468, acceptance as amended): a topic whose slices have all landed still has its
        tracking issue, and hiding it lost the live one from ``/topics`` entirely. No state
        is read on that link -- a tracking link cannot carry one (``records.Link``), so a
        finished round leaves the default view by closing its topic or unlinking its
        tracking issue. A *reference* link is a pointer and never keeps a topic in.
        """
        if str(status or "open").strip().lower() not in self.topic_states:
            return False
        items, tracked = [], []
        for link in links:
            role = str(dict(link or {}).get("role") or "work").strip().lower()
            if role == "work":
                items.append(link)
            elif role == "tracking":
                tracked.append(link)
        if not items:
            if self.types and "none" not in self.types:
                return False
            return not (self.stages or self.states == ("closed",))
        passed = [link for link in items if self.item_ok(link)]
        if self.types or self.stages or self.states:
            return bool(passed)
        # The default view: open, and still something to do in it -- or a tracking issue of
        # its own, which is the topic's last live item once every slice has landed.
        if tracked:
            return True
        return bool(passed) or not any(item_stage(link) in DONE_STAGES for link in items)

    # -- the words -------------------------------------------------------------------
    def words(self) -> List[str]:
        """The filter as ``/topics`` arguments, for an answer or the page's URL."""
        out: List[str] = []
        if self.types:
            out.append("type=" + ",".join(self.types))
        if self.states:
            out.append("state=" + ",".join(self.states))
        if self.stages:
            out.append("stage=" + ",".join(self.stages))
        if self.show_closed and not self.states:
            out.append("all")
        return out

    def describe(self) -> str:
        """One short line naming what is filtered, for a count line."""
        return " ".join(self.words())

    def as_query(self) -> Dict[str, str]:
        """The same filters as query parameters, for the page's URL."""
        query: Dict[str, str] = {}
        if self.types:
            query["type"] = ",".join(self.types)
        if self.states:
            query["state"] = ",".join(self.states)
        if self.stages:
            query["stage"] = ",".join(self.stages)
        if self.show_closed:
            query["all"] = "1"
        return query


#: The view an admin gets with no arguments at all.
DEFAULT = Filters()


def parse(words: Iterable[Any] = ()) -> Filters:
    """Read ``/topics`` arguments -- or the page's query -- into :class:`Filters`.

    ``type=issue,pr``, ``state=closed``, ``stage=review``, ``all``. A word this module does
    not know is returned in :attr:`Filters.unknown` rather than dropped: an admin who typed
    ``stage=reveiw`` must be told, not quietly shown everything.
    """
    found: Dict[str, List[str]] = {"types": [], "states": [], "stages": []}
    unknown: List[str] = []
    show_closed = False
    for token in _words_or_tokens(words):
        token = token.strip()
        if token.lower() in ("all", "show-all"):
            # `/topics ALL` has been the same ask since #435: a word this command accepts is
            # never reported back as unknown (#438 review).
            show_closed = True
            continue
        key, _, value = token.partition("=")
        key = key.strip().lower()
        if not value:
            unknown.append(token)
            continue
        field_name = _KEYS.get(key)
        if field_name is None:
            unknown.append(token)
            continue
        for word in _words(value):
            allowed = {"types": TYPES, "states": STATES, "stages": STAGES}[field_name]
            if word not in allowed:
                unknown.append(f"{key}={word}")
                continue
            if word not in found[field_name]:
                found[field_name].append(word)
    return Filters(types=tuple(found["types"]), states=tuple(found["states"]),
                   stages=tuple(found["stages"]), show_closed=show_closed,
                   unknown=tuple(unknown))


def _words_or_tokens(words: Iterable[Any]) -> List[str]:
    """The argument words as they were typed: one per token, handles already dropped."""
    if isinstance(words, str):
        return words.split()
    return [str(word) for word in words or ()]


def from_query(query: Mapping[str, Any] = ()) -> Filters:
    """Read the page's URL query (``type=issue&state=open``) into :class:`Filters`."""
    words: List[str] = []
    for key, value in dict(query or {}).items():
        if str(key).lower() == "all":
            words.append("all")
        else:
            words.append(f"{key}={value}")
    return parse(words)


def select_topics(records: Sequence[Mapping[str, Any]], filters: Optional[Filters] = None,
                  *, status_of: Any = None) -> List[Mapping[str, Any]]:
    """The records that pass *filters*, in the order they came (``/topics``' own list)."""
    chosen = DEFAULT if filters is None else filters
    out: List[Mapping[str, Any]] = []
    for record in records:
        status = status_of(record) if callable(status_of) else \
            str(dict(record or {}).get("status") or "open").strip().lower()
        if chosen.topic_ok(status, list(dict(record or {}).get("links") or ())):
            out.append(record)
    return out


def select_cards(cards: Sequence[Mapping[str, Any]], filters: Optional[Filters] = None
                 ) -> List[Mapping[str, Any]]:
    """The panel's topic cards that pass *filters* -- the same rule as the records (#438)."""
    chosen = DEFAULT if filters is None else filters
    out: List[Mapping[str, Any]] = []
    for card in cards:
        data = dict(card or {})
        if chosen.topic_ok(str(data.get("status") or "open"), list(data.get("links") or ())):
            out.append(card)
    return out
