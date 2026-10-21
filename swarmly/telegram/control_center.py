"""The pinned dashboard message, and who is editing it (round 5 item 9, issue #117).

:mod:`swarmly.telegram.control` builds the *text*; this module owns the *message*: it finds
the one pinned dashboard, edits it in place on each beat, creates and pins it the first time,
and picks it up again when the coordinator changes. It talks to an injected bot-like object
(``send_message``, ``edit_message_text``, ``pin_chat_message``, ``get_chat``), so it is
testable without aiogram and without the network.

Three decisions shape it.

**The pin is the state.** The message id is not kept in a file: the new coordinator cannot
read the old one's disk, and a stale file would send a second coordinator to an old message.
Telegram already remembers, so the coordinator asks the chat for its pinned message and adopts
it when it is one of ours -- that is what makes a failover take over *the same* message
instead of pinning a second one.

**A refresh never breaks the beat.** Every failure -- no right to pin, a message deleted by
the admin, a flood wait -- is a returned status and a log line, never an exception: the
heartbeat that carries the dashboard also carries this bot's own liveness (#82), and a
dashboard must not be able to stop it.

**Nothing is sent when nothing changed.** Editing a message to the same text is an error in
the Telegram API and a wasted round trip; the last text is remembered and an identical
refresh is a no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from swarmly.telegram.control import MARKER

logger = logging.getLogger("swarmly.telegram.control_center")

#: What a refresh did. ``taken`` is the failover case: an existing pin was adopted.
PINNED = "pinned"
CREATED = "created"
EDITED = "edited"
TAKEN = "taken"
UNCHANGED = "unchanged"
FAILED = "failed"


@dataclass
class Refreshed:
    """The outcome of one refresh, for the log and for a test."""

    status: str
    message_id: Optional[int] = None
    detail: str = ""


def _is_ours(message: Any) -> bool:
    """Whether a chat's pinned message is a dashboard: it starts with the marker."""
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return str(text).startswith(MARKER)


def _pinned_of(chat: Any) -> Any:
    """The pinned message of a ``get_chat`` result, across aiogram versions."""
    return getattr(chat, "pinned_message", None)


def _thread_of(message: Any) -> Optional[int]:
    return getattr(message, "message_thread_id", None)


def _not_modified(exc: BaseException) -> bool:
    return "not modified" in str(exc).lower()


def _gone(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "message to edit not found" in text or "message_id_invalid" in text \
        or "message can't be edited" in text


class ControlCenter:
    """One bot's hold on the dashboard message.

    ``bot`` is anything with the four aiogram methods; ``render`` is called with no arguments
    and returns the text to show, so a caller can pass a closure over its own state.
    """

    def __init__(self, bot: Any, *, chat_id: int, thread_id: Optional[int] = None,
                 render: Callable[[], str], clock: Optional[Callable[[], float]] = None):
        self.bot = bot
        self.chat_id = int(chat_id)
        self.thread_id = thread_id
        self._render = render
        self._clock = clock
        self.message_id: Optional[int] = None
        self.last_text: Optional[str] = None
        #: The last status a refresh returned, for /diag and for a test.
        self.status: str = ""

    # -- the message -------------------------------------------------------------------
    async def refresh(self, text: Optional[str] = None) -> Refreshed:
        """Bring the pinned message up to date. Never raises: every failure is a status.

        *text* is the dashboard as the caller built it -- off the event loop, because building
        it reads the bus. Without it, ``render`` is called here.
        """
        text = self._render() if text is None else text
        if text == self.last_text and self.message_id is not None:
            self.status = UNCHANGED
            return Refreshed(UNCHANGED, self.message_id)
        try:
            if self.message_id is None:
                adopted = await self._adopt(text)
                if adopted is not None:
                    return adopted
            if self.message_id is None:
                return await self._create(text)
            return await self._edit(text)
        except Exception as exc:                       # noqa: BLE001 - a beat must not die
            self.status = FAILED
            logger.warning("control center: refresh failed: %s", exc)
            return Refreshed(FAILED, self.message_id, str(exc))

    async def _adopt(self, text: str) -> Optional[Refreshed]:
        """Take over the chat's pinned dashboard, if the one there is ours (failover)."""
        get_chat = getattr(self.bot, "get_chat", None)
        if get_chat is None:
            return None
        try:
            chat = await get_chat(self.chat_id)
        except Exception as exc:                       # noqa: BLE001 - fall back to creating
            logger.info("control center: cannot read the chat's pin (%s); will create", exc)
            return None
        pinned = _pinned_of(chat)
        if pinned is None or not _is_ours(pinned):
            return None
        if self.thread_id is not None and _thread_of(pinned) not in (None, self.thread_id):
            return None                                # another topic's dashboard: not ours
        self.message_id = getattr(pinned, "message_id", None)
        if self.message_id is None:
            return None
        if str(getattr(pinned, "text", "") or "") == text:
            self.last_text = text
            self.status = UNCHANGED
            return Refreshed(TAKEN, self.message_id)
        result = await self._edit(text, taken=True)
        return result

    async def _create(self, text: str) -> Refreshed:
        sent = await self.bot.send_message(chat_id=self.chat_id, text=text,
                                           message_thread_id=self.thread_id,
                                           disable_web_page_preview=True)
        message_id = getattr(sent, "message_id", None)
        self.message_id = message_id
        self.last_text = text
        await self._pin(message_id)
        self.status = CREATED
        return Refreshed(CREATED, message_id)

    async def _pin(self, message_id: Optional[int]) -> None:
        if message_id is None:
            return
        pin = getattr(self.bot, "pin_chat_message", None)
        if pin is None:
            return
        try:
            # A pin is per chat: Telegram pins the message where it lives, so the topic does
            # not need naming here.
            await pin(chat_id=self.chat_id, message_id=message_id, disable_notification=True)
        except Exception as exc:                       # noqa: BLE001 - an unpinned dashboard
            logger.warning("control center: the dashboard did not pin: %s", exc)

    async def _edit(self, text: str, *, taken: bool = False) -> Refreshed:
        assert self.message_id is not None
        try:
            await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id,
                                             text=text, disable_web_page_preview=True)
        except Exception as exc:                       # noqa: BLE001 - see the branches
            if _not_modified(exc):
                self.last_text = text
                self.status = UNCHANGED
                return Refreshed(UNCHANGED, self.message_id, str(exc))
            if _gone(exc):
                # The admin deleted it, or it is too old to edit: send a fresh one and pin it.
                logger.info("control center: the pinned message is gone (%s); recreating", exc)
                self.message_id = None
                return await self._create(text)
            raise
        self.last_text = text
        self.status = TAKEN if taken else EDITED
        return Refreshed(self.status, self.message_id)
