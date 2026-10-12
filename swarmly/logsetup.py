"""Persistent debug logs for every swarmly process.

Each process writes a rotating DEBUG log file in addition to its console output, so a
problem can be debugged after the fact even when the console went nowhere (a scheduled
task, a service whose journal rotated, a hidden window). One file per process role
avoids two processes rotating the same file.

* Directory: ``--log-dir``, else ``$A2A_LOG_DIR``, else ``~/.a2a/logs``. ``off`` disables
  the file.
* File level: ``$A2A_LOG_LEVEL`` (default ``DEBUG``). Console: INFO, or DEBUG with ``-v``.
* Size: ``$A2A_LOG_MAX_MB`` per file (default 10) and ``$A2A_LOG_BACKUPS`` old files
  (default 5).

Secrets never reach a handler: bot tokens, API keys and bearer tokens are masked in
every record.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Optional

FORMAT = "%(asctime)s %(process)d [%(levelname)s] %(name)s: %(message)s"
# Third-party loggers that are too chatty at DEBUG to be useful in the file.
QUIET = ("aiogram.event", "aiohttp", "asyncio", "urllib3", "googleapiclient", "google",
         "matrix", "websockets")

_SECRETS = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<telegram-token>"),
    (re.compile(r"/bot\d{6,12}:[A-Za-z0-9_-]+"), "/bot<telegram-token>"),
    (re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}\b"), r"\1-<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh_<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "AIza<redacted>"),
    (re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password)(\s*[=:]\s*|\s+)"
                r"([^\s,;'\"]{8,})"), r"\1\2<redacted>"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Mask secrets in the fully formatted message (args included) of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not lose the record
            message = str(record.msg)
        clean = redact(message)
        if clean != message or record.args:
            record.msg, record.args = clean, None
        return True


def log_dir(explicit: Optional[str] = None) -> Optional[Path]:
    value = explicit or os.environ.get("A2A_LOG_DIR") or "~/.a2a/logs"
    if value.strip().lower() in ("off", "none", "0", "false"):
        return None
    return Path(value).expanduser()


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "a2a"


def configure(name: str, *, verbose: bool = False, directory: Optional[str] = None,
              console: bool = True) -> Optional[Path]:
    """Set up console + rotating file logging for this process; return the file path.

    Safe to call more than once (later calls replace the handlers it installed). A file
    that cannot be opened is reported on the console and skipped, never fatal.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_a2a", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(FORMAT)
    redactor = RedactingFilter()
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.DEBUG if verbose else logging.INFO)
        stream.setFormatter(fmt)
        stream.addFilter(redactor)
        stream._a2a = True  # type: ignore[attr-defined]
        root.addHandler(stream)
    for noisy in QUIET:
        logging.getLogger(noisy).setLevel(logging.INFO)
    folder = log_dir(directory)
    if folder is None:
        return None
    path = folder / f"{safe_name(name)}.log"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        max_mb = float(os.environ.get("A2A_LOG_MAX_MB") or 10)
        backups = int(os.environ.get("A2A_LOG_BACKUPS") or 5)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=int(max_mb * 1024 * 1024), backupCount=backups, encoding="utf-8",
            delay=True)
    except (OSError, ValueError) as exc:
        logging.getLogger("swarmly.logsetup").warning("debug log disabled: %s: %s", path, exc)
        return None
    level = os.environ.get("A2A_LOG_LEVEL", "DEBUG").upper()
    handler.setLevel(getattr(logging, level, logging.DEBUG))
    handler.setFormatter(fmt)
    handler.addFilter(redactor)
    handler._a2a = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    logging.getLogger("swarmly.logsetup").debug("logging to %s (argv: %s)", path,
                                            redact(" ".join(sys.argv)))
    return path
