"""
Adapter for **Reasonix** (`reasonix`), the DeepSeek coding agent CLI (reasonix.io).

Verified surface (``reasonix run --help``, v2.35.0 on Windows, 2026-10-11):

* ``reasonix run [flags] <task>`` -- one task, non-interactively; ``-p/--print`` prints only
  the final response. ``--output-format text|json|stream-json`` (default text).
* ``--permission-mode read-only|manual|ask|auto|acceptEdits|dontAsk|bypassPermissions``.
  Without one, Reasonix picks ``auto`` only where the OS sandbox confines writes and the
  folder is trusted (``reasonix trust --dir PATH --yes``), otherwise ``ask``, which a headless
  run cannot answer. The default command therefore always passes a mode.
* ``--model NAME`` picks a configured provider (default ``deepseek-flash``).
* ``--dir PATH`` changes into the project root first; the subprocess already runs in
  ``adapter.cwd``, so the default command does not need it.
* The API key comes from ``DEEPSEEK_API_KEY`` (or ``%APPDATA%\\reasonix\\.env`` /
  ``~/.config/reasonix/.env``, written by ``reasonix setup``).

* ``--resume <session id>`` continues a saved session (sessions live per working folder);
  ``--output-format json`` reports the run's ``session_id`` (checked live, 2026-10-12: a
  resumed run remembered the first turn and read its prompt from the cache). An unknown id
  fails with ``no session matches``; it never creates a named session.

Each discussion keeps its own Reasonix session: the first turn in a topic runs with
``--output-format json`` and the adapter stores the ``session_id`` it reports; later turns
there add ``--resume <id>``. One topic, one context, as with Codex and ``claude_thread``.
A custom command (not ``reasonix run``) stays one fresh run per message. Default command::

    reasonix run -p --output-format text {model} {permissions} -- {prompt}
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from swarmly.telegram.adapters.handover import HANDOVER_PROMPT, NO_HANDOVER_LEAD, Handovers
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

logger = logging.getLogger(__name__)

_SESSION_ID = re.compile(r"[\w.:-]{6,120}")

#: Turns a topic's Reasonix session may run before it is retired (adapter.session_turn_limit).
SESSION_TURN_LIMIT = 150


def parse_result_json(out: str) -> Optional[dict]:
    """The ``--output-format json`` result object; progress lines Reasonix prints before it
    are skipped. ``None`` when there is none."""
    text = (out or "").strip()
    candidates = [text] + [line.strip() for line in reversed(text.splitlines())]
    if "{" in text:
        candidates.append(text[text.index("{"):])
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and ("result" in data or "session_id" in data):
            return data
    return None


class ReasonixAdapter(SubprocessAdapter):
    """Drive Reasonix, one process per incoming message, resuming the topic's session."""

    TYPE = "reasonix"
    WANTS_TOPIC_RECAP = True                  # until a topic has a session of its own
    SESSIONS_STATE = "reasonix-sessions"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sessions: Dict[str, str] = {}              # context_id -> Reasonix session id
        # context_id -> hash of the channel rules its session was given: a resumed turn
        # then carries the one-line envelope, not the rules again (they stay in the session).
        self._rules_held: Dict[str, str] = {}
        self._state_path: Optional[Path] = None
        # Per turn: None (a plain run), "" (start the topic's session) or the id to resume.
        self._turn_session: contextvars.ContextVar[Optional[str]] = \
            contextvars.ContextVar("a2a_reasonix_session", default=None)
        self._topic_turns: Dict[str, asyncio.Lock] = {}
        self._session_turns: Dict[str, int] = {}         # context_id -> turns in its session
        self._handovers = Handovers(self.alias)

    def use_state_file(self, path: Optional[Path]) -> None:
        """Keep each discussion's session id in ``path`` (JSON) across restarts."""
        self._state_path = path
        self._handovers.attach(path)
        for context_id, entry in load_state(path, f"{self.alias}: Reasonix sessions").items():
            if not isinstance(entry, dict):
                continue
            session, rules = entry.get("session"), entry.get("rules")
            if isinstance(session, str) and _SESSION_ID.fullmatch(session):
                self._sessions[str(context_id)] = session
                if isinstance(rules, str):
                    self._rules_held[str(context_id)] = rules
                turns = entry.get("turns")
                if isinstance(turns, int) and not isinstance(turns, bool):
                    self._session_turns[str(context_id)] = turns

    def _sessions_supported(self) -> bool:
        """Only a ``reasonix run`` command can be resumed (not a custom wrapper)."""
        template = shlex.split(self.settings.command_template(), posix=(os.name != "nt"))
        return (len(template) > 1 and Path(template[0].strip("'\"")).stem.lower() == "reasonix"
                and template[1] == "run" and not {"--resume", "-c", "--continue"} & set(template))

    def topic_mode(self, context_id: Optional[str]) -> str:
        """Fresh until the discussion has a session of its own, then its session."""
        if context_id and context_id in self._sessions and self._sessions_supported():
            return "session"
        return "fresh"

    def session_for(self, context_id: Optional[str]) -> Optional[str]:
        return self._sessions.get(context_id) if context_id else None

    def _remember(self, context_id: str, session: Optional[str],
                  rules: Optional[str] = None) -> None:
        if session:
            self._sessions[context_id] = session
            if rules:
                self._rules_held[context_id] = rules
        else:
            self._sessions.pop(context_id, None)
            self._rules_held.pop(context_id, None)
            self._session_turns.pop(context_id, None)
        save_state(self._state_path, {
            key: {"session": value, "rules": self._rules_held.get(key),
                  "turns": self._session_turns.get(key, 0)}
            for key, value in self._sessions.items()}, f"{self.alias}: Reasonix sessions")

    def _turn_limit(self) -> int:
        limit = self.settings.session_turn_limit
        return SESSION_TURN_LIMIT if limit is None else max(0, int(limit))

    def _count_turn(self, context_id: str) -> bool:
        """Every turn re-reads the whole session, so a long topic gets slower and dearer
        (live run: 2.8 MB after a day, and a turn that timed out). True once the session has
        run ``adapter.session_turn_limit`` turns: it then hands over to a new one."""
        turns = self._session_turns.get(context_id, 0) + 1
        self._session_turns[context_id] = turns
        self._remember(context_id, self._sessions.get(context_id),
                       self._rules_held.get(context_id))
        limit = self._turn_limit()
        return bool(limit and turns >= limit)

    async def _hand_over(self, message: IncomingMessage, context_id: str) -> None:
        """The retiring session writes its handover; the next turn starts a new session from
        it, ahead of the topic recap."""
        session = self.session_for(context_id) or ""
        logger.info("%s: Reasonix session for %s has run %d turns; handing over to a new one",
                    self.alias, context_id, self._session_turns.get(context_id, 0))
        text: Optional[str] = None
        try:
            reply = await self._run_in(dataclasses.replace(
                message, text=HANDOVER_PROMPT, fresh_text=None), session)
            text = reply.text if reply is not None and reply.ok else None
        except Exception:                                  # noqa: BLE001 - fall back
            logger.warning("%s: the handover turn failed", self.alias, exc_info=True)
        self._handovers.save(context_id, text)
        self._remember(context_id, None)

    def _rules_digest(self, message: IncomingMessage) -> str:
        return rules_digest(channel_rules(getattr(message, "mentions", None),
                                          getattr(message, "maintainer_username", None),
                                          role=self.role_text,
                             reply_chars=getattr(message, "reply_chars", None)))

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        context_id = getattr(message, "context_id", None)
        if not context_id or not self._sessions_supported():
            return await super().respond(message)
        context_id = str(context_id)
        # Reasonix locks a session while a run holds it: one turn per topic at a time.
        lock = self._topic_turns.setdefault(context_id, asyncio.Lock())
        async with lock:
            session = self.session_for(context_id)
            taking_over = not session and self._handovers.pending(context_id)
            if taking_over:
                message = dataclasses.replace(
                    message, text=self._handovers.lead(context_id) + (message.text or ""))
            reply = await self._run_in(message, session or "")
            if session and reply is not None and not reply.ok \
                    and "no session matches" in (reply.error or ""):
                logger.warning("%s: session %s for %s is gone; starting a new one",
                               self.alias, session, context_id)
                self._remember(context_id, None)
                if message.fresh_text:          # a new session needs the task and progress
                    message = dataclasses.replace(
                        message, text=NO_HANDOVER_LEAD + message.fresh_text)
                reply = await self._run_in(message, "")
            if reply is not None and reply.ok:
                found = str((reply.meta or {}).get("session_id") or "")
                digest = self._rules_digest(message)
                if _SESSION_ID.fullmatch(found) and (
                        found != self.session_for(context_id)
                        or digest != self._rules_held.get(context_id)):
                    if found != self.session_for(context_id):
                        self._session_turns.pop(context_id, None)
                    self._remember(context_id, found, digest)
                if taking_over:
                    self._handovers.done(context_id)
                if self.session_for(context_id) and self._count_turn(context_id):
                    await self._hand_over(message, context_id)
                    reply.meta["rotated"] = True
            return reply

    async def _run_in(self, message: IncomingMessage, session: str) -> Optional[AdapterReply]:
        token = self._turn_session.set(session)
        try:
            return await super().respond(message)
        finally:
            self._turn_session.reset(token)

    def failure_summary(self, out: str, err: str, returncode: int) -> str:
        # A lost session is told apart by this line wherever it sits in the output.
        for line in f"{err}\n{out}".splitlines():
            if "no session matches" in line:
                return line.strip()[:200]
        return super().failure_summary(out, err, returncode)

    def build_argv(self, prompt: str) -> List[str]:
        argv = super().build_argv(prompt)
        session = self._turn_session.get()
        if session is None:
            return argv
        at = argv.index("--") if "--" in argv else len(argv) - 1
        head, tail = argv[:at], argv[at:]
        if "--output-format" in head:
            index = head.index("--output-format")
            if index + 1 < len(head):
                head[index + 1] = "json"
        else:
            head += ["--output-format", "json"]
        if session:
            head += ["--resume", session]
        return head + tail

    def parse_output(self, out: str, duration: float) -> AdapterReply:
        if self._turn_session.get() is None:
            return super().parse_output(out, duration)
        data = parse_result_json(out)
        if data is None:
            return super().parse_output(out, duration)
        meta = {key: data[key] for key in ("session_id", "total_cost_usd", "num_turns")
                if key in data}
        result = str(data.get("result") or "").strip()
        if data.get("is_error"):
            reason = result or str(data.get("subtype") or "error")
            return AdapterReply(ok=False, error=f"{self.alias} failed: {reason}",
                                duration_seconds=duration, meta=meta)
        if not result:
            return AdapterReply(ok=False, error=f"{self.alias} produced an empty result",
                                duration_seconds=duration, meta=meta)
        return AdapterReply(text=self.trim(result), duration_seconds=duration, meta=meta)

    def build_prompt(self, message) -> Optional[str]:
        text = (message.text or "").strip()
        if not text:
            return None
        session = self._turn_session.get()
        context_id = str(getattr(message, "context_id", None) or "")
        if session and self._rules_held.get(context_id) == self._rules_digest(message):
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
