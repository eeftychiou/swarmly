"""A button press: who may make it, what it does, and what is logged (issue #117).

The dashboard's buttons are worth nothing without these three answers, and each of them is
easy to get subtly wrong, so they live here as one small policy rather than inside the
aiogram layer.

**Admin only.** A callback arrives with the Telegram user id of whoever pressed it, and the
check is the first thing that happens: an unknown presser is refused *before* the action is
even looked up, so a bug in the table cannot be reached by someone who is not the admin.

**Anything that stops or changes something asks twice.** A first tap on such a control mints
a one-use code and says what the second tap will do; only ``confirm <code>`` runs it, and a
code dies after :data:`CONFIRM_SECONDS` or when it is used. This is the same shape as
``/cleanup`` (#22), because a stop button that works on one tap is a stop button that works
by accident.

**Every action is logged with who, what and when.** The decision carries the line; the
transport puts it on the durable plane. A control that leaves no record is a control nobody
can be held to.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from swarmly.telegram.control import control as lookup, is_picker

#: The callback-data prefix. Every button this module handles starts with it, so a callback
#: from anything else on the bot's keyboard is ignored rather than parsed.
PREFIX = "ctl:"

#: How long a confirmation code stands. Short: a stop button should not wait all day for a
#: second tap, and a stale code should not be able to act on a swarm that has moved on.
CONFIRM_SECONDS = 120.0

#: The verdicts a decision can carry.
RUN = "run"
CONFIRM = "confirm"
REFUSED = "refused"

#: Why a press was refused. Named so a test and a log can tell them apart.
NOT_ADMIN = "not_admin"
UNKNOWN = "unknown_action"
EXPIRED = "expired_code"
NOT_OURS = "not_a_control"
TARGET = "unknown_target"


@dataclass(frozen=True)
class Press:
    """One button press, as the transport sees it."""

    user_id: int
    data: str
    #: When it was pressed (wall clock), for the code's window and the log line.
    at: float = 0.0


@dataclass(frozen=True)
class Note:
    """A ``CONTROL`` line off the bus: another bot passing on the admin's press."""

    action: str
    target: str = ""
    by: str = ""


def parse_note(text: str) -> Optional[Note]:
    """``CONTROL <action> target=<t> by=<id> at=<utc> state=done`` -> a :class:`Note`.

    None for anything that is not one of these lines, so a peer's prose can never be read as
    a command: the shape is fixed, and a missing field means the note is not ours.
    """
    if not isinstance(text, str):
        return None
    words = text.strip().split()
    if len(words) < 2 or words[0] != "CONTROL":
        return None
    fields = {}
    for word in words[2:]:
        key, _, value = word.partition("=")
        fields[key] = value
    if "by" not in fields or "target" not in fields:
        return None
    return Note(action=words[1], target=fields["target"], by=fields["by"])


#: Telegram's limit on one button's callback data, in bytes. A picker carries a value in that
#: budget: ``ctl`` (3) + the action (17 at most today) + a target + a value must fit.
MAX_CALLBACK_BYTES = 64

#: The most a picked value may be, so a long model name cannot push the callback over the
#: limit and make Telegram reject the whole keyboard.
MAX_VALUE_CHARS = 32


@dataclass(frozen=True)
class Command:
    """What a button's callback data says: the action, its target, a picked value, a code."""

    action: str
    target: str = ""
    code: str = ""
    #: The value a picker chose -- a model, an effort, a timer id, ``<alias>:<role>``. Empty
    #: for the buttons that need no value, so every existing callback stays valid (#153).
    value: str = ""


def parse(data: str) -> Optional[Command]:
    """``ctl:<action>[:<target>[:<value>]]`` or ``ctl:confirm:<code>``.

    The value is the third field, and it is always last, so the two-field form the dashboard
    already sends keeps its meaning (#153). None when the data is not ours.
    """
    if not isinstance(data, str) or not data.startswith(PREFIX):
        return None
    body = data[len(PREFIX):]
    if not body:
        return None
    head, _, rest = body.partition(":")
    head = head.strip()
    if head == "confirm":
        return Command(action="confirm", code=rest.strip())
    if not head:
        return None
    target, _, value = rest.partition(":")
    return Command(action=head, target=target.strip(), value=value.strip())


def too_long(data: str) -> bool:
    """Whether a callback data would be refused by Telegram (over :data:`MAX_CALLBACK_BYTES`)."""
    return len(str(data).encode("utf-8")) > MAX_CALLBACK_BYTES


def every_bot(action: str, actor: str, moment: Optional[float] = None) -> str:
    """A CONTROL line for the whole swarm: ``target=*``, so every bot applies it.

    It keeps the *presser's* Telegram id in ``by``: the press was authorised where it landed,
    and each target repeats that check against its own admin, so a broadcast is exactly as
    trusted as a press, and no bot acts on a line that names nobody.
    """
    return PressPolicy.log_line(action, "*", actor,
                                time.time() if moment is None else moment, done=True)


def callback_data(action: str, target: str = "", value: str = "") -> str:
    """The callback data for one control, so the keyboard and the parser cannot drift.

    A value is written only when there is one, and the target is written when either the
    target or the value is set (``ctl:bot.model::claude-sonnet-4``), so a plain button keeps
    the exact data it had before pickers existed (#153).
    """
    if value:
        return f"{PREFIX}{action}:{target}:{value}"
    return f"{PREFIX}{action}" + (f":{target}" if target else "")


@dataclass
class Decision:
    """What to do with a press: run it, ask again, or refuse it — with the words to answer."""

    verdict: str
    command: Optional[Command] = None
    reason: str = ""
    text: str = ""
    #: The action that will run, also when the verdict is ``CONFIRM``.
    action: str = ""
    target: str = ""
    #: The line to put on the durable plane when something actually happens.
    log: str = ""
    #: The value the press picked (a model, an effort, a timer): empty for plain controls.
    value: str = ""


