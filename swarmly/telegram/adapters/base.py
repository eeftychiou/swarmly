"""
Adapter interface: how a chat message reaches an agent runtime, and how its answer comes back.

The channel is deliberately dumb about runtimes. An adapter receives the message text (with
the author's alias) and returns a reply, or ``None`` to stay silent. Anything that can be
driven from a command line — Codex CLI, Hermes Agent, Antigravity CLI, or your own script —
plugs in here.

No shell is ever involved: the command template is split with ``shlex`` and the prompt is
substituted as a single argument, so message text containing ``; rm -rf`` is inert.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from swarmly.telegram.children import ChildRegistry
from swarmly.telegram.config import AdapterSettings

#: Review P0-6: variables a runtime never inherits (unless listed in adapter.pass_env):
#: bot and doorbell tokens, bus signing keys, passwords, access tokens and credentials
#: files. This is a denylist, not an allowlist: CLI API keys such as ANTHROPIC_API_KEY or
#: OPENAI_API_KEY still pass (the CLI needs them to run at all), and so does any secret
#: whose name matches none of these (docs/telegram-channel.md, "What a runtime inherits").
SECRET_ENV = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|^A2A_KEY_|CREDENTIALS|_PAT$|_PASS$|_PWD$", re.I)

#: Names SECRET_ENV matches that are not secrets: A2A_KEY_DIR is the key folder's path.
NOT_SECRET_ENV = frozenset({"A2A_KEY_DIR"})

#: pass_env names worth a warning: another process's identity, not the runtime's own need.
_RISKY_PASS_ENV = re.compile(r"^A2A_KEY_|BOT_TOKEN", re.I)


def is_secret_env(name: str) -> bool:
    """Whether a runtime is kept from inheriting the variable ``name`` (review P0-6)."""
    return name.upper() not in NOT_SECRET_ENV and bool(SECRET_ENV.search(name))

logger = logging.getLogger("swarmly.telegram.adapter")


@dataclass
class IncomingMessage:
    """What the channel hands to an adapter."""

    text: str
    author_alias: str
    author_id: Optional[int] = None
    author_username: Optional[str] = None
    peer_username: Optional[str] = None
    is_admin: bool = False
    message_id: Optional[int] = None
    reply_to_alias: Optional[str] = None
    raw: Any = None
    response_instructions: str = ""
    mentions: Optional[Dict[str, str]] = None      # alias -> Telegram username used in routing prompts
    maintainer_username: Optional[str] = None      # the human's @handle, when configured
    context_id: Optional[str] = None               # the discussion (forum topic); None = General
    topic_name: Optional[str] = None               # that topic's name, when known
    general_is_for_setup: bool = False             # in General, with limits.work_in_topics on
    model: Optional[str] = None                    # the topic's /model choice; None = adapter.model
    effort: Optional[str] = None                   # the topic's /reasoning choice; None = adapter.effort
    role_brief: Optional[str] = None               # "reviewer: <description>", for the turn card
    # For a runtime that resumes the topic's session: the text as a fresh run gets it (the
    # topic's task and progress first), used when the session is gone and a new one starts.
    fresh_text: Optional[str] = None
    project: Optional[str] = None                  # the bound project key; None = unbound
    project_repo: Optional[str] = None             # the project's github repo (owner/name)
    #: Issue #31: the reply budget for this turn, set by the project bound to its topic; None
    #: means the configured default (``limits.reply_chars``, 500 unless changed).
    reply_chars: Optional[int] = None
    # Issue #29: the channel joined the ``↪ (k/n)`` parts of a split reply before this turn, so
    # ``text`` is the whole message the sender wrote -- one message, one answer.
    reassembled: bool = False


#: Characters a chat-controlled name (topic, sender) never carries into a prompt line:
#: controls and line separators, the envelope's brackets, quotes, and what cmd.exe treats
#: specially, so a crafted name can neither fake the framing nor reach a shell.
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029\[\]"`&|<>^%!]')


#: An npm shim's last line: ``"%_prog%" "%dp0%\node_modules\...\cli.js" %*``.
_NPM_SHIM_SCRIPT = re.compile(r'"%~?dp0%?\\?([^"%]+?\.[cm]?js)"\s+%\*', re.I)
#: An npm shim for a native program: ``"%dp0%\node_modules\...\tool.exe" %*``.
_NPM_SHIM_EXE = re.compile(r'"%~?dp0%?\\?([^"%]+?\.exe)"\s+%\*', re.I)
#: Patched by tests; the argv rewrite below only applies on Windows.
_ON_WINDOWS = os.name == "nt"


#: Windows caps a whole command line at 32,767 characters; past it CreateProcess fails with
#: WinError 206 and the turn never starts (live, 22 Oct). A prompt that would push the line
#: over this goes through stdin instead, for an adapter whose CLI reads it there.
ARGV_PROMPT_LIMIT = 24_000


class BatchFileRefused(RuntimeError):
    """A program resolved to a ``.cmd``/``.bat`` that cannot be run safely (review P0-8)."""


def is_batch_file(path: str) -> bool:
    """Whether Windows would run *path* through cmd.exe. Windows ignores trailing dots and
    spaces in a file name, so ``codex.cmd.`` is still ``codex.cmd``."""
    return str(path).rstrip(". ").lower().endswith((".cmd", ".bat"))


def windows_argv(argv: Sequence[str], which: Callable[[str], Optional[str]] = shutil.which
                 ) -> List[str]:
    """The argv to start on Windows: the program resolved on PATH, never a batch file.

    Review P0-8: Python does not quote arguments for cmd.exe, which runs a ``.cmd`` or
    ``.bat`` target, so prompt text such as ``"&calc&"`` would run a command outside the
    agent's sandbox. An npm shim (``codex.cmd``, ``claude.cmd``) is replaced by the node
    script it wraps; any other batch file is refused.
    """
    exec_argv = list(argv)
    if not exec_argv:
        return exec_argv
    resolved = which(exec_argv[0])
    if not resolved:
        return exec_argv
    if not is_batch_file(resolved):
        exec_argv[0] = resolved
        return exec_argv
    shim = Path(resolved.rstrip(". "))
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    refused = BatchFileRefused(
        f"refusing to run {resolved}: cmd.exe would interpret the prompt; point "
        "adapter.command at the program's .exe (or node plus its script) instead")

    def beside(relative: str) -> str:
        return str(shim.parent / relative.replace("\\", os.sep).lstrip("\\/"))

    match = _NPM_SHIM_SCRIPT.search(text)
    if match is None:
        native = _NPM_SHIM_EXE.search(text)
        if native is None:
            raise refused
        return [beside(native.group(1)), *exec_argv[1:]]
    node = shim.parent / "node.exe"
    runtime = str(node) if node.is_file() else which("node")
    if not runtime or is_batch_file(runtime):
        raise refused
    return [runtime, beside(match.group(1)), *exec_argv[1:]]


def plain(text: str, limit: int = 80) -> str:
    """A chat-controlled name made safe for a prompt's framing line."""
    return " ".join(_UNSAFE_NAME.sub(" ", str(text)).split())[:limit].strip()


