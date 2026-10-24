"""
Adapter for the **Antigravity CLI** (`agy`).

Verified surface (Antigravity CLI operator guide, checked 2026-09-16):

* ``agy -p "<prompt>"`` / ``--print`` — non-interactive one-shot; prints **plain text only**
  (there is no ``--output-format json`` and no result envelope, unlike Codex).
* ``--print-timeout 20m`` — bounds a print run (default ``5m``); ``--max-turns`` does not exist.
* ``--model`` — engine selection, e.g. ``'Gemini 3.1 Pro (High)'`` (``agy models`` lists them).
* ``--add-dir`` — extra context roots; ``--sandbox`` / ``--dangerously-skip-permissions`` —
  launch-time permission overrides.
* interactive sessions use ``agy -i`` under a PTY. ``--conversation <id>`` resumes a
  conversation by id; ``-c`` is the short alias for ``--continue`` -- a flag with **no
  value** that continues the most recent conversation (``agy --help``, checked 2026-10-17).
* token usage: ``--output-format json`` (print mode, checked 2026-10-25) returns
  ``{"conversation_id", "status", "response", "usage": {"input_tokens", "output_tokens",
  "thinking_tokens", "cache_read_tokens", "total_tokens"}}``. The adapter asks for it, so
  the turn ledger, the pulse and ``/usage`` carry Antigravity's tokens (they were unknown
  before, issue #166), and a new topic's conversation id comes from the reply itself.

Default command::

    agy -p {prompt} --print-timeout 20m
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
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

from swarmly.telegram.adapters.base import (
    AdapterReply,
    AgentAdapter,
    IncomingMessage,
    SubprocessAdapter,
    channel_envelope,
    channel_rules,
    discussion_line,
    format_channel_prompt,
    is_batch_file,
    load_state,
    plain,
    project_line,
    rules_digest,
    save_state,
    turn_card,
)
from swarmly.telegram import endpoint as endpoint_probe
from swarmly.telegram.config import (ACTIVE_CONVERSATION_FILENAME,
                                       CONVERSATION_FLAGS, AdapterSettings, ConfigError,
                                       command_words, names_a_conversation)


#: Fragments that mark agentapi's refusal as an ENDPOINT problem -- the live
#: session could not be reached at all, rather than the call being rejected on
#: its merits. Measured forms on this host (2026-09-21):
#:   {"error": "ANTIGRAVITY_LS_ADDRESS is not set"}
#:   {"error": "rpc error: code = Unauthenticated desc = missing CSRF token"}
#:   {"error": "rpc error: code = Unauthenticated desc = invalid CSRF token"}
#:   {"error": "rpc error: code = Unavailable desc = connection error: ..."}
#: Matched case-insensitively against the extracted reason.
#:
#: The two CSRF forms are as much endpoint failures as the missing-address form:
#: a credential that the server refuses means no dispatch is possible, and the
#: blocking ``agy -p`` fallback cannot fix it either. Leaving them out is what
#: made a stale credential fail SILENTLY (measured: the fast error was emitted
#: for the address string, while the credential string fell through to the
#: 1500s blocking path) -- the exact asymmetry this tuple must not have.
_ENDPOINT_FAILURE_MARKERS = (
    "antigravity_ls_address",
    "csrf token",
    "code = unauthenticated",
    "connection refused",
    "connection error",
    "code = unavailable",
    "code = deadlineexceeded",
    "no such host",
    "i/o timeout",
)


def is_endpoint_failure(reason: str) -> bool:
    """True when agentapi refused because the session endpoint was unreachable.

    Issue #85 mode 5. This distinction is the whole point of the fix: the
    blocking ``agy`` fallback is *pathological* when the IDE is present (measured:
    an ``agy -p --conversation <id>`` run against this host's open session
    started at 16:59 and was still holding the channel's turn lock 16 minutes
    later), and *supported* when it is absent (a headless CI host has no session
    to resume, so the one-shot CLI is the only delivery path). The caller pairs
    this with whether an endpoint was actually resolvable -- see
    :meth:`AntigravityThreadAdapter.respond`.
    """
    flat = " ".join(str(reason or "").lower().split())
    return any(marker in flat for marker in _ENDPOINT_FAILURE_MARKERS)


_CONTEXT_SWITCH = re.compile(
    r"^(?:switch\s+(?:context|conversation)\s+to|set\s+conversation\s+to)\s+[\"']?(.+?)[\"']?\s*$",
    re.IGNORECASE)


def match_context_switch(text: str, alias: str = "") -> Optional[str]:
    """Return the target of an in-chat ``switch context to <X>`` command, else ``None``.

    Addressed group messages carry the bot's ``@mention`` (routing depends on it), and
    peers often lead with the alias (``Antigravity: ...``), so leading mentions and the
    alias are skipped before the anchored match (issue #130).
    """
    rest = (text or "").strip()
    prefix = re.compile(
        r"^(?:@[A-Za-z0-9_]+|" + (re.escape(alias.strip()) if alias.strip() else r"(?!)")
        + r")(?:[\s,:;]+|$)", re.IGNORECASE)
    while True:
        stripped = prefix.sub("", rest, count=1)
        if stripped == rest:
            break
        rest = stripped.lstrip()
    match = _CONTEXT_SWITCH.match(rest)
    return match.group(1).strip() if match else None


@dataclass
class _ConversationDb:
    """An opened conversation store plus the mode it was opened with.

    ``immutable=True`` marks the fallback reader used when ``mode=ro`` cannot
    open the file: it IGNORES the store's ``-wal``, so its HITs are trustworthy
    while its MISSES are NOT (the row may be committed but still only in the
    WAL). ``verify_delivery`` turns such a miss into a probe error instead of a
    clean negative (review N1, PR #60 round 3).
    """
    conn: sqlite3.Connection
    immutable: bool = False

logger = logging.getLogger("swarmly.telegram.antigravity")

def agentapi_title(message: Any) -> str:
    """The ``--title`` for agentapi: the author's name with shell characters removed
    (review P0-8: agentapi.bat was once on this path, and the name is chat-controlled)."""
    author = getattr(message, "author_username", None) or message.author_alias or ""
    return f"Telegram: @{plain(str(author).lstrip('@'))}"


def resolve_agentapi() -> Optional[List[str]]:
    """Locate the Antigravity agentapi command prefix if available.

    On Windows, directly invoking `language_server.exe agentapi` avoids cmd.exe
    batch file argument newline truncation.
    """
    # 1. Direct language_server binary on Windows (avoids batch newline truncation)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        ls_exe = Path(local_app_data) / "Programs" / "Antigravity" / "resources" / "bin" / "language_server.exe"
        if ls_exe.is_file():
            return [str(ls_exe), "agentapi"]

    # 2. Check if agentapi.bat points to language_server.exe
    for name in ("agentapi", "agentapi.bat"):
        found = shutil.which(name)
        if found:
            if is_batch_file(found):
                try:
                    content = Path(found).read_text(encoding="utf-8")
                    m = re.search(r'"([^"]+language_server\.exe)"\s+agentapi', content, re.IGNORECASE)
                    if m and Path(m.group(1)).is_file():
                        return [m.group(1), "agentapi"]
                except Exception:
                    pass
                # Review P0-8: never the batch file itself: cmd.exe would parse the prompt.
                continue
            return [found]

    gemini_bin = Path.home() / ".gemini" / "antigravity" / "bin"
    target_bat = gemini_bin / "agentapi.bat"
    if target_bat.is_file():
        try:
            content = target_bat.read_text(encoding="utf-8")
            m = re.search(r'"([^"]+language_server\.exe)"\s+agentapi', content, re.IGNORECASE)
            if m and Path(m.group(1)).is_file():
                return [m.group(1), "agentapi"]
        except Exception:
            pass
        # Review P0-8: the batch file itself is never run (the agy -p path takes over).

    target_noext = gemini_bin / "agentapi"
    if target_noext.is_file():
        return [str(target_noext)]

    return None


# The reason is surfaced in a log line AND in a user-visible Telegram error, so it
# must stay short and single-line. An unbounded blob (agentapi could echo a large
# JSON body, a path, or a token) would flood the message, and an embedded newline
# would break the one-line envelope rule the transport relies on.
_MAX_REASON_CHARS = 200


def _bound_reason(text: str) -> str:
    """Collapse to one line and cap the length of a failure reason."""
    flat = " ".join(str(text or "").split())
    if len(flat) > _MAX_REASON_CHARS:
        flat = flat[: _MAX_REASON_CHARS - 1].rstrip() + "…"
    return flat


def describe_agentapi_failure(result) -> str:
    """Name WHY ``agentapi`` refused, from its own JSON error payload.

    Issue #85: resolving the ``agentapi`` *binary* says nothing about whether a
    live session endpoint is reachable. On this host the binary resolves fine and
    ``send-message`` exits 1 with ``{"error": "ANTIGRAVITY_LS_ADDRESS is not
    set"}`` — the endpoint is per-process and rotates, so it cannot be pinned in
    an env file. Surfacing that string (rather than only the exit code) is what
    lets a later timeout line name the cause instead of looking like an overflow.

    The returned value is bounded (see ``_bound_reason``): it reaches Telegram.
    """
    payload = ((getattr(result, "stderr", "") or getattr(result, "stdout", "")) or "").strip()
    detail = ""
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                detail = str(parsed.get("error") or parsed.get("message") or "").strip()
        except Exception:
            detail = ""
        if not detail:
            detail = payload.splitlines()[0].strip()
    if not detail:
        detail = f"exit code {result.returncode}"
    return _bound_reason(detail)


class AntigravityContextMixin:
    """Shared conversation discovery, sync, and model resolution logic for Antigravity adapters."""

    settings: AdapterSettings
    #: Issue #78: ``limits.max_parallel_turns`` as the channel set it. Above 1 every topic
    #: keeps its own conversation, so a *runtime* switch cannot pin a single one -- it would
    #: interleave the parallel turns that the limit exists to allow.
    max_parallel_turns: int = 1

    def use_parallel_limit(self, turns: int) -> None:
        """Tell the adapter how many topics may run at once (`limits.max_parallel_turns`)."""
        try:
            self.max_parallel_turns = max(1, int(turns))
        except (TypeError, ValueError):
            self.max_parallel_turns = 1

    #: Words that mean "back to one conversation per topic": the unpinning form. ``auto`` is
    #: *not* one of them -- it pins the workspace's most recent conversation.
    UNPIN_WORDS = ("per-topic", "pertopic", "per_topic", "none", "off")

    def _unpin_conversation(self) -> Tuple[bool, str]:
        """Drop the pinned conversation: every topic gets its own again (#78)."""
        self.settings.conversation = ""
        removed = False
        if self.settings.cwd:
            active_file = Path(self.settings.cwd) / ACTIVE_CONVERSATION_FILENAME
            try:
                if active_file.exists():
                    active_file.unlink()
                    removed = True
            except OSError as exc:
                return False, f"Failed to remove {ACTIVE_CONVERSATION_FILENAME}: {exc}"
        return True, ("Switched back to one conversation per topic"
                      + (f" (removed {ACTIVE_CONVERSATION_FILENAME})." if removed else "."))

    def _get_summary_dbs(self) -> List[Path]:
        gemini_dir = Path.home() / ".gemini"
        return [
            gemini_dir / "antigravity" / "conversation_summaries.db",
            gemini_dir / "antigravity-cli" / "conversation_summaries.db",
        ]

    def resolve_conversation(self) -> Optional[Tuple[str, str]]:
        """Resolve the active conversation ID and title.

        Checks:
        1. ``.active_conversation`` file in ``cwd``
        2. ``self.settings.conversation``
        Looks up UUIDs or titles in ~/.gemini/antigravity/conversation_summaries.db.
        If an explicit query is not found in the summary database, it is passed
        through as (query, query) to support custom session names or external harnesses.
        """
        want_auto = False
        query: Optional[str] = None
        if self.settings.cwd:
            active_file = Path(self.settings.cwd) / ACTIVE_CONVERSATION_FILENAME
            if active_file.is_file():
                try:
                    content = active_file.read_text(encoding="utf-8").strip().lstrip("\ufeff").strip()
                    if content.lower() == "auto":
                        want_auto = True
                    elif content:
                        query = content
                except Exception:
                    pass

        if not query and not want_auto:
            if self.settings.conversation:
                conv_setting = self.settings.conversation.strip().lstrip("\ufeff").strip()
                if conv_setting.lower() == "auto":
                    want_auto = True
                elif conv_setting:
                    query = conv_setting
            elif isinstance(self, AntigravityThreadAdapter):
                want_auto = True

        if query:
            resolved = self._lookup_conversation(query)
            # If resolved from database, return canonical (conv_id, title). Otherwise, pass
            # through the explicit query as (query, query) to support custom session names,
            # external harnesses, or sessions not yet indexed in conversation_summaries.db.
            return resolved or (query, query)

        if not want_auto:
            return None

        # Auto-resolve the most recent top-level conversation associated with this workspace
        if self.settings.cwd:
            norm_target = Path(self.settings.cwd).resolve()
            sum_dbs = self._get_summary_dbs()
            for sum_db in sum_dbs:
                if not sum_db.is_file():
                    continue
                try:
                    conn = sqlite3.connect(sum_db)
                    try:
                        # Filter out subagent sessions (nesting_depth > 0 or parent_conversation_id)
                        # and killed sessions to target the main user session.
                        rows = conn.execute("""
                            SELECT conversation_id, title, workspace_uris
                            FROM conversation_summaries
                            WHERE (parent_conversation_id = '' OR parent_conversation_id IS NULL)
                              AND (nesting_depth = 0 OR nesting_depth IS NULL)
                              AND (killed = 0 OR killed IS NULL OR killed = 'false')
                            ORDER BY last_modified_time DESC
                        """).fetchall()
                        for conv_id, title, uris_raw in rows:
                            if not uris_raw:
                                continue
                            try:
                                uris = json.loads(uris_raw)
                                for u in uris:
                                    parsed = urlparse(u)
                                    path_str = unquote(parsed.path)
                                    if path_str.startswith('/') and len(path_str) > 2 and path_str[2] == ':':
                                        path_str = path_str[1:]
                                    if Path(path_str).resolve() == norm_target:
                                        return (conv_id, title or conv_id)
                            except Exception:
                                pass
                    finally:
                        conn.close()
                except Exception as exc:
                    logger.debug("Failed auto-detecting workspace conversation from %s: %s", sum_db, exc)

        return None

    def set_active_conversation(self, query: str) -> Tuple[bool, str]:
        """Switch the active conversation pointer and synchronize to CLI.

        With ``limits.max_parallel_turns > 1`` a switch to a fixed conversation (or to
        ``auto``, which is one conversation too) is refused: the topics run in parallel, so
        one shared context would interleave them -- and the ``.active_conversation`` file it
        writes would make the next start refuse to load the config at all. Unpinning, back to
        one conversation per topic, is always allowed.
        """
        query_stripped = query.strip()
        if query_stripped.lower() in self.UNPIN_WORDS:
            return self._unpin_conversation()
        if int(getattr(self, "max_parallel_turns", 1) or 1) > 1:
            return False, (
                f"Refused: this bot runs parallel turns (limits.max_parallel_turns: "
                f"{self.max_parallel_turns}), so every topic keeps its own conversation. "
                "Pinning one conversation would interleave them, and the next start would "
                "refuse the config. Set limits.max_parallel_turns to 1 first, then switch; "
                "send 'switch context to per-topic' to stay unpinned.")
        if query_stripped.lower() == "auto":
            self.settings.conversation = "auto"
            if self.settings.cwd:
                active_file = Path(self.settings.cwd) / ACTIVE_CONVERSATION_FILENAME
                try:
                    active_file.write_text("auto", encoding="utf-8")
                except Exception as exc:
                    return False, f"Failed to update {ACTIVE_CONVERSATION_FILENAME}: {exc}"
            return True, "Switched active context to auto-detection (most recent conversation for this workspace)."

        resolved = self._lookup_conversation(query_stripped)
        if not resolved:
            return False, f"Conversation '{query}' not found in Antigravity conversation records."

        conv_id, title = resolved
        self.settings.conversation = conv_id
        if self.settings.cwd:
            active_file = Path(self.settings.cwd) / ACTIVE_CONVERSATION_FILENAME
            try:
                active_file.write_text(conv_id, encoding="utf-8")
            except Exception as exc:
                return False, f"Failed to update {ACTIVE_CONVERSATION_FILENAME}: {exc}"

        # Perform initial sync
        self.sync_conversation_to_cli(conv_id)
        return True, f'Switched active context to: "{title}" (id: {conv_id})'

    def _lookup_conversation(self, query: str) -> Optional[Tuple[str, str]]:
        try:
            norm_uuid = str(uuid.UUID(query.strip()))
            is_uuid = True
        except (ValueError, AttributeError):
            norm_uuid = query.strip()
            is_uuid = False

        sum_dbs = self._get_summary_dbs()

        for sum_db in sum_dbs:
            if not sum_db.is_file():
                continue
            try:
                conn = sqlite3.connect(sum_db)
                try:
                    if is_uuid:
                        row = conn.execute(
                            "SELECT conversation_id, title FROM conversation_summaries WHERE conversation_id = ?",
                            (norm_uuid,)
                        ).fetchone()
                        if row:
                            return (row[0], row[1] or row[0])
                    else:
                        row = conn.execute(
                            "SELECT conversation_id, title FROM conversation_summaries WHERE title = ? ORDER BY last_modified_time DESC LIMIT 1",
                            (query.strip(),)
                        ).fetchone()
                        if row:
                            return (row[0], row[1])
                        row = conn.execute(
                            "SELECT conversation_id, title FROM conversation_summaries WHERE title LIKE ? ORDER BY last_modified_time DESC LIMIT 1",
                            (f"%{query.strip()}%",)
                        ).fetchone()
                        if row:
                            return (row[0], row[1])
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug("Error querying summary db %s: %s", sum_db, exc)
        return (norm_uuid, norm_uuid) if is_uuid else None


    def sync_conversation_to_cli(self, conv_id: str) -> bool:
        """Safely sync conversation SQLite db and summary row from IDE to CLI."""
        gemini_dir = Path.home() / ".gemini"
        src_db = gemini_dir / "antigravity" / "conversations" / f"{conv_id}.db"
        dst_db = gemini_dir / "antigravity-cli" / "conversations" / f"{conv_id}.db"
        src_sum = gemini_dir / "antigravity" / "conversation_summaries.db"
        dst_sum = gemini_dir / "antigravity-cli" / "conversation_summaries.db"

        if not src_db.is_file():
            return False

        try:
            dst_db.parent.mkdir(parents=True, exist_ok=True)
            src_conn = sqlite3.connect(src_db)
            dst_conn = sqlite3.connect(dst_db)
            try:
                src_conn.backup(dst_conn)
            finally:
                src_conn.close()
                dst_conn.close()

            # Sync summary row
            if src_sum.is_file() and dst_sum.is_file():
                s_sum = sqlite3.connect(src_sum)
                d_sum = sqlite3.connect(dst_sum)
                try:
                    dst_cols = [r[1] for r in d_sum.execute("PRAGMA table_info(conversation_summaries)").fetchall()]
                    col_str = ",".join(dst_cols)
                    row = s_sum.execute(
                        f"SELECT {col_str} FROM conversation_summaries WHERE conversation_id = ?",
                        (conv_id,)
                    ).fetchone()
                    if row:
                        placeholders = ",".join(["?"] * len(dst_cols))
                        d_sum.execute(
                            f"INSERT OR REPLACE INTO conversation_summaries ({col_str}) VALUES ({placeholders})",
                            row
                        )
                        d_sum.commit()
                finally:
                    s_sum.close()
                    d_sum.close()
            return True
        except Exception as exc:
            logger.warning("Failed to sync conversation to CLI: %s", exc)
            return False

    def sync_conversation_to_ide(self, conv_id: str) -> bool:
        """Safely sync conversation SQLite db back from CLI to IDE after execution."""
        gemini_dir = Path.home() / ".gemini"
        src_db = gemini_dir / "antigravity-cli" / "conversations" / f"{conv_id}.db"
        dst_db = gemini_dir / "antigravity" / "conversations" / f"{conv_id}.db"

        if not src_db.is_file():
            return False

        try:
            dst_db.parent.mkdir(parents=True, exist_ok=True)
            src_conn = sqlite3.connect(src_db)
            dst_conn = sqlite3.connect(dst_db)
            try:
                src_conn.backup(dst_conn)
            finally:
                src_conn.close()
                dst_conn.close()
            return True
        except Exception as exc:
            logger.warning("Failed to sync conversation to IDE: %s", exc)
            return False

    def resolve_conversation_model(self, conv_id: str) -> Optional[str]:
        """Detect the model and reasoning effort configured for a conversation from its trajectory metadata."""
        gemini_dir = Path.home() / ".gemini"
        db_paths = [
            gemini_dir / "antigravity" / "conversations" / f"{conv_id}.db",
            gemini_dir / "antigravity-cli" / "conversations" / f"{conv_id}.db",
        ]
        for db_path in db_paths:
            if not db_path.is_file():
                continue
            try:
                conn = sqlite3.connect(db_path)
                try:
                    rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 5").fetchall()
                    for r in rows:
                        if not r or not r[0]:
                            continue
                        # Match protobuf model/effort field (\xe2\x01<len><model-slug>)
                        m = re.search(rb"\xe2\x01[\x01-\x50]((?:gemini|claude|gpt)-[a-zA-Z0-9\.-]+)", r[0])
                        if not m:
                            m = re.search(rb"((?:gemini|claude|gpt)-[0-9\.-]+(?:[a-zA-Z0-9\.-]+)?)", r[0])
                        if m:
                            model_str = m.group(1).decode("utf-8").strip()
                            if model_str.endswith("-tiered"):
                                model_str = model_str.replace("-tiered", "-high")
                            if model_str.startswith("gemini-") and not any(model_str.endswith(f"-{eff}") for eff in ("high", "medium", "low")):
                                model_str += "-high"
                            return model_str
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug("Error reading gen_metadata for %s: %s", conv_id, exc)
        return None


def _unquote_model(argv: List[str]) -> List[str]:
    """Drop the quotes ``shlex``'s non-POSIX mode leaves around a ``--model`` value.

    ``default_settings`` single-quotes the engine name so a name with spaces stays one token
    in the command template, and every shell-style POSIX split removes that quoting again.
    On Windows the split runs non-POSIX (quotes kept), so ``agy`` would receive
    ``"'Gemini 3.1 Pro (High)'"`` -- a name that matches no model. Review finding on PR #17:
    the token must reach ``agy`` unquoted, and the test must assert it exactly.
    """
    for index, arg in enumerate(argv):
        if arg == "--model" and index + 1 < len(argv):
            value = argv[index + 1]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                argv[index + 1] = value[1:-1]
        elif arg.startswith("--model="):
            value = arg[len("--model="):]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                argv[index] = f"--model={value[1:-1]}"
    return argv


def _command_words(settings: AdapterSettings) -> List[str]:
    """``adapter.command`` split the way the argv builder splits it (kept as this module's
    name for it; the split itself lives in :mod:`swarmly.telegram.config`, where the loader
    needs it too -- #67)."""
    return command_words(settings.command or "")


def argv_conversation(argv: List[str]) -> Optional[Tuple[str, str]]:
    """The conversation id an ``agy`` argv names, as ``(id, id)``, or None.

    Only ``--conversation <id>`` (and its ``--conversation=<id>`` form) carries an id.
    ``-c`` is the short alias for ``--continue``: a bare flag, verified against
    ``agy --help``, so it must never swallow the token that follows it -- the old parser
    read the argument after ``-c`` as a conversation id and dropped it from the run.
    """
    for idx, arg in enumerate(argv):
        if arg == "--conversation" and idx + 1 < len(argv):
            return (argv[idx + 1], argv[idx + 1])
        if arg.startswith("--conversation=") and len(arg) > len("--conversation="):
            return (arg.split("=", 1)[1], arg.split("=", 1)[1])
    return None


def skips_approvals(settings: AdapterSettings) -> bool:
    """Whether a run skips Antigravity's approvals: the default command does; an explicit
    ``adapter.command`` only when it says ``--dangerously-skip-permissions`` itself."""
    if settings.command:
        return "--dangerously-skip-permissions" in _command_words(settings)
    return True


def sandboxed(settings: AdapterSettings) -> bool:
    """Whether an explicit ``adapter.command`` asks for ``--sandbox``; the ``agy -p``
    fallback of ``antigravity_thread`` keeps it (review P0-5)."""
    return "--sandbox" in _command_words(settings)


def _cli_conversations() -> Set[str]:
    """The conversation ids ``agy`` has stored (one entry per conversation, named by id)."""
    found: Set[str] = set()
    for folder in (Path.home() / ".gemini" / "antigravity-cli" / "conversations",
                   Path.home() / ".gemini" / "antigravity" / "conversations"):
        try:
            found.update(entry.name.split(".", 1)[0] for entry in folder.iterdir())
        except OSError:
            continue
    return found


class AntigravityAdapter(AntigravityContextMixin, SubprocessAdapter):
    """Drive the Antigravity CLI in print mode, one process per incoming message.

    Unless a conversation is configured (``adapter.conversation``, ``.active_conversation``
    or ``--conversation`` in the command), each discussion gets its own Antigravity
    conversation: the first turn in a topic starts one, the adapter notes the id ``agy``
    stored for it, and later turns there pass ``--conversation <id>``. One topic, one
    context, as with Codex and ``claude_thread``.
    """

    TYPE = "antigravity"
    WANTS_TOPIC_RECAP = True                  # until a topic has its own conversation
    SESSIONS_STATE = "antigravity-sessions"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._topic_conversations: Dict[str, str] = {}
        # context_id -> hash of the channel rules its conversation was given: a resumed turn
        # then carries the one-line envelope, not the rules again.
        self._topic_rules: Dict[str, str] = {}
        self._state_path: Optional[Path] = None
        self._turn_conversation: contextvars.ContextVar[Optional[str]] = \
            contextvars.ContextVar("a2a_agy_conversation", default=None)
        self._new_conversation = asyncio.Lock()   # one new conversation at a time
        self._topic_turns: Dict[str, asyncio.Lock] = {}

    def use_state_file(self, path: Optional[Path]) -> None:
        """Keep each discussion's conversation id in ``path`` (JSON) across restarts."""
        self._state_path = path
        for context_id, entry in load_state(path, f"{self.alias}: Antigravity sessions").items():
            if not isinstance(entry, dict):
                continue
            conv, rules = entry.get("conversation"), entry.get("rules")
            if isinstance(conv, str) and re.fullmatch(r"[\w-]{8,80}", conv):
                self._topic_conversations[str(context_id)] = conv
                if isinstance(rules, str):
                    self._topic_rules[str(context_id)] = rules

    def _per_topic(self) -> bool:
        if (self.settings.conversation or "").strip():
            return False
        if self.settings.cwd and (Path(self.settings.cwd) / ACTIVE_CONVERSATION_FILENAME).is_file():
            return False
        # The operator's command already picks a conversation: either by id
        # (``--conversation <id>``) or the most recent one (``-c``/``--continue``).
        return not names_a_conversation(_command_words(self.settings))

    def topic_mode(self, context_id: Optional[str]) -> str:
        """Fresh until the discussion has a conversation of its own, then its session."""
        conv = self.conversation_for(context_id)
        if conv and self._per_topic() and conv in _cli_conversations():
            return "session"
        return "fresh"

    def conversation_for(self, context_id: Optional[str]) -> Optional[str]:
        return self._topic_conversations.get(context_id) if context_id else None

    def forget_session(self, context_id: Optional[str]) -> bool:
        """A deleted topic's conversation is dropped (issue #63): nothing resumes it again."""
        if not context_id or context_id not in self._topic_conversations:
            return False
        self._remember_conversation(context_id, None)
        return True

    def _remember_conversation(self, context_id: str, conv: Optional[str],
                               rules: Optional[str] = None) -> None:
        if conv:
            self._topic_conversations[context_id] = conv
            if rules:
                self._topic_rules[context_id] = rules
        else:
            self._topic_conversations.pop(context_id, None)
            self._topic_rules.pop(context_id, None)
        save_state(self._state_path, {
            key: {"conversation": value, "rules": self._topic_rules.get(key)}
            for key, value in self._topic_conversations.items()},
            f"{self.alias}: Antigravity sessions")

    def _rules_digest(self, message: IncomingMessage) -> str:
        return rules_digest(channel_rules(getattr(message, "mentions", None),
                                          getattr(message, "maintainer_username", None),
                                          role=self.role_text,
                                          me=self.alias,
                                          reply_chars=getattr(message, "reply_chars", None)))

    async def _respond_in_topic(self, message: IncomingMessage,
                                context_id: str) -> Optional[AdapterReply]:
        lock = self._topic_turns.setdefault(context_id, asyncio.Lock())
        async with lock:
            conv = self.conversation_for(context_id)
            if conv and conv not in await asyncio.to_thread(_cli_conversations):
                # agy does not fail on an unknown id: it starts an empty conversation.
                logger.warning("%s: conversation %s for %s is gone; starting a new one",
                               self.alias, conv, context_id)
                self._remember_conversation(context_id, None)
                conv = None
                if message.fresh_text:          # a new conversation needs the task
                    message = dataclasses.replace(message, text=message.fresh_text)
            if conv:
                token = self._turn_conversation.set(conv)
                try:
                    reply = await SubprocessAdapter.respond(self, message)
                finally:
                    self._turn_conversation.reset(token)
                if reply is not None and reply.ok \
                        and self._topic_rules.get(context_id) != self._rules_digest(message):
                    self._remember_conversation(context_id, conv, self._rules_digest(message))
                if reply is not None and not reply.ok \
                        and conv not in await asyncio.to_thread(_cli_conversations):
                    # Only a conversation agy no longer has is dropped; a timeout or a
                    # quota error keeps the topic's context for the next turn.
                    logger.warning("%s: conversation %s is gone; the next turn in %s starts "
                                   "a new one", self.alias, conv, context_id)
                    self._remember_conversation(context_id, None)
                return reply
            async with self._new_conversation:
                before = await asyncio.to_thread(_cli_conversations)
                reply = await SubprocessAdapter.respond(self, message)
                new = (await asyncio.to_thread(_cli_conversations)) - before
            reported = (reply.meta or {}).get("conversation") if reply is not None else None
            if reply is not None and reply.ok and reported and \
                    str(reported) in (new or await asyncio.to_thread(_cli_conversations)):
                new = {str(reported)}                  # the reply says which one it was
            if reply is not None and reply.ok and len(new) == 1:
                found = next(iter(new))
                logger.info("%s: discussion %s continues in conversation %s", self.alias,
                            context_id, found)
                self._remember_conversation(context_id, found, self._rules_digest(message))
            elif reply is not None and reply.ok:
                logger.warning("%s: could not tell which conversation agy stored for %s "
                               "(%d new); this discussion stays one run per message",
                               self.alias, context_id, len(new))
            return reply

    @classmethod
    def default_settings(cls, cwd: Optional[str] = None, model: Optional[str] = None,
                         print_timeout: str = "20m", timeout_seconds: int = 1500,
                         sandbox: bool = False,
                         skip_permissions: bool = True,
                         conversation: Optional[str] = None) -> AdapterSettings:
        parts = ["agy", "-p", "{prompt}", "--print-timeout", print_timeout]
        if skip_permissions:
            parts.append("--dangerously-skip-permissions")
        if model:
            parts += ["--model", f"'{model}'"]
        if sandbox:
            parts.append("--sandbox")
        return AdapterSettings(type=cls.TYPE, cwd=cwd, timeout_seconds=timeout_seconds,
                               command=" ".join(parts), conversation=conversation)

    def accepts_explicit_username_delivery(self) -> bool:
        """Keep every explicit username-addressed message flowing into the active task.

        When ``conversation: auto`` is configured, incoming messages are routed into
        the most recently active conversation for this workspace, allowing seamless
        hands-free interaction.
        """
        return True

    def build_prompt(self, message: IncomingMessage) -> Optional[str]:
        text = (message.text or "").strip()
        if not text:
            return None
        context_id = str(getattr(message, "context_id", None) or "")
        if self._turn_conversation.get() \
                and self._topic_rules.get(context_id) == self._rules_digest(message):
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

    # -- execution hooks ---------------------------------------------------------------
    def build_argv(self, prompt: str) -> List[str]:
        argv = super().build_argv(prompt)
        if ("-p" in argv or "--print" in argv or "--prompt" in argv) \
                and "--output-format" not in argv:
            # The JSON result carries the token usage and the conversation id.
            argv.extend(["--output-format", "json"])
        # Check if conversation is specified in template or dynamically. Only
        # ``--conversation <id>`` names an id; ``-c``/``--continue`` is a bare flag.
        active = None
        topic_conv = self._turn_conversation.get()
        if names_a_conversation(argv):
            active = argv_conversation(argv)
        elif topic_conv:
            argv.extend(["--conversation", topic_conv])
        else:
            active = self.resolve_conversation()
            if active:
                argv.extend(["--conversation", active[0]])

        # Review P0-5: the configured command decides whether approvals are skipped. The
        # default template carries --dangerously-skip-permissions; a command without it
        # keeps the approvals (it used to be appended behind the operator's back).

        # Model selection: If --model is not already specified in the command template,
        # inherit the model selected for the target conversation.
        if "--model" not in argv and active:
            conv_id, _ = active
            conv_model = self.resolve_conversation_model(conv_id)
            if conv_model:
                argv.extend(["--model", conv_model])

        return _unquote_model(argv)

    def parse_output(self, out: str, duration: float) -> AdapterReply:
        """The JSON result of ``--output-format json``; plain text when it is not JSON (a
        custom command, an older agy)."""
        text = (out or "").strip()
        data = None
        # The result is the last JSON object on stdout: a notice printed before it (an update
        # banner, a warning) must not turn the whole blob into the reply.
        for start in [i for i, ch in enumerate(text) if ch == "{"][::-1][:20]:
            try:
                found, end = json.JSONDecoder().raw_decode(text[start:])
            except ValueError:
                continue
            if isinstance(found, dict) and "response" in found and not text[start + end:].strip():
                data = found
                break
        if not isinstance(data, dict) or "response" not in data:
            return super().parse_output(out, duration)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        meta: Dict[str, Any] = {}

        def count(*keys: str) -> Optional[int]:
            values = [usage.get(k) for k in keys if isinstance(usage.get(k), int)]
            return sum(values) if values else None
        tokens_in = count("input_tokens", "cache_read_tokens")
        tokens_out = count("output_tokens", "thinking_tokens")
        if tokens_in is not None:
            meta["tokens_in"] = tokens_in
        if tokens_out is not None:
            meta["tokens_out"] = tokens_out
        if data.get("conversation_id"):
            meta["conversation"] = str(data["conversation_id"])
        status = str(data.get("status") or "SUCCESS").upper()
        response = self.trim(str(data.get("response") or ""))
        if status != "SUCCESS":
            reason = str(data.get("error") or response or status)
            return AdapterReply(text="", ok=False, error=f"Antigravity: {reason}",
                                duration_seconds=duration, meta=meta)
        return AdapterReply(text=response, duration_seconds=duration, meta=meta)

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        if not self.settings.enabled:
            return None

        # Check for administrative context switch commands
        target = match_context_switch(message.text or "", self.alias)
        if target is not None:
            if message.is_admin:
                ok, response_text = self.set_active_conversation(target)
                return AdapterReply(text=response_text, ok=ok, error="" if ok else response_text)
            err_msg = "Unauthorized: context switching is restricted to administrators."
            return AdapterReply(text=err_msg, ok=False, error=err_msg)

        context_id = getattr(message, "context_id", None)
        if context_id and self._per_topic():
            return await self._respond_in_topic(message, str(context_id))

        # Pre-execution sync from IDE to CLI if conversation active
        active = self.resolve_conversation()
        if active:
            conv_id, _ = active
            self.sync_conversation_to_cli(conv_id)

        reply = await super().respond(message)

        # Post-execution sync from CLI back to IDE so turns are visible in IDE
        if reply and reply.ok and active:
            conv_id, _ = active
            self.sync_conversation_to_ide(conv_id)

        return reply


class AntigravityThreadAdapter(AntigravityContextMixin, AgentAdapter):
    """Queue Telegram messages into an existing Antigravity conversation session.

    Prefers `agentapi send-message` to route messages directly into the running
    Antigravity IDE session with MESSAGE_PRIORITY_HIGH in real time, sharing
    conversational context and the active session model (e.g. Gemini 3.8 Flash High).

    If agentapi is not available (e.g. headless/CI environment), gracefully falls
    back to `agy -p` passing `--conversation` and the detected `--model`.

    By default, targets "auto", which dynamically tracks the most recently active
    conversation for the configured workspace (`cwd`) without requiring manual
    updates to config.yaml.
    """

    TYPE = "antigravity_thread"

    @classmethod
    def default_settings(cls, cwd: Optional[str] = None, conversation: Optional[str] = "auto",
                         timeout_seconds: int = 1500) -> AdapterSettings:
        return AdapterSettings(type=cls.TYPE, cwd=cwd, timeout_seconds=timeout_seconds,
                               conversation=conversation or "auto")

    def __init__(self, alias: str, settings: Optional[AdapterSettings] = None,
                 runner=None, git_identity=None):
        super().__init__(alias, settings, git_identity=git_identity)
        if not self.settings.conversation:
            if self.settings.cwd:
                active_file = Path(self.settings.cwd) / ACTIVE_CONVERSATION_FILENAME
                if active_file.is_file():
                    try:
                        c = active_file.read_text(encoding="utf-8").strip().lstrip("\ufeff").strip()
                        if c:
                            self.settings.conversation = c
                    except Exception:
                        pass
        if not self.settings.conversation:
            self.settings.conversation = "auto"

        # Issue #85 mode 5: whether an unreachable endpoint may take the blocking
        # ``agy -p`` path. Default True -- the class contract above documents the
        # fallback as the supported headless/CI behaviour, and a one-shot
        # ``agy -p`` with no session to resume is genuinely the delivery path
        # there. The long-running transport sets this False while the IDE is
        # present, because there the fallback is pathological: measured
        # 2026-09-21, one such run held the channel's turn lock past its
        # 1500s-class budget while later addressed messages were dropped.
        self.allow_blocking_fallback = True
        # Issue #123: honest liveness for the heartbeat's transport block. None until a
        # dispatch runs; True once one reached the session via the live endpoint; False
        # when an endpoint-class failure forced (or, with the gate armed, would have
        # forced) the blocking path. The scheduled poller cannot see any of this --
        # that is exactly why "fresh beat" hid a 25-minute mute.
        self._last_dispatch_endpoint_ok: Optional[bool] = None

    def arm_transport_failfast(self) -> None:
        """Issue #123: arm the fail-fast gate for the long-running transport only.

        Deliberately NOT the class default: a one-shot ``bus-poll`` on a headless
        host, and CI, keep the documented blocking fallback. Only the transport
        knows it holds the channel's turn lock, and it arms via
        ``build_adapter(..., transport=True)``.
        """
        self.allow_blocking_fallback = False

    def endpoint_ok(self) -> Optional[bool]:
        """Issue #123: endpoint health of the last dispatch (None = none yet)."""
        return self._last_dispatch_endpoint_ok

    def _dispatch_env(self, endpoint_address: Optional[str],
                      csrf_token: str = "") -> dict:
        """Environment for one agentapi dispatch, with the endpoint injected.

        ``environment()`` carries the git identity and ``settings.extra_env``; the
        discovered endpoint and credential are layered on top. An IDE-set
        variable is already in ``os.environ`` and therefore already in the base,
        so passing ``None``/``""`` leaves an operator's own value untouched.

        The credential is REQUIRED alongside the address: measured on this host,
        a correct address without it fails every dispatch with
        ``missing CSRF token``. It is never logged.
        """
        env = self.environment()
        if endpoint_address:
            env[endpoint_probe.LS_ADDRESS_ENV] = endpoint_address
        # environment() drops names with TOKEN in them (review P0-6); agentapi is the
        # transport's own call to the IDE, not the agent's shell, so an inherited
        # credential is handed back here.
        inherited = os.environ.get(endpoint_probe.CSRF_TOKEN_ENV)
        if csrf_token or inherited:
            env[endpoint_probe.CSRF_TOKEN_ENV] = csrf_token or inherited
        return env

    def accepts_explicit_username_delivery(self) -> bool:
        """Keep every explicit username-addressed message flowing into the active task.

        When ``conversation: auto`` is configured, incoming messages are routed into
        the most recently active conversation for this workspace, allowing seamless
        hands-free interaction.
        """
        return True

    def accepts_exact_username_during_loop_guard(self) -> bool:
        return True

    def build_fallback_argv(self, prompt: str, conversation_id: str) -> list[str]:
        timeout_min = max(1, int(self.settings.timeout_seconds / 60))
        argv = ["agy", "-p", prompt, "--print-timeout", f"{timeout_min}m"]
        if skips_approvals(self.settings):
            argv.append("--dangerously-skip-permissions")
        if sandboxed(self.settings):
            argv.append("--sandbox")
        if conversation_id and conversation_id.lower() != "auto":
            argv.extend(["--conversation", conversation_id])
            conv_model = self.resolve_conversation_model(conversation_id)
            if conv_model:
                argv.extend(["--model", conv_model])
        return argv

    def build_argv(self, prompt: str) -> list[str]:
        conv_id = self.settings.conversation or "auto"
        resolved = self.resolve_conversation()
        if resolved:
            conv_id = resolved[0]
        return self.build_fallback_argv(prompt, conv_id)

    def _timeout_error(self, fallback_reason: str = "") -> str:
        """Name the CAUSE of a print-run timeout (issue #85).

        A 1500 s timeout used to read identically whether the session had
        overflowed its window or whether the delivery endpoint was simply
        unreachable so every message took the slow ``agy -p`` path. Those need
        different responses (rotate/re-anchor vs fix the endpoint), so the
        cause is stated explicitly.

        Of the vocabulary discussed on #85 — ``context overflow`` / ``slow
        path`` / ``generic`` — only the latter two are emitted here. This
        adapter cannot distinguish an overflow from an ordinary long run, and a
        *known* overflow timing out to ``timeout_seconds`` is itself a contract
        violation: it should be detected upstream (a re-anchored API 400), not
        classified after the budget is spent. So a bare ``generic`` timeout
        remains possible and is not silently relabelled.
        """
        elapsed = self.settings.timeout_seconds
        if fallback_reason:
            return (
                f"{self.alias} thread execution exceeded {elapsed}s on the slow path "
                f"(delivery endpoint unresolved; fell back to agy -p: {fallback_reason})"
            )
        return f"{self.alias} thread execution timed out after {elapsed}s (generic)"

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        if not self.settings.enabled:
            return None

        # Check for administrative context switch commands
        target = match_context_switch(message.text or "", self.alias)
        if target is not None:
            if message.is_admin:
                ok, response_text = self.set_active_conversation(target)
                return AdapterReply(text=response_text, ok=ok, error="" if ok else response_text)
            err_msg = "Unauthorized: context switching is restricted to administrators."
            return AdapterReply(text=err_msg, ok=False, error=err_msg)

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

        # Review round 2 (B3): the planted delivery token travels in
        # ``response_instructions`` — it MUST reach the session prompt or the
        # delivery probe can never confirm (codex_thread prepends the same way).
        instructions = (getattr(message, "response_instructions", "") or "").strip()
        if instructions:
            prompt = f"{instructions}\n\n{prompt}"

        resolved = self.resolve_conversation()
        if not resolved:
            return AdapterReply(
                ok=False,
                error=f"{self.alias} could not resolve an active conversation for workspace {self.settings.cwd}",
            )
        conv_id, _ = resolved

        # Try agentapi send-message first (direct queueing into active IDE session)
        # Issue #85: remember WHY we degraded, so a subsequent timeout can name the
        # cause (endpoint unresolved) rather than looking like a context overflow.
        fallback_reason = ""
        fallback_rc: Optional[int] = None
        agentapi_res = resolve_agentapi()
        # Issue #85 mode 5: make the fast path work from a process the IDE did not
        # spawn. A scheduler-launched transport has no ANTIGRAVITY_LS_ADDRESS (the
        # endpoint is per-process and rotates), so without this every dispatch
        # exits 1 and falls through to the blocking agy -p path. Recovery is
        # measured, not assumed: an endpoint counts only once agentapi has
        # actually answered from it.
        endpoint_address: Optional[str] = None
        if agentapi_res:
            agentapi_cmd = ([agentapi_res] if isinstance(agentapi_res, str)
                            else list(agentapi_res))
            argv = [
                *agentapi_cmd,
                "send-message",
                f"--title={agentapi_title(message)}",
                conv_id,
                prompt,
            ]
            # Two attempts at most: the first with whatever endpoint we have, the
            # second (only if the first failed for an ENDPOINT reason) with a
            # freshly probed one. A stale port or a stale credential survives an
            # IDE restart, so the retry is the difference between "recovered" and
            # "wedged until the next transport restart".
            for attempt in (1, 2):
                recovery_error = False
                try:
                    # resolve_endpoint, not resolve_ls_address: the address alone
                    # cannot dispatch, because agentapi authenticates with a
                    # per-session credential that a scheduler-launched transport
                    # does not inherit (measured: "missing CSRF token" on every
                    # dispatch while the address was correct).
                    resolved_endpoint = await asyncio.to_thread(
                        endpoint_probe.resolve_endpoint, agentapi_cmd, conv_id)
                except Exception as exc:                  # noqa: BLE001 - recovery is best-effort
                    logger.debug("endpoint recovery failed (%s); continuing", exc)
                    resolved_endpoint = None
                    recovery_error = True
                # Assigned unconditionally, INCLUDING to None. If the second
                # discovery finds nothing, attempt 1's now-stale address must not
                # linger: the fail-fast gate reads an address as "the endpoint is
                # known", and keeping a dead one there would silently re-arm the
                # 1500s blocking path this guard exists to prevent.
                endpoint_address = (resolved_endpoint.address
                                    if resolved_endpoint is not None else None)
                csrf_token = (resolved_endpoint.csrf_token
                              if resolved_endpoint is not None else "")
                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        argv,
                        cwd=self.settings.cwd,
                        env=self._dispatch_env(endpoint_address, csrf_token),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30.0,
                        check=False,
                    )
                except Exception as exc:
                    fallback_reason = _bound_reason(f"agentapi execution error: {exc}")
                    logger.warning("agentapi execution error (%s), falling back to agy -p", exc)
                    break
                if result.returncode == 0:
                    logger.info(
                        "Queued Telegram message into Antigravity session %s via agentapi%s",
                        conv_id, f" (endpoint {endpoint_address})" if endpoint_address else "",
                    )
                    self._last_dispatch_endpoint_ok = True
                    return None
                fallback_reason = describe_agentapi_failure(result)
                fallback_rc = result.returncode
                if attempt == 1 and is_endpoint_failure(fallback_reason):
                    # The endpoint we had is not answering, or its credential is
                    # no longer accepted. Drop both and let the next iteration
                    # re-discover before giving up on the fast path.
                    #
                    # ONLY when something was actually resolved. When discovery
                    # found nothing there is no stale pair to drop, and clearing
                    # the negative record here would defeat it completely: the
                    # retry would re-read the host, the retry would fail the same
                    # way, and every message would pay a full discovery -- exactly
                    # the per-message tax _NEGATIVE_TTL_SECONDS exists to remove.
                    if endpoint_address:
                        endpoint_probe.invalidate_cache()
                        logger.info(
                            "endpoint: agentapi refused (%s); re-discovering the "
                            "endpoint and credential before falling back",
                            fallback_reason,
                        )
                        continue
                    if recovery_error:
                        # Recovery RAISED, so it never reached the negative-cache
                        # arm: the next attempt does a real discovery and can
                        # genuinely resolve a working endpoint. Keep the retry --
                        # skipping it here would trade a per-message cost for a
                        # lost recovery path.
                        continue
                    # Nothing was resolved, and recovery did not error -- so the
                    # retry cannot differ. Retrying anyway ran agentapi a SECOND
                    # time per message (measured: 2.0 processes per message on a
                    # headless host) with byte-identical argv: attempt 2 resolves
                    # the same None straight off the negative cache and hands the
                    # child the same environment. The first attempt's exit code and
                    # reason are already in hand, so a retry that can only repeat
                    # them is pure cost.
                    logger.debug(
                        "endpoint: agentapi refused (%s) with no endpoint "
                        "resolved; not retrying an identical dispatch",
                        fallback_reason,
                    )
                    break
                break
        # NOTE (issue #85): when ``agentapi`` is simply not resolvable we do NOT
        # record a fallback reason. The class docstring treats an absent agentapi
        # as a supported headless/CI configuration ("gracefully falls back to
        # agy -p"), so labelling every such timeout a degraded "slow path" would
        # raise a false alarm on every message. "slow path" is reserved for the
        # real defect: agentapi RESOLVED but refused because the session endpoint
        # was unreachable.

        # Issue #85 mode 5 -- fail FAST when the endpoint is the problem.
        #
        # The blocking ``agy -p`` fallback cannot succeed when the endpoint is
        # unreachable in a way that is not about the endpoint being unknown: the
        # CLI is handed the same live-IDE conversation and would block for the
        # whole ``timeout_seconds`` budget. Measured 2026-09-21: one such run
        # (pid 37060, started 16:59) held the channel's turn lock while 15
        # addressed messages were dropped at channel.py:1021 and the transport
        # still reported a fresh heartbeat. A fast, visible AdapterReply is
        # strictly better: the channel already surfaces ``ok=False``
        # (channel.py:1113-1120) and releases the turn lock in milliseconds, so
        # the NEXT message is handled instead of discarded.
        #
        # The guard is intentionally narrow -- it fires only when agentapi
        # resolved AND refused with an endpoint cause AND no endpoint was
        # resolvable. A genuinely headless host keeps the documented fallback.
        if (fallback_reason and is_endpoint_failure(fallback_reason)
                and not endpoint_address and not self.allow_blocking_fallback):
            self._last_dispatch_endpoint_ok = False
            return AdapterReply(
                ok=False,
                error=(f"{self.alias} could not reach the live session endpoint "
                       f"({fallback_reason}); refusing to block on the fallback path "
                       f"for up to {self.settings.timeout_seconds}s while the endpoint "
                       f"is unreachable -- fix {endpoint_probe.LS_ADDRESS_ENV} or the "
                       f"IDE session, then re-send"),
            )

        # Fallback to agy -p
        if fallback_reason:
            if is_endpoint_failure(fallback_reason):
                self._last_dispatch_endpoint_ok = False
            # Logged HERE, not at the point agentapi refused: the fail-fast gate
            # below may return instead, and a warning announcing a fallback that
            # never happened is exactly the kind of misleading line that made the
            # original outage hard to read.
            logger.warning(
                "agentapi queue failed with exit code %s, falling back to agy -p: %s",
                fallback_rc, fallback_reason,
            )
        self.sync_conversation_to_cli(conv_id)
        argv = self.build_fallback_argv(prompt, conv_id)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                argv,
                cwd=self.settings.cwd,
                env=self.environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(self.settings.timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AdapterReply(
                ok=False,
                error=self._timeout_error(fallback_reason),
            )
        except FileNotFoundError as exc:
            return AdapterReply(ok=False, error=f"{self.alias} could not start: {exc}")
        except OSError as exc:
            return AdapterReply(ok=False, error=f"{self.alias} thread execution failed: {exc}")

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            summary = detail[-1] if detail else f"exit code {result.returncode}"
            return AdapterReply(ok=False, error=f"{self.alias} thread execution failed: {summary}")

        self.sync_conversation_to_ide(conv_id)

        # The active Antigravity session owns the response. Returning None
        # prevents Telegram from echoing or creating a duplicate agent turn.
        return None

    # -- delivery probe (issue #59 deliverable 6 + spec amendment 2) ---------------
    def _conversation_db_paths(self, conv_id: str) -> List[Path]:
        """Candidate on-disk paths for a conversation's SQLite store."""
        gemini_dir = Path.home() / ".gemini"
        return [
            gemini_dir / "antigravity" / "conversations" / f"{conv_id}.db",
            gemini_dir / "antigravity-cli" / "conversations" / f"{conv_id}.db",
        ]

    def _open_conversation_db(self, conv_id: str):
        """Open the first existing conversation DB read-only; ``None`` if absent.

        Override-friendly seam for tests (so a fixture DB can be injected
        without touching the real ``~/.gemini`` tree).
        """
        import sqlite3 as _sqlite
        for path in self._conversation_db_paths(conv_id):
            if path.is_file():
                try:
                    return _ConversationDb(
                        _sqlite.connect(f"file:{path}?mode=ro", uri=True))
                except Exception:
                    # Never open the operator's live store read-WRITE (review
                    # B9): retry in immutable mode. It can open where mode=ro
                    # cannot, but it IGNORES the -wal — see verify_delivery's
                    # miss rule (review N1).
                    try:
                        return _ConversationDb(
                            _sqlite.connect(f"file:{path}?immutable=1", uri=True),
                            immutable=True)
                    except Exception:
                        return None
        return None

    async def verify_delivery(self, message: IncomingMessage) -> Optional[AdapterReply]:
        """Probe the Antigravity conversation store for the planted delivery token.

        Issue #59 spec amendment (2) — verified 1:1 against ``agentapi`` acks:

            open ``~/.gemini/antigravity/conversations/<conversation_id>.db`` READ-ONLY
            delivered ⇔ ``SELECT idx FROM steps WHERE step_type = 101
                              AND step_payload LIKE '%<token>%'
                              ORDER BY idx DESC LIMIT 1`` returns a row

        The ``step_type = 101`` filter is **mandatory**. The ``steps`` table has
        no direction/author column, and a LIKE-alone match false-positives on the
        agent's own tool-argument echoes (``step_type = 15``). ``step_type = 101``
        is the only inbound record. The probe:

        * returns ``AdapterReply(ok=True, ...)`` when a step_type=101 row exists
          whose ``step_payload`` contains the planted token from the prompt,
        * returns ``AdapterReply(ok=False, ...)`` when the row is absent (clean
          negative; the poller escalates after N attempts) — but ONLY when the
          store was read in the normal ``mode=ro``; under the ``immutable=1``
          fallback (which ignores the ``-wal``) an absent row is UNKNOWN and is
          raised as a probe error instead (review N1),
        * raises on probe *errors* — the poller's hard degrade rule logs and
          continues, never escalating on a probe error.
        """
        token = self._extract_planted_token(message)
        if not token:
            return None
        conv_id = self.settings.conversation or "auto"
        if not conv_id or conv_id.lower() == "auto":
            # ``auto`` means the active conversation — resolve through the
            # shared helper (no ``agentapi`` call per spec amendment 3).
            try:
                resolved = self.resolve_conversation()
            except Exception:
                raise
            if not resolved:
                raise RuntimeError("antigravity verify_delivery: no active conversation")
            conv_id = resolved[0]

        opened = self._open_conversation_db(conv_id)
        if opened is None:
            # The DB does not exist yet (the session may be still warming up);
            # surface that as a probe *error* so the poller degrades — the row
            # is not (yet) absent, and we must not falsely escalate.
            raise RuntimeError(f"antigravity conversation DB not found: {conv_id}")
        conn = opened.conn
        try:
            row = conn.execute(
                "SELECT idx FROM steps "
                "WHERE step_type = 101 AND step_payload LIKE ? "
                "ORDER BY idx DESC LIMIT 1",
                (f"%{token}%",)).fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if row is None:
            if opened.immutable:
                # `immutable=1` ignores the -wal: the row may be committed and
                # only present in the WAL (the live-session case this probe
                # exists for). A miss here is UNKNOWN, not a clean negative —
                # escalate as a probe error so the poller degrades (review N1).
                raise RuntimeError(
                    "antigravity verify_delivery: immutable fallback cannot see "
                    "WAL rows; miss is unconfirmable (degrading, not escalating)")
            return AdapterReply(ok=False,
                                error="no inbound step (step_type=101) for planted token",
                                meta={"conversation": conv_id,
                                      "token": token})
        return AdapterReply(ok=True,
                            meta={"conversation": conv_id,
                                  "token": token,
                                  "step_idx": row[0]})

    @staticmethod
    def _extract_planted_token(message: IncomingMessage) -> str:
        """Scrape ``[BUS_DELIVERY_TOKEN=<token>]`` from the adapter's instructions."""
        instr = (getattr(message, "response_instructions", "") or "")
        match = re.search(r"\[BUS_DELIVERY_TOKEN=([^\]]+)\]", instr)
        return match.group(1).strip() if match else ""

