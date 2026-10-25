"""Durable first, then instant: post an envelope to the bus and point Telegram at it.

The durable transports (git, folder, gdrive) are the record but are polled, so a peer may
not see a message for minutes. Telegram is instant but lossy and is never the record. This
module combines them the way the protocol prescribes (AGENTS.md §3, PROTOCOL.md rule 8):
the envelope lands on the durable plane first, then one short Telegram message carrying a
``bus=`` pointer wakes the recipient now. If Telegram is down, the poller still delivers
the durable copy on its next cycle; the seen-ledger makes sure it is handled once.
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from swarmly.telegram.wire import Envelope, MessageType, format_message

logger = logging.getLogger("swarmly.telegram.relay")


class NoBusError(RuntimeError):
    """The agent has no ``bus.repo``, so there is nowhere to post (review P0-4)."""


#: Telegram carries a pointer and a preview, never the whole payload.
PREVIEW_CHARS = 280


@dataclass
class RelayResult:
    message_id: str
    location: str
    bus_pointer: str
    telegram: str = "skipped"          # sent | skipped | failed | suppressed
    telegram_message_id: Optional[int] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def bus_pointer(transport: Any, location: str, filename: str) -> str:
    """The ``bus=`` value a receiver can verify: ``<path>@<sha>`` for gitbus (rule 8),
    the transport location otherwise (``folder:<name>``, ``gdrive:<folder>/<name>``)."""
    from swarmly.transports import GitTransport
    if isinstance(transport, GitTransport):
        sha = ""
        try:
            sha = subprocess.run(["git", "-C", str(transport.root), "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=10,
                                 check=True).stdout.strip()
        except Exception:  # noqa: BLE001 - pointer without a sha is still a pointer
            logger.warning("relay: could not resolve HEAD for the bus pointer")
        path = f".agents/inbox/{filename}"
        return f"{path}@{sha}" if sha else path
    if location.startswith("gdrive:"):
        return location
    return f"{transport.kind}:{filename}"


async def post_and_notify(channel: Any, *, to: str, text: str, intent: str = "STATUS_UPDATE",
                          telegram_type: Optional[str] = None, notify: bool = True,
                          root: Optional[Path] = None, transport: Any = None,
                          extra: Optional[Dict[str, Any]] = None) -> RelayResult:
    """Write one signed envelope to the durable bus, then send a Telegram pointer to it.

    *to* is a peer alias (or ``*``). *telegram_type* defaults to ``REQUEST`` for a named
    peer and ``NOTE`` for a broadcast. A durable write failure raises (nothing was
    sent); a Telegram failure is reported in the result, because the durable copy will
    still be delivered by the poller.
    """
    from swarmly.bus import DurableBus
    from swarmly.telegram.redact import Redactor
    from swarmly.models import (A2AMessage, MessageIntent, MessagePriority, RecipientInfo,
                            SenderInfo)

    cfg = channel.config
    if not (root or cfg.bus.repo):
        raise NoBusError("no bus.repo configured: there is no durable bus to write to")
    root = Path(root or cfg.bus.repo).expanduser()
    transport = transport or channel.bus_transport(root)
    alias = cfg.identity.alias
    recipient = (to or "*").lstrip("@")
    bus = DurableBus(root_dir=root, transport=transport)

    message = A2AMessage(
        protocol="a2a/v1.0",
        message_id=f"msg-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:16]}",
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        sender=SenderInfo(agent_id=alias.lower(), model_family="custom", framework="custom"),
        recipient=RecipientInfo(agent_id=recipient.lower()),
        intent=MessageIntent(intent), priority=MessagePriority.ROUTINE,
        signature="",
        # Issue #22: one choke point for everything that leaves the host -- the maintainer's
        # handle, chat ids and absolute paths are replaced here, so a new sender cannot
        # forget it. Raw values stay in local config and state.
        payload=Redactor.from_config(cfg).value({"text": text, **(extra or {})}))
    key, key_type = bus.signing_key(alias.lower())     # MissingKeyError: nothing is sent
    path = bus.post_message(message, private_key=key, key_type=key_type, git_push=True)
    location = str(path)
    filename = Path(location.replace("gdrive:", "", 1)).name
    result = RelayResult(message_id=message.message_id, location=location,
                         bus_pointer=bus_pointer(transport, location, filename))
    if not notify:
        return result
    barred = getattr(channel, "token_barred", None)
    place = barred(None, None) if callable(barred) else None
    if isinstance(place, str):
        # The pointer goes to General, which is transport-only: no bot takes a turn there, so
        # it would wake nobody and only put an agent's words in front of people. The poller
        # delivers the durable copy (admin, 25 Oct: no token space posts in General).
        result.error = f"not pointed at from Telegram: {place} is transport-only"
        logger.info("relay: %s is transport-only; %s is delivered by the poller", place,
                    message.message_id)
        return result

    kind = (telegram_type or (MessageType.NOTE if recipient == "*" else MessageType.REQUEST))
    envelope = Envelope(type=kind, to=recipient, intent=intent, bus=result.bus_pointer,
                        id=message.message_id[-8:])
    preview = text.strip()
    if len(preview) > PREVIEW_CHARS:
        preview = preview[:PREVIEW_CHARS - 1].rstrip() + "…"
    username = None
    for peer_alias, peer_username in (cfg.peers or {}).items():
        if peer_alias.lower() == recipient.lower():
            username = str(peer_username).lstrip("@")
    body = f"@{username} {preview}" if username else preview
    try:
        sent = await channel.send(format_message(envelope, body), return_message_id=True)
    except Exception as exc:  # noqa: BLE001 - the durable copy still stands
        result.telegram, result.error = "failed", str(exc)[:200]
        logger.warning("relay: Telegram pointer failed; the poller will deliver %s",
                       message.message_id)
        return result
    from swarmly.telegram.channel import _SEND_SUPPRESSED
    if sent is _SEND_SUPPRESSED:
        result.telegram = "suppressed"      # transport frozen: the poller still delivers it
    else:
        result.telegram = "sent"
        result.telegram_message_id = sent if isinstance(sent, int) else None
    return result