GENERAL_RULE = ("GENERAL: this message is in the group's General topic, which is for setup and "
                "quick questions only. If answering takes real work (code, research, a review, "
                "a plan, a discussion with peers), do not do it here: reply with a first line "
                "NEW_TOPIC: <short title> and the brief, and the work continues in that topic.")


def discussion_line(message: Any) -> str:
    """Which discussion (forum topic) a message belongs to, for the prompt; in General, the
    rule that work moves to a topic (when the transport sets ``general_is_for_setup``)."""
    context_id = getattr(message, "context_id", None)
    if not context_id:
        return GENERAL_RULE if getattr(message, "general_is_for_setup", False) else ""
    name = plain(getattr(message, "topic_name", None) or "")
    topic = f" (Telegram topic: {name})" if name else ""
    return (f"DISCUSSION: this message is in discussion {context_id}{topic}. Use context_id "
            f"{context_id} for swarm posts about it.")


def project_line(message: Any) -> str:
    """Which project repository this message is bound to, for the prompt; "" when unbound."""
    project = plain(getattr(message, "project", None) or "")
    if not project:
        return ""
    repo = plain(getattr(message, "project_repo", None) or "")
    if repo:
        return f"Project: {project} (github.com/{repo})"
    return f"Project: {project}"


def _recipient_tag(author_alias: str, author_username: Optional[str] = None,
                   peer_username: Optional[str] = None,
                   maintainer: Optional[str] = None) -> str:
    """The @handle a reply to this message should open with."""
    if author_username:
        return f"@{author_username.lstrip('@')}"
    if peer_username:
        return f"@{peer_username.lstrip('@')}"
    if author_alias.lower() in ("admin", "human", "maintainer") and maintainer:
        return f"@{maintainer.lstrip('@')}"
    return f"@{author_alias}"


def identity_lines(me: str) -> List[str]:
    """Who this bot is. Without it a model names itself after its CLI: DeepSeek, running in
    the Reasonix CLI, signed its messages "— Reasonix" (live, round 5)."""
    name = plain(me or "", 40)
    if not name:
        return []
    return [f"- YOU ARE {name}. Speak and sign only as {name}: never as a peer, and never "
            "by the name of the CLI or model you run in."]


def _roster_lines(mentions: Optional[Dict[str, str]], maintainer: Optional[str]) -> List[str]:
    roster = [(alias, str(target).lstrip("@")) for alias, target in (mentions or {}).items()]
    roster = [(alias, handle) for alias, handle in roster if handle and not handle.isdigit()]
    if not roster and not maintainer:
        return []
    lines = ["- SWARM BOT MENTIONS: When referring to or addressing other bots, you MUST use "
             "their Telegram @username, NOT their alias:"]
    lines.extend(f"  * {alias}: @{handle}" for alias, handle in roster)
    if maintainer:
        lines.append(f"  * Human Maintainer: @{maintainer.lstrip('@')}")
    return lines


#: An agent's whole reply when it decides to say nothing; the transport posts nothing.
NO_REPLY = "NO_REPLY"
_SILENT_LEAD = re.compile(r"^(?:\s*@\w+[:,]?)*\s*")


