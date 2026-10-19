"""
Adapters for **Claude Code** (`claude`).

Verified headless surface (``claude --help``, Claude Code 2.1, checked 2026-10-07):

* ``claude -p "<prompt>"``         — print mode: one non-interactive turn, then exit. The
                                     working directory's ``CLAUDE.md`` loads as normal.
* ``--output-format json``         — one JSON object on stdout: ``result`` (the reply text),
                                     ``is_error``, ``session_id``, ``total_cost_usd``, ...
* ``--permission-mode MODE``       — ``acceptEdits`` lets the run edit files without asking;
                                     ``bypassPermissions`` also runs shell commands unprompted
* ``--session-id UUID``            — start a new session with this id
* ``--resume ID``                  — run the turn inside an existing session (its whole
                                     history is in context); unknown ids exit 1 with
                                     ``No conversation found with session ID: ...``
* ``--append-system-prompt-file F`` — add F's text to the system prompt; the channel rules
                                     go there, so each message carries one framing line
* ``--``                           — end of options, so a prompt can never be read as a flag

Two adapter types:

``claude``
    One fresh session per incoming message (stateless, like ``codex`` and ``hermes``)::

        claude -p --output-format json --permission-mode acceptEdits -- {prompt}

``claude_thread``
    Every message continues one persistent session, so the agent remembers the channel's
    earlier messages. The session is ``adapter.conversation`` when set (a session id from
    ``claude --resume``'s picker or ``~/.claude/projects/``), else a stable id derived from
    the alias and working directory, created on first use.

``adapter.command`` overrides the template for both (for example to add ``--model`` or to
choose ``--permission-mode bypassPermissions`` on a sandboxed host); keep ``-p``, the JSON
output format and the ``{prompt}`` placeholder.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import json
import os
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from swarmly.telegram.adapters.base import (
    AdapterReply,
    IncomingMessage,
    SubprocessAdapter,
    channel_envelope,
    channel_rules,
    discussion_line,
    format_channel_prompt,
    load_state,
    project_line,
    rules_digest,
    save_state,
    turn_card,
)
from swarmly.telegram.config import AdapterSettings

_SESSION_NAMESPACE = uuid.UUID("6f1c2a7e-5d4b-4c1e-9a0f-a2a0c1a0de00")
_NO_SESSION_MARKER = "no conversation found"
_SESSION_IN_USE_MARKER = "is already in use"
_SYSTEM_PROMPT_FLAGS = ("--system-prompt", "--system-prompt-file", "--append-system-prompt",
                        "--append-system-prompt-file")


_rules_dir: Optional[str] = None


def rules_file(alias: str, rules: str) -> Path:
    """A file holding ``rules``, named by their hash so a changed roster gets a new file.

    A file rather than an argument: on Windows a multi-line argument may pass through
    ``cmd.exe`` (an npm ``.cmd`` shim), which cuts it at the first line break. It lives in
    a folder only this process can write (``mkdtemp``, mode 0700), removed at exit, so no
    other user can plant or swap the rules."""
    global _rules_dir
    if _rules_dir is None or not os.path.isdir(_rules_dir):
        _rules_dir = tempfile.mkdtemp(prefix="a2a-rules-")
        atexit.register(shutil.rmtree, _rules_dir, True)
    safe = "".join(c if c.isalnum() else "-" for c in alias.lower()) or "agent"
    path = Path(_rules_dir) / f"{safe}-{rules_digest(rules)}.txt"
    if not path.is_file():
        path.write_text(rules, encoding="utf-8")
    return path


def parse_claude_json(out: str) -> Optional[dict]:
    """The ``--output-format json`` result object, or ``None`` when stdout is not JSON
    (for example a custom command without ``--output-format json``)."""
    text = (out or "").strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        # Some wrappers print a line before the JSON; take the last JSON line.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
        else:
            return None
    return data if isinstance(data, dict) else None


class ClaudeAdapter(SubprocessAdapter):
    WANTS_TOPIC_RECAP = True                  # one fresh session per turn
    """Drive Claude Code in print mode, one process (and one fresh session) per message."""

    TYPE = "claude"

    @classmethod
    def default_settings(cls, cwd: Optional[str] = None, model: Optional[str] = None,
                         permission_mode: str = "acceptEdits",
                         timeout_seconds: int = 900) -> AdapterSettings:
        parts = ["claude", "-p", "--output-format", "json"]
        if permission_mode:
            parts += ["--permission-mode", permission_mode]
        if model:
            parts += ["--model", model]
        parts += ["--", "{prompt}"]
        return AdapterSettings(type=cls.TYPE, cwd=cwd, timeout_seconds=timeout_seconds,
                               command=" ".join(parts))

    def build_prompt(self, message: IncomingMessage) -> Optional[str]:
        """Claude Code loads the working directory's ``CLAUDE.md`` itself, so the prompt
        carries only the channel framing: who is asking, plus the reply rules when they
        cannot go in the system prompt."""
        text = (message.text or "").strip()
        if not text:
            return None
        rules = self.system_rules(message)
        if rules and not self.rules_inline(rules):
            return f"{channel_envelope(message)}\n{text}"
        return format_channel_prompt(
            message.author_alias,
            text,
            author_username=getattr(message, "author_username", None),
            peer_username=getattr(message, "peer_username", None),
            mentions=getattr(message, "mentions", None),
            maintainer=getattr(message, "maintainer_username", None),
            discussion=discussion_line(message),
            project=project_line(message),
            role=self.role_text,
            card=turn_card(message),
            reply_chars=getattr(message, "reply_chars", None),
        )

    def system_rules(self, message: IncomingMessage) -> Optional[str]:
        """The channel rules, unless the command is not ``claude`` or sets its own."""
        template = shlex.split(self.settings.command_template(), posix=(os.name != "nt"))
        if not template or Path(template[0].strip("'\"")).stem.lower() != "claude" \
                or any(t.split("=", 1)[0] in _SYSTEM_PROMPT_FLAGS for t in template):
            return None
        return channel_rules(getattr(message, "mentions", None),
                             getattr(message, "maintainer_username", None),
                             role=self.role_text,
                             reply_chars=getattr(message, "reply_chars", None))

    def rules_inline(self, rules: str) -> bool:
        """Whether this turn also needs the rules in the message (a fresh session never does)."""
        return False

    def add_system_rules(self, argv: List[str], rules: str) -> List[str]:
        flags = ["--append-system-prompt-file", str(rules_file(self.alias, rules))]
        at = argv.index("--") if "--" in argv else len(argv) - 1
        return argv[:at] + flags + argv[at:]

    def parse_output(self, out: str, duration: float) -> AdapterReply:
        data = parse_claude_json(out)
        if data is None:
            return super().parse_output(out, duration)
        meta = {key: data[key] for key in ("session_id", "total_cost_usd", "num_turns")
                if key in data}
        result = str(data.get("result") or "").strip()
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            reason = result or str(data.get("subtype") or "error")
            return AdapterReply(ok=False, error=f"{self.alias} failed: {reason}",
                                duration_seconds=duration, meta=meta)
        if not result:
            return AdapterReply(ok=False, error=f"{self.alias} produced an empty result",
                                duration_seconds=duration, meta=meta)
        return AdapterReply(text=self.trim(result), duration_seconds=duration, meta=meta)


class ClaudeThreadAdapter(ClaudeAdapter):
    WANTS_TOPIC_RECAP = False
    KEEPS_TOPIC_SESSION = True                # resumes the topic's session
    """Continue one persistent Claude Code session with every incoming message.

    Each discussion (forum topic) has its own session: the General topic uses
    ``adapter.conversation`` or the derived id, and a topic's ``context_id`` derives its
    own, so parallel topics never share a conversation. Turns on one session are
    serialised: two messages never resume the same session at once.
    """

    TYPE = "claude_thread"
    SESSIONS_STATE = "claude-sessions"

    @classmethod
    def default_settings(cls, cwd: Optional[str] = None, session: Optional[str] = None,
                         model: Optional[str] = None, permission_mode: str = "acceptEdits",
                         timeout_seconds: int = 900) -> AdapterSettings:
        settings = super().default_settings(cwd=cwd, model=model,
                                            permission_mode=permission_mode,
                                            timeout_seconds=timeout_seconds)
        settings.type = cls.TYPE
        settings.conversation = session
        return settings

    def __init__(self, alias: str, settings: Optional[AdapterSettings] = None,
                 runner=None, git_identity=None):
        super().__init__(alias, settings, runner=runner, git_identity=git_identity)
        configured = (self.settings.conversation or "").strip()
        self.session_id = configured or self.derived_session_id(alias, self.settings.cwd)
        self.session_configured = bool(configured)
        # Per turn, so turns in different topics can run at once: which session this turn
        # resumes, and whether to create it instead (resume first; create once if missing).
        self._session: contextvars.ContextVar[str] = contextvars.ContextVar(
            "a2a_claude_session", default=self.session_id)
        self._create: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "a2a_claude_create", default=False)
        self._turns: Dict[str, asyncio.Lock] = {}
        self._known: set = set()            # sessions this process has seen work
        # session -> hash of the rules it is known to hold. Claude Code records a session's
        # system prompt on its first request and replays that record on resume, so a
        # session made before the rules moved there, or with an older roster, gets them
        # inline once.
        self._rules_held: Dict[str, str] = {}
        self._state_path: Optional[Path] = None

    def use_state_file(self, path: Optional[Path]) -> None:
        """Keep, per session, the hash of the channel rules it holds in ``path`` (JSON), so a
        restart does not send them inline again to sessions that already have them."""
        self._state_path = path
        held = load_state(path, f"{self.alias}: Claude sessions")
        self._rules_held.update({str(k): str(v) for k, v in held.items() if v})
        self._known.update(self._rules_held)    # these exist: resume them, never re-create

    @property
    def _create_next(self) -> bool:
        return self._create.get()

    @staticmethod
    def derived_session_id(alias: str, cwd: Optional[str],
                           context_id: Optional[str] = None) -> str:
        """A stable session id per agent, working directory and discussion."""
        seed = f"a2a:{alias.strip().lower()}:{os.path.abspath(cwd or os.getcwd())}"
        if context_id:
            seed += f":{context_id}"
        return str(uuid.uuid5(_SESSION_NAMESPACE, seed))

    def session_for(self, message: IncomingMessage) -> str:
        context_id = getattr(message, "context_id", None)
        if not context_id:
            return self.session_id
        return self.derived_session_id(self.alias, self.settings.cwd, context_id)

    def forget_session(self, context_id: Optional[str]) -> bool:
        """A deleted topic's Claude session is dropped (issue #63).

        The session id is derived from the discussion, so there is nothing to keep: dropping
        the id from the known set means the topic -- if a new one ever reused the id, which it
        cannot -- would start fresh rather than resume a discussion that is gone.
        """
        if not context_id:
            return False
        session = self.derived_session_id(self.alias, self.settings.cwd, context_id)
        if session not in self._known and session not in self._rules_held:
            return False
        self._known.discard(session)
        self._rules_held.pop(session, None)
        save_state(self._state_path, self._rules_held, f"{self.alias}: Claude sessions")
        return True

    def _can_create(self, session_id: Optional[str] = None) -> bool:
        try:
            uuid.UUID(session_id or self._session.get())
        except ValueError:
            return False                 # a session *name* can be resumed, not created
        return True

    def build_argv(self, prompt: str) -> List[str]:
        argv = super().build_argv(prompt)
        session = self._session.get()
        flags = (["--session-id", session] if self._create.get()
                 else ["--resume", session])
        # Insert before the end-of-options marker (or before the prompt when there is none).
        at = argv.index("--") if "--" in argv else len(argv) - 1
        return argv[:at] + flags + argv[at:]

    def rules_inline(self, rules: str) -> bool:
        return (not self._create.get()
                and self._rules_held.get(self._session.get()) != rules_digest(rules))

    def accepts_explicit_username_delivery(self) -> bool:
        """A persistent session should see every message explicitly addressed to it."""
        return True

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        session = self.session_for(message)
        lock = self._turns.setdefault(session, asyncio.Lock())
        async with lock:
            self._session.set(session)
            # A topic's session is usually new: create it first rather than fail a resume.
            # One that exists already (after a restart) refuses, and is then resumed.
            # A configured session is the user's existing one: resume it first.
            configured = self.session_configured and session == self.session_id
            create_first = (not configured and session not in self._known
                            and self._can_create(session))
            self._create.set(create_first)
            try:
                reply = await super().respond(message)
            finally:
                self._create.set(False)
            if (create_first and reply is not None and not reply.ok
                    and _SESSION_IN_USE_MARKER in (reply.error or "").lower()):
                reply = await super().respond(message)
            elif (reply is not None and not reply.ok
                    and _NO_SESSION_MARKER in (reply.error or "").lower()):
                if not self._can_create(session):
                    return AdapterReply(
                        ok=False, duration_seconds=reply.duration_seconds,
                        error=(f"{self.alias}: no Claude Code session named "
                               f"{session!r} in {self.settings.cwd or 'this directory'}"
                               " (set adapter.conversation to an existing session id)"))
                self._create.set(True)
                try:
                    reply = await super().respond(message)
                finally:
                    self._create.set(False)
            if reply is not None and reply.ok:
                self._known.add(session)
                rules = self.system_rules(message)
                if rules and self._rules_held.get(session) != rules_digest(rules):
                    self._rules_held[session] = rules_digest(rules)
                    save_state(self._state_path, self._rules_held,
                               f"{self.alias}: Claude sessions")
            if reply is not None:
                reply.meta.setdefault("conversation", session)
                if getattr(message, "context_id", None):
                    reply.meta.setdefault("context_id", message.context_id)
            return reply
