"""
Adapter for **OpenAI Codex CLI** (`codex`).

Verified non-interactive surface (developers.openai.com/codex/noninteractive and the CLI
reference, checked 2026-09-16):

* ``codex exec "<prompt>"``      — non-interactive run; final response on stdout, no TUI
* ``--sandbox workspace-write``  — allow edits inside the workspace (``read-only`` and
                                   ``danger-full-access`` are the other two levels)
* ``--json``                     — emit a JSON event stream instead of prose
* ``-o/--output-last-message``   — write the final message to a file
* ``codex exec [options] resume --last`` — continue the previous session (exec
                                   options such as ``--sandbox`` go before ``resume``)
* ``codex queue --thread THREAD --message TEXT`` — add a message to an existing session
* ``--full-auto``                — deprecated; the CLI warns and it is not used here

Default command::

    codex exec --sandbox workspace-write {prompt}

When the swarm's MCP server is configured (``swarmly mcp-setup codex``), each run also gets
``-c mcp_servers.a2a.tools.<tool>.approval_mode=approve`` for the six a2a tools: ``codex
exec`` never prompts, so without it every a2a call is refused. The approval lives on this
command line only, so the owner's interactive Codex still asks, and the sandbox stays on.

The channel's standing rules go in ``-c developer_instructions=...`` (a TOML string on one
line), so each message carries only a one-line envelope instead of the whole rule block.
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
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from swarmly.telegram.adapters.base import (
    AdapterReply,
    AgentAdapter,
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
from swarmly.telegram.adapters.handover import HANDOVER_PROMPT, NO_HANDOVER_LEAD, Handovers
from swarmly.telegram.config import AdapterSettings, ConfigError

logger = logging.getLogger(__name__)

#: The a2a MCP tools a swarm run may call without a prompt (swarmly/swarm/mcp.py).
A2A_TOOLS = ("a2a_escalations", "a2a_guard", "a2a_inbox", "a2a_next", "a2a_post", "a2a_tasks")
_A2A_TABLE = re.compile(r"""^\s*\[\s*mcp_servers\s*\.\s*(["']?)a2a\1\s*\]""", re.M)


def a2a_server_configured(env: dict) -> bool:
    """Whether ``$CODEX_HOME/config.toml`` (``~/.codex``) has an ``[mcp_servers.a2a]`` table.

    Approving tools of a server Codex does not load makes it refuse to start ("invalid
    transport in mcp_servers.a2a"), so the override is only added when the table exists.
    A project's ``.codex/config.toml`` is not counted: Codex skips it in an untrusted
    folder, and ``mcp-setup`` never writes there.
    """
    home = env.get("CODEX_HOME") or os.environ.get("CODEX_HOME")
    place = Path(home) if home else Path.home() / ".codex"
    try:
        text = (place / "config.toml").read_text(encoding="utf-8-sig")
    except OSError:
        return False
    return bool(_A2A_TABLE.search(text))


def a2a_approval_args() -> List[str]:
    out: List[str] = []
    for tool in A2A_TOOLS:
        out += ["-c", f"mcp_servers.a2a.tools.{tool}.approval_mode=approve"]
    return out


class CodexAdapter(SubprocessAdapter):
    """Drive the Codex CLI, one process per incoming message.

    Each discussion (forum topic) keeps its own Codex session: the first message runs
    ``codex exec --json`` and keeps the ``thread_id`` it reports, and later messages run
    ``codex exec ... resume <thread_id> --json``, so the topic's earlier turns stay in
    context. Messages in General start fresh, as before. The ids are kept in a state file
    when the channel gives one (``use_state_file``), so they survive a restart.
    """

    TYPE = "codex"
    SESSIONS_STATE = "codex-sessions"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._threads: Dict[str, str] = {}          # context_id -> Codex thread_id
        self._state_path: Optional[Path] = None
        self._turns: Dict[str, asyncio.Lock] = {}
        # Per turn: None (plain run), "" (new session, --json), or the thread to resume.
        self._thread: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            "a2a_codex_thread", default=None)
        # Per turn: whether failure_summary found that the resumed session is gone.
        self._gone: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "a2a_codex_gone", default=False)
        # context_id -> hash of the rules its session is known to hold; a session resumed
        # without a match (made before the rules moved, or an older roster) gets them inline
        # once.
        self._rules_held: Dict[str, str] = {}
        # Topics whose session was retired for size: their next session starts from the recap.
        self._retired: Set[str] = set()
        self._handovers = Handovers(self.alias)

    # -- sessions per discussion ---------------------------------------------------------
    def use_state_file(self, path: Optional[Path]) -> None:
        """Keep each discussion's Codex thread id, and the hash of the channel rules its
        session holds, in ``path`` (JSON), across restarts."""
        self._state_path = path
        self._handovers.attach(path)
        for context_id, entry in load_state(path, f"{self.alias}: Codex sessions").items():
            thread = entry if isinstance(entry, str) else (
                entry.get("thread") if isinstance(entry, dict) else None)
            if not thread:
                continue
            # The file sits where the agent may write, and the id goes on the command line
            # after `resume`: anything but one plain id would be read as an option.
            if not isinstance(thread, str) or not _THREAD_ID.fullmatch(thread):
                logger.warning("%s: ignoring Codex session %r for %s in %s: not a session id",
                               self.alias, thread, context_id, path)
                continue
            self._threads[str(context_id)] = thread
            if isinstance(entry, dict) and entry.get("rules"):
                self._rules_held[str(context_id)] = str(entry["rules"])

    def _remember(self, context_id: str, thread_id: Optional[str],
                  rules: Optional[str] = None) -> None:
        if thread_id and not _THREAD_ID.fullmatch(thread_id):
            logger.warning("%s: not keeping Codex session %r: not a session id",
                           self.alias, thread_id)
            thread_id = None
        if thread_id:
            self._threads[context_id] = thread_id
        else:
            self._threads.pop(context_id, None)
        if rules:
            self._rules_held[context_id] = rules
        else:
            self._rules_held.pop(context_id, None)
        state = {key: ({"thread": tid, "rules": self._rules_held[key]}
                       if key in self._rules_held else tid)
                 for key, tid in self._threads.items()}
        save_state(self._state_path, state, f"{self.alias}: Codex sessions")

    def topic_mode(self, context_id: Optional[str]) -> str:
        """A ``codex exec`` command resumes the discussion's thread; a custom one does not."""
        return "session" if context_id and self._sessions_supported() else ""

    def thread_for(self, context_id: Optional[str]) -> Optional[str]:
        return self._threads.get(context_id) if context_id else None

    def forget_session(self, context_id: Optional[str]) -> bool:
        """A deleted topic's thread is dropped (issue #63): nothing can resume it again."""
        if not context_id or context_id not in self._threads:
            return False
        self._remember(context_id, None)
        return True

    def _sessions_supported(self) -> bool:
        """Only a ``codex exec`` command can be resumed (not a custom wrapper)."""
        template = shlex.split(self.settings.command_template(), posix=(os.name != "nt"))
        return (len(template) > 1 and Path(template[0].strip("'\"")).stem.lower() == "codex"
                and template[1] == "exec" and "--ephemeral" not in template
                and "resume" not in template)

    def _token_limit(self) -> int:
        limit = self.settings.session_token_limit
        return SESSION_TOKEN_LIMIT if limit is None else max(0, int(limit))

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        context_id = getattr(message, "context_id", None)
        if not context_id or not self._sessions_supported():
            return await super().respond(message)
        lock = self._turns.setdefault(context_id, asyncio.Lock())
        async with lock:
            thread = self.thread_for(context_id)
            taking_over = not thread and (context_id in self._retired
                                          or self._handovers.pending(context_id))
            if taking_over:
                # The topic's session was retired for size: the new one starts from the old
                # one's handover, then the task and progress, not just the latest messages.
                message = dataclasses.replace(
                    message, text=self._handovers.lead(context_id)
                    + (message.fresh_text or message.text or ""))
            self._thread.set(thread or "")
            self._gone.set(False)
            try:
                reply = await super().respond(message)
            finally:
                self._thread.set(None)
            if thread and reply is not None and not reply.ok and self._gone.get():
                logger.warning("%s: Codex session %s for %s is gone; starting a new one",
                               self.alias, thread, context_id)
                self._thread.set("")
                if message.fresh_text:          # a new session needs the task and progress
                    message = dataclasses.replace(
                        message, text=NO_HANDOVER_LEAD + message.fresh_text)
                try:
                    reply = await super().respond(message)
                finally:
                    self._thread.set(None)
            if reply is not None:
                found = reply.meta.get("conversation") or self.thread_for(context_id)
                rules = self.system_rules(message)
                digest = rules_digest(rules) if rules else None
                if reply.ok and found and (found != thread
                                           or digest != self._rules_held.get(context_id)):
                    self._remember(context_id, found, digest)
                    self._retired.discard(context_id)
                if reply.ok and taking_over:
                    self._handovers.done(context_id)
                used = reply.meta.get("input_tokens")
                limit = self._token_limit()
                if reply.ok and limit and isinstance(used, int) and used >= limit and found:
                    # Every call re-reads the whole thread, so a long topic costs more each
                    # turn: past the limit the old session writes a handover and the next
                    # turn starts a new session from it.
                    logger.info("%s: Codex session for %s has read %d input tokens (limit "
                                "%d); handing over to a new one", self.alias,
                                context_id, used, limit)
                    self._handovers.save(context_id,
                                         await self._handover_turn(message, found))
                    self._remember(context_id, None)
                    self._retired.add(context_id)
                    reply.meta["rotated"] = True
                reply.meta.setdefault("context_id", context_id)
            return reply

    async def _handover_turn(self, message: IncomingMessage, thread: str) -> Optional[str]:
        """One more turn in the retiring session: its handover (or None)."""
        self._thread.set(thread)
        try:
            reply = await super().respond(dataclasses.replace(
                message, text=HANDOVER_PROMPT, fresh_text=None))
        except Exception:                                  # noqa: BLE001 - fall back
            logger.warning("%s: the handover turn failed", self.alias, exc_info=True)
            return None
        finally:
            self._thread.set(None)
        return reply.text if reply is not None and reply.ok else None

    def build_argv(self, prompt: str) -> List[str]:
        argv = self._with_approvals(super().build_argv(prompt))
        thread = self._thread.get()
        if thread is None:
            return argv
        at = len(argv) - 1 - argv[::-1].index(prompt) if prompt in argv else len(argv)
        extra = (["resume", thread] if thread else []) + ["--json"]
        return argv[:at] + extra + argv[at:]

    def system_rules(self, message: IncomingMessage) -> Optional[str]:
        """The channel rules, for a ``codex exec`` command that sets no instructions itself."""
        template = shlex.split(self.settings.command_template(), posix=(os.name != "nt"))
        if len(template) < 2 or Path(template[0].strip("'\"")).stem.lower() != "codex" \
                or template[1] != "exec" or any("developer_instructions" in t for t in template):
            return None
        return channel_rules(getattr(message, "mentions", None),
                             getattr(message, "maintainer_username", None),
                             role=self.role_text,
                             me=self.alias,
                             reply_chars=getattr(message, "reply_chars", None))

    def add_system_rules(self, argv: List[str], rules: str) -> List[str]:
        # A JSON string is a TOML basic string, and it keeps the value on one line.
        at = argv.index("exec", 1) + 1
        value = "developer_instructions=" + json.dumps(rules, ensure_ascii=False)
        return argv[:at] + ["-c", value] + argv[at:]

    def _with_approvals(self, argv: List[str]) -> List[str]:
        # Decide from the template alone: the prompt may well mention mcp_servers.a2a.
        template = shlex.split(self.settings.command_template(), posix=(os.name != "nt"))
        if "exec" not in template[1:] or "--ignore-user-config" in template \
                or any("mcp_servers.a2a" in t for t in template):
            return argv
        if not a2a_server_configured(self.environment()):
            return argv
        at = argv.index("exec", 1) + 1
        return argv[:at] + a2a_approval_args() + argv[at:]

    def failure_summary(self, out: str, err: str, returncode: int) -> str:
        thread_id, _, error = (parse_codex_events(out) if self._thread.get() is not None
                               else (None, "", None))
        if self._thread.get():
            # A resume that cannot find or read its session: report that line, so the turn
            # starts a new session. Only Codex's own stderr counts, or an error event before
            # any session started; stdout also carries the model's text and tool output,
            # which may quote these words in a turn that failed for another reason.
            for line in err.splitlines():
                if _MISSING_THREAD.search(line):
                    self._gone.set(True)
                    return line.strip()
            if not thread_id and error and _MISSING_THREAD.search(error):
                self._gone.set(True)
                return error
        if error:
            return error
        lines = [line for line in (err or out).strip().splitlines()
                 if "reading additional input from stdin" not in line.lower()]
        return lines[-1] if lines else f"exit code {returncode}"

    def parse_output(self, out: str, duration: float) -> AdapterReply:
        if self._thread.get() is None:
            return super().parse_output(out, duration)
        thread_id, text, error = parse_codex_events(out)
        meta: Dict[str, Any] = {"conversation": thread_id} if thread_id else {}
        used = codex_input_tokens(out)
        if used is not None:
            meta["input_tokens"] = used
        wrote = codex_output_tokens(out)
        if wrote is not None:
            meta["output_tokens"] = wrote               # the ledger had input only (#125)
        if error and not text:
            return AdapterReply(ok=False, error=f"{self.alias} failed: {error}",
                                duration_seconds=duration, meta=meta)
        if not text:
            return AdapterReply(ok=False, error=f"{self.alias} produced no reply",
                                duration_seconds=duration, meta={**meta, "empty": True})
        return AdapterReply(text=self.trim(text), duration_seconds=duration, meta=meta)

    @classmethod
    def default_settings(cls, cwd: Optional[str] = None, model: Optional[str] = None,
                         sandbox: str = "workspace-write", timeout_seconds: int = 900,
                         resume: bool = False) -> AdapterSettings:
        parts = ["codex", "exec"]
        if sandbox:
            parts.append(f"--sandbox {sandbox}")
        if model:
            parts.append(f"-m {model}")
        if resume:                       # after the exec options: resume rejects them
            parts.append("resume --last")
        parts.append("{prompt}")
        return AdapterSettings(type=cls.TYPE, cwd=cwd, timeout_seconds=timeout_seconds,
                               command=" ".join(parts))

    def build_prompt(self, message) -> Optional[str]:
        """
        Codex runs with the working directory's ``AGENTS.md`` already in context, so the
        prompt carries only the channel framing: who is asking, plus the reply rules when
        they cannot go in ``developer_instructions``.
        """
        text = (message.text or "").strip()
        if not text:
            return None
        rules = self.system_rules(message)
        if rules and not (self._thread.get() and self._rules_held.get(
                getattr(message, "context_id", None) or "") != rules_digest(rules)):
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
            me=self.alias,
            card=turn_card(message),
            reply_chars=getattr(message, "reply_chars", None),
        )


