"""Keep the maintainer's handle, chat ids and absolute local paths off the durable plane.

Issue #22 (the rescoped #1): bot @usernames may stay visible -- they are public on Telegram,
and the bots' allowlists already ignore strangers -- but three things must not leave the
host: the maintainer's handle, chat ids, and absolute local paths. The raw values stay in
local config and state; what goes on the bus carries a **topic token** instead of a chat id.

The token has two forms:

- ``topic:<project>:<thread|general>`` -- from the agent's own ``projects:`` block. Every
  agent resolves it through its own config, so dedupe keeps working without any chat id
  leaving a host.
- ``topic:hmac-<16 hex>`` -- an HMAC of the chat id with a secret from
  ``SWARMLY_REDACTION_SECRET``, used when no project is bound. No peer can resolve it, so a
  peer that already took the Telegram copy runs the mirror too: **processed twice, never
  lost**, which is the direction the issue chose.

A redactor is built once per config and applied at the two outbound choke points -- the bus
envelope in :func:`swarmly.telegram.relay.post_and_notify` and the GitHub-facing helpers --
so a new call site cannot forget it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["TOPIC_PREFIX", "SECRET_ENV", "topic_token", "hmac_token", "resolve_topic_token",
           "Redactor", "CHAT", "PATH", "USER", "HANDLE"]

TOPIC_PREFIX = "topic:"
#: Shared secret for the HMAC form. Without it there is no token at all, which degrades to
#: the peer running the mirrored turn (a duplicate, never a lost hand-off).
SECRET_ENV = "SWARMLY_REDACTION_SECRET"

CHAT = "<chat>"
PATH = "<path>"
USER = "<user>"
HANDLE = "<maintainer>"

#: Backstops for ids and paths the config does not name (a peer's chat id quoted in text, an
#: absolute path under another account). Both are sensitive wherever they appear.
_CHAT_PATTERN = re.compile(r"(?<!\d)-100\d{6,}(?!\d)")
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\{1,2}(?:Users|Documents and Settings)\\{1,2}[^\\\s\"']+")
_POSIX_PATH = re.compile(r"(?<![\w/])/(?:home|Users)/[^/\s\"']+")


def topic_token(project: str, thread_id: Optional[int]) -> str:
    """The token for a topic bound to ``project`` (``general`` when no thread is bound)."""
    key = str(project).strip()
    which = "general" if thread_id is None else str(int(thread_id))
    return f"{TOPIC_PREFIX}{key}:{which}"


def hmac_token(chat_id: int, secret: str) -> str:
    """The fallback token: an HMAC of the chat id, opaque to every peer by design."""
    if not secret:
        return ""
    digest = hmac.new(secret.encode("utf-8"), str(int(chat_id)).encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"{TOPIC_PREFIX}hmac-{digest[:16]}"


def resolve_topic_token(token: Any, router: Any) -> List[Tuple[int, Optional[int]]]:
    """The local ``(chat_id, thread_id)`` pairs a token names, for *this* agent.

    Empty for the HMAC form and for a project this agent does not configure: the caller then
    treats the mirror as not yet handled, which is the duplicate-not-lost direction.
    """
    if not isinstance(token, str) or not token.startswith(TOPIC_PREFIX) or router is None:
        return []
    rest = token[len(TOPIC_PREFIX):]
    if rest.startswith("hmac-"):
        return []
    project, _, which = rest.rpartition(":")
    if not project or not which:
        return []
    topics = router.topics_of(project) or []
    if which == "general":
        # Only a project that really is bound without a thread: a threadless fallback for a
        # threaded project would claim messages it never took.
        return [(chat, None) for chat, thread_id in topics if thread_id is None]
    try:
        thread = int(which)
    except ValueError:
        return []
    return [(chat, thread_id) for chat, thread_id in topics if thread_id == thread]


@dataclass
class Redactor:
    """Replace the maintainer's handle, chat ids and absolute paths in outbound text."""

    literals: Dict[str, str] = field(default_factory=dict)   # value -> replacement
    chat_ids: Tuple[int, ...] = ()
    user_ids: Tuple[int, ...] = ()
    paths: Tuple[str, ...] = ()
    secret: str = ""

    # -- construction ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Any) -> "Redactor":
        identity = getattr(config, "identity", None)
        literals = dict(getattr(config, "public_redactions", None) or {})
        maintainer = str(getattr(identity, "maintainer_username", "") or "").strip()
        if maintainer:
            literals.setdefault(maintainer, HANDLE)
            literals.setdefault(maintainer.lstrip("@"), HANDLE)
        home = getattr(identity, "home_channel_id", None)
        chat_ids: List[int] = []
        paths: List[str] = []
        if home:
            chat_ids.append(int(home))
        adapter = getattr(config, "adapter", None)
        if getattr(adapter, "cwd", None):
            paths.append(str(adapter.cwd))
        home_channel = int(home or 0)
        for project in (getattr(config, "projects", None) or {}).values():
            if getattr(project, "cwd", None):
                paths.append(str(project.cwd))
            for chat, _thread in (getattr(project, "topics", None) or []):
                chat_ids.append(int(chat) if chat is not None else home_channel)
        user_ids: List[int] = []
        admin = getattr(identity, "admin_user_id", None)
        if admin:
            user_ids.append(int(admin))
        for value in (getattr(identity, "allowed_users", None) or []):
            if str(value).lstrip("-").isdigit():
                user_ids.append(int(value))
        for target in (getattr(config, "peers", None) or {}).values():
            if str(target).lstrip("-").isdigit():
                user_ids.append(int(target))
        try:
            paths.append(str(Path.home()))
        except Exception:                                   # noqa: BLE001 - no home, no path
            pass
        return cls(
            literals=literals,
            chat_ids=tuple(sorted({c for c in chat_ids if c})),
            user_ids=tuple(sorted({u for u in user_ids if u})),
            paths=tuple(sorted({p for p in paths if p}, key=len, reverse=True)),
            secret=os.environ.get(SECRET_ENV, ""),
        )

    # -- redaction ---------------------------------------------------------------------
    def text(self, value: str) -> str:
        """``value`` with every configured secret replaced by its placeholder."""
        out = str(value)
        for literal in sorted(self.literals, key=len, reverse=True):
            if literal:
                out = out.replace(literal, self.literals[literal])
        for chat in self.chat_ids:
            # The signed form first: replacing the digits of a negative id first leaves a
            # stray "-" behind ("chat -<chat>").
            for form in (str(chat), str(abs(chat))):
                out = out.replace(form, CHAT)
        for user in self.user_ids:
            if len(str(abs(user))) >= 5:            # a short id would eat unrelated numbers
                for form in (str(user), str(abs(user))):
                    out = out.replace(form, USER)
        for path in self.paths:
            if len(path) >= 4:
                out = out.replace(path, PATH)
                out = out.replace(path.replace("\\", "/"), PATH)
        out = _CHAT_PATTERN.sub(CHAT, out)
        out = _WINDOWS_PATH.sub(PATH, out)
        out = _POSIX_PATH.sub(PATH, out)
        return out

    def value(self, value: Any) -> Any:
        """``value`` with the redaction applied through dicts and lists (JSON stays valid)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return self.number(int(value))
        if isinstance(value, dict):
            # A key is redacted like a value: an int key is how a chat id travels as
            # ``{<chat id>: ...}``, and the first cut only looked at string keys (review of
            # PR #23).
            return {self.key(k): self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value

    def key(self, value: Any) -> Any:
        """A dict key after the redaction: a string is scrubbed, an int id replaced."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return self.number(int(value))
        return value

    def number(self, value: int) -> Any:
        """An int as it may leave the host: an id becomes its placeholder, anything else
        stays a number. A structured payload carries ids as ints -- a chat id here, the
        admin's user id there -- so the check covers both signs, not only the home chat."""
        for chat in self.chat_ids:
            if value in (chat, abs(chat)):
                return CHAT
        for user in self.user_ids:
            if value in (user, abs(user)):
                return USER
        return value

    # -- tokens ------------------------------------------------------------------------
    def chat_token(self, chat_id: int) -> str:
        return hmac_token(int(chat_id), self.secret)

    def topic_ref(self, chat_id: int, thread_id: Optional[int], router: Any) -> Dict[str, Any]:
        """The ``telegram`` reference a hand-off mirror carries: a token, never the id."""
        project = None
        if router is not None and thread_id is not None:
            project = router.project_for(int(chat_id), thread_id)
        token = topic_token(project.key, thread_id) if project is not None else self.chat_token(chat_id)
        return {"topic": token} if token else {}
