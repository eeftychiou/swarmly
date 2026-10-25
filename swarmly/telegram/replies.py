"""Durable, explicit replies for queued tasks; never scrape unrelated task output.

SQLite transactions coordinate the polling bridge and short-lived reply CLI processes.
An uncertain network send is NOT retried automatically: Telegram has no idempotency key.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional, Union

#: The channel's default reply budget, shared with the turn rules (issue #31). Imported here
#: rather than duplicated so one number cannot drift from the other.
from swarmly.telegram.adapters.base import DEFAULT_REPLY_CHARS, drop_stray_silence


def reply_state_path(config) -> Path:
    if config.adapter.reply_state_path:
        path = Path(config.adapter.reply_state_path).expanduser()
        if not path.is_absolute() and config.source_path:
            path = config.source_path.resolve().parent / path
        return path.resolve()
    if config.source_path is None:
        raise ValueError("queued replies require a config file or adapter.reply_state_path")
    return config.source_path.resolve().with_suffix(".replies.sqlite3")


def reply_state_path_for(raw_path: str, base_dir: Optional[str] = None) -> Path:
    """Resolve a raw ``adapter.reply_state_path`` string into an absolute Path.

    Issue #59 deliverable 6: the ``codex_thread`` adapter probes the durable session
    record using its own configured path. The :func:`reply_state_path` helper takes a
    whole :class:`AgentConfig`; this one takes just the path string so the adapter
    can read it without an AgentConfig.

    Review round 2 (B9): relative paths resolve against ``base_dir`` (the adapter's
    configured cwd), matching how :func:`reply_state_path` resolves against
    ``config.source_path.parent`` — resolving against the process CWD silently
    pointed at a different file.
    """
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base_dir:
        path = Path(base_dir).expanduser() / path
    return path.resolve()


class ReplyStore:
    def __init__(self, path: Path, reply_chars: Optional[int] = None):
        self.path = Path(path)
        #: Issue #31: the reply budget a ticket's answer is held to. A ticket has no topic,
        #: so this is the channel's ``limits.reply_chars`` (500 unless changed); a project's
        #: per-topic override reaches the turn rules, not this sender.
        self.reply_chars = (reply_chars if reply_chars and reply_chars > 0
                            else DEFAULT_REPLY_CHARS)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                # A first-time legacy migration can race another opener while
                # SQLite changes the journal mode. The schema transaction below
                # still serializes on the same connection; keep the current
                # mode for this opener rather than failing startup.
                if "locked" not in str(exc).lower():
                    raise
            db.execute("""CREATE TABLE IF NOT EXISTS replies (
                id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER,
                recipient TEXT NOT NULL, original TEXT NOT NULL, created REAL NOT NULL,
                deadline REAL NOT NULL, state TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '', telegram_message_id INTEGER,
                notified INTEGER NOT NULL DEFAULT 0,
                receipt_reserved INTEGER NOT NULL DEFAULT 0,
                receipt_state TEXT NOT NULL DEFAULT 'none',
                receipt_detail TEXT NOT NULL DEFAULT '',
                receipt_notified INTEGER NOT NULL DEFAULT 0,
                receipt_message_id INTEGER,
                alerts INTEGER NOT NULL DEFAULT 0,
                reply_chars INTEGER,
                UNIQUE(chat_id, message_id))""")
            # Issue #31 review: a ticket raised in a topic with its own reply budget must be
            # judged by that budget, so the number travels with the ticket. An older database
            # has no such column yet; add it in place (NULL = the store's budget).
            if "reply_chars" not in {row[1] for row in db.execute("PRAGMA table_info(replies)")}:
                try:
                    db.execute("ALTER TABLE replies ADD COLUMN reply_chars INTEGER")
                except sqlite3.OperationalError as exc:
                    # Another opener added it between the check and the ALTER: same column,
                    # same schema, no work left. Anything else ("database is locked", a
                    # corrupt file) must not be swallowed -- the next register would fail
                    # with "no such column" far from the cause (review of #31).
                    if "duplicate column name" not in str(exc).lower():
                        raise
            # Existing stores predate the atomic receipt claim. Inspect the schema
            # once so normal CLI startup does not rely on exception-driven ALTERs.
            columns = {row['name'] for row in db.execute("PRAGMA table_info(replies)")}
            for column, definition in (
                ("receipt_reserved", "INTEGER NOT NULL DEFAULT 0"),
                ("receipt_state", "TEXT NOT NULL DEFAULT 'none'"),
                ("receipt_detail", "TEXT NOT NULL DEFAULT ''"),
                ("receipt_notified", "INTEGER NOT NULL DEFAULT 0"),
                ("receipt_message_id", "INTEGER"),
                ("alerts", "INTEGER NOT NULL DEFAULT 0"),
                # The topic the message was in: its answer is posted there (and refused in
                # General and Swarm pulse when they are transport-only). NULL: an older ticket.
                ("thread_id", "INTEGER"),
            ):
                if column not in columns:
                    try:
                        db.execute(f"ALTER TABLE replies ADD COLUMN {column} {definition}")
                    except sqlite3.OperationalError as exc:
                        # Another bridge/CLI may have observed the same legacy
                        # schema and won the migration race. Preserve the
                        # explicit schema check for normal startup, but tolerate
                        # only that expected concurrent outcome.
                        if "duplicate column name" not in str(exc).lower():
                            raise
                    columns.add(column)
            # A legacy reservation on a resolved task is already accounted for.  Do
            # not turn it into a fresh warning merely because the newer receipt
            # columns were added after the task finished.
            db.execute("""UPDATE replies SET receipt_state='sent', receipt_detail=''
                         WHERE receipt_reserved=1 AND receipt_state='none'
                           AND state IN ('sent', 'skipped')""")
            db.execute("""UPDATE replies SET receipt_state='unknown',
                         receipt_detail='legacy reservation found after bridge upgrade; inspect channel'
                         WHERE receipt_reserved=1 AND receipt_state='none'
                           AND state NOT IN ('sent', 'skipped')""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, *, chat_id, message_id, recipient, text, timeout=600, now=None,
                 reply_chars: Optional[int] = None, thread_id: Optional[int] = None):
        if message_id is None:
            raise ValueError("reply tickets require a Telegram message_id for deduplication")
        now = time.time() if now is None else now
        recipient = (recipient or "").lstrip("@")
        if recipient and not re.fullmatch(r"[A-Za-z0-9_]+", recipient):
            raise ValueError("invalid reply recipient")
        request_id = uuid.uuid4().hex
        with self._db() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO replies
                (id,chat_id,message_id,recipient,original,created,deadline,state,reply_chars,
                 thread_id)
                VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
                (request_id, chat_id, message_id, recipient, text, now, now + timeout,
                 reply_chars, thread_id))
            if cursor.rowcount:
                return dict(db.execute("SELECT * FROM replies WHERE id=?", (request_id,)).fetchone()), True
            row = db.execute("SELECT * FROM replies WHERE chat_id=? AND message_id=?",
                             (chat_id, message_id)).fetchone()
            return dict(row), False

    def budget_for(self, row: Any) -> int:
        """The reply budget a ticket is held to: the topic's own when it came from one.

        Issue #31 review: a ticket raised in a project topic whose ``reply_chars`` is larger
        than the channel's would otherwise be refused for following the turn rules it was
        given. The number rides on the ticket; an older ticket (or General) has none.
        """
        try:
            own = row["reply_chars"]
        except (KeyError, IndexError, TypeError):
            own = None
        return int(own) if own else self.reply_chars

    def get(self, request_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM replies WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("unknown reply ticket")
        return dict(row)

    def queued(self, request_id):
        with self._db() as db:
            db.execute("UPDATE replies SET state='queued' WHERE id=? AND state='pending'", (request_id,))

    def queue_failed(self, request_id, detail):
        with self._db() as db:
            db.execute("UPDATE replies SET state='queue_failed', detail=? WHERE id=? AND state='pending'",
                       (detail, request_id))

    def claim_receipt(self, request_id):
        """Atomically claim the queued receipt immediately before its network send.

        A state read followed by a send leaves a TOCTOU window: a task could submit ``--skip``
        between those operations. The conditional UPDATE is the linearization point; exactly
        one sender can reserve a ticket, and a prior answer/skip wins by returning zero rows.
        """
        with self._db() as db:
            cursor = db.execute(
                """UPDATE replies SET receipt_reserved=1, receipt_state='attempted', receipt_detail=''
                   WHERE id=? AND state IN ('queued','overdue') AND receipt_state='none'""",
                (request_id,),
            )
            return cursor.rowcount == 1

    def receipt_sent(self, request_id, message_id):
        with self._db() as db:
            db.execute("""UPDATE replies SET receipt_state='sent', receipt_message_id=?, receipt_detail=''
                         WHERE id=? AND receipt_state='attempted'""", (message_id, request_id))

    def receipt_unknown(self, request_id, detail):
        with self._db() as db:
            db.execute("""UPDATE replies SET receipt_state='unknown', receipt_detail=?,
                         receipt_notified=0 WHERE id=? AND receipt_state='attempted'""", (detail, request_id))

    def submit(self, request_id, text=None, skip_reason=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM replies WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("unknown reply ticket")
            if skip_reason is not None:
                if not skip_reason.strip():
                    raise ValueError("silence requires a reason (pure ACK/status only)")
                body, state, detail = "", "skipped", skip_reason.strip()
            else:
                body, state, detail = (text or "").strip(), "ready", ""
                if not body:
                    raise ValueError("reply cannot be empty; use --skip with a reason for non-actionable messages")
                tag = f"@{row['recipient']}" if row['recipient'] else ""
                if tag and not body.lower().startswith(tag.lower() + ":"):
                    body = f"{tag}: {body}"
                if not drop_stray_silence(body).strip():
                    # The transport never posts the sentinel, so this answer would post nothing.
                    raise ValueError("NO_REPLY is not an answer; use --skip with a reason for a "
                                     "pure ACK/status")
                limit = self.budget_for(row)
                if len(body) >= limit:
                    # The budget is "under N": N itself is already over it, as it always was
                    # for the default 500 (issue #31 keeps that boundary, only the number moves).
                    raise ValueError(
                        f"Telegram reply must be under {limit} characters including "
                        f"recipient (got {len(body)})")
            if row['state'] in ('ready', 'claimed', 'sending', 'sent', 'skipped'):
                if row['text'] == body and (state != 'skipped' or row['detail'] == detail):
                    return dict(row)  # idempotent submission, never a second send
                raise ValueError("ticket already has a different response")
            if row['state'] == 'unposted':
                raise ValueError(f"ticket is unposted ({row['detail']}); the transport will not "
                                 "post an answer for it")
            if row['state'] not in ('pending', 'queued', 'overdue'):
                raise ValueError(f"ticket is {row['state']}; inspect delivery before retrying")
            db.execute("UPDATE replies SET state=?, text=?, detail=?, notified=0 WHERE id=?", (state, body, detail, request_id))
        return self.get(request_id)

    def claim_ready(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM replies WHERE state='ready' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE replies SET state='claimed' WHERE id=?", (row['id'],))
        return dict(row)

    def attempting(self, request_id):
        with self._db() as db:
            db.execute("UPDATE replies SET state='sending' WHERE id=? AND state='claimed'", (request_id,))

    def release_claim(self, request_id):
        with self._db() as db:
            db.execute("UPDATE replies SET state='ready' WHERE id=? AND state='claimed'", (request_id,))

    def sent(self, request_id, message_id):
        with self._db() as db:
            db.execute("UPDATE replies SET state='sent', telegram_message_id=? WHERE id=? AND state='sending'",
                       (message_id, request_id))

    def unposted(self, request_id, detail):
        """The transport refused the answer before sending it (its topic is transport-only).

        Nothing was posted, and a retry would be refused the same way, so the ticket stops
        here. Its overdue alert then tells the admin the answer was not posted -- with a fresh
        alert count: a late answer to a ticket whose overdue alerts were already spent must
        still say it was not posted.
        """
        with self._db() as db:
            db.execute("UPDATE replies SET state='unposted', notified=0, alerts=0, detail=? "
                       "WHERE id=? AND state='claimed'", (detail, request_id))

    def uncertain(self, request_id):
        with self._db() as db:
            db.execute("UPDATE replies SET state='delivery_unknown', notified=0, detail='send interrupted or failed; inspect Telegram before retry' WHERE id=? AND state='sending'",
                       (request_id,))

    def recover(self):
        with self._db() as db:
            db.execute("""UPDATE replies SET receipt_state='unknown',
                         receipt_detail='bridge restarted during receipt send; inspect Telegram before retry',
                         receipt_notified=0
                         WHERE receipt_state='attempted'""")
            db.execute("UPDATE replies SET state='delivery_unknown', notified=0, detail='bridge restarted during send; inspect Telegram before retry' WHERE state='sending'")
            db.execute("UPDATE replies SET state='ready' WHERE state='claimed'")
            # A crash between queue submission and recording success is ambiguous too.
            db.execute("UPDATE replies SET state='overdue', detail='bridge restarted before queue confirmation' WHERE state='pending'")

    def overdue(self, now=None, alert_cap: int = 2):
        """Not-yet-notified obligations; a ticket stops alerting after ``alert_cap`` sends (#49)."""
        now = time.time() if now is None else now
        alert_cap = max(1, int(alert_cap))
        with self._db() as db:
            db.execute("UPDATE replies SET state='overdue', detail='task reply deadline exceeded' WHERE state='queued' AND deadline <= ?", (now,))
            rows = []
            for row in db.execute("""SELECT * FROM replies
                WHERE alerts < ? AND state IN ('overdue','delivery_unknown','queue_failed',
                                                'unposted')
                ORDER BY created""", (alert_cap,)):
                item = dict(row)
                item['alert_kind'] = 'task_overdue'
                rows.append(item)
            for row in db.execute("""SELECT * FROM replies
                WHERE receipt_notified=0 AND receipt_state='unknown'
                ORDER BY created"""):
                item = dict(row)
                item['alert_kind'] = 'receipt_failure'
                rows.append(item)
            return sorted(rows, key=lambda item: (item['created'], item['alert_kind']))

    def notified(self, request_id):
        with self._db() as db:
            db.execute("UPDATE replies SET notified=1, alerts=alerts+1 WHERE id=?", (request_id,))

    def resolve_prefix(self, ref, author=None):
        """#49: deterministically close ticket(s) named by a short ref from their author.

        A CLOSED-envelope from the originating author resolves without any task turn; the
        state becomes ``skipped`` (no Telegram post, no obligation), which also removes the
        row from the overdue queue. Covers every unfinished state — a broken queue
        (``queue_failed``) or an interrupted send (``delivery_unknown``) must not escape
        the author's closure, or exactly the tickets created by a dead adapter would be
        the ones that can never be closed (found live, issue #49 follow-up).
        """
        ref = str(ref or "").strip().lstrip("#!@").lower()
        if len(ref) < 4:
            return 0
        recipient = str(author or "").lstrip("@")
        with self._db() as db:
            query = ("UPDATE replies SET state='skipped', detail='closed by originating author', "
                     "notified=1 WHERE id LIKE ? AND state IN ('pending','queued','overdue',"
                     "'queue_failed','delivery_unknown','unposted')")
            params: List[Any] = [f"{ref}%"]
            if recipient:
                query += " AND lower(recipient)=lower(?)"
                params.append(recipient)
            cursor = db.execute(query, params)
            return int(cursor.rowcount or 0)

    def receipt_notified(self, request_id):
        with self._db() as db:
            db.execute("UPDATE replies SET receipt_notified=1 WHERE id=?", (request_id,))

    def summary(self):
        with self._db() as db:
            return dict(db.execute("SELECT state, COUNT(*) FROM replies GROUP BY state").fetchall())

    def public_status(self, request_id):
        row = self.get(request_id)
        return {key: row[key] for key in ('id', 'state', 'detail', 'telegram_message_id',
                                          'receipt_state', 'receipt_detail', 'receipt_notified',
                                          'receipt_message_id')}


def reply_instructions(config, row, workdir: Optional[Union[str, Path]] = None):
    # JSON quoting is illustrative and works for the ordinary paths used by both shells;
    # the task should invoke the argument vector, never evaluate incoming text as code.
    if config.source_path is None:
        raise ValueError("queued reply instructions require config.source_path")
    command = [sys.executable, "-m", "swarmly.telegram.cli", "--config", str(config.source_path.resolve()),
               "reply", row['id'], "--text", "your concise answer or blocker", "--wait", "45"]
    cwd = workdir or config.adapter.cwd or Path.cwd()
    budget = (int(row["reply_chars"]) if row and row.get("reply_chars")
              else int(config.limits.reply_chars))
    return (
        f"[A2A REPLY TICKET {row['id']}]\n"
        f"This was routed to YOU ({config.identity.username}). Original mentions below are intact.\n"
        "A local final answer is NOT sent to Telegram. Before finishing, submit an explicit reply using the local CLI.\n"
        f"Working directory: {cwd}\n"
        f"Command argument vector (quote each argument for your shell; this is JSON, not shell code): {json.dumps(command)}\n"
        f"Use the configured Python environment. Keep the answer under {budget} characters; "
        "the sender's mention is added automatically. "
        "Do not include private task content. Post substantive findings on GitHub first.\n"
        f"For a PURE ACK/status/sign-off with no question or action, use reply {row['id']} --skip \"reason no reply is needed\" instead (no Telegram post).\n"
        f"Check delivery with reply-status {row['id']}. Only state=sent proves Telegram accepted the reply; queued/ready is not completion. "
        "If unable to act, submit the blocker rather than staying silent.\n"
    )