#: Input tokens a topic's Codex session may reach before it is retired (adapter.session_token_limit).
SESSION_TOKEN_LIMIT = 10_000_000

#: A Codex session id (a UUID today) or session name: never an option or two arguments.
_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

#: ``codex exec resume`` with an id it has no record of (codex-cli 0.157, on stderr):
#: "thread/resume failed: no rollout found for thread id ...".
_MISSING_THREAD = re.compile(
    r"(?i)no rollout found|thread/resume failed|rollout for \S+ could not be read")


def parse_codex_events(out: str):
    """``(thread_id, final agent message, error)`` from ``codex exec --json`` output.

    The stream is JSONL: ``thread.started`` carries ``thread_id``; each finished
    ``agent_message`` item carries ``item.text`` (the last one is the reply); ``error``
    and ``turn.failed`` events carry a message. Lines that are not JSON are skipped.
    """
    thread_id = text = error = None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            thread_id = str(event["thread_id"])
        elif kind == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message" \
                    and str(item.get("text") or "").strip():
                text = str(item["text"]).strip()
        elif kind in ("error", "turn.failed"):     # an item of type "error" is a warning
            detail = event.get("message") or (event.get("error") or {})
            if isinstance(detail, dict):
                detail = detail.get("message")
            error = _readable(detail) or kind
    return thread_id, text, error


