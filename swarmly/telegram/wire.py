"""
Wire format for the A2A Telegram channel protocol v1.1 — see ``docs/telegram/PROTOCOL.md``.

Two things live here that are easy to get wrong and expensive to debug:

1. **Offsets are UTF-16 units.** Telegram counts entity offsets and lengths in UTF-16 code
   units, not code points. An emoji before a mention shifts every following offset by one, so
   a mention built with ``len()`` silently points at the wrong characters — and the peer never
   sees it.
2. **A plain-text ``@username`` is not a mention.** Telegram does not resolve usernames inside
   message bodies; without an entity the message is not delivered to the peer's bot. That
   failure cost an hour on 2026-09-17, which is why ``mention_entities`` exists.

The header is one line by construction (``format_message`` never wraps it) so a machine can
parse it without unwrapping prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

PREFIX = "[A2A v1.1]"

#: A header-shaped line: the marker followed by an uppercase type token. Used only to spot a
#: near-miss marker sitting where ``parse_message`` cannot see it (see ``ignored_envelope_marker``).
_HEADER_SHAPE = re.compile(re.escape(PREFIX) + r"\s+[A-Z]")

#: Protocol budget for a chat message. ``max_message_chars`` (4096) is only Telegram's hard
#: API limit; this is the swarm's own signalling budget — details belong in the durable record.
CHANNEL_BUDGET_CHARS = 500

_FIELD_ORDER = ("to", "intent", "ref", "rev", "bus", "ttl", "id")


class MessageType:
    """Envelope types (spec §4). Plain strings so they serialise without a mapping."""

    HELLO = "HELLO"
    ACK = "ACK"
    REQUEST = "REQUEST"
    RESPONSE = "RESPONSE"
    STATUS = "STATUS"
    BLOCKED = "BLOCKED"
    NOTE = "NOTE"
    BYE = "BYE"
    RECEIPT = "RECEIPT"
    ALERT = "ALERT"

    ALL = (HELLO, ACK, REQUEST, RESPONSE, STATUS, BLOCKED, NOTE, BYE, RECEIPT, ALERT)


@dataclass
class Envelope:
    """The machine-first header of a channel message.

    A message carrying no header is a best-effort ``NOTE`` broadcast (spec §4) — peers speak
    prose, and the protocol degrades rather than rejects.
    """

    type: str
    to: str = "*"
    intent: Optional[str] = None
    ref: Optional[str] = None
    bus: Optional[str] = None
    ttl: Optional[str] = None
    id: Optional[str] = None
    rev: Optional[str] = None

    def __post_init__(self) -> None:
        self.type = str(self.type).upper()
        if self.type not in MessageType.ALL:
            raise ValueError(f"unknown message type {self.type!r}; known: {list(MessageType.ALL)}")

    def fields(self) -> Dict[str, str]:
        out: Dict[str, str] = {"to": self.to}
        for name in _FIELD_ORDER[1:]:
            value = getattr(self, name)
            if value:
                out[name] = str(value)
        return out

    def header(self) -> str:
        """One line, never wrapped. Values must not contain whitespace."""
        parts = []
        for name, value in self.fields().items():
            if any(ch.isspace() for ch in value):
                raise ValueError(f"envelope field {name}={value!r} must not contain whitespace")
            parts.append(f"{name}={value}")
        return f"{PREFIX} {self.type} " + " ".join(parts)

    @classmethod
    def parse_header(cls, line: str) -> "Envelope":
        body = line[len(PREFIX):].strip()
        tokens = body.split()
        if not tokens:
            raise ValueError("empty envelope header")
        kind = tokens[0].upper()
        fields: Dict[str, str] = {}
        for token in tokens[1:]:
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            if key in _FIELD_ORDER:
                fields[key] = value
        return cls(type=kind, **fields)


def format_message(envelope: Envelope, body: str = "") -> str:
    """Header line, blank line, body. The header is always line 1."""
    text = envelope.header()
    body = (body or "").strip()
    return f"{text}\n\n{body}" if body else text


def parse_message(text: str) -> Tuple[Envelope, str]:
    """Split an inbound message into (envelope, body).

    The envelope lives on the first line — but not necessarily at position 0, because protocol
    §3 addressing puts the mention or banner *before* it. The header is therefore located
    anywhere on line 1, and a malformed one degrades to the no-header case instead of raising.
    Without a header the message is treated as ``NOTE to=*`` — the body is returned untouched
    so nothing a peer wrote is ever lost to a parsing rule.
    """
    text = text or ""
    lines = text.splitlines()
    if lines:
        index = lines[0].find(PREFIX)
        if index >= 0:
            try:
                envelope = Envelope.parse_header(lines[0][index:].strip())
            except ValueError:
                envelope = None
            if envelope is not None:
                return envelope, "\n".join(lines[1:]).strip()
    return Envelope(type=MessageType.NOTE, to="*"), text.strip()


def has_explicit_envelope(text: str) -> bool:
    """Whether *text* carries a parseable protocol envelope on its first line.

    A bare prose message is also parsed as a best-effort ``NOTE`` for compatibility, but it
    may still contain an implicit question or request.  Only an explicit NOTE/STATUS/etc.
    marker is authoritative for suppressing a reply obligation.
    """
    lines = (text or "").splitlines()
    if not lines:
        return False
    index = lines[0].find(PREFIX)
    if index < 0:
        return False
    try:
        Envelope.parse_header(lines[0][index:].strip())
    except ValueError:
        return False
    return True

def ignored_envelope_marker(text: str) -> Optional[str]:
    """Return a reason when an A2A marker is present but unreachable by ``parse_message``.

    ``parse_message`` reads the header on line 1 only (tolerating banner/addressing before
    it on that same line). A header pushed below line 1 - or a malformed one - degrades the
    whole message to a broadcast ``NOTE``: machine intents (``CLOSED``, ``FYI``, ...) never
    fire, the message routes like prose, and it can open a reply ticket of its own. Seen
    live 2026-09-20: a closure preceded by its sender's turn narration no-oped exactly this
    way. Callers log the returned reason as a near-miss warning; ``None`` = nothing to warn.
    """
    lines = (text or "").splitlines()
    if not lines:
        return None
    first = lines[0]
    index = first.find(PREFIX)
    if index >= 0:
        try:
            Envelope.parse_header(first[index:].strip())
            return None  # a valid line-1 header governs the message
        except ValueError as exc:
            return f"malformed header on line 1: {exc}"
    for number, extra in enumerate(lines[1:], start=2):
        if _HEADER_SHAPE.match(extra.strip()):
            return (f"header-shaped marker on line {number} is ignored - "
                    "the envelope must be on line 1")
    return None


def is_within_budget(text: str, budget: int = CHANNEL_BUDGET_CHARS) -> bool:
    """True when *text* fits the channel budget, counted in UTF-16 units as Telegram does."""
    return utf16_length(text) <= budget


# -- addressing ---------------------------------------------------------------------------

def utf16_length(text: str) -> int:
    """Length in UTF-16 code units — what Telegram counts."""
    return len(text.encode("utf-16-le")) // 2


def addressed(text: str, username: str, alias: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Return ``(text, entities)`` for a message meant to reach *username*'s bot.

    Telegram resolves neither aliases nor plain-text usernames: a mention only exists as an
    entity covering real ``@username`` characters. So when the text addresses a peer by alias
    only, the handle is appended — visible, deliberate, and greppable — rather than inventing an
    entity over characters that do not mention anything (spec rule 4: alias in the body,
    username in the entity).
    """
    handle = "@" + username.lstrip("@")
    if not text:
        return text, []
    body = text
    # An entity can only cover characters that exist: the handle must be in the text before a
    # mention can be built over it, so an alias-addressed message gets it appended.
    if handle.lower() not in body.lower() and _addresses_alias(body, alias):
        body = f"{body.rstrip()} {handle}"
    return body, mention_entities(body, username)


