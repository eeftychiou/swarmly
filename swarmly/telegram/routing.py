"""Routing from the topic registry (docs/design/topic-registry.md, §2; phase 6).

The transport, not each bot's mention heuristics, decides who takes a message in a work topic
(the flag that used to choose between them is gone, #472):

- the **coordinator** (the store's lease holder) takes every message in the topic that is not
  addressed only to other agents;
- another **linked** agent takes a message only when it names it, replies to it, or hands it
  work (an ``ASSIGN:`` line for it);
- a mention of an agent that **is not linked** links it when the author is the coordinator or
  the admin (the holder records it); from anyone else it is refused, with a transport notice,
  and the coordinator decides.

This module is the decision only: plain values in, a :class:`Route` out, so every case is a
unit test. The channel gathers the facts and acts on the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple


@dataclass(frozen=True)
class Facts:
    """What the registry says about one topic, for one bot."""
    me: str                                  # this bot's alias, normalised
    coordinator: Optional[str]               # the lease holder's alias, normalised
    linked: FrozenSet[str]                   # linked agents' aliases *and* handles, lower case
    coordinator_handle: Optional[str] = None  # its Telegram username (profile), lower case
    me_handle: Optional[str] = None          # this bot's Telegram username, lower case

    def is_me(self, name: Optional[str]) -> bool:
        return name is not None and name in (self.me, self.me_handle)

    @property
    def i_coordinate(self) -> bool:
        return self.coordinator is not None and (
            self.me == self.coordinator
            or (self.me_handle is not None and self.me_handle == self.coordinator_handle))

    @property
    def i_am_linked(self) -> bool:
        return self.me in self.linked or (self.me_handle is not None
                                          and self.me_handle in self.linked)


@dataclass(frozen=True)
class Message:
    """What routing reads from one message."""
    author: Optional[str]                    # a peer bot's alias (normalised), else None
    author_is_bot: bool
    author_is_admin: bool
    names_me: bool                           # by handle, a leading "Alias:", or @all
    replies_to_me: bool
    handed_to_me: bool                       # an ASSIGN line for this bot, or an expected hand-off
    named: Tuple[str, ...] = ()              # other agents it names or replies to (handles)
    names_me_explicitly: Optional[bool] = None   # by handle or alias, not only by @all
    author_handle: Optional[str] = None      # the author's Telegram username, lower case

    @property
    def names_others(self) -> bool:
        return bool(self.named)


@dataclass(frozen=True)
class Route:
    respond: bool
    reason: str
    refuse_unlinked: bool = False            # post the "isn't linked" notice


def decide(facts: Facts, message: Message) -> Route:
    addressed = message.names_me or message.replies_to_me or message.handed_to_me
    named_me = (message.names_me if message.names_me_explicitly is None
                else message.names_me_explicitly)
    explicit = named_me or message.replies_to_me or message.handed_to_me
    by_coordinator = message.author_is_bot and facts.coordinator is not None and (
        message.author == facts.coordinator
        or (message.author_handle is not None
            and message.author_handle == facts.coordinator_handle))
    by_admin = message.author_is_admin and not message.author_is_bot
    # The named agents that will take the message: linked ones, or any the coordinator or
    # the admin names (that links them). One that is refused leaves it to the coordinator.
    takers = [a for a in message.named if a in facts.linked or by_coordinator or by_admin]
    if facts.i_coordinate:
        if addressed:
            return Route(True, "registry: addressed to the coordinator")
        if takers:
            return Route(False, "registry: addressed to other agents, not the coordinator")
        if message.named:
            return Route(True, "registry: the agents it names are not linked; the "
                               "coordinator decides")
        if message.author_is_bot:
            # A peer's unaddressed post (progress, a report) is not a request: answering
            # each one would ping-pong with the agent up to the peer-turn cap (review).
            return Route(False, "registry: an unaddressed peer message")
        return Route(True, "registry: the coordinator takes every message in the topic")
    if not addressed:
        return Route(False, "registry: not addressed (the coordinator takes it)")
    if facts.i_am_linked:
        return Route(True, "registry: linked and addressed")
    if not explicit:
        return Route(False, "registry: a broadcast, and not linked to this topic")
    if by_coordinator or by_admin:
        return Route(True, "registry: named by the coordinator or the admin (links it)")
    return Route(False, "registry: not linked to this topic", refuse_unlinked=True)