def codex_input_tokens(out: str) -> Optional[int]:
    """The session's input tokens so far: ``turn.completed`` reports a running total for
    the thread (cached tokens included), so the last one is the session's size."""
    used = None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or "turn.completed" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if event.get("type") == "turn.completed" and isinstance(usage, dict) \
                and isinstance(usage.get("input_tokens"), int):
            used = usage["input_tokens"]
    return used


def codex_output_tokens(out: str) -> Optional[int]:
    """Output tokens from the last ``turn.completed`` event, counted the same way as input."""
    wrote = None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or "turn.completed" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if event.get("type") == "turn.completed" and isinstance(usage, dict) \
                and isinstance(usage.get("output_tokens"), int) \
                and not isinstance(usage.get("output_tokens"), bool):
            wrote = usage["output_tokens"]
    return wrote


def _readable(detail) -> Optional[str]:
    """Codex nests the API's JSON error inside ``message``; dig out its text."""
    text = str(detail or "").strip()
    for _ in range(3):
        try:
            inner = json.loads(text)
        except ValueError:
            break
        if isinstance(inner, dict):
            inner = inner.get("error") if isinstance(inner.get("error"), dict) else inner
            text = str(inner.get("message") or text)
        else:
            break
    return text or None


class CodexThreadAdapter(AgentAdapter):
    """Queue Telegram messages into an existing Codex session.

    This is intentionally separate from :class:`CodexAdapter`. The original ``codex``
    adapter remains a one-shot subprocess adapter for deployments that want a Telegram
    reply. ``codex_thread`` is opt-in and targets a session UUID or exact session name
    via ``adapter.conversation``. ``codex queue`` adds the message to that session; the
    active Codex task must submit its substantive response using the durable reply ticket
    supplied by the channel. Queueing alone is not completion, and ordinary task output
    is deliberately not scraped or forwarded to Telegram.

    The queue command runs in a worker thread because Windows' ProactorEventLoop creates
    overlapped stdout pipes for ``asyncio.create_subprocess_exec``. Some managed Windows
    environments deny those named pipes with ``WinError 5``. ``subprocess.run`` uses the
    regular synchronous pipe path and keeps this Codex-only workaround out of the other
    runtime adapters.
    """

    TYPE = "codex_thread"

    @classmethod
    def default_settings(cls, cwd: Optional[str] = None, thread: Optional[str] = None,
                         timeout_seconds: int = 30) -> AdapterSettings:
        return AdapterSettings(type=cls.TYPE, cwd=cwd, timeout_seconds=timeout_seconds,
                                conversation=thread)

    def __init__(self, alias: str, settings: Optional[AdapterSettings] = None,
                 runner=None, git_identity=None):
        super().__init__(alias, settings, git_identity=git_identity)
        if not self.settings.conversation:
            raise ConfigError(
                "adapter.type 'codex_thread' requires adapter.conversation containing "
                "a Codex session UUID or exact session name"
            )

    def build_argv(self, prompt: str) -> list[str]:
        return [
            "codex",
            "queue",
            "--thread",
            self.settings.conversation,
            "--message",
            prompt,
        ]

    def accepts_explicit_username_delivery(self) -> bool:
        """Keep every explicit ``@example_codex_bot`` message flowing into the active task."""
        return True

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        if not self.settings.enabled:
            return None
        text = (message.text or "").strip()
        if not text:
            return None

        prompt = format_channel_prompt(
            message.author_alias,
            text,
            author_username=getattr(message, "author_username", None),
            peer_username=getattr(message, "peer_username", None),
            mentions=getattr(message, "mentions", None),
            maintainer=getattr(message, "maintainer_username", None),
            discussion=discussion_line(message),
            project=project_line(message),
            role=self.role_text,
            me=self.alias,
            card=turn_card(message),
            reply_chars=getattr(message, "reply_chars", None),
        )
        if message.response_instructions:
            prompt = message.response_instructions + "\n\n" + prompt
        argv = self.build_argv(prompt)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                argv,
                cwd=self.settings.cwd,
                env=self.environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=float(self.settings.timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AdapterReply(
                ok=False,
                error=f"{self.alias} thread queue timed out after "
                      f"{self.settings.timeout_seconds}s",
            )
        except FileNotFoundError as exc:
            return AdapterReply(
                ok=False,
                error=(
                    f"{self.alias} queue could not start: the codex CLI is not on this "
                    f"daemon's PATH ({exc}). Start the transport via "
                    f"scripts/run_codex_telegram.* (it resolves the CLI and pins its "
                    f"directory) or put codex on PATH; check reports this up front."
                ),
            )
        except OSError as exc:
            return AdapterReply(ok=False, error=f"{self.alias} thread queue failed: {exc}")

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            summary = detail[-1] if detail else f"exit code {result.returncode}"
            return AdapterReply(ok=False, error=f"{self.alias} thread queue failed: {summary}")

        # This is acceptance, not completion. The channel tracks the reply ticket separately.
        return AdapterReply(
            meta={"queued": True, "conversation": self.settings.conversation},
        )

    async def verify_delivery(self, message: IncomingMessage) -> Optional[AdapterReply]:
        """Probe the Codex session store for the planted delivery token.

        Issue #59 deliverable 6 + spec amendment (4): ``codex_thread`` searches
        the reply store for the planted token. Review round 2 (B9) corrections:
        a relative ``reply_state_path`` resolves against the adapter cwd (not
        the process CWD), and a MISS now reports ``None`` (unknown → degrade)
        instead of a clean negative — the reply store cannot positively confirm
        a bus-path injection (no row is created for it), so a miss must never
        false-escalate. A real codex delivery signal (the session/thread store)
        is tracked as a follow-up (issue #62).

        Probe *errors* (e.g. a corrupt SQLite file) are reported by raising —
        the poller's hard rule "probe error ⇒ log and continue (NOT failure)"
        keeps a transient DB lock from blocking delivery escalation.
        """
        from swarmly.telegram.replies import ReplyStore, reply_state_path_for
        import re as _re

        if not self.settings.reply_state_path:
            return None

        # Scrape the planted token from the prompt the poller injected. When
        # the token is missing (e.g. an old-style injection), fall back to
        # the message_id so legacy callers keep working.
        instr = getattr(message, "response_instructions", "") or ""
        match = _re.search(r"\[BUS_DELIVERY_TOKEN=([^\]]+)\]", instr)
        token = match.group(1).strip() if match else ""
        if not token:
            token = str(getattr(message, "message_id", "") or "")
        if not token:
            return None

        try:
            store = ReplyStore(reply_state_path_for(self.settings.reply_state_path,
                                                    base_dir=self.settings.cwd))
        except Exception:                              # noqa: BLE001
            raise
        with store._db() as db:
            # Review N2: match the IDENTIFIER only. Free-text columns
            # (original/text/detail) merely CONTAIN the token when a ticket
            # quotes it — a quoted token scored ok=True and suppressed a real
            # escalation. The docstring above already says a bus injection
            # cannot be positively confirmed; the query now agrees with it.
            row = db.execute(
                "SELECT state, telegram_message_id FROM replies "
                "WHERE id = ? LIMIT 1",
                (token,)).fetchone()
        if row is None:
            return None
        return AdapterReply(ok=True,
                            meta={"thread": self.settings.conversation,
                                  "token": token,
                                  "state": row["state"]})