def usage_tokens(data: Any) -> Dict[str, int]:
    """``tokens_in``/``tokens_out`` from a result's ``usage`` block, or nothing (issue #125).

    Two shapes are read. Claude's ``input_tokens`` leaves the prompt cache out, so the cache
    reads and writes are added back (live: 192 tokens for 17 turns that cost $4.60). The
    OpenAI/DeepSeek shape's ``prompt_tokens`` already counts its cache hits. A count that is
    not reported stays out, so the ledger records unknown rather than zero.
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        if isinstance(data, dict) and any(k in data for k in (
            "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
            "inputTokens", "outputTokens", "promptTokens", "completionTokens",
        )):
            usage = data
        else:
            return {}

    def count(*sources: str) -> Optional[int]:
        for source in sources:
            value = usage.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    found: Dict[str, int] = {}
    fresh = count("input_tokens", "inputTokens")
    if fresh is not None:
        found["tokens_in"] = fresh + sum(
            count(source) or 0 for source in (
                "cache_creation_input_tokens", "cache_read_input_tokens",
                "cacheCreationInputTokens", "cacheReadInputTokens",
            ))
    elif count("prompt_tokens", "promptTokens") is not None:
        found["tokens_in"] = count("prompt_tokens", "promptTokens") or 0
    out = count("output_tokens", "outputTokens", "completion_tokens", "completionTokens")
    if out is not None:
        found["tokens_out"] = out
    return found


_STRAY_SILENCE = re.compile(r"^\s*(?:@\w+[:,]?\s*)*[*_`]*NO_REPLY[*_`.!]*\s*$", re.IGNORECASE)
_ONLY_ELAPSED = re.compile(r"^\(\d+s\)$")


def drop_stray_silence(text: str) -> str:
    """A reply without its stray ``NO_REPLY`` lines (#180).

    Antigravity wrote the sentinel above real answers ("NO_REPLY\nNO_REPLY\n@peer: QA is
    PASS", ten in a row once), and a bare "NO_REPLY" reached General: the sentinel is for
    the transport, never for the group. A reply that was nothing else comes back empty.
    """
    raw = str(text or "")
    lines = raw.splitlines()
    kept = [line for line in lines if not _STRAY_SILENCE.match(line)]
    if len(kept) == len(lines):
        return raw
    out = "\n".join(kept).strip()
    return "" if _ONLY_ELAPSED.match(out) else out


def is_silent_reply(text: str) -> bool:
    """Whether a reply means "say nothing": the NO_REPLY sentinel (after any @handles), or
    a reply that only explains, in parentheses, that it is not replying."""
    core = _SILENT_LEAD.sub("", text or "").strip().strip("*_`").strip()
    if core.rstrip(".!").upper() == NO_REPLY:
        return True
    if re.match(r"^\(\s*no reply\b", core, re.I):
        return True
    # A short note that it is staying silent is silence too, not a message for the group.
    return len(core) < 200 and bool(re.search(r"\bstay(?:ing)? silent\b", core, re.I))


#: Issue #31: the channel's reply budget when nothing says otherwise. ``limits.reply_chars``
#: overrides it, and ``projects.<key>.reply_chars`` overrides that for one project's topics.
DEFAULT_REPLY_CHARS = 500

_LEAD_RULES = [
    "- The transport selected this message for you. Preserve its original addressing; a message addressed to several peers also addresses you.",
    "- An addressed question, review request, routing test, or action MUST get an answer or an explicit blocker. A transport ACK only means received, never completed.",
    "- Output ONLY your final, concise reply when ready. DO NOT output intermediate thoughts, reasoning, tool notes, or progress chatter.",
]
_TAIL_RULES = [
    "- BOTS IGNORE UNMENTIONED MESSAGES: Other bots will strictly ignore any message unless their exact @username is mentioned.",
    "- Keep the response strictly under {limit} characters.",
    "- For deep research, tables, or proposals, post on GitHub and refer to it here.",
    "- NEVER WAIT ON CI IN A LOOP: no `gh pr checks --watch`, `gh run watch`, or repeated `gh pr checks` / `gh run view` with sleeps between. Name the PR in your reply and end the turn: the transport watches its CI and starts a turn when the checks finish.",
    "- STAY IN YOUR WORKSPACE: never search the filesystem outside your working directory and the project repository (no `find /`, no scans of the home directory or other bots' folders). If what you need is not there or in the context you were given, say so and ask the admin instead of looking for it.",
    "- NO PING-PONG / NO ACK LOOPS: Stay silent ONLY for a pure acknowledgment, status confirmation or sign-off with NO question or requested action. Mixed status plus a request still requires a reply; these silence rules never cancel an action obligation.",
    "- An answer to a question you asked, a confirmation, or a peer's description of itself needs no reply unless it asks you something or hands you work.",
    f"- To stay silent, output exactly {NO_REPLY} and nothing else. Never post why you are not replying.",
    "- To take work into its own topic (separate research, a side task), reply with a first line NEW_TOPIC: <short title> and the brief below it; mention a peer in the brief to hand it over. In a topic you opened, start your final reply with REPORT_BACK: <summary> to send the summary back where the work was asked for.",
]


def _tail_rules(reply_chars: Optional[int] = None) -> List[str]:
    """The standing rules, with the reply budget this turn is held to (#31)."""
    limit = reply_chars if reply_chars and reply_chars > 0 else DEFAULT_REPLY_CHARS
    return [rule.format(limit=limit) if "{limit}" in rule else rule for rule in _TAIL_RULES]


def format_channel_prompt(
    author_alias: str,
    text: str,
    author_username: Optional[str] = None,
    peer_username: Optional[str] = None,
    mentions: Optional[Dict[str, str]] = None,
    maintainer: Optional[str] = None,
    discussion: str = "",
    project: str = "",
    role: str = "",
    card: str = "",
    reply_chars: Optional[int] = None,
    me: str = "",
) -> str:
    """Frame an inbound chat message with operational rules to suppress intermediate thinking and ping-pong chatter.

    The swarm roster and the human's handle are injected from configuration — never hardcoded
    deployment values — so the prompt stays reusable across projects. Adapters whose CLI
    takes a system prompt send :func:`channel_rules` there once and frame each message
    with :func:`channel_envelope` instead, so the rules are not repeated in every turn.
    """
    recipient_tag = plain(_recipient_tag(author_alias, author_username, peer_username,
                                         maintainer))
    lines = [
        f"[Telegram Home Channel — Inbound message from {plain(author_alias)} "
        f"({recipient_tag})]",
        "OPERATIONAL CHANNEL INSTRUCTIONS:",
        *_LEAD_RULES,
        *identity_lines(me),
        f"- Address the recipient clearly at the very start of your reply (e.g. '{recipient_tag}: ...').",
        *_roster_lines(mentions, maintainer),
        *_tail_rules(reply_chars),
    ]
    if discussion:
        lines.append(f"- {discussion}")
    if project:
        lines.append(f"- {project}")
    if role:
        lines += ["", role.strip()]
    if card:
        lines += ["", f"[{card}]"]
    lines += [
        "",
        text.strip(),
    ]
    return "\n".join(lines)


def channel_rules(mentions: Optional[Dict[str, str]] = None,
                  maintainer: Optional[str] = None, role: str = "",
                  reply_chars: Optional[int] = None, me: str = "") -> str:
    """The channel's standing rules, for a system prompt: the same text on every turn, so a
    resumed session never accumulates copies of them and the prompt cache keeps them."""
    return "\n".join([
        "You are reached through a Telegram group chat. Each user message opens with one line "
        "in square brackets saying who sent it, the @handle to answer, and, in a forum topic, "
        "which discussion it belongs to; the message itself follows.",
        "OPERATIONAL CHANNEL INSTRUCTIONS:",
        *_LEAD_RULES,
        *identity_lines(me),
        "- Address the recipient clearly at the very start of your reply, with the @handle "
        "from the message's first line (e.g. '@name: ...').",
        *_roster_lines(mentions, maintainer),
        *_tail_rules(reply_chars),
        "- When a message names a discussion, use its context_id for swarm posts about it.",
        *(["", role.strip()] if role else []),
    ])


def topic_mode(adapter: Any, context_id: Optional[str]) -> str:
    """How a turn of ``adapter`` in discussion ``context_id`` gets the discussion's context:
    "fresh" (it starts without the earlier turns, so the transport tells it the task and the
    progress), "session" (it resumes a session that holds them, so it only hears what others
    posted since its last reply) or "" (neither: a plain command, an echo)."""
    mode = getattr(adapter, "topic_mode", None)
    if callable(mode):
        return str(mode(context_id) or "")
    return "fresh" if getattr(adapter, "WANTS_TOPIC_RECAP", False) else ""


def load_state(path: Optional["os.PathLike[str]"], what: str) -> Dict[str, Any]:
    """A JSON object kept by an adapter (``use_state_file``); {} when absent or unreadable."""
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("%s file %s is unreadable (%s); starting fresh", what, path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Optional["os.PathLike[str]"], data: Dict[str, Any], what: str) -> None:
    """Write ``data`` to ``path`` atomically; a failure is logged, never raised."""
    if path is None:
        return
    target = os.fspath(path)
    tmp = None
    try:
        folder = os.path.dirname(target) or "."
        os.makedirs(folder, exist_ok=True)
        # A temp file of its own per write: two processes (the transport and the bus
        # poller) may save the same state at once.
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".", suffix=".tmp",
                                   dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, target)
        tmp = None
    except OSError as exc:
        logger.warning("could not save %s to %s: %s", what, target, exc)
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def rules_digest(rules: str) -> str:
    """A short hash of :func:`channel_rules`, to tell whether a session holds the current ones."""
    return hashlib.sha256(rules.encode("utf-8")).hexdigest()[:12]


def channel_envelope(message: Any) -> str:
    """The one line that frames a message when :func:`channel_rules` are in the system
    prompt: who sent it (a bot or a person), the @handle to answer, what it replies to, and
    its discussion."""
    alias = plain(getattr(message, "author_alias", "") or "") or "someone"
    tag = plain(_recipient_tag(alias, getattr(message, "author_username", None),
                               getattr(message, "peer_username", None),
                               getattr(message, "maintainer_username", None)))
    kind = "bot" if getattr(message, "peer_username", None) else "person"
    parts = [f"Telegram message from {alias} ({kind})", f"answer {tag}"]
    reply_to = plain(getattr(message, "reply_to_alias", None) or "")
    if reply_to:
        parts.append(f"replying to {reply_to}")
    context_id = plain(getattr(message, "context_id", None) or "")
    if context_id:
        name = plain(getattr(message, "topic_name", None) or "")
        topic = f" (topic: {name})" if name else ""
        parts.append(f"discussion {context_id}{topic}")
    elif getattr(message, "general_is_for_setup", False):
        parts.append("in General: setup and quick questions only; move real work to its own "
                     "topic with a first line NEW_TOPIC: <title>")
    proj = plain(getattr(message, "project", None) or "")
    if proj:
        repo = plain(getattr(message, "project_repo", None) or "")
        parts.append(f"project {proj} (github.com/{repo})" if repo else f"project {proj}")
    return f"[{' · '.join(parts)} — {turn_card(message)}]"


def turn_card(message: Any) -> str:
    """A short reminder of the rules that matter most, on every turn: the standing rules sit
    at the start of a long session and lose weight as it grows (live test: agents drifted to
    the wrong topic and forgot the bus). The transport enforces placement and the bus
    itself; this keeps the model from fighting it."""
    role = plain(getattr(message, "role_brief", None) or "", limit=160)
    where = ("this topic" if getattr(message, "context_id", None)
             else "General (setup only; real work goes to a topic)")
    limit = getattr(message, "reply_chars", None) or DEFAULT_REPLY_CHARS
    parts = [f"reply here, in {where}",
             "hand work to a bot with a line that starts with its @username and the task "
             "(the transport also records it on the bus)",
             f"under {limit} characters", f"{NO_REPLY} to stay silent"]
    if role:
        parts.insert(0, f"your role: {role}; stay within it")
    return f"turn rules: {' · '.join(parts)}"


@dataclass
class AdapterReply:
    """What an adapter hands back to the channel."""

    text: str = ""
    ok: bool = True
    error: str = ""
    duration_seconds: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.text.strip())


class AgentAdapter(ABC):
    """Base class for every agent runtime the channel can drive."""

    #: A runtime that starts every turn without the topic's earlier turns: the channel adds
    #: the discussion's recent messages to the prompt (live test: a bot did not recognise
    #: the answer to a hand-off it had made itself twenty minutes earlier).
    WANTS_TOPIC_RECAP = False
    #: Resumes one session per discussion, so it remembers its own turns there.
    KEEPS_TOPIC_SESSION = False

    def topic_mode(self, context_id: Optional[str]) -> str:
        """See ``topic_mode`` (module level)."""
        if self.KEEPS_TOPIC_SESSION:
            return "session"
        return "fresh" if self.WANTS_TOPIC_RECAP else ""

    def forget_session(self, context_id: Optional[str]) -> bool:
        """Drop the session this discussion resumed, if this runtime keeps one (issue #63).

        Called when a topic is deleted: the discussion it stood for is gone, so a session
        kept for its id can only be resumed by a topic that no longer exists. Returns whether
        anything was dropped; an adapter that keeps no per-topic session drops nothing.
        """
        return False

    def __init__(self, alias: str, settings: Optional[AdapterSettings] = None,
                 git_identity: Optional[Tuple[str, str]] = None):
        self.alias = alias
        self.settings = settings or AdapterSettings()
        # Who the agent is when it commits: passed through to the runtime's environment so
        # commits are authored by the agent, not by whoever owns the shell account.
        self.git_identity = git_identity or (alias, f"{alias.strip().lower()}@a2a.local")
        # github.token_env: the agent's own GitHub token, handed to its runtime as GH_TOKEN
        # so `gh` (and git over HTTPS through gh) acts as the agent, not as the host owner.
        self.github_token_env: Optional[str] = None
        # agent.role, rendered (roles/): rides with the channel rules into every turn.
        self.role_text: str = ""
        self.role_name: str = ""                    # agent.role, for the role guard
        self.acting_coordinator: bool = False       # holds the lease: guarded as coordinator
        self.drift_path: str = ""                   # the drift ledger the guard reports to
        self.lease_term: int = 0                    # the term that lease is at (#228)
        self.guard_dir: Optional[Path] = None       # where guard.py's wrappers go
        # Review R6-1: exact names, so A2A_KEY_REVIEW_CODEX or ..._BOT_TOKEN_OLD_CODEX is
        # not mistaken for Codex's own.
        upper = re.sub(r"[^A-Z0-9_]", "_", alias.strip().upper())
        own = {f"A2A_KEY_{upper}", f"A2A_TELEGRAM_BOT_TOKEN_{upper}"}
        for name in self.settings.pass_env:
            risky = _RISKY_PASS_ENV.search(name) or is_secret_env(name)
            if risky and name.upper() not in own:
                logger.warning("adapter.pass_env hands %s to %s's runtime: a secret that is "
                               "not this agent's own key or default bot token, which that "
                               "agent can then use", name, alias)

    def environment(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """The environment this agent's runtime will see.

        Every adapter carries the agent's git identity, so a commit made from a run is
        authored by the agent rather than by whoever owns the shell account:
        ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` override the repository's
        ``user.name``/``user.email`` for this process only.
        """
        source = base if base is not None else os.environ
        # Review P0-6: the runtime runs whatever a message talks it into; it must not be
        # able to print the bot tokens or bus keys this process holds.
        env = {k: v for k, v in source.items() if not is_secret_env(k)}
        for name in self.settings.pass_env:
            if name in source:
                env[name] = source[name]
        if self.github_token_env and source.get(self.github_token_env):
            # Deliberate exception to the secret filter: this token IS the agent's identity
            # on GitHub (github: in config.yaml), scoped to what that account may do.
            env["GH_TOKEN"] = source[self.github_token_env]
        env.setdefault("A2A_AGENT_ALIAS", self.alias)
        name, email = self.git_identity
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_NAME"] = name
        env["GIT_COMMITTER_EMAIL"] = email
        if self.role_name and self.guard_dir is not None:
            # gh and git wrappers that refuse what the role rules out (guard.py).
            from swarmly.telegram.guard import prepare
            role = "coordinator" if self.acting_coordinator else self.role_name
            env = prepare(self.guard_dir, role, env, alias=self.alias, term=self.lease_term,
                          drift=self.drift_path)
        return env

    @abstractmethod
    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        """Produce a reply for an incoming message, or None to stay silent."""

    async def close(self) -> None:                       # pragma: no cover - trivial
        """Release resources (sessions, subprocess pools)."""

    def describe(self) -> str:
        return f"{type(self).__name__}(alias={self.alias!r}, type={self.settings.type!r})"

    def accepts_exact_username_during_loop_guard(self) -> bool:
        """Whether an exact username mention is always delivered to this adapter.

        Stateful adapters use this narrow escape hatch to preserve addressed-session
        delivery (the method name predates the removal of the turn cap; behavior is
        unchanged). Stateless adapters retain the default ``False``.
        """
        return self.accepts_explicit_username_delivery()

    def accepts_explicit_username_delivery(self) -> bool:
        """Whether exact username-addressed peer messages must reach this adapter.

        Stateless adapters retain the existing status/ack filtering and loop protection.
        Stateful adapters may opt in when the addressed message must be inserted into an
        active conversation regardless of its surface wording.
        """
        return False

    async def verify_delivery(self, message: IncomingMessage) -> Optional[AdapterReply]:
        """Optional adapter-level delivery probe (issue #59 deliverable 6).

        The bus poller calls this AFTER :meth:`respond` has accepted the message. Its job
        is to confirm the durable delivery signal is in place (a queued Codex message
        whose session recorded it; an Antigravity thread that actually enqueued the
        prompt; etc.). The default implementation returns ``None``, meaning
        "unknown / not implemented" — the poller treats that as "do not escalate".

        Contract:

        * Return an :class:`AdapterReply` with ``ok=True`` when the delivery is confirmed.
        * Return an :class:`AdapterReply` with ``ok=False`` when the delivery is *known*
          not to have happened (clean negative).
        * Raise an exception to report a probe *error* — the poller logs and degrades,
          NEVER escalating on a probe error (spec: "probe error ⇒ log and continue").
        """
        return None


Runner = Callable[[Sequence[str], Optional[str], Dict[str, str], float], Awaitable[Any]]


class SubprocessAdapter(AgentAdapter):
    """
    Runs an external CLI once per incoming message and returns its stdout.

    ``runner`` is injectable so tests never spawn a process.
    """

    def __init__(self, alias: str, settings: Optional[AdapterSettings] = None,
                 runner: Optional[Runner] = None,
                 git_identity: Optional[Tuple[str, str]] = None):
        super().__init__(alias, settings, git_identity=git_identity)
        self._runner = runner or self._default_runner
        # Per turn, so parallel topics can run different models: the topic's /model choice.
        self._turn_model: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            "a2a_turn_model", default=None)
        self._turn_effort: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            "a2a_turn_effort", default=None)
        # Issue #68: the pids of the CLIs this adapter runs. With no path nothing is kept,
        # so a transport that was never given a state file cannot reap what it left behind.
        self.children = ChildRegistry(None)
        # Round 5.5 (#161): when the CLI of each running turn last wrote anything, keyed by
        # the discussion that turn belongs to -- one adapter can run a topic's turn and
        # another's at the same time (limits.max_parallel_turns), and a chatty topic must not
        # make a hung one look alive. Times are monotonic; a discussion missing from here has
        # no run this adapter can see, so the caller falls back to "since the turn started".
        self._output_at: Dict[str, float] = {}
        self._run_key: contextvars.ContextVar[str] = contextvars.ContextVar(
            "a2a_run_key", default="")
        #: The prompt this task's CLI reads on stdin, when argv could not carry it (#179).
        self._stdin_text: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            "a2a_stdin_text", default=None)

    def last_output_at(self, key: Optional[str] = None) -> Optional[float]:
        """``time.monotonic()`` when a running CLI last wrote to stdout or stderr.

        *key* names the discussion (``IncomingMessage.context_id``); without one this answers
        for the discussion the current task is running a turn for. An adapter that cannot see
        its CLI's output (it reads the pipes only at the end) simply has nothing here, and the
        transport then reports "silent since the turn started".
        """
        return self._output_at.get(self._run_key.get() if key is None else str(key))

    def _note_output(self) -> None:
        """Record that this task's CLI said something just now."""
        self._output_at[self._run_key.get()] = time.monotonic()

    def use_child_registry(self, path: Optional[Path]) -> None:
        """Persist the pids of the CLIs this adapter starts, so a restart can reap orphans."""
        self.children = ChildRegistry(path)

    #: Whether this CLI reads the prompt from stdin when argv carries none (``claude -p``).
    STDIN_PROMPT = False

    def prompt_off_argv(self, argv: List[str], prompt: str) -> Tuple[List[str], Optional[str]]:
        """``(argv, stdin_text)``: the prompt moves to stdin when it would make the command
        line too long for Windows, if this CLI reads it there; otherwise argv is unchanged."""
        if not self.STDIN_PROMPT or not prompt or prompt not in argv:
            return argv, None
        if sum(len(word) + 3 for word in argv) <= ARGV_PROMPT_LIMIT:
            return argv, None
        at = len(argv) - 1 - argv[::-1].index(prompt)
        out = argv[:at] + argv[at + 1:]
        if out and out[-1] == "--":                 # nothing positional follows it any more
            out = out[:-1]
        return out, prompt

    # -- command construction ----------------------------------------------------------
    def build_argv(self, prompt: str) -> List[str]:
        template = self.settings.command_template(model=self._turn_model.get(),
                                                  effort=self._turn_effort.get())
        argv = shlex.split(template, posix=(os.name != "nt"))
        if not argv:
            raise ValueError(f"adapter '{self.settings.type}' produced an empty command")
        out: List[str] = []
        substituted = False
        for token in argv:
            if "{prompt}" in token:
                out.append(token.replace("{prompt}", prompt))
                substituted = True
            else:
                out.append(token)
        if not substituted:
            out.append(prompt)               # template without {prompt}: append it
        # Windows splitting (posix=False) keeps the quotes around a quoted program path,
        # but the program must reach CreateProcess unquoted -- and the `check` preflight
        # resolves the same unquoted form, so the two have to agree.
        out[0] = out[0].strip("'\"")
        return out

    def environment(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = super().environment(base)
        env.update(self.settings.extra_env)
        return env

    # -- execution ---------------------------------------------------------------------
    def _default_runner(self, argv: Sequence[str], cwd: Optional[str], env: Dict[str, str],
                        timeout: float) -> Awaitable[Any]:
        exec_argv = windows_argv(argv) if _ON_WINDOWS else list(argv)
        # No stdin: a CLI that reads it when it is not a terminal (``codex exec`` does)
        # sees EOF at once. Its own process group (POSIX) lets a timeout or /stop end
        # the whole tree, launcher and children included.
        extra: Dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
        stdin = (asyncio.subprocess.PIPE if self._stdin_text.get() is not None
                 else asyncio.subprocess.DEVNULL)
        return asyncio.create_subprocess_exec(
            *exec_argv, cwd=cwd, env=env, stdin=stdin,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **extra)

    async def _run(self, argv: Sequence[str]) -> Any:
        logger.debug("%s: running %s in %s (timeout %ss)", self.alias, _describe_argv(argv),
                     self.settings.cwd or ".", self.settings.timeout_seconds)
        started = time.monotonic()
        process = await self._runner(argv, self.settings.cwd, self.environment(),
                                     float(self.settings.timeout_seconds))
        try:
            self.children.add(getattr(process, "pid", None), _describe_argv(argv))
        except Exception:                                    # noqa: BLE001 - never blocks a turn
            logger.warning("%s: could not record the CLI pid %s", self.alias,
                           getattr(process, "pid", None), exc_info=True)
        self._note_output()                                  # the run started: not silent yet
        try:
            stdout, stderr = await asyncio.wait_for(self._communicate(process),
                                                    timeout=float(self.settings.timeout_seconds))
        except BaseException as exc:
            # A timeout or a /stop cancels the wait, not the program: end it too, or it keeps
            # running (and editing files) after the channel has moved on.
            logger.warning("%s: CLI run ended by %s after %.1fs; killing pid %s", self.alias,
                           type(exc).__name__, time.monotonic() - started,
                           getattr(process, "pid", "?"))
            await _kill(process)
            self._forget_child(process)                     # it is gone, not orphaned (#68)
            raise
        rc = getattr(process, "returncode", 0)
        out, err = _decode(stdout), _decode(stderr)
        logger.log(logging.WARNING if rc else logging.DEBUG,
                   "%s: CLI exited %s after %.1fs (stdout %d chars, stderr %d chars)%s",
                   self.alias, rc, time.monotonic() - started, len(out), len(err),
                   f"; stderr tail:\n{_tail(err)}" if rc and err.strip() else "")
        if out:
            logger.debug("%s: stdout head:\n%s", self.alias, out[:LOG_HEAD_CHARS])
        if err and not rc:
            logger.debug("%s: stderr tail:\n%s", self.alias, _tail(err))
        self._forget_child(process)
        return process, stdout, stderr

    async def _communicate(self, process: Any) -> Tuple[bytes, bytes]:
        """Read both pipes as they arrive, recording when the CLI last said anything.

        ``communicate()`` hands everything back at the end, so a turn that printed for twenty
        minutes looked silent for twenty minutes and could only be judged by the hard timeout
        (#161). Reading chunk by chunk keeps the silence clock honest and costs nothing when
        the CLI writes nothing. A process without readable pipes -- the runner a test injects
        -- is left to ``communicate()``: its silence is then "since the run started".
        """
        text = self._stdin_text.get()
        sink = getattr(process, "stdin", None)
        if text is not None and sink is not None and hasattr(sink, "write"):
            sink.write(text.encode("utf-8"))           # the prompt, when argv could not carry it
            try:
                await sink.drain()
            finally:
                sink.close()
        out, err = getattr(process, "stdout", None), getattr(process, "stderr", None)
        if out is None or err is None or not hasattr(out, "read") or not hasattr(err, "read"):
            return await process.communicate()
        buffers = (bytearray(), bytearray())

        async def pump(stream: Any, sink: bytearray) -> None:
            while True:
                chunk = await stream.read(65536)
                if not chunk:                            # EOF: the CLI closed its side
                    return
                sink.extend(chunk)
                self._note_output()

        await asyncio.gather(pump(out, buffers[0]), pump(err, buffers[1]))
        await process.wait()
        return bytes(buffers[0]), bytes(buffers[1])

    def _forget_child(self, process: Any) -> None:
        """The CLI exited: drop its pid from the registry (#68)."""
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            self.children.remove(pid)
        except Exception:                                    # noqa: BLE001 - best effort
            logger.warning("%s: could not drop the CLI pid %s", self.alias, pid, exc_info=True)

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        if not self.settings.enabled:
            return None
        # Which discussion this turn is for, so the silence clock of two turns this adapter
        # runs at once stay apart (#161 review). A task of its own, so it needs no reset.
        self._run_key.set(str(getattr(message, "context_id", "") or ""))
        prompt = self.build_prompt(message)
        if prompt is None:
            return None
        token = self._turn_model.set(getattr(message, "model", None))
        effort_token = self._turn_effort.set(getattr(message, "effort", None))
        try:
            argv = self.build_argv(prompt)
        finally:
            self._turn_model.reset(token)
            self._turn_effort.reset(effort_token)
        rules = self.system_rules(message)
        if rules:
            argv = self.add_system_rules(argv, rules)
        argv, stdin_text = self.prompt_off_argv(argv, prompt)
        self._stdin_text.set(stdin_text)
        started = time.monotonic()
        try:
            process, stdout, stderr = await self._run(argv)
        except asyncio.TimeoutError:
            return AdapterReply(ok=False, error=f"{self.alias} timed out after "
                                                f"{self.settings.timeout_seconds}s",
                                duration_seconds=time.monotonic() - started,
                                meta={"timed_out": True})
        except FileNotFoundError as exc:
            return AdapterReply(ok=False, error=f"{self.alias} could not start: {exc}",
                                duration_seconds=time.monotonic() - started)
        except BatchFileRefused as exc:
            return AdapterReply(ok=False, error=f"{self.alias} could not start: {exc}",
                                duration_seconds=time.monotonic() - started)
        except PermissionError as exc:
            return AdapterReply(ok=False, error=f"{self.alias} permission error (check binary path, cwd, or directory collision): {exc}",
                                duration_seconds=time.monotonic() - started)

        duration = time.monotonic() - started
        out = _decode(stdout)
        err = _decode(stderr)
        returncode = getattr(process, "returncode", 0)

        if returncode != 0:
            summary = self.failure_summary(out, err, returncode)
            return AdapterReply(ok=False, error=f"{self.alias} failed: {summary}",
                                duration_seconds=duration, meta={"returncode": returncode})
        if not out.strip():
            err_tail = (err or "").strip()
            if err_tail:
                last_err = err_tail.splitlines()[-1]
                detail = f"empty stdout; stderr: {last_err}"
            else:
                detail = "empty stdout and stderr"
            return AdapterReply(
                ok=False,
                error=f"{self.alias} produced no output ({detail}, returncode: {returncode}, duration: {duration:.1f}s)",
                duration_seconds=duration,
                meta={"returncode": returncode, "empty": True},
            )
        return self.parse_output(out, duration)

    # -- hooks -------------------------------------------------------------------------
    def system_rules(self, message: IncomingMessage) -> Optional[str]:
        """The channel rules to pass as a system prompt for this run, or ``None`` when the
        CLI has no such option (then :meth:`build_prompt` keeps them in the message)."""
        return None

    def add_system_rules(self, argv: List[str], rules: str) -> List[str]:
        """Put :meth:`system_rules` on the command line; only called when they are set."""
        return argv

    def failure_summary(self, out: str, err: str, returncode: int) -> str:
        """One line saying why a run that exited non-zero failed."""
        detail = (err or out).strip().splitlines()
        return detail[-1] if detail else f"exit code {returncode}"

    def parse_output(self, out: str, duration: float) -> AdapterReply:
        """Turn a successful run's stdout into the reply. Override for structured output."""
        return AdapterReply(text=self.trim(out), duration_seconds=duration)

    def build_prompt(self, message: IncomingMessage) -> Optional[str]:
        """How an incoming chat message becomes a prompt. Override for richer framing."""
        text = (message.text or "").strip()
        if not text:
            return None
        line = discussion_line(message)
        proj = project_line(message)
        if self.role_text:
            text = f"{self.role_text.strip()}\n\n{text}"
        parts = []
        if line:
            parts.append(f"[{line}]")
        if proj:
            parts.append(f"[{proj}]")
        if parts:
            return f"{''.join(parts)}\n{text}"
        return text

    def trim(self, text: str) -> str:
        limit = self.settings.max_output_chars
        if len(text) <= limit:
            return text
        head = text[:limit].rstrip()
        return f"{head}\n… [{len(text) - limit} more characters truncated]"


async def _kill(process: Any) -> None:
    """End a run and everything it started: a launcher (an npm shim, a wrapper script) can
    leave the real CLI running, still writing to the session the next turn resumes."""
    if getattr(process, "returncode", 0) is not None:
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            if os.name == "nt":
                # By full path (a bare name may be found in the working folder first). Wait
                # for it, without blocking the loop, before the fallback kill() below: with
                # the root gone first, taskkill /T has no tree to walk.
                taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                        "System32", "taskkill.exe")
                killer = await asyncio.create_subprocess_exec(
                    taskkill, "/PID", str(pid), "/T", "/F", stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), timeout=10)
            elif os.getpgid(pid) == pid:            # its own group (start_new_session)
                os.killpg(pid, signal.SIGKILL)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            _kill_direct(process)          # even if a second /stop cancels the wait above
    else:
        _kill_direct(process)