def _addresses_alias(text: str, alias: Optional[str]) -> bool:
    if not alias:
        return False
    return re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE) is not None


def mention_entities(text: str, username: str,
                     command: Optional[str] = None) -> List[Dict[str, Any]]:
    """Build the entities that make *text* actually reach *username*'s bot.

    With ``command`` set, an existing ``/<command>@<username>`` is tagged ``bot_command`` —
    the form Telegram documents for reaching another bot. Otherwise every ``@username`` in the
    body is tagged ``mention``. Returns ``[]`` when the username does not appear: better to
    send nothing than to send a message that looks addressed and is not.
    """
    if not text or not username:
        return []
    handle = "@" + username.lstrip("@")

    if command:
        needle = f"/{command}{handle}"
        index = text.lower().find(needle.lower())
        if index >= 0:
            return [{"type": "bot_command",
                     "offset": utf16_length(text[:index]),
                     "length": utf16_length(text[index:index + len(needle)])}]

    entities: List[Dict[str, Any]] = []
    lowered = text.lower()
    needle = handle.lower()
    start = 0
    while True:
        index = lowered.find(needle, start)
        if index < 0:
            break
        end = index + len(needle)
        # boundary: @example_codex_bot must not match inside @example_codex_botsuffix
        if end >= len(text) or not (text[end].isalnum() or text[end] == "_"):
            entities.append({"type": "mention",
                             "offset": utf16_length(text[:index]),
                             "length": utf16_length(text[index:end])})
        start = end
    return entities


_MD_SPAN = re.compile(r"```(?:[A-Za-z0-9_+-]*\n)?(?P<pre>.+?)```|\*\*(?P<bold>[^*\n]+?)\*\*"
                      r"|`(?P<code>[^`\n]+)`", re.S)


def markdown_entities(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Agents write Markdown; Telegram shows it literally unless it is sent as entities.
    Return the text without the **bold**, `code` and ```fenced``` markers, plus the entities
    (UTF-16 offsets) that render them. Anything else is left as written."""
    out: List[str] = []
    entities: List[Dict[str, Any]] = []
    position = 0
    for match in _MD_SPAN.finditer(text):
        out.append(text[position:match.start()])
        kind = match.lastgroup or "code"
        inner = match.group(kind)
        offset = utf16_length("".join(out))
        out.append(inner)
        entities.append({"type": kind, "offset": offset, "length": utf16_length(inner)})
        position = match.end()
    out.append(text[position:])
    return "".join(out), entities


_AUTOLINK = re.compile(r"<((?:https?|mailto|tg):[^\s<>]+)>")


def strip_autolinks(text: str) -> str:
    """Markdown ``<https://...>`` autolinks as bare URLs: Telegram links a bare URL itself and
    would show the angle brackets literally."""
    return _AUTOLINK.sub(r"\1", text)


def markdown_html(escaped: str) -> str:
    """The same conversion for an already HTML-escaped message (the parse_mode=HTML path)."""
    def render(match: "re.Match[str]") -> str:
        kind = match.lastgroup or "code"
        tag = {"bold": "b", "code": "code", "pre": "pre"}[kind]
        return f"<{tag}>{match.group(kind)}</{tag}>"
    return _MD_SPAN.sub(render, escaped)
