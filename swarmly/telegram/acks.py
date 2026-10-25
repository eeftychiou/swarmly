"""
Acknowledgement tracking for the A2A Telegram channel protocol v1.1 (spec §5).

Chat has no delivery receipt: a sender cannot tell whether its message arrived, and a silent
peer is indistinguishable from a dead transport. On 2026-09-17 that ambiguity cost an hour of
guessing. The fix is explicit and cheap — every ``REQUEST`` is acknowledged, and a request with
no ACK by its deadline escalates on a fixed ladder rather than being retried silently.

The tracker is pure logic with an injected clock: the tests drive time instead of sleeping.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from swarmly.telegram.wire import MessageType

#: Types that expect an acknowledgement. Everything else is informational (spec §5).
EXPECTS_ACK = (MessageType.REQUEST,)
#: Types that are themselves acknowledgements — never tracked, never ACKed (rule: never ACK an ACK).
IS_ACKNOWLEDGEMENT = (MessageType.ACK,)

DEFAULT_TTL_SECONDS = 1800
MAX_RUNG = 3

_TTL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhSMH]?)\s*$")


def parse_ttl(value: Optional[str]) -> Optional[float]:
    """Parse an envelope ``ttl`` ('30m', '90s', '2h', or bare seconds) into seconds.

    Returns ``None`` for absent or unparseable values so the caller falls back to its
    configured default rather than inventing a deadline.
    """
    if value is None:
        return None
    match = _TTL_RE.match(str(value))
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return amount * {"s": 1.0, "m": 60.0, "h": 3600.0}[unit]


@dataclass(frozen=True)
class Escalation:
    """What to do about an unacknowledged message, and what to say while doing it."""

    msg_id: str
    to: str
    rung: int
    action: str
    announcement: str


_RUNG_ACTIONS = {
    1: "re-send with a command mention (/ping@<peer>)",
    2: "mirror to the durable bus and re-send carrying bus=path@commit",
    3: "escalate to the admin with a one-line status",
}


@dataclass
class _Outstanding:
    msg_id: str
    to: str
    sent_at: float
    ttl_seconds: float
    acked: bool = False


class AckTracker:
    """Tracks which of our messages are waiting on an acknowledgement.

    Rung is derived from how many whole TTLs have elapsed (``elapsed // ttl``, capped at 3), so
    one tracker covers the whole ladder without per-rung bookkeeping.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None,
                 default_ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._clock = clock or time.monotonic
        self._default_ttl = float(default_ttl_seconds)
        self._outstanding: Dict[str, _Outstanding] = {}

    def expect(self, msg_id: str, to: str, ttl_seconds: Optional[float] = None,
               type: str = MessageType.REQUEST) -> bool:
        """Register *msg_id* as awaiting an ACK. Returns False when the type expects none.

        An ``ACK`` never registers: acknowledging an acknowledgement would let two agents keep
        each other busy forever without exchanging information.
        """
        if type in IS_ACKNOWLEDGEMENT or type not in EXPECTS_ACK:
            self._outstanding.pop(msg_id, None)
            return False
        self._outstanding[msg_id] = _Outstanding(
            msg_id=msg_id, to=to, sent_at=self._clock(),
            ttl_seconds=float(ttl_seconds) if ttl_seconds else self._default_ttl)
        return True

    def ack(self, msg_id: str) -> bool:
        """Mark *msg_id* acknowledged. Unknown ids are ignored, not errors (duplicates happen)."""
        record = self._outstanding.get(msg_id)
        if record is None:
            return False
        record.acked = True
        return True

    def pending(self, now: Optional[float] = None) -> List[str]:
        """Unacknowledged and still within its deadline."""
        moment = now if now is not None else self._clock()
        return [r.msg_id for r in self._outstanding.values()
                if not r.acked and (moment - r.sent_at) < r.ttl_seconds]

    def escalations(self, now: Optional[float] = None) -> List[Escalation]:
        """Unacknowledged past their deadline, as ladder rungs (1..3)."""
        moment = now if now is not None else self._clock()
        out: List[Escalation] = []
        for record in self._outstanding.values():
            if record.acked:
                continue
            elapsed = moment - record.sent_at
            if elapsed < record.ttl_seconds:
                continue
            rung = min(MAX_RUNG, int(elapsed // record.ttl_seconds))
            rung = max(1, rung)
            out.append(Escalation(
                msg_id=record.msg_id, to=record.to, rung=rung,
                action=_RUNG_ACTIONS[rung],
                announcement=(f"no ACK for {record.msg_id} from {record.to} "
                              f"after {int(elapsed)}s — rung {rung}: {_RUNG_ACTIONS[rung]}")))
        return out

    def for_message(self, msg_id: str) -> Optional[Escalation]:
        for escalation in self.escalations():
            if escalation.msg_id == msg_id:
                return escalation
        return None