def _kill_direct(process: Any) -> None:
    kill = getattr(process, "kill", None)
    if kill is None or getattr(process, "returncode", 0) is not None:
        return
    try:
        kill()
    except ProcessLookupError:
        pass


LOG_HEAD_CHARS = 2000


def _tail(text: str, lines: int = 30, chars: int = 4000) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:]


def _describe_argv(argv: Sequence[str]) -> str:
    """argv for the log, with the (long, private) prompt shortened."""
    shown = []
    for arg in argv:
        arg = str(arg)
        shown.append(arg if len(arg) <= 200 else f"<{len(arg)} chars: {arg[:80]!r}…>")
    return " ".join(shown)


def _decode(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def looks_like_event_stream(text: str) -> bool:
    """Whether *text* is a CLI's JSON event stream (``--output-format stream-json``).

    Such a run prints one object per line, each with a ``type``. Asking this is how a caller
    tells "the CLI streamed events and no result event arrived" from "the CLI printed plain
    text": the first is a broken turn, the second is a reply, and posting the events as prose
    would put machine noise in the topic (round 5.5, #161).
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 1:
        return False
    typed = 0
    for line in lines[:50]:
        if not line.startswith("{"):
            return False
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict) or not event.get("type"):
            return False
        typed += 1
    return typed > 0 and len(lines) > 1
