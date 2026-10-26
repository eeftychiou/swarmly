"""The pinned dashboard message, and who is editing it (round 5 item 9, issue #117).

:mod:`swarmly.telegram.control` builds the *text*; this module owns the *message*: it finds
the one pinned dashboard, edits it in place on each beat, creates and pins it the first time,
and picks it up again when the coordinator changes. It talks to an injected bot-like object
(``send_message``, ``edit_message_text``, ``pin_chat_message``, ``get_chat``), so it is
testable without aiogram and without the network.

Three decisions shape it.

**The pin is the state, and a failover retires it rather than adopting it.** The message id
is not kept in a file: the new coordinator cannot read the old one's disk, and a stale file
would send a second coordinator to an old message. Telegram already remembers, so the
coordinator asks the chat for its pinned message and finds the previous dashboard by its
marker -- but a bot may edit only its **own** messages, so the new coordinator cannot take the
old one over: it **unpins** the predecessor's message (retiring its keyboard with it) and pins
its own. The pin is therefore still the state, and there is still exactly one live dashboard;
it is just a different message than the one it replaced.

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
from typing import Any, Awaitable, Callable, Dict, Optional

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


def _sender_username(message: Any) -> str:
    """The @username of whoever sent a pinned message, when Telegram tells us.

    This is what tells a bot's *own* dashboard (a restart: adopt it and keep editing) from the
    previous coordinator's (a failover: a bot may not edit another bot's message, so it must
    retire it). Empty when the field is absent, and then the edit is simply attempted and the
    refusal is caught.
    """
    sender = getattr(message, "from_user", None) or getattr(message, "from", None)
    return str(getattr(sender, "username", "") or "")


def _not_modified(exc: BaseException) -> bool:
    return "not modified" in str(exc).lower()


def _gone(exc: BaseException) -> bool:
    """Whether the message is really gone: the admin deleted it, or it is too old."""
    text = str(exc).lower()
    return "message to edit not found" in text or "message_id_invalid" in text


def _not_ours(exc: BaseException) -> bool:
    """Whether Telegram refused because the message belongs to *another* bot.

    "message can't be edited" is what a failover hits: the dashboard pinned by the previous
    coordinator is a message this bot may read and may not edit -- Telegram lets a bot edit
    only its own messages. Reading that as "gone" pins a second dashboard while the old pin
    and its buttons stay live, so it is its own outcome (review, #133).
    """
    return "message can't be edited" in str(exc).lower()


class ControlCenter:
    """One bot's hold on the dashboard message.

    ``bot`` is anything with the four aiogram methods; ``render`` is called with no arguments
    and returns the text to show, so a caller can pass a closure over its own state.
    """

    def __init__(self, bot: Any, *, chat_id: int, thread_id: Optional[int] = None,
                 render: Callable[[], str], markup: Optional[Callable[[], Any]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 me_username: str = "",
                 cards: Optional[Callable[[], Optional[Dict[str, int]]]] = None,
                 record: Optional[Callable[[int], Awaitable[Any]]] = None,
                 me_alias: str = ""):
        self.bot = bot
        #: This bot's own @username, so a pin it did not send is recognised as a failover's.
        self.me_username = str(me_username or "").lstrip("@").strip()
        self.chat_id = int(chat_id)
        self.thread_id = thread_id
        self._render = render
        self._markup = markup
        self._clock = clock
        #: Every bot's dashboard message id as the state store records it (alias -> id), or
        #: None when there is no store: then the chat's pin is asked, as before. The snapshot
        #: is always the newest pin, so asking the pin never found the card again and every
        #: restart pinned a new one (live, 26 Oct: 43 pins).
        self._cards = cards
        self._record = record
        self.me_alias = str(me_alias or "").strip().lower()
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
            if self.message_id is None and self._cards is not None:
                known = self._cards()
                if known is None:                      # the store is not read yet: wait
                    self.status = UNCHANGED
                    return Refreshed(UNCHANGED, None, "the state store is not ready")
                for alias, mid in sorted(known.items()):
                    if alias != self.me_alias and mid:
                        await self._unpin(int(mid))    # a previous coordinator's card
                mine = known.get(self.me_alias)
                if mine:
                    self.message_id = int(mine)
                    return await self._edit(text, taken=True)
                return await self._create(text)
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
        author = _sender_username(pinned)
        if self.me_username and not author:
            # Telegram did not say who sent the pin, so nothing here can tell our own dashboard
            # (a restart: adopt it) from a predecessor's (a failover: retire it). The edit is
            # attempted and a refusal retires it -- and this line is what makes a deployment
            # whose Telegram @username differs from ``identity.name`` visible, because it lands
            # here on every restart instead of adopting its own message (review, #133).
            logger.info("control center: the pinned dashboard's author is not reported by "
                        "Telegram, and this bot is @%s; attempting to edit it, and retiring the "
                        "pin if Telegram refuses", self.me_username)
        if author and self.me_username and author.lstrip("@").lower() != self.me_username.lower():
            # A failover: this is the previous coordinator's dashboard. A bot may edit only its
            # own messages, so retire that pin (its keyboard with it) and pin our own.
            logger.info("control center: the pinned dashboard belongs to @%s and this bot is "
                        "@%s, and a bot may edit only its own messages, so the pin is treated as "
                        "a previous coordinator's: retiring it and pinning our own",
                        author, self.me_username)
            await self._unpin(getattr(pinned, "message_id", None))
            return None
        self.message_id = getattr(pinned, "message_id", None)
        if self.message_id is None:
            return None
        if str(getattr(pinned, "text", "") or "") == text:
            self.last_text = text
            self.status = UNCHANGED
            return Refreshed(TAKEN, self.message_id)
        result = await self._edit(text, taken=True)
        return result

    def reply_markup(self) -> Any:
        """The buttons, as a markup object when aiogram is importable, else the bare dict."""
        rows = self._markup() if self._markup is not None else None
        if not rows:
            return None
        try:
            from aiogram.types import InlineKeyboardMarkup
        except Exception:                      # noqa: BLE001 - no aiogram: send the plain shape
            return {"inline_keyboard": rows}
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _create(self, text: str) -> Refreshed:
        sent = await self.bot.send_message(chat_id=self.chat_id, text=text,
                                           message_thread_id=self.thread_id,
                                           disable_web_page_preview=True,
                                           reply_markup=self.reply_markup())
        message_id = getattr(sent, "message_id", None)
        self.message_id = message_id
        self.last_text = text
        await self._pin(message_id)
        if self._record is not None and message_id is not None:
            try:
                await self._record(int(message_id))
            except Exception:                          # noqa: BLE001 - kept in memory anyway
                logger.warning("control center: the card's id was not recorded", exc_info=True)
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

    async def _unpin(self, message_id: Optional[int]) -> None:
        """Retire a message this bot cannot edit: the keyboard under it must go too.

        Best effort on purpose -- a bot that is not a group admin may not unpin at all, and a
        dashboard that fails to retire its predecessor must still be pinned itself. The press
        path is the second line of defence: a control pressed on a bot that no longer
        coordinates is refused, so a surviving stale pin answers nothing.
        """
        if message_id is None:
            return
        unpin = getattr(self.bot, "unpin_chat_message", None)
        if unpin is None:
            return
        try:
            await unpin(chat_id=self.chat_id, message_id=message_id)
            logger.info("control center: retired the previous dashboard (%s)", message_id)
        except Exception as exc:                       # noqa: BLE001 - the press check covers it
            logger.warning("control center: could not retire the previous dashboard: %s", exc)

    async def _edit(self, text: str, *, taken: bool = False) -> Refreshed:
        assert self.message_id is not None
        try:
            await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id,
                                             text=text, disable_web_page_preview=True,
                                             reply_markup=self.reply_markup())
        except Exception as exc:                       # noqa: BLE001 - see the branches
            if _not_modified(exc):
                self.last_text = text
                self.status = UNCHANGED
                return Refreshed(UNCHANGED, self.message_id, str(exc))
            if _not_ours(exc):
                # A failover: the dashboard on the pin is the previous coordinator's, and this
                # bot may not edit it. Retire it -- its buttons would otherwise keep answering
                # presses on a bot that no longer coordinates -- and pin our own.
                logger.info("control center: the pinned dashboard is another bot's (%s); "
                            "retiring it and pinning this bot's", exc)
                stale, self.message_id = self.message_id, None
                await self._unpin(stale)
                return await self._create(text)
            if _gone(exc):
                # The admin deleted it, or it is too old to edit: send a fresh one and pin it.
                logger.info("control center: the pinned message is gone (%s); recreating", exc)
                self.message_id = None
                return await self._create(text)
            raise
        self.last_text = text
        self.status = TAKEN if taken else EDITED
        return Refreshed(self.status, self.message_id)
