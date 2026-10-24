"""
Channel commands of the A2A Telegram channel protocol v1.1 (spec §6).

A peer cannot see whether another runtime is alive, cannot read channel history, and cannot ask
BotFather about Bot-to-Bot mode. These three commands answer what is answerable and label the
rest ``unknown`` — a diagnostics line that guesses is worse than one that admits a gap, because
the guess is what a peer debugs against.

All replies are single-line and inside the channel budget: they are signalling, not reporting.
"""

from __future__ import annotations

import datetime
import re
from typing import List, Optional, Tuple

#: ``/cmd`` or ``/cmd@username``, optionally with trailing arguments.
# A bot's own posts open with its "[Alias] " label, so a peer's /ping arrives as
# "[Codex] /ping@bot": the label is allowed before the command.
_COMMAND = re.compile(r"^\s*(?:\[[^\]\n]{1,40}\]\s*)?/(?P<cmd>[A-Za-z0-9_]+)"
                      r"(?:@(?P<target>[A-Za-z0-9_]+))?")

DIAG_COMMANDS = ("ping", "status", "diag", "where", "role")

#: The menu Telegram offers when someone types "/" (setMyCommands). Every transport registers
#: this list on start, replacing whatever an earlier program left on the same bot token
#: (live: one bot still offered another tool's /memory, /cron and /session).
MENU_COMMANDS: Tuple[Tuple[str, str], ...] = (
    ("status", "This bot's state, model and peers"),
    ("usage", "What this bot spent today; add 'all' for the swarm"),
    ("swarm", "The swarm: bus, coordinator and members"),
    ("round", "What the open round waits on"),
    ("wakes", "Retired: a promise is a row now -- see /owed"),
    ("owed", "What is owed right now: the open rows"),
    ("drift", "Rule violations this bot made, and its drift score"),
    ("storetest", "Admin: check the Telegram facts the state store relies on (-Alias skips)"),
    ("state", "The shared state store as this bot sees it"),
    ("model", "Show or set the model for this topic"),
    ("reasoning", "Show or set the reasoning effort for this topic"),
    ("memory", "Admin: show this bot's memory.md, or clear it"),
    ("role", "Show or set a bot's role"),
    ("ping", "Check that the bot answers"),
    ("where", "Where this bot runs"),
    ("stop", "Admin: stop this bot's running turn"),
    ("pause", "Admin: pause this bot"),
    ("resume", "Admin: resume a paused bot"),
    ("hold", "Admin: hold the swarm: answer, but start nothing"),
    ("release", "Admin: release a held swarm"),
    ("cli", "Admin: run a native CLI command, no tokens"),
    ("topic", "Admin: these issues or PRs live in this topic"),
    ("cleanup", "Admin: tidy finished topics"),
    ("admit", "Admin: admit a bot to the swarm"),
    ("remove", "Admin: remove a bot from the swarm"),
    ("sync", "Admin: resync the roster"),
)

#: A peer transport's built-in answer to one of those commands (or /model): machine traffic.
_COMMAND_REPLY = re.compile(r"^(?:@\w+[\s:,]*)*(?:\[[^\]\n]{1,40}\]\s*)?"
                            r"(?:PONG|STATUS|DIAG|WHERE|ROLE|MODEL)\s+\S")
# The /model and /reasoning answers lead with the bot's alias ("Codex runs gpt-x here ...").
_SETTING_REPLY = re.compile(
    r"^(?:@\w+[\s:,]*)*(?:\[[^\]\n]{1,40}\])?[\w .-]{0,40}?"
    r"(?: (?:runs|reasons at|goes back to) [^\n]{1,60}? here (?:\(|from the next turn)"
    r"| kept [^\n]{1,60}?: | cannot (?:switch models|set reasoning effort):"
    r"|: only the admin can (?:switch my model|set my reasoning effort)"
    r"|: /(?:model|reasoning) takes one)")


def is_command_reply(text: str) -> bool:
    """Whether ``text`` is a transport's built-in command answer (PONG, STATUS, ROLE, ...).
    Such answers go out as Telegram replies; the asking bot must not take them as a turn."""
    text = (text or "").strip()
    return bool(_COMMAND_REPLY.match(text) or _SETTING_REPLY.match(text))


def parse_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Return ``(command, target_username)`` for ``/cmd`` and ``/cmd@bot``, else ``None``."""
    if not text:
        return None
    match = _COMMAND.match(text)
    if not match:
        return None
    return match.group("cmd").lower(), match.group("target")


def command_args(text: str) -> List[str]:
    """The words after ``/cmd[@bot]`` (a sender's "[Alias]" banner is not one), without the
    @handles of other bots the message names."""
    match = _COMMAND.match(text or "")
    rest = (text or "")[match.end():] if match else ""
    return [word for word in rest.split() if not word.startswith("@")]


def _utc(now: Optional[float] = None) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) if now is None \
        else datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: Optional[object]) -> str:
    """Collapse whitespace so a reply stays one parseable line."""
    if value is None:
        return "unknown"
    text = " ".join(str(value).split())
    return text or "unknown"


def ping_reply(alias: str, state: str = "idle", now: Optional[float] = None) -> str:
    """Definitive liveness for the runtime behind the bot — not for the Telegram account."""
    return f"PONG {_clean(alias)} state={_clean(state)} t={_utc(now)}"


def status_reply(alias: str, task: str, result: str, blocker: str = "none") -> str:
    """One line: what it is doing, what it last produced, what is in the way."""
    return (f"STATUS {_clean(alias)} task={_clean(task)} "
            f"result={_clean(result)} blocker={_clean(blocker)}")


def diag_reply(alias: str, bot: str, node: str, transport: str,
               session: Optional[str], group: str, admin: Optional[bool], seen: int) -> str:
    """The facts a peer cannot otherwise discover.

    ``admin=None`` renders as ``admin=unknown``: group-admin rights cannot be observed from
    inside the process, and a guessed true/false would be worse than an admitted gap.
    ``b2b=unknown`` is permanent and correct for the Hermes side: Bot-to-Bot Communication Mode
    is a BotFather setting with no API. Peers must test delivery empirically instead of asking.
    """
    bot_name = _clean(bot)
    if not bot_name.startswith("@"):
        bot_name = "@" + bot_name
    admin_text = "unknown" if admin is None else ("true" if admin else "false")
    return (f"DIAG alias={_clean(alias)} bot={bot_name} node={_clean(node)} "
            f"transport={_clean(transport)} session={_clean(session)} group={_clean(group)} "
            f"admin={admin_text} b2b=unknown seen={int(seen)}")


def where_reply(alias: str, chat_id: int, thread_id: Optional[int],
                project: Optional[str] = None, repo: Optional[str] = None,
                workdir: Optional[str] = None) -> str:
    """Report the chat, topic id, and bound project workspace."""
    topic_str = str(thread_id) if thread_id is not None else "general"
    if project:
        parts = [
            f"chat={chat_id}",
            f"topic={topic_str}",
            f"project={_clean(project)}",
            f"repo={_clean(repo)}",
            f"workdir={_clean(workdir)}",
        ]
        return f"WHERE {_clean(alias)} {' '.join(parts)}"
    return f"WHERE {_clean(alias)} chat={chat_id} topic={topic_str} not bound: Telegram only"
