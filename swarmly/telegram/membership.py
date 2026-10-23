"""Dynamic swarm membership: agents join, get a role and a first task over Telegram.

No agent's config lists its peers or its role any more. A newcomer started with
``swarmly --cli <runtime> --bot-token ... --admin-userid ... --home-channel ...`` knows only
its bot, the group and the maintainer; everything else is agreed in the group:

1. **JOIN.** The newcomer posts ``HELLO intent=JOIN ref=<alias>`` (its runtime and its bus
   signing key's public half in the body).
2. **PRESENT.** Every agent already in the swarm checks with Telegram that the newcomer is an
   administrator of the group (``getChatMember``: only the group's owner makes a bot an
   admin, and every swarm bot must be one to see the other bots), adds it as a peer and
   answers in reply to the JOIN with its own alias, role, the coordinator and the bus
   location. The coordinator's answer also lists every member with its Telegram user id, so
   the newcomer learns members that are offline right now, and verifies each one the same
   way.
3. **Bus.** The newcomer takes the bus location from the answers (the launcher clones it and
   registers the signing key there). With no answer at all it is the first agent: it asks the
   maintainer for the bus (``/bus <git URL or path>``) and takes the coordinator role unless
   the maintainer gives it another (``/role @bot <role>``).
4. **ROLE.** The coordinator's agent is asked, in a turn of its own, which role the newcomer
   should take; its answer line ``ROLE: @newbot <role>`` becomes a
   ``HELLO intent=ROLE to=<alias> ref=<role>`` that every agent applies. The maintainer can
   override any role at any time with ``/role @bot <role>``.
5. **Onboarding.** The newcomer's next turn carries the role's full instructions (its system
   prompt from then on) and asks it to introduce itself to the coordinator and ask for its
   first task, which the coordinator then hands it like any other work.

Trust: a role comes from the coordinator's bot, never the coordinator role, and never for the
coordinator itself. The coordinator gives a newcomer its first role and switches a peer's role
later, when a role is left without a working bot or a bot does not perform: a
``ROLE: @bot <role>`` line in any of its replies (the swarm heals itself; the admin should not
have to). The admin overrides any role and is the only one who moves the coordinator. What an agent says about itself (online notice, JOIN, PRESENT) is recorded but
never makes it the coordinator, except that a newcomer still joining takes the coordinator
from the answer of the agent that says it is one. Only an agent the launcher started
(``dynamic``) joins by itself; one set up by hand only answers newcomers.

Agents drop out at any time: ``BYE intent=OFFLINE`` on a clean stop marks one offline (it
keeps its role and rejoins with its ``HELLO intent=ONLINE``), and leaving the group removes
it from every roster. What was agreed is kept beside the config (``<config>.swarm.json``),
so a restart rejoins without asking again.

Telegram limits the design works around (see docs/JOIN.md): a bot cannot list a group's
members (only ``getChatAdministrators``, documented as returning non-bot admins, the member
count, and one member at a time by user id), and it sees another bot's message only with
Bot-to-Bot Communication Mode on and either a mention of it or admin rights with Group
Privacy Mode off. Hence the self-announcements, the user ids in the coordinator's member
list, and admin status as the proof of membership.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

from swarmly.telegram.adapters.base import load_state, save_state
from swarmly.telegram.commands import command_args, parse_command
from swarmly.telegram.roster import ALIAS_RE
from swarmly.telegram.wire import Envelope, MessageType, format_message

if TYPE_CHECKING:
    from swarmly.telegram.channel import AgentChannel, ChannelEvent

logger = logging.getLogger("swarmly.telegram.membership")

#: How long a newcomer waits for the swarm to answer its JOIN before it asks the maintainer.
JOIN_WAIT_SECONDS = 90.0
#: How long a newcomer waits for the coordinator to give it a role before asking the maintainer.
ROLE_WAIT_SECONDS = 600.0
#: How long Telegram's "not an admin" answer stands before the bot is asked about again.
NOT_ADMIN_RECHECK_SECONDS = 60.0
#: How long a refused JOIN is re-checked by itself (every NOT_ADMIN_RECHECK_SECONDS), for an
#: owner who makes the bot an admin after the newcomer has stopped posting.
WAITING_JOIN_SECONDS = 30 * 60.0
#: Marks a turn the transport started for membership (a role decision, an onboarding).
MEMBERSHIP_MARK = "[swarm membership]"
_DECIDE_MARK = "A new agent joined the swarm:"
_ONBOARD_MARK = "You have joined"
#: The newcomer a role-decision turn is about: ``<alias> (@<username>)``.
_DECIDE_FOR = re.compile(re.escape(_DECIDE_MARK) + r" ([A-Za-z0-9_-]{1,32}) \(@([A-Za-z0-9_]{5,32})\)")
#: Roles handed out, in order, when nobody decides one: the gaps a working team fills first.
DEFAULT_ROLE_ORDER = ("developer", "reviewer", "qa-tester", "researcher")
#: ``ROLE: @handle role`` in a coordinator's reply.
_ROLE_LINE = re.compile(r"(?im)^\W*ROLE:\s*@([A-Za-z0-9_]{5,32})\s+([a-z][a-z0-9_-]{0,39})\b")
#: ``key: value`` lines of a JOIN or PRESENT body.
_FIELD_LINE = re.compile(r"(?m)^\s*(cli|key|role|coordinator|bus|members):[ \t]*(.*?)\s*$")
_TRAILING_HANDLES = re.compile(r"(?:\s+@[A-Za-z0-9_]{5,32})+$")
#: One member of a coordinator's list: ``Alias=@username#user_id:role``.
_MEMBER = re.compile(r"([A-Za-z0-9_-]{1,32})=@([A-Za-z0-9_]{5,32})#(\d{1,20})(?::([a-z][a-z0-9_-]{0,39}))?")
#: The "[Alias]" banner a transport puts in front of everything it posts.
_BANNER = re.compile(r"^\s*\[([A-Za-z0-9_-]{1,32})\]")
#: Telegram statuses that make a bot a swarm member: the group owner made it an admin.
ADMIN_STATUSES = ("administrator", "creator")


@dataclass
class SwarmState:
    """What this agent agreed with the swarm, kept across restarts."""

    bus: Optional[str] = None                 # the bus location everyone shares
    coordinator: Optional[str] = None         # the coordinator's alias
    role: Optional[str] = None                # this agent's role
    peer_roles: Dict[str, str] = field(default_factory=dict)
    peer_ids: Dict[str, int] = field(default_factory=dict)    # alias -> Telegram user id
    onboard_pending: bool = False             # a role was given; its first turn has not run
    bus_ready: bool = False                   # the launcher has a checkout of ``bus``
    self_appointed: bool = False              # coordinator only because nobody answered

    @classmethod
    def load(cls, path: Optional[Path]) -> "SwarmState":
        raw = load_state(path, "swarm membership")
        roles = raw.get("peer_roles") if isinstance(raw.get("peer_roles"), dict) else {}
        ids = raw.get("peer_ids") if isinstance(raw.get("peer_ids"), dict) else {}
        return cls(bus=_text(raw.get("bus")), coordinator=_text(raw.get("coordinator")),
                   role=_text(raw.get("role")),
                   peer_roles={str(a): str(r) for a, r in roles.items() if _ROLE_NAME(r)},
                   peer_ids={str(a): int(i) for a, i in ids.items() if _is_int(i)},
                   onboard_pending=bool(raw.get("onboard_pending")), bus_ready=bool(raw.get("bus_ready")),
                   self_appointed=bool(raw.get("self_appointed")))

    def save(self, path: Optional[Path]) -> None:
        save_state(path, {"bus": self.bus, "coordinator": self.coordinator, "role": self.role,
                          "peer_roles": dict(self.peer_roles), "peer_ids": dict(self.peer_ids),
                          "onboard_pending": self.onboard_pending, "bus_ready": self.bus_ready,
                          "self_appointed": self.self_appointed},
                   "swarm membership")


def _text(value: Any) -> Optional[str]:
    return str(value).strip() or None if value not in (None, "") else None


def _banner_alias(text: Optional[str]) -> Optional[str]:
    match = _BANNER.match(text or "")
    return match.group(1) if match else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ROLE_NAME(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", value))


def swarm_state_path(cfg: Any) -> Optional[Path]:
    source = getattr(cfg, "source_path", None)
    return Path(source).with_suffix(".swarm.json") if source else None


def apply_swarm_state(cfg: Any) -> SwarmState:
    """Put what the swarm agreed into *cfg*: this agent's role and its peers' roles. The
    agreed role wins over ``agent.role`` (which is only where a config starts)."""
    state = SwarmState.load(swarm_state_path(cfg))
    if state.role:
        cfg.identity.role = state.role
    elif cfg.identity.role:
        state.role = cfg.identity.role          # a role written in the config, until changed
    for alias, role in state.peer_roles.items():
        if alias in cfg.peers:
            cfg.peer_roles[alias] = role
    return state


def parse_fields(body: str) -> Dict[str, str]:
    """The ``key: value`` lines of a JOIN, PRESENT or ROLE body (first one of each wins)."""
    found: Dict[str, str] = {}
    for key, value in _FIELD_LINE.findall(body or ""):
        # The transport appends a peer's @handle to a message that names it by alias only
        # (wire.addressed), and it lands on the last field: "bus: <url> @coordinator_bot"
        # (live test: a newcomer could not clone that bus). No field value ends in a handle.
        found.setdefault(key, _TRAILING_HANDLES.sub("", value))
    return found


def safe_bus_location(location: str) -> str:
    """A bus location fit to post: credentials in a URL (``https://user:token@host``) are
    dropped, so a token in a remote URL never reaches the group. The user of an ssh URL
    (``ssh://git@host/...``, ``git@host:...``) is not a secret and stays: without it the
    URL no longer works."""
    location = location.strip()
    location = re.sub(r"(?i)^(https?://)[^/@\s]+@", r"\1", location)
    return re.sub(r"(?i)^([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", r"\1", location)


def is_remote_bus(location: Optional[str]) -> bool:
    """A location another machine can clone: a URL or an scp-style ``user@host:path``, not a
    folder on this machine (``file://`` is a folder too)."""
    if not location:
        return False
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", location):
        return not location.lower().startswith("file:")
    return bool(re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^/]", location))


#: Looks a user up in the home group: ``(status, username, is_bot)`` or None when unknown.
MemberLookup = Callable[[int], Awaitable[Optional[Tuple[str, str, bool]]]]


class Membership:
    """The join / present / role handshake for one agent's transport."""

    def __init__(self, channel: "AgentChannel") -> None:
        self.ch = channel
        self.cfg = channel.config
        self.path = swarm_state_path(self.cfg)
        self.state = apply_swarm_state(self.cfg)
        #: Set by the transport (getChatMember); tests inject their own.
        self.lookup: Optional[MemberLookup] = None
        #: Set by the ``swarmly --cli`` launcher: prepare a checkout of the agreed bus.
        self.on_bus: Optional[Callable[[str], Awaitable[None]]] = None
        #: Bots this newcomer heard of but has not met (mentioned in its JOIN).
        self.candidates: Dict[str, str] = {}
        self.cli = ""                                    # the runtime, for the JOIN
        self.public_key = ""                             # the bus key's public half
        self._verified: Dict[int, bool] = {}             # user id -> confirmed bot admin
        self._not_admin: Dict[int, float] = {}           # user id -> when Telegram said no
        #: JOINs from bots that were not admins yet: user id -> (alias, body, the JOIN event),
        #: answered once Telegram says the bot is an admin.
        self._waiting_joins: Dict[int, Tuple[str, str, "ChannelEvent"]] = {}
        self._remote_checked = False                     # bus_location asked git once
        self._joined_at: Optional[float] = None          # when this agent posted its JOIN
        self._present_from: List[str] = []               # members that answered the JOIN
        #: Newcomers a role is being decided for: Telegram username (lower) -> alias.
        self._pending_roles: Dict[str, str] = {}
        self._onboarding = False                         # an onboarding turn is running
        #: Set by the ``swarmly --cli`` launcher. Only such an agent joins by itself (posts a
        #: JOIN, asks the maintainer for a bus, takes the first agent's coordinator role); an
        #: agent from a hand-written config only answers newcomers and records roles.
        self.dynamic = False
        self._tasks: List[asyncio.Task] = []
        #: alias -> {"online": bool, "at": wall clock} for /swarm.
        self.presence: Dict[str, Dict[str, Any]] = {}
        if self.state.coordinator is None and not self.is_coordinator():
            # A hand-written config names its coordinator among its peers' roles.
            self.state.coordinator = next((a for a, r in self.cfg.peer_roles.items()
                                           if r == "coordinator"), None)

    # -- small helpers --------------------------------------------------------------------
    @property
    def alias(self) -> str:
        return self.cfg.identity.alias

    def _me(self) -> str:
        return self.cfg.identity.name.lstrip("@").lower()

    def _save(self) -> None:
        self.state.save(self.path)

    def joined(self) -> bool:
        """Whether this agent is past the handshake: it has a role and a bus to work on."""
        return bool(self.state.role and self.bus_location() and self.bus_ready())

    def bus_ready(self) -> bool:
        """Whether this agent works on the bus: the launcher made its checkout, or the
        config names one (an agent set up by hand)."""
        if self.state.bus_ready:
            return True
        bus = self.cfg.bus
        return not self.dynamic and bool(bus.repo or (bus.transport == "folder" and bus.folder))

    def bus_location(self) -> Optional[str]:
        """The bus everyone shares: the agreed one, else the configured checkout's remote (or
        the configured folder)."""
        if self.state.bus:
            return self.state.bus
        bus = self.cfg.bus
        if bus.transport == "folder" and bus.folder:
            return bus.folder
        url = self._origin_url()
        if url:
            self.state.bus = url
            self._save()
        return url

    def _origin_url(self) -> Optional[str]:
        """The configured checkout's ``origin`` remote (asked once), fit to post."""
        if not self.cfg.bus.repo or self._remote_checked:
            return None
        self._remote_checked = True
        try:
            out = subprocess.run(["git", "-C", str(Path(self.cfg.bus.repo).expanduser()),
                                  "remote", "get-url", "origin"], capture_output=True,
                                 text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        url = out.stdout.strip()
        return safe_bus_location(url) if out.returncode == 0 and url else None

    def advertised_bus(self) -> Optional[str]:
        """The bus location to tell a newcomer: one it can clone from its own machine, never
        a folder on this one."""
        bus = self.bus_location()
        if is_remote_bus(bus):
            return bus
        self._remote_checked = False
        origin = self._origin_url()
        return origin if is_remote_bus(origin) else None

    def coordinator_handle(self) -> Optional[str]:
        coord = self.state.coordinator
        if not coord:
            return None
        if coord.lower() == self.alias.lower():
            return self.cfg.identity.username
        return self.cfg.peer_username_for(coord)

    def is_coordinator(self) -> bool:
        if self.ch is not None and hasattr(self.ch, "is_coordinator"):
            try:
                res = self.ch.is_coordinator()
                if isinstance(res, bool):
                    return res
            except Exception:
                pass
        return (self.state.role or "") == "coordinator"

    def _role_names(self) -> List[str]:
        from swarmly.telegram.roles import known_roles, role_dirs
        return sorted(known_roles(role_dirs(self.cfg.bus.repo)))

    def _role_catalogue(self) -> str:
        from swarmly.telegram.roles import RoleError, load_role, role_dirs
        lines = []
        for name in self._role_names():
            try:
                role = load_role(name, role_dirs(self.cfg.bus.repo))
            except RoleError:
                continue
            lines.append(f"- {role.name}: {role.description}")
        return "\n".join(lines)

    def team(self) -> Dict[str, str]:
        """alias -> role for every member this agent knows, itself included."""
        roles = {alias: self.cfg.peer_roles.get(alias, "") for alias in self.cfg.peers}
        roles[self.alias] = self.state.role or ""
        return roles

    def default_role(self) -> str:
        taken = {role for role in self.team().values() if role}
        if "coordinator" not in taken and not self.cfg.peers:
            return "coordinator"
        return next((r for r in DEFAULT_ROLE_ORDER if r not in taken), DEFAULT_ROLE_ORDER[0])

    async def _verified_admin(self, user_id: Optional[int], username: Optional[str]) -> bool:
        """Telegram says this user is a bot and an administrator of the home group."""
        if user_id is None or self.lookup is None:
            return False
        user_id = int(user_id)
        if self._verified.get(user_id):
            return True
        # A "no" is only remembered for a while: the owner often makes a bot an admin after it
        # first spoke (live run: a newcomer stayed a stranger until every transport restarted).
        checked = self._not_admin.get(user_id)
        if checked is not None and self.ch._clock() - checked < NOT_ADMIN_RECHECK_SECONDS:
            return False
        try:
            found = await self.lookup(user_id)
        except Exception as exc:                     # noqa: BLE001 - not verified, logged
            logger.warning("membership: cannot look up user %s in the group: %s", user_id, exc)
            return False
        ok = bool(found and found[0] in ADMIN_STATUSES and found[2]
                  and found[1].lower().endswith("bot")
                  and (not username or found[1].lower() == username.lstrip("@").lower()))
        if ok:
            self._verified[user_id] = True
            self._not_admin.pop(user_id, None)
        else:
            self._not_admin[user_id] = self.ch._clock()
            logger.warning("membership: @%s (id %s) is not a bot administrator of the group; "
                           "make it an admin, or /admit it", username, user_id)
        return ok

    def _add_peer(self, alias: str, username: str, user_id: Optional[int],
                  role: Optional[str] = None) -> bool:
        """Make *alias* a peer (the roster keeps it); True when it is one now."""
        known = self.cfg.peer_alias_for(username)
        if known is None:
            if not self.ch.roster._add(alias, username, via="admin"):
                return False
            known = alias
            self.ch.peers_changed()
        if user_id is not None:
            self.state.peer_ids[known] = int(user_id)
        if role and _ROLE_NAME(role) and role != "coordinator" \
                and not self.cfg.peer_roles.get(known):
            self._set_peer_role(known, role)        # what it says it is, until told otherwise
        self._save()
        return True

    def _set_peer_role(self, alias: str, role: str, coordinator: bool = False) -> bool:
        """Record *alias*'s *role*. A coordinator is recorded only when *coordinator* says the
        source may make one (the admin, or the swarm's answer to this agent's own JOIN)."""
        if not _ROLE_NAME(role) or role not in self._role_names():
            logger.warning("membership: unknown role %r for %s; ignored", role, alias)
            return False
        if role == "coordinator" and not coordinator:
            logger.warning("membership: %s cannot be made coordinator that way; ignored", alias)
            return False
        self.state.peer_roles[alias] = role
        self.cfg.peer_roles[alias] = role
        if role == "coordinator":
            self.state.coordinator = alias
        elif self.state.coordinator == alias:
            self.state.coordinator = None
        self.ch.roles_changed()
        return True

    def seen(self, alias: Optional[str], online: bool = True) -> None:
        if alias:
            self.presence[alias] = {"online": online, "at": time.time()}

    # -- start and stop -------------------------------------------------------------------
    async def on_start(self) -> None:
        """After the online notice: a newcomer (no role or no bus yet) asks to join."""
        if self.is_coordinator():
            self.state.coordinator = self.alias
        if not self.dynamic:
            return                    # set up by hand: it answers newcomers, never joins
        if self.joined():
            if self.state.onboard_pending:
                self._onboard()
            return
        if self.state.bus and not self.state.bus_ready:
            # The swarm named a bus but its checkout did not finish (a crash, a stop): again.
            await self.adopt_bus(self.state.bus)
            return
        if self.state.bus and self.state.bus_ready:
            # Started again on the agreed bus: the swarm knows this agent; only the role is
            # still to come.
            self._spawn(self._await_role())
            return
        await self.post_join()
        self._spawn(self._await_swarm())

    def _spawn(self, coro: Awaitable[None]) -> None:
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(asyncio.ensure_future(coro))

    def cancel(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    async def candidates_from_admins(self) -> None:
        """Bots among the group's administrators, so the JOIN can mention them and reach
        bots that see other bots only when mentioned. Telegram documents
        getChatAdministrators as listing the admins that are not bots; whatever bots it does
        return are used, and the JOIN still goes to everyone that reads the group."""
        bot = getattr(self.ch, "bot", None)
        home = self.cfg.identity.home_channel_id
        if bot is None or home is None:
            return
        try:
            admins = await bot.get_chat_administrators(chat_id=home)
        except Exception as exc:                     # noqa: BLE001 - best effort
            logger.info("membership: cannot list the group's administrators: %s", exc)
            return
        for member in admins or []:
            user = getattr(member, "user", None)
            name = str(getattr(user, "username", "") or "")
            if getattr(user, "is_bot", False) and name and name.lower() != self._me():
                self.candidates[name] = name
                self._verified[int(user.id)] = True

    async def post_join(self) -> None:
        await self.candidates_from_admins()
        self._joined_at = self.ch._clock()
        lines = [f"🙋 {self.alias} wants to join the swarm. Who is here, where is the bus, "
                 "and which role should I take?"]
        if self.cli:
            lines.append(f"cli: {self.cli}")
        if self.public_key:
            lines.append(f"key: {self.public_key}")
        if self.state.role:
            lines.append(f"role: {self.state.role}")
        mention = " ".join(f"@{h}" for h in self.candidates)
        if mention:
            lines.append(mention)
        try:
            await self.ch.send(format_message(
                Envelope(type=MessageType.HELLO, to="*", intent="JOIN",
                         ref=self.ch._alias_ref()), "\n".join(lines)),
                silent=True, allow_when_frozen=True)
        finally:
            self.candidates.clear()                  # only the JOIN mentions them

    async def _await_swarm(self) -> None:
        """No answer to the JOIN: this is the first agent. No role after a while: ask."""
        await self.ch._sleep(JOIN_WAIT_SECONDS)
        if not self.bus_location():
            first = not self._present_from
            await self.ask_maintainer(
                f"🙋 {self.alias} here. "
                + ("No agent answered, so I am the first one in this swarm. "
                   if first else "The swarm did not tell me where its bus is. ")
                + f"Where is the bus? Reply /bus{self._at()} <git URL or folder>."
                + (f" I will coordinate unless you give me another role with "
                   f"/role {self.cfg.identity.username} <role>." if first and not self.state.role
                   else ""))
            if first and not self.state.role:
                self.set_own_role("coordinator", by="default: the first agent")
                self.state.self_appointed = True
                self._save()
            return
        await self._await_role()

    async def _await_role(self) -> None:
        if not self.state.role:
            await self.ch._sleep(ROLE_WAIT_SECONDS)
            if not self.state.role:
                await self.ask_maintainer(
                    f"🙋 {self.alias} has no role yet"
                    + (f" (the coordinator, {self.state.coordinator}, has not given one)"
                       if self.state.coordinator else " (no coordinator answered)")
                    + f". Give me one with /role {self.cfg.identity.username} <role>: "
                    + ", ".join(self._role_names()) + ".")

    def _at(self) -> str:
        """``@thisbot``, to address a command to this agent only."""
        return "@" + self.cfg.identity.name.lstrip("@")

    async def ask_maintainer(self, text: str) -> None:
        handle = self.cfg.identity.maintainer_username
        who = f"@{handle.lstrip('@')} " if handle else ""
        await self.ch.send(f"{who}{text}", allow_when_frozen=True)

    async def on_stop(self) -> None:
        self.cancel()
        await self.ch.send(format_message(
            Envelope(type=MessageType.BYE, to="*", intent="OFFLINE", ref=self.ch._alias_ref()),
            f"🔴 {self.alias} offline"), silent=True, allow_when_frozen=True, notice=True)

    # -- inbound --------------------------------------------------------------------------
    async def handle(self, envelope: Envelope, body: str, event: "ChannelEvent") -> bool:
        """Membership traffic from a bot; True when consumed (never a turn)."""
        intent = (envelope.intent or "").upper()
        ref = str(envelope.ref) if envelope.ref else None
        if envelope.type == MessageType.BYE:
            self.seen(self.cfg.peer_alias_for(event.author_username), online=False)
            return False
        if envelope.type != MessageType.HELLO:
            return False
        sender = self.cfg.peer_alias_for(event.author_username, event.author_id)
        if intent in ("ONLINE", "SEEN"):
            # A peer that answers our online notice with SEEN is online too: after a restart
            # it is the only sign of the agents that were already running (live test: /swarm
            # showed a running peer as "unknown"). Neither says anything about roles: anyone
            # can write "role: coordinator" in one.
            if sender is None:
                # ONLINE names its sender in ref=; SEEN names whom it sees, so its sender
                # comes from the "[Alias]" banner.
                sender = await self._meet(ref if intent == "ONLINE" else _banner_alias(event.text),
                                          event)
            self.seen(sender)
            return False
        if intent == "JOIN":
            await self._on_join(ref, body, event)
            return True
        if intent == "PRESENT":
            if (envelope.to or "").lower() == self.alias.lower():
                await self._on_present(ref, body, event)
            return True
        if intent == "ROLE":
            await self._on_role(envelope, body, event, sender)
            return True
        return False

    async def _on_join(self, alias: Optional[str], body: str, event: "ChannelEvent") -> None:
        username = (event.author_username or "").lstrip("@")
        if not alias or not ALIAS_RE.match(alias) or username.lower() == self._me():
            return
        if not self.joined():
            # Still joining ourselves: nothing to tell it. A bot that joins at the same time
            # is someone the maintainer sees asking too.
            return
        known = self.cfg.peer_alias_for(username)
        if known is None and not await self._verified_admin(event.author_id, username):
            self.ch.roster.saw_online(alias, username)        # the admin can still /admit it
            if event.author_id is not None:
                fresh = int(event.author_id) not in self._waiting_joins
                self._waiting_joins[int(event.author_id)] = (alias, body, event)
                if fresh:
                    self._spawn(self._watch_waiting(int(event.author_id)))
            return
        if event.author_id is not None:
            self._waiting_joins.pop(int(event.author_id), None)
        fields = parse_fields(body)
        if not self._add_peer(known or alias, username, event.author_id, fields.get("role")):
            await self.ch.send(f"⚠️ @{username} the alias {alias} is taken in this swarm; "
                               "restart it with another --alias.", silent=True,
                               reply_to_message_id=event.message_id, notice=True)
            return
        alias = known or alias
        self.seen(alias)
        self._vouch(alias, fields.get("key"))
        lines = [f"@{username} 👋 {self.alias} here."]
        lines.append(f"role: {self.state.role or 'none'}")
        lines.append(f"coordinator: {self.state.coordinator or 'none'}")
        bus = self.advertised_bus()
        if bus:
            lines.append(f"bus: {bus}")
        if self.is_coordinator():
            members = [f"{a}=@{str(u).lstrip('@')}#{self.state.peer_ids[a]}"
                       + (f":{self.cfg.peer_roles[a]}" if self.cfg.peer_roles.get(a) else "")
                       for a, u in self.cfg.peers.items()
                       if a in self.state.peer_ids and a != alias]
            if members:
                lines.append("members: " + " ".join(members))
        await self.ch.send(format_message(
            Envelope(type=MessageType.HELLO, to=alias, intent="PRESENT",
                     ref=self.ch._alias_ref()), "\n".join(lines)),
            reply_to_message_id=event.message_id, silent=True, allow_when_frozen=True)
        if self.is_coordinator() and not self.cfg.peer_roles.get(alias):
            self._decide_role(alias, username, fields.get("cli") or "unknown")

    async def _meet(self, alias: Optional[str], event: "ChannelEvent") -> Optional[str]:
        """An unknown bot says it is online, or answers our online notice: a member that
        joined while this agent was offline, so it missed the JOIN (live run: Codex ignored
        two members until the admin /admitted them). The proof is the JOIN's: Telegram says
        it is a bot administrator of the group. Returns its alias once it is a peer."""
        username = (event.author_username or "").lstrip("@")
        if not alias or not ALIAS_RE.match(alias) or not username \
                or username.lower() == self._me() or not self.joined():
            return None
        if not await self._verified_admin(event.author_id, username):
            return None                                   # roster.saw_online keeps it pending
        if not self._add_peer(alias, username, event.author_id):
            return None
        logger.info("membership: %s (@%s) is a member this agent missed while offline; "
                    "added as a peer", alias, username)
        return alias

    async def retry_join(self, event: "ChannelEvent") -> bool:
        """A bot whose JOIN went unanswered because it was not an admin yet posts again (its
        question to the maintainer, an online notice, anything): ask Telegram once more and,
        if it is an admin now, answer the JOIN it posted. True when the JOIN was answered."""
        user_id = event.author_id
        if user_id is None or int(user_id) not in self._waiting_joins:
            return False
        if self.cfg.peer_alias_for(event.author_username, user_id) is not None:
            self._waiting_joins.pop(int(user_id), None)     # admitted another way meanwhile
            return False
        if not await self._verified_admin(user_id, event.author_username):
            return False
        alias, body, join = self._waiting_joins.pop(int(user_id))
        logger.info("membership: %s is an admin now; answering its JOIN", alias)
        await self._on_join(alias, body, join)
        return True

    async def _watch_waiting(self, user_id: int) -> None:
        """Re-check a refused JOIN now and then: a newcomer that heard no answer stops
        posting after asking the maintainer, so its next post may never come."""
        waited = 0.0
        while waited < WAITING_JOIN_SECONDS and user_id in self._waiting_joins:
            await self.ch._sleep(NOT_ADMIN_RECHECK_SECONDS + 1)
            waited += NOT_ADMIN_RECHECK_SECONDS + 1
            entry = self._waiting_joins.get(user_id)
            if entry is not None:
                await self.retry_join(entry[2])
        # After that only the newcomer's own next post re-checks it (retry_join).

    def _vouch(self, alias: str, key: Optional[str]) -> None:
        """Verify *alias*'s bus messages with the key from its JOIN until this host pulls
        the registry commit that registers it (bus.vouch_key)."""
        if not key or not self.cfg.bus.repo:
            return
        from swarmly.bus import vouch_key
        try:
            if vouch_key(Path(self.cfg.bus.repo).expanduser(), alias.lower(), key.strip()):
                logger.info("membership: %s's bus key comes from its JOIN", alias)
        except OSError as exc:
            logger.warning("membership: cannot keep %s's bus key: %s", alias, exc)

    async def _on_present(self, alias: Optional[str], body: str, event: "ChannelEvent") -> None:
        username = (event.author_username or "").lstrip("@")
        if not alias or not ALIAS_RE.match(alias):
            return
        known = self.cfg.peer_alias_for(username)
        if known is None and not await self._verified_admin(event.author_id, username):
            return
        fields = parse_fields(body)
        role = fields.get("role")
        role = role if role and role != "none" and _ROLE_NAME(role) else None
        if not self._add_peer(known or alias, username, event.author_id, role):
            return
        alias = known or alias
        self._present_from.append(alias)
        self.seen(alias)
        # Only while this agent is still joining, and only from the coordinator itself: an
        # answer that names itself the coordinator. Once the swarm has a coordinator, only
        # the admin changes it (/role @bot coordinator).
        claims = role == "coordinator" and (fields.get("coordinator") or "").lower() \
            == alias.lower()
        if claims and not self.joined() and (self.state.coordinator is None
                                             or self.state.self_appointed):
            if self.state.self_appointed:
                self._step_down(alias)
            self._set_peer_role(alias, "coordinator", coordinator=True)
        if self.state.coordinator == alias:
            for m_alias, m_user, m_id, m_role in _MEMBER.findall(fields.get("members", "")):
                if self.cfg.peer_alias_for(m_user) or m_user.lower() == self._me():
                    continue
                if await self._verified_admin(int(m_id), m_user):
                    self._add_peer(m_alias, m_user, int(m_id), m_role or None)
        bus = fields.get("bus")
        self._save()
        if bus and not self.state.bus and not self.bus_ready():
            if is_remote_bus(bus):
                await self.adopt_bus(bus)
            else:
                logger.warning("membership: %s named the bus %r, a folder on its machine; "
                               "ask the admin for a URL (/bus%s)", alias, bus, self._at())

    def _step_down(self, coordinator: str) -> None:
        """This agent took the coordinator role only because nobody answered its JOIN (the
        others could not verify it yet); the swarm's coordinator has answered now, so it gives
        the role back and waits for the one the coordinator decides."""
        logger.info("membership: %s answered as the coordinator; %s steps down from the "
                    "coordinator role it took as the first agent", coordinator, self.alias)
        self.state.role = None
        self.cfg.identity.role = None
        self.state.onboard_pending = False
        self.state.self_appointed = False
        self.state.coordinator = None
        self._save()
        self.ch.roles_changed()

    async def adopt_bus(self, location: str) -> None:
        """Take *location* as this swarm's bus (the launcher makes a checkout of it)."""
        location = safe_bus_location(location)
        if not location or (self.state.bus == location and self.state.bus_ready):
            return
        self.state.bus = location
        self._save()
        logger.info("membership: the swarm's bus is %s", location)
        if self.on_bus is not None:
            await self.on_bus(location)               # the launcher restarts the transport
            return
        if not self.cfg.bus.repo:
            logger.warning("membership: set bus.repo in %s to a checkout of %s",
                           self.cfg.source_path, location)
        if self.state.onboard_pending and self.state.role and self.bus_ready():
            self._onboard()

    async def _on_role(self, envelope: Envelope, body: str, event: "ChannelEvent",
                       sender: Optional[str]) -> None:
        """A role the coordinator gave; only the coordinator's own bot may give one."""
        target, role = envelope.to or "", str(envelope.ref or "")
        if not sender or not _ROLE_NAME(role):
            return
        coordinator = self.state.coordinator
        if coordinator is None or sender.lower() != coordinator.lower():
            logger.warning("membership: ROLE from %s ignored: it is not the coordinator",
                           sender)
            return
        self.seen(sender)
        if role == "coordinator":
            logger.warning("membership: %s cannot hand out the coordinator role; only the "
                           "admin can", sender)
            return
        if target.lower() == self.alias.lower():
            current = self.state.role
        else:
            current = next((r for a, r in self.cfg.peer_roles.items()
                            if a.lower() == target.lower()), None)
        if current == "coordinator":
            logger.warning("membership: ROLE %s for %s ignored: only the admin moves the "
                           "coordinator", role, target)
            return
        if current == role:
            return
        self.apply_role(target, role, by=sender)

    def apply_role(self, target: str, role: str, by: str) -> None:
        """Record that *target* (an alias) now has *role*."""
        if target.lower() == self.alias.lower():
            self.set_own_role(role, by=by)
            return
        alias = next((a for a in self.cfg.peers if a.lower() == target.lower()), None)
        if alias is None:
            return
        self._set_peer_role(alias, role)
        self._save()

    def set_own_role(self, role: str, by: str) -> None:
        if role not in self._role_names():
            logger.warning("membership: %s gave %s the unknown role %r; ignored", by,
                           self.alias, role)
            return
        changed = role != self.state.role
        self.state.self_appointed = False
        self.state.role = role
        self.cfg.identity.role = role
        if role == "coordinator":
            self.state.coordinator = self.alias
        elif self.state.coordinator == self.alias:
            self.state.coordinator = None
        if changed:
            self.state.onboard_pending = True
        self._save()
        self.ch.roles_changed()
        logger.info("membership: %s is now %s (given by %s)", self.alias, role, by)
        if self.state.onboard_pending and self.bus_ready():
            self._onboard()           # else on the start that has the bus checkout

    # -- admin commands -------------------------------------------------------------------
    async def handle_command(self, event: "ChannelEvent") -> bool:
        """``/bus``, ``/role`` and ``/swarm`` from the admin; True when consumed."""
        parsed = parse_command(event.text or "")
        if parsed is None or parsed[0] not in ("bus", "role", "swarm") or event.author_is_bot:
            return False
        command, target = parsed
        if target and target.lstrip("@").lower() != self._me():
            return True
        if command == "role" and not command_args(event.text or ""):
            return False                              # plain /role: the diagnostic answer
        if not self.cfg.is_admin(event.author_id):
            logger.warning("/%s from @%s ignored: only the admin changes the swarm", command,
                           event.author_username)
            return True
        args = (event.text or "").split()[1:]
        if command == "bus":
            if args:
                if not self.dynamic:
                    logger.info("/bus ignored: %s has a configured bus", self.alias)
                    return True
                if self.bus_ready() and not target:
                    return True       # a working bus moves only with /bus@thisbot
                if safe_bus_location(args[0]) != self.state.bus:
                    self.state.bus_ready = False          # a new bus: set it up again
                await self.adopt_bus(args[0])
            return True
        if command == "swarm":
            # With no coordinator every agent answers, but not a newcomer still joining: it
            # knows nothing yet (live test: a starting agent answered a /swarm from its
            # backlog with "bus: unknown, coordinator: none" while the coordinator was up).
            joining = self.dynamic and not self.joined()
            if self.is_coordinator() or target or (not self.state.coordinator and not joining):
                await self.ch.send(await self.status_text(),
                                   reply_to_message_id=event.message_id)
            return True
        handles = [a.lstrip("@") for a in args if a.startswith("@")]
        roles = [a for a in args if not a.startswith("@")]
        if len(handles) != 1 or len(roles) != 1 or not _ROLE_NAME(roles[0].lower()):
            return True
        role = roles[0].lower()
        if role not in self._role_names():
            logger.warning("/role: unknown role %r", role)
            return True
        mine = handles[0].lower() == self._me()
        alias = self.alias if mine else self.cfg.peer_alias_for(handles[0])
        if alias is None:
            return True
        if role == "coordinator":
            self._demote_coordinators(but=alias)
        if mine:
            self.set_own_role(role, by="admin")
            if role == "coordinator" and hasattr(self.ch, "_on_promoted_to_coordinator"):
                self.ch._on_promoted_to_coordinator(event)
        else:
            self._set_peer_role(alias, role, coordinator=True)
            self._save()
        return True

    def _demote_coordinators(self, but: str) -> None:
        """The admin made *but* the coordinator: whoever coordinated before becomes a
        developer (every agent applies the same /role, so all of them agree)."""
        demoted = DEFAULT_ROLE_ORDER[0]
        was_coord = self.is_coordinator()
        for alias, role in list(self.cfg.peer_roles.items()):
            if role == "coordinator" and alias.lower() != but.lower():
                self._set_peer_role(alias, demoted)
        if was_coord and self.alias.lower() != but.lower():
            self.set_own_role(demoted, by="admin: a new coordinator")
            if hasattr(self.ch, "_on_demoted_from_coordinator"):
                self.ch._on_demoted_from_coordinator()

    async def status_text(self) -> str:
        lines = [f"SWARM bus: {self.bus_location() or 'unknown'}",
                 f"coordinator: {self.state.coordinator or 'none'}"]
        # Issue #82: the same liveness line /status carries, so a peer that has gone quiet is
        # visible where the swarm is listed as well as where its own bot reports.
        presence_line = getattr(self.ch, "presence_line", None)
        if callable(presence_line):
            try:
                lines.append(presence_line())
            except Exception:                       # noqa: BLE001 - /swarm must answer
                logger.warning("membership: no presence line for /swarm", exc_info=True)
        old_releases: tuple[str, ...] = ()
        if hasattr(self.ch, "old_releases"):
            try:
                old_releases = self.ch.old_releases()
            except Exception:
                pass
        for alias, role in sorted(self.team().items()):
            if alias == self.alias:
                state = "online (me)"
            else:
                seen = self.presence.get(alias)
                state = "unknown" if seen is None else ("online" if seen["online"] else "offline")
            extra = " (running an old release)" if alias in old_releases else ""
            lines.append(f"- {alias}: {role or 'no role'}, {state}{extra}")
        for alias in sorted(old_releases):
            lines.append(f"⚠️ {alias} is running an old release")
        count = await self.member_count()
        if count is not None:
            known = len(self.cfg.peers) + 1 + 1 + len(self.cfg.identity.allowed_users)
            if count > known:
                lines.append(f"⚠️ the group has {count} members and {known} are accounted for "
                             "(swarm agents, the admin, allowed users): only those belong here")
        return "\n".join(lines)

    async def member_count(self) -> Optional[int]:
        bot = getattr(self.ch, "bot", None)
        home = self.cfg.identity.home_channel_id
        if bot is None or home is None:
            return None
        try:
            return int(await bot.get_chat_member_count(chat_id=home))
        except Exception:                            # noqa: BLE001 - shown as unknown
            return None

    # -- group service messages -----------------------------------------------------------
    async def member_left(self, user_id: Optional[int], username: Optional[str]) -> None:
        """A bot left the group: it is no longer in the swarm (it rejoins with a JOIN)."""
        if user_id is not None:
            self._waiting_joins.pop(int(user_id), None)
        alias = self.cfg.peer_alias_for(username, user_id)
        if alias is None:
            return
        if user_id is not None:
            self._verified.pop(int(user_id), None)
        self.ch.roster.drop(alias)
        was_coordinator = self.forget(alias)
        logger.info("membership: %s left the group", alias)
        # One notice, from the member whose alias sorts first, not one per bot.
        online = [a for a, p in self.presence.items() if p.get("online")] + [self.alias]
        if was_coordinator and sorted(online, key=str.lower)[0] == self.alias:
            await self.ask_maintainer(
                f"⚠️ {alias}, the coordinator, left the group. Pick a new one with "
                "/role @bot coordinator.")

    def forget(self, alias: str) -> bool:
        """*alias* is out of the swarm (it left the group, or the admin removed it): its
        role, its user id and the bus key from its JOIN go. True when it was the
        coordinator."""
        self.ch.peers_changed()
        self.state.peer_roles.pop(alias, None)
        self.cfg.peer_roles.pop(alias, None)
        user_id = self.state.peer_ids.pop(alias, None)
        if user_id is not None:
            self._verified.pop(int(user_id), None)
        self.presence.pop(alias, None)
        self._pending_roles = {u: a for u, a in self._pending_roles.items() if a != alias}
        was_coordinator = self.state.coordinator == alias
        if was_coordinator:
            self.state.coordinator = None
        self._save()
        self.ch.roles_changed()
        if self.cfg.bus.repo:
            from swarmly.bus import unvouch_key
            try:
                unvouch_key(Path(self.cfg.bus.repo).expanduser(), alias.lower())
            except OSError as exc:
                logger.warning("membership: cannot drop %s's vouched bus key: %s", alias, exc)
        return was_coordinator

    # -- turns ----------------------------------------------------------------------------
    def _decide_role(self, alias: str, username: str, cli: str) -> None:
        """Ask this coordinator's agent which role the newcomer takes."""
        team = "; ".join(f"{a}: {r or 'no role yet'}" for a, r in self.team().items()
                         if a != alias)
        text = (f"{MEMBERSHIP_MARK} {_DECIDE_MARK} {alias} (@{username}), "
                f"running {cli}.\nThe team now: {team}.\nRoles you can give:\n"
                f"{self._role_catalogue()}\n\nPick the role that best fills the team's gap "
                f"for the current work. Answer with exactly one line\nROLE: @{username} <role>\n"
                "and one short sentence saying why. Nothing else.")
        self._pending_roles[username.lstrip("@").lower()] = alias
        self.ch._run_own_task(text, int(self.cfg.identity.home_channel_id or 0), None, None)

    def _onboard(self) -> None:
        """The role's first turn: its full instructions, and the ask for a first task."""
        from swarmly.telegram.roles import RoleError
        try:
            role = self.cfg.role()
        except RoleError as exc:
            logger.warning("membership: cannot read the role %s: %s", self.state.role, exc)
            return
        if role is None or self._onboarding:
            return
        self._onboarding = True       # onboard_pending clears when the turn has answered
        coordinator = self.coordinator_handle()
        if self.is_coordinator() or not coordinator:
            # This turn's author is the agent itself: name whom to address, or it greets
            # itself (live test: "@Claudebot: Hi, I'm Claudebot").
            maintainer = (self.cfg.identity.maintainer_username or "").lstrip("@")
            ask = (f"You coordinate this swarm. Greet the maintainer"
                   f"{f' (@{maintainer})' if maintainer else ''} in two lines: say you are "
                   "ready and ask what the swarm should work on. Address the maintainer, "
                   f"not yourself: never open with @{self.cfg.identity.name.lstrip('@')}.")
        else:
            ask = (f"Introduce yourself to {coordinator} in one or two lines (your alias, your "
                   "role, your runtime) and ask for your first task.")
        text = (f"{MEMBERSHIP_MARK} {_ONBOARD_MARK} the swarm as its {role.name}. These are "
                f"your standing instructions from now on:\n\n{role.prompt(self.cfg.peer_roles)}"
                f"\n\nBus: {self.bus_location() or 'unknown'}. Coordinator: "
                f"{self.state.coordinator or 'none'}.\n\n{ask} Reply with that message only.")
        self.ch._run_own_task(text, int(self.cfg.identity.home_channel_id or 0), None, None)

    async def on_reply(self, body: str, event: "ChannelEvent") -> bool:
        """This agent's answer to a membership turn. True when it is consumed (a role
        decision is not posted as prose).

        Only the turn the transport started to decide a newcomer's role is read for a
        ``ROLE: @bot role`` line, and only the line for that newcomer counts: a ROLE line
        anywhere else (an agent quoting the protocol, a task's answer) assigns nothing."""
        text = event.text or ""
        if not event.self_task or not text.startswith(MEMBERSHIP_MARK):
            return False
        if _ONBOARD_MARK in text.split("\n", 1)[0]:
            self._onboarding = False
            if self.state.onboard_pending:
                self.state.onboard_pending = False
                self._save()
            # The coordinator answers the introduction (its first task) within seconds: let
            # that answer past this agent's bot-to-bot cooldown once, as assign() does for
            # the introduction (live test: the answer was dropped by the cooldown).
            if not self.is_coordinator() and self.state.coordinator:
                self.ch._expect_handoff(event, self.state.coordinator)
            return False              # the introduction is posted as written
        found = _DECIDE_FOR.search(text.split("\n", 1)[0])
        if found is None:
            return False
        username = found.group(2).lower()
        alias = self._pending_roles.pop(username, None)
        if alias is None or not self.is_coordinator():
            return True
        role = next((r.lower() for h, r in _ROLE_LINE.findall(body or "")
                     if h.lower() == username), None)
        if role is None or role == "coordinator" or role not in self._role_names():
            role, reason = self.default_role_for(alias), "default: no role was decided"
        else:
            reason = _reason(body)
        await self.assign(found.group(2), role, reason=reason)
        return True

    def default_role_for(self, alias: str) -> str:
        taken = {r for a, r in self.team().items() if r and a != alias}
        return next((r for r in DEFAULT_ROLE_ORDER if r not in taken), DEFAULT_ROLE_ORDER[0])

    async def apply_role_lines(self, body: str, event: "ChannelEvent") -> str:
        """The coordinator's ``ROLE: @bot <role> <reason>`` lines in any reply: switch that
        peer's role, and take the line out of the prose.

        Live, 23 Oct: after the failover drill Reasonix was left a developer, Codex was frozen
        and Antigravity paused, so two PRs had no reviewer and only the admin could fix it.
        The coordinator now fills the gap itself. A line naming no peer, an unknown role or the
        coordinator role stays in the reply as written and assigns nothing.
        """
        if "ROLE:" not in (body or "").upper() or not self.is_coordinator():
            return body
        if event.self_task and (event.text or "").startswith(MEMBERSHIP_MARK):
            return body                   # a newcomer's role turn: on_reply reads that line
        kept: List[str] = []
        for line in body.splitlines():
            found = _ROLE_LINE.match(line)
            if found is not None:
                handle, role = found.group(1), found.group(2).lower()
                reason = " ".join(line[found.end():].strip(" :-—–").split())[:200]
                if await self.assign(handle, role, reason=reason):
                    continue
            kept.append(line)
        return "\n".join(kept)

    async def assign(self, handle: str, role: str, reason: str = "") -> bool:
        """Give the agent with Telegram *handle* the *role*, as the coordinator."""
        if role not in self._role_names() or role == "coordinator" \
                or handle.lower() == self._me():
            logger.warning("membership: cannot give @%s the role %r", handle, role)
            return False
        alias = self.cfg.peer_alias_for(handle)
        if alias is None:
            return False
        if (self.cfg.peer_roles.get(alias) or "") == "coordinator" \
                or (self.state.coordinator or "").lower() == alias.lower():
            logger.warning("membership: %s is the coordinator; only the admin moves it", alias)
            return False
        if not self._set_peer_role(alias, role):
            return False
        self._save()
        # The newcomer's introduction comes right after this turn: let it past the
        # bot-to-bot cooldown once.
        home = self.cfg.identity.home_channel_id
        if home is not None:
            from swarmly.telegram.channel import ChannelEvent
            self.ch._expect_handoff(ChannelEvent(text="", chat_id=int(home)), alias)
        await self.ch.send(format_message(
            Envelope(type=MessageType.HELLO, to=alias, intent="ROLE", ref=role),
            f"🎭 @{handle.lstrip('@')} you are the swarm's {role}." + (f" {reason}" if reason
                                                                        else "")),
            silent=True, allow_when_frozen=True, notice=True)
        return True


def _reason(body: str) -> str:
    """The sentence after the ROLE line, short."""
    rest = _ROLE_LINE.sub("", body or "").strip()
    return " ".join(rest.split())[:200]