@dataclass
class _Pending:
    action: str
    target: str
    code: str
    expires: float
    actor: str = ""
    #: The value the press picked, carried through the second tap so the confirmed action runs
    #: with exactly what the first tap chose (#153).
    value: str = ""


@dataclass
class PressPolicy:
    """The admin check, the second tap, and the log line: one object, one clock."""

    admin_id: int
    clock: Callable[[], float] = time.time
    confirm_seconds: float = CONFIRM_SECONDS
    code: Callable[[], str] = lambda: secrets.token_hex(2)
    #: The controls this build can actually carry out. Anything else is refused as unknown,
    #: so a button is never shown (or run) for an action the transport cannot perform.
    available: Tuple[str, ...] = ()
    _pending: Dict[str, _Pending] = field(default_factory=dict)

    # -- the policy --------------------------------------------------------------------
    def decide(self, press: Press, *, actor: str = "") -> Decision:
        """What this press does. Never raises: every path answers something."""
        command = parse(press.data)
        if command is None:
            return Decision(REFUSED, reason=NOT_OURS, text="That is not a control.")
        moment = press.at or self.clock()
        actor = actor or str(press.user_id)
        # Admin first: an unauthorised presser must not learn whether an action exists.
        if not self._is_admin(press.user_id):
            return Decision(REFUSED, command=command, reason=NOT_ADMIN,
                            text="Only the admin can use the controls.",
                            action=command.action)
        if command.action == "confirm":
            return self._confirm(command, moment, press)
        found = lookup(command.action)
        if found is None or (self.available and command.action not in self.available):
            return Decision(REFUSED, command=command, reason=UNKNOWN,
                            text=f"There is no control {command.action!r} here.",
                            action=command.action)
        if command.value:
            # A picked value *changes* something (a model, an effort, a role), so it always
            # asks for the second tap, whatever the bare control's own rule is (#153).
            code = self._mint(command, moment, actor)
            return Decision(CONFIRM, command=command, action=command.action,
                            target=command.target, value=command.value,
                            text=(f"Tap again to set {found.label} to `{command.value}`. "
                                  f"Or answer `{PREFIX}confirm:{code}` within "
                                  f"{self.confirm_seconds / 60:g} minute(s)."))
        if not command.value and is_picker(command.action):
            # Step one of a pick -- offering the list -- changes nothing, so it is not
            # confirmed even when the action itself is one that changes something (#153).
            return Decision(RUN, command=command, action=command.action,
                            target=command.target, text=f"{found.label}: pick one below.")
        if not found.confirm:
            # A report changes nothing, so it leaves no CONTROL note: the durable line is for
            # "who stopped what", and a read-only press would only add noise to the bus.
            log = "" if found.report else self.log_line(command.action, command.target,
                                                        actor, moment, done=True)
            return Decision(RUN, command=command, action=command.action,
                            target=command.target, text=f"{found.label} done.", log=log)
        code = self._mint(command, moment, actor)
        note = f" — {found.note}" if found.note else ""
        return Decision(CONFIRM, command=command, action=command.action,
                        target=command.target,
                        text=(f"Tap again to confirm {found.label}{note}. "
                              f"Or answer `{PREFIX}confirm:{code}` within "
                              f"{self.confirm_seconds / 60:g} minute(s)."))

    def _confirm(self, command: Command, moment: float, press: Press) -> Decision:
        pending = self._pending.pop(str(command.code).lower(), None)
        if pending is None:
            return Decision(REFUSED, command=command, reason=EXPIRED,
                            text="That confirmation expired or was already used.")
        if pending.expires < moment:
            return Decision(REFUSED, command=command, reason=EXPIRED,
                            text="That confirmation expired before it was used.")
        if pending.actor and str(press.user_id) != pending.actor:
            return Decision(REFUSED, command=command, reason=NOT_ADMIN,
                            text="Only the admin who asked can confirm that.")
        found = lookup(pending.action)
        label = found.label if found else pending.action
        said = (f"{label} set to {pending.value}." if pending.value else f"{label} done.")
        return Decision(RUN, command=command, action=pending.action, target=pending.target,
                        value=pending.value, text=said,
                        log=self.log_line(pending.action, pending.target, pending.actor,
                                          moment, done=True))

    # -- pieces ------------------------------------------------------------------------
    def _is_admin(self, user_id: Any) -> bool:
        try:
            return int(user_id) == int(self.admin_id)
        except (TypeError, ValueError):
            return False

    def _mint(self, command: Command, moment: float, actor: str) -> str:
        self._expire(moment)
        code = self.code().lower()
        self._pending[code] = _Pending(action=command.action, target=command.target, code=code,
                                       expires=moment + self.confirm_seconds,
                                       actor=str(actor or ""), value=command.value)
        return code

    def _expire(self, moment: float) -> None:
        for code, pending in list(self._pending.items()):
            if pending.expires < moment:
                self._pending.pop(code, None)

    def pending(self) -> List[_Pending]:
        return list(self._pending.values())

    @staticmethod
    def log_line(action: str, target: str, actor: str, moment: float, *,
                 done: bool = False) -> str:
        """``CONTROL bot.pause target=Codex by=4242 at=2026-10-21T20:00:00Z``.

        Who, what and when, on one line: the transport posts it as a NOTE, so it is on the
        durable plane and every peer can see who stopped what.
        """
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
        state = "done" if done else "asked"
        return (f"CONTROL {action} target={target or 'self'} by={actor or 'unknown'} "
                f"at={when} state={state}")
