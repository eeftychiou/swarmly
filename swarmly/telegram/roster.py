"""Dynamic swarm membership (#152): a bot joins the group's swarm with the owner's approval.

Each bot's ``peers:`` config used to be the only list of the other bots, so adding one meant
editing every other config, and configs that disagreed misrouted messages. Now:

1. On start every transport posts ``HELLO intent=ONLINE ref=<its alias>``. A bot that does
   not know the sender remembers it as *pending*: its alias, and its Telegram username,
   which Telegram vouches for.
2. The admin approves it in the group with ``/admit @newbot`` (several handles may follow).
   Only the admin can: any bot added to the group would otherwise join the swarm.
3. Every bot holding that bot as pending adds it to its roster and posts a silent
   ``HELLO intent=WELCOME ref=<its alias>`` that mentions the newcomer.
4. The newcomer, having seen the admin admit it, adds each welcoming bot to its own roster.

``/remove @bot`` from the admin takes a bot out of every roster. The roster is kept next to
the config (``<config>.roster.json``) and merged into ``peers`` at load; a peer written in
the config always wins and is never removed by a command. Everything goes through the group,
so it works with the bots on different machines.

``/sync`` from the admin catches up a bot that was offline during an ``/admit``: every bot
posts a silent ``HELLO intent=SYNC ref=<its alias>`` listing its *first-hand* peers (its
config, the bots it admitted itself, and the bots Telegram confirmed as group administrators
when they joined) as ``Alias=@username``. In the next 10 minutes a bot
adds any bot that one of its known peers lists. A peer learned from a welcome or a list is
not passed on, so a bot that slipped into one roster goes no further. ``/remove`` leaves a
tombstone: a removed bot comes back only with a new ``/admit``.

The newcomer has no anchor of its own: during its window it trusts the bot accounts that
welcome it by its alias and @username (trust on first use). After an ``/admit``, check the
@username of each bot that welcomed it and ``/remove`` any that is not a swarm bot (``/status``
lists aliases only).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from swarmly.telegram.adapters.base import load_state, save_state

logger = logging.getLogger("swarmly.telegram.roster")

#: How long after the admin admits this bot it accepts welcomes from the swarm.
ADMIT_WINDOW_SECONDS = 600.0
#: How long a HELLO from an unknown bot stays admissible.
PENDING_SECONDS = 24 * 3600.0
#: At most this many unknown bots are remembered at once.
PENDING_LIMIT = 32
#: How long after the admin's ``/sync`` this bot takes peer lists from the swarm.
SYNC_WINDOW_SECONDS = 600.0
#: How a runtime peer was learned that counts as first hand: the admin's ``/admit``, or
#: Telegram confirming it as a bot administrator of the group (the membership handshake).
FIRST_HAND = ("admit", "admin")
#: An alias is a single header token (it travels in ``ref=``).
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
#: Telegram usernames: 5-32 characters, and a bot's ends in "bot".
_HANDLE = re.compile(r"@([A-Za-z0-9_]{5,32})\b")
#: One ``Alias=@username`` entry of a ``/sync`` peer list.
_ENTRY = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{1,32})=@([A-Za-z0-9_]{5,32})\b")


def roster_path(cfg: Any) -> Optional[Path]:
    source = getattr(cfg, "source_path", None)
    return Path(source).with_suffix(".roster.json") if source else None


def _is_bot_username(username: str) -> bool:
    return bool(username) and username.lower().endswith("bot")


def apply_roster(cfg: Any) -> Dict[str, str]:
    """Merge the saved roster into ``cfg.peers``; returns what it added. Config peers win."""
    joined = load_state(roster_path(cfg), "roster").get("peers") or {}
    added: Dict[str, str] = {}
    if not isinstance(joined, dict):
        return added
    own = str(cfg.identity.alias).lower()
    aliases = {a.lower() for a in cfg.peers}
    targets = {str(t).lstrip("@").lower() for t in cfg.peers.values()}
    targets.add(str(cfg.identity.name).lstrip("@").lower())          # never this bot itself
    for alias, username in joined.items():
        alias, username = str(alias), str(username).lstrip("@")
        if (not ALIAS_RE.match(alias) or alias.lower() in aliases or alias.lower() == own
                or username.lower() in targets or not _is_bot_username(username)):
            continue
        cfg.peers[alias] = username
        added[alias] = username
        aliases.add(alias.lower())
        targets.add(username.lower())
    # What came from the file, as opposed to the config: only these are runtime peers that
    # /remove may drop. A config peer that is also in the file stays a config peer.
    merged = getattr(cfg, "_roster_added", None)
    if merged is None:
        merged = cfg._roster_added = {}
    merged.update(added)
    return added


class Roster:
    """The bots this transport learned at runtime, on top of its configured peers."""

    def __init__(self, cfg: Any, *, clock: Any = time.time) -> None:
        self.cfg = cfg
        self.path = roster_path(cfg)
        self._clock = clock
        apply_roster(cfg)                  # no-op when AgentConfig.load already merged it
        self.joined: Dict[str, str] = {
            alias: str(username) for alias, username in getattr(cfg, "_roster_added", {}).items()
            if str(cfg.peers.get(alias, "")).lstrip("@").lower() == str(username).lower()}
        state = load_state(self.path, "roster")
        via = state.get("via") if isinstance(state.get("via"), dict) else {}
        #: How each runtime peer was learned: "admit" or "admin" (first hand), "welcome" or
        #: "sync".
        self.via: Dict[str, str] = {a: str(via.get(a, "admit")) for a in self.joined}
        removed = state.get("removed")
        #: Bots the admin removed: a welcome or a peer list does not bring them back.
        self.removed = ({str(u).lstrip("@").lower() for u in removed}
                        if isinstance(removed, list) else set())
        self.pending: Dict[str, Tuple[str, float]] = {}     # username -> (alias, seen at)
        self._admitted_at: Optional[float] = None
        self._sync_at: Optional[float] = None

    # -- helpers -----------------------------------------------------------------------
    def _me(self) -> str:
        return self.cfg.identity.name.lstrip("@").lower()

    def _known(self, username: str) -> bool:
        handle = username.lstrip("@").lower()
        return handle == self._me() or any(
            str(t).lstrip("@").lower() == handle for t in self.cfg.peers.values())

    def _alias_taken(self, alias: str) -> bool:
        """An alias in use, or one that is a bot's @username (it would shadow that bot)."""
        low = alias.lower()
        return low in (self.cfg.identity.alias.lower(), self._me()) or any(
            a.lower() == low or str(t).lstrip("@").lower() == low
            for a, t in self.cfg.peers.items())

    def _alias_of(self, handle: str) -> Optional[str]:
        low = handle.lstrip("@").lower()
        for alias, target in self.cfg.peers.items():
            if str(target).lstrip("@").lower() == low:
                return alias
        return None

    def _save(self) -> None:
        save_state(self.path, {"peers": dict(self.joined),
                               "via": {a: self.via.get(a, "admit") for a in self.joined},
                               "removed": sorted(self.removed)}, "roster")

    def _add(self, alias: str, username: str, via: str = "admit") -> bool:
        if username.lstrip("@").lower() in self.removed:
            logger.warning("roster: not adding %s (@%s): the admin removed it", alias, username)
            return False
        if self._alias_taken(alias) or self._known(username):
            logger.warning("roster: not adding %s (@%s): the alias or the bot is already a "
                           "peer", alias, username)
            return False
        self.cfg.peers[alias] = username
        self.joined[alias] = username
        self.via[alias] = via
        self._save()
        logger.info("roster: added %s (@%s)", alias, username)
        return True

    @staticmethod
    def handles(text: str) -> List[str]:
        """The @handles a command names after the command word itself."""
        rest = re.sub(r"^\s*/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?", "", text or "")
        return [h for h in _HANDLE.findall(rest)]

    # -- events ------------------------------------------------------------------------
    def saw_online(self, alias: Optional[str], username: Optional[str]) -> None:
        """An unknown bot came online: remember it until the admin admits it."""
        handle = (username or "").lstrip("@")
        if not alias or not ALIAS_RE.match(alias) or not _is_bot_username(handle) \
                or self._known(handle):
            return
        now = self._clock()
        self.pending = {u: v for u, v in self.pending.items() if now - v[1] < PENDING_SECONDS}
        self.pending.pop(handle.lower(), None)
        self.pending[handle.lower()] = (alias, now)
        while len(self.pending) > PENDING_LIMIT:
            self.pending.pop(next(iter(self.pending)))
        logger.info("roster: %s (@%s) is online and not a peer; the admin can /admit @%s",
                    alias, handle, handle)

    def admit(self, handles: List[str]) -> List[Tuple[str, str]]:
        """Apply the admin's ``/admit``: returns the (alias, username) pairs to welcome, the
        bots this bot added and the named bots it already had. A handle naming this bot
        opens the window for the swarm's welcomes."""
        added = []
        now = self._clock()
        lifted = {h.lower() for h in handles} & self.removed
        if lifted:
            self.removed -= lifted                # the admin admits it again
            self._save()
        for handle in handles:
            low = handle.lower()
            if low == self._me():
                self._admitted_at = now
                continue
            known = self._alias_of(low) if _is_bot_username(low) else None
            if known is not None:
                added.append((known, handle))     # already a peer: welcome it again, so a
                continue                          # newcomer that missed it can catch up
            entry = self.pending.get(low)
            if entry is None or now - entry[1] >= PENDING_SECONDS:
                logger.info("roster: /admit @%s, but no HELLO from it reached this bot",
                            handle)
                continue
            if self._add(entry[0], handle):
                added.append((entry[0], handle))
            self.pending.pop(low, None)
        return added

    def welcomed(self, alias: Optional[str], username: Optional[str]) -> bool:
        """A swarm bot welcomed this one after the admin admitted it: add the sender."""
        handle = (username or "").lstrip("@")
        if self._admitted_at is None or self._clock() - self._admitted_at > ADMIT_WINDOW_SECONDS:
            return False
        if not alias or not ALIAS_RE.match(alias) or not _is_bot_username(handle) \
                or self._known(handle):
            return False
        return self._add(alias, handle, "welcome")

    def start_sync(self) -> None:
        """The admin asked for ``/sync``: take peer lists from known peers for a while."""
        self._sync_at = self._clock()

    def sync_list(self) -> str:
        """This bot's first-hand peers, for its ``/sync`` answer: its config, the bots it
        admitted itself and the bots Telegram confirmed as group administrators when they
        joined (bots only: never the owner). A peer it only heard of from a welcome or another
        bot's list is not passed on, so trust does not travel further. (Live run: every bot
        had joined with ``swarmly --cli``, so every answer was "peers: none".)"""
        second_hand = {alias for alias in self.joined
                       if self.via.get(alias) not in FIRST_HAND}
        return " ".join(f"{alias}=@{str(target).lstrip('@')}"
                        for alias, target in self.cfg.peers.items()
                        if alias not in second_hand and ALIAS_RE.match(alias)
                        and _is_bot_username(str(target).lstrip("@")))

    def synced(self, sender: Optional[str], text: str) -> List[Tuple[str, str]]:
        """Add the bots a known peer lists in its ``/sync`` answer. Returns what was added."""
        if self._sync_at is None or self._clock() - self._sync_at > SYNC_WINDOW_SECONDS:
            return []
        if not sender or not self._known(sender) or sender.lower() == self._me():
            return []
        added = []
        for alias, handle in _ENTRY.findall(text or ""):
            if not _is_bot_username(handle) or self._known(handle) or self._alias_taken(alias):
                continue
            if self._add(alias, handle, "sync"):
                added.append((alias, handle))
        return added

    def remove(self, handles: List[str]) -> List[str]:
        """Apply the admin's ``/remove``: drop runtime peers (never configured ones)."""
        removed = []
        wanted = {h.lower() for h in handles} - {self._me()}
        for alias, username in list(self.joined.items()):
            if username.lstrip("@").lower() in wanted:
                del self.joined[alias]
                self.via.pop(alias, None)
                self.cfg.peers.pop(alias, None)
                removed.append(alias)
        for handle in wanted:
            self.pending.pop(handle, None)
        if removed or not wanted <= self.removed:
            self.removed |= wanted                # a welcome or a peer list won't bring it back
            self._save()
        if removed:
            logger.info("roster: removed %s", ", ".join(removed))
        return removed

    def drop(self, alias: str) -> bool:
        """Forget a runtime peer that left the group, without the tombstone ``/remove``
        leaves: it may come back (a configured peer stays)."""
        if alias not in self.joined:
            return False
        del self.joined[alias]
        self.via.pop(alias, None)
        self.cfg.peers.pop(alias, None)
        self._save()
        logger.info("roster: %s left", alias)
        return True
