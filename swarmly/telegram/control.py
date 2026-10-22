"""The command centre's pinned dashboard -- the text (round 5 item 9, issue #117).

The coordinator's transport keeps one message pinned in a control topic and edits it in
place on every heartbeat. This module is the *text* half of that: alerts first, a swarm
header, the round board, and one line per bot. It is built from what the transport already
holds -- the #124 snapshots, the #125 ledger, the #86 round state -- so rendering it calls
no adapter and no network, and never costs a model turn.

Three rules decide the shape.

**Alerts first, and the alerts are what survives.** The message is read at a glance, in a
chat, when something has gone wrong: a bot that stopped beating, a turn over its limit, a
hand-off nobody answered, a quota that ran out. Those lines come first, and when the
message runs out of room it is the *detail* that is dropped, never an alert.

**A dashboard is a status, not a leak.** Every field here is picked by name, so a record
carrying a chat id, a thread id, a token or a path shows nothing extra: a topic appears as
its discussion token (``tg-307``) and nothing else. The one 4096-character limit is
Telegram's, and it shapes the layout rather than being discovered in production.

**Unknown stays unknown.** A number the runtime did not report is ``unknown`` here too, and
a total says how many of its rows reported one -- the same rule the ``/usage`` ledger keeps
(#125), because a dashboard that silently reads a missing number as zero is worse than one
that admits it does not know.

The buttons under the message are :data:`CONTROLS`: a plain table of what the transport may
offer, whether the action needs a second tap (anything that stops or changes something),
and the one control that costs a turn. Wiring them to callbacks, and checking that the
presser is the admin, is the transport's job -- the table is here so a test can pin it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from swarmly.telegram.snapshot import (BUSY, DOWN, FROZEN, IDLE, PAUSED, STUCK, Snapshot)

#: Telegram's hard limit on one message. The dashboard is edited in place, so it has to fit.
MAX_CHARS = 4096

#: How many bot lines the compact view keeps before it says how many it dropped.
COMPACT_BOTS = 12

#: The first word of the message. A pinned message is recognised as the dashboard by this
#: marker alone (#117): change it and every pin on every host is orphaned.
MARKER = "SWARM"

#: Section order in the rendered message: an alert outranks a bot line, which outranks the
#: round board. `_fill` walks this order and stops when the next section would not fit.
_ORDER = ("alerts", "bots", "board")


# ------------------------------------------------------------------------------------
# The controls under the message
# ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Control:
    """One button the transport may offer. ``confirm`` means a second tap before it runs."""

    action: str
    label: str
    confirm: bool = False
    #: One short line saying what the press costs or does, shown before a confirm.
    note: str = ""
    #: A control that only *reports*: it changes nothing, so it is never confirmed and never
    #: leaves a CONTROL note (there is no "who stopped what" in reading a setting).
    report: bool = False


CONTROLS: Tuple[Control, ...] = (
    # per bot
    Control("bot.pause", "Pause"),
    Control("bot.resume", "Resume"),
    Control("bot.stop_turn", "Stop turn", confirm=True, note="keeps the message for replay"),
    Control("bot.restart", "Restart", confirm=True,
            note="waits for the queue to empty and no reply to be unsent"),
    Control("bot.model", "Model", report=True),
    Control("bot.effort", "Effort", report=True),
    Control("bot.wake", "WAKE", report=True),
    Control("bot.freeze", "Freeze", confirm=True),
    Control("bot.cli", "CLI", note="read-only commands only", report=True),
    # the swarm
    Control("swarm.pause_all", "Pause all", confirm=True),
    Control("swarm.lease", "Hand lease", confirm=True),
    Control("swarm.failback", "Fail back", confirm=True),
    Control("swarm.role", "Reassign role", confirm=True),
    Control("swarm.cleanup", "Cleanup", confirm=True, note="dry run first, then confirm"),
    Control("swarm.sync", "Sync"),
    Control("swarm.admit", "Admit bot", confirm=True),
    Control("item.nudge", "Nudge", note="costs the owner a turn"),
)

_CONTROLS = {control.action: control for control in CONTROLS}


def control(action: str) -> Optional[Control]:
    return _CONTROLS.get(str(action))


def reports() -> Tuple[str, ...]:
    """The controls that only answer a question: pressing one changes nothing."""
    return tuple(row.action for row in CONTROLS if row.report)


def needs_confirm(action: str) -> bool:
    """Whether a press of *action* must be confirmed. Unknown actions are never run."""
    found = control(action)
    return bool(found and found.confirm)


#: How many buttons fit on one row of the keyboard.
PER_ROW = 3


def keyboard(actions: Iterable[str]) -> List[List[Dict[str, str]]]:
    """The keyboard under the message: one button per action, three to a row.

    Only actions the caller can actually carry out are offered, so a button that exists is a
    button that works. Plain dicts, which is what aiogram's ``InlineKeyboardMarkup`` takes --
    and what a test can read without importing aiogram.
    """
    from swarmly.telegram.control_press import callback_data
    buttons = []
    for action in actions:
        found = control(action)
        if found is None:
            continue
        buttons.append({"text": found.label, "callback_data": callback_data(action)})
    return [buttons[index:index + PER_ROW] for index in range(0, len(buttons), PER_ROW)]


def available(*prefixes: str) -> List[Control]:
    """The controls under one message: by prefix (``bot`` for per-bot, ``swarm``, ``item``)."""
    wanted = tuple(prefixes) or ("bot", "swarm", "item")
    return [row for row in CONTROLS if row.action.split(".", 1)[0] in wanted]


# ------------------------------------------------------------------------------------
# Reading the records
# ------------------------------------------------------------------------------------


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _dur(seconds: Any) -> str:
    seconds = max(0.0, _num(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _count(value: Optional[float]) -> str:
    """A token count for a dashboard: short, and never a made-up zero."""
    if value is None:
        return "unknown"
    value = _num(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def _tokens(value: Optional[float], known: Any, turns: Any) -> str:
    """A token total that says how much of it was reported (#125's rule, short form)."""
    turns, known = int(_num(turns)), int(_num(known))
    if value is None:
        return "unknown"
    if turns and known < turns:
        return f"{_count(value)} ({known}/{turns})"
    return _count(value)


def _money(value: Any, known: Any, turns: Any) -> str:
    turns, known = int(_num(turns)), int(_num(known))
    if value is None:
        return "unknown"
    text = f"${_num(value):.2f}"
    if turns and known < turns:
        text += f" ({known}/{turns})"
    return text


def usage_line(alias: str, usage: Optional[Mapping[str, Any]]) -> str:
    """``today=1.2k/340 $0.42`` for one bot, or ``unknown`` where the runtime was silent."""
    if not usage:
        return f"{alias}: usage unknown (no turns recorded)"
    turns = usage.get("turns")
    text = (f"today={_tokens(usage.get('tokens_in'), usage.get('tokens_in_known'), turns)}"
            f"/{_tokens(usage.get('tokens_out'), usage.get('tokens_out_known'), turns)}"
            f" {_money(usage.get('cost_usd'), usage.get('cost_known'), turns)}")
    if usage.get("quota_left") is not None:
        text += f" quota={_count(usage.get('quota_left'))}"
        if usage.get("quota_resets_at"):
            text += " resets=" + time.strftime("%H:%MZ", time.gmtime(
                _num(usage.get("quota_resets_at"))))
    else:
        text += " quota=unknown"
    return f"{alias}: {text}"


# ------------------------------------------------------------------------------------
# Alerts
# ------------------------------------------------------------------------------------


def alerts(*, now: Optional[float] = None, snapshots: Sequence[Snapshot] = (),
           round_state: Any = None,
           usage: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[str]:
    """What a reader must see first, worst first. One line each, ids never included."""
    moment = time.time() if now is None else float(now)
    usage = usage or {}
    found: List[Tuple[int, str]] = []
    for row in sorted(snapshots, key=lambda snap: str(snap.alias).lower()):
        alias = row.alias
        if row.state == DOWN:
            found.append((0, f"DOWN {alias}: reported down by a peer"))
            continue
        if row.is_stale(moment):
            found.append((0, f"DOWN {alias}: no beat for {_dur(row.age(moment))}"
                             f" (last {row.state} for {_dur(moment - row.since)})"
                          if row.since else f"DOWN {alias}: no beat for {_dur(row.age(moment))}"))
            continue
        if row.state == STUCK:
            over = f"{_dur(row.turn_elapsed)} over {_dur(row.turn_timeout)}" \
                if row.turn_timeout else f"turn {_dur(row.turn_elapsed)}"
            found.append((1, f"STUCK {alias}: {over}"))
        elif row.state == FROZEN:
            found.append((1, f"FROZEN {alias}: quota out, frozen {_dur(moment - row.since)}"))
        elif row.state == PAUSED:
            found.append((3, f"PAUSED {alias}: {_dur(moment - row.since)}"))
        elif row.state == BUSY and row.turn_timeout and row.turn_elapsed > row.turn_timeout:
            found.append((1, f"OVER LIMIT {alias}: turn {_dur(row.turn_elapsed)}"
                             f"/{_dur(row.turn_timeout)}"))
        mine = usage.get(alias) or {}
        if mine.get("quota_left") is not None and _num(mine.get("quota_left")) <= 0:
            when = ("resets " + time.strftime("%H:%MZ", time.gmtime(
                _num(mine.get("quota_resets_at"))))) if mine.get("quota_resets_at") else ""
            found.append((1, f"QUOTA {alias}: none left{' ' + when if when else ''}"))
    for key, item in _items(round_state):
        if item.get("waits_on"):
            found.append((2, f"HANDOFF #{key}: waits on {item.get('waits_on')}"))
    for blocker in list(getattr(round_state, "blockers", ()) or ()):
        found.append((2, f"BLOCKED round: {blocker}"))
    found.sort(key=lambda pair: (pair[0], pair[1]))
    seen: List[str] = []
    for _, line in found:
        if line not in seen:
            seen.append(line)
    return [f"! {line}" for line in seen]


# ------------------------------------------------------------------------------------
# The header, the round board and the bot lines
# ------------------------------------------------------------------------------------


def header(*, now: Optional[float] = None, round_state: Any = None,
           coordinator: str = "", usage: Optional[Mapping[str, Any]] = None,
           lease: Optional[Snapshot] = None) -> str:
    """``SWARM round=5 items=6/9 coord=Claude term=4 lease=12m today=1.2M/340 $0.42``."""
    moment = time.time() if now is None else float(now)
    parts = [MARKER]
    round_name = str(getattr(round_state, "round", "") or "")
    status = str(getattr(round_state, "status", "") or "")
    items = _items(round_state)
    if round_name:
        parts.append(f"round={round_name}")
    if status and status != "ok":
        # Missing is not empty (#86): an unreadable round is never "no open items".
        parts.append(f"state={status}")
    elif items:
        done = sum(1 for _, item in items
                   if str(item.get("state") or "").lower() in ("merged", "closed", "done"))
        parts.append(f"items={done}/{len(items)}")
    if coordinator:
        parts.append(f"coord={coordinator}")
    term = int(_num(getattr(round_state, "term", 0))) or int(_num(getattr(lease, "term", 0)))
    if term:
        parts.append(f"term={term}")
    ends = _num(getattr(lease, "lease_ends", 0.0))
    if ends > moment:
        parts.append(f"lease={_dur(ends - moment)}")
    if usage:
        turns = usage.get("turns")
        parts.append("today="
                     f"{_tokens(usage.get('tokens_in'), usage.get('tokens_in_known'), turns)}"
                     f"/{_tokens(usage.get('tokens_out'), usage.get('tokens_out_known'), turns)}"
                     f" {_money(usage.get('cost_usd'), usage.get('cost_known'), turns)}")
    return " ".join(parts)


def _items(round_state: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """The round's items in its own order, read by name so unknown keys never leak."""
    items = getattr(round_state, "items", None)
    if not isinstance(items, dict):
        return []
    order = [str(key) for key in (getattr(round_state, "order", None) or ())]
    keys = [key for key in order if key in items]
    keys += sorted(str(key) for key in items if str(key) not in keys)
    out: List[Tuple[str, Dict[str, Any]]] = []
    for key in keys:
        raw = items.get(key)
        if not isinstance(raw, dict):
            continue
        clean: Dict[str, Any] = {"state": str(raw.get("state") or "")}
        for name in ("owner", "waits_on", "ci", "note"):
            clean[name] = str(raw.get(name) or "")
        for name in ("issue", "pr"):
            value = raw.get(name)
            clean[name] = value if isinstance(value, int) and not isinstance(value, bool) else 0
        head = str(raw.get("head") or "")
        clean["head"] = head[:7]
        for kind in ("review", "qa"):
            verdict = raw.get(kind)
            clean[kind] = ""
            if isinstance(verdict, dict):
                text = str(verdict.get("verdict") or "")
                # A verdict is bound to a head (#86): one on another SHA is not a verdict.
                if text and str(verdict.get("sha") or "").lower() in ("", head.lower()):
                    clean[kind] = text
        out.append((key, clean))
    return out


def can_merge(item: Mapping[str, Any]) -> Tuple[bool, str]:
    """``(ready, why not)`` for one round-board row: CI, both verdicts, one head."""
    if str(item.get("state") or "").lower() in ("merged", "closed", "done"):
        return False, "merged"
    if not item.get("pr"):
        return False, "no PR"
    if not item.get("head"):
        return False, "no head"
    ci = str(item.get("ci") or "")
    if ci.lower() not in ("pass", "green", "ok", "7/7") and not ci.startswith("7/"):
        return False, f"ci {ci or 'unknown'}"
    review = str(item.get("review") or "")
    if review.lower() not in ("merge", "pass", "approved"):
        return False, f"review {review or 'open'}"
    qa = str(item.get("qa") or "")
    if qa.lower() not in ("pass", "passed", "ok"):
        return False, f"qa {qa or 'open'}"
    return True, ""


def board(round_state: Any) -> List[str]:
    """One line per item: owner, PR, head, CI, the two verdicts, and whether it can merge."""
    lines: List[str] = []
    for key, item in _items(round_state):
        parts = [f"#{key}"]
        if item.get("owner"):
            parts.append(str(item["owner"]))
        if item.get("pr"):
            parts.append(f"PR#{item['pr']}")
        if item.get("head"):
            parts.append(str(item["head"]))
        parts.append(f"ci={item['ci'] or 'unknown'}")
        parts.append(f"review={item['review'] or 'open'}")
        parts.append(f"qa={item['qa'] or 'open'}")
        if item.get("waits_on"):
            parts.append(f"waits={item['waits_on']}")
        ready, why = can_merge(item)
        parts.append("ready" if ready else f"blocked: {why}")
        lines.append(" ".join(parts))
    return lines


def bot_lines(*, now: Optional[float] = None, snapshots: Sequence[Snapshot] = (),
              usage: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[str]:
    """One line per bot: the snapshot the heartbeat took, plus today's tokens and cost."""
    moment = time.time() if now is None else float(now)
    usage = usage or {}
    newest: Dict[str, Snapshot] = {}
    for row in snapshots:
        key = str(row.alias).strip().lower()
        if key not in newest or row.at >= newest[key].at:
            newest[key] = row
    lines: List[str] = []
    for key in sorted(newest):
        row = newest[key]
        line = row.line(moment)
        if row.model:
            line += f" {row.model}"
        if row.effort:
            line += f"/{row.effort}"
        if row.rev:
            line += f" rev={row.rev}"
        if row.state == IDLE and not row.queue:
            line += " ready"
        lines.append(line + " " + usage_line(row.alias, usage.get(row.alias)))
    return lines


# ------------------------------------------------------------------------------------
# The message
# ------------------------------------------------------------------------------------


def _fill(sections: Mapping[str, Sequence[str]], order: Iterable[str],
          limit: int) -> Tuple[List[str], int]:
    """Take whole lines in priority order until the message is full. Returns (lines, dropped)."""
    out: List[str] = []
    used = 0
    dropped = 0
    for name in order:
        lines = list(sections.get(name) or ())
        if not lines:
            continue
        if out:
            used += 1                            # the blank line between sections
        for index, line in enumerate(lines):
            cost = len(line) + (1 if (out or index) else 0)
            if used + cost > limit:
                dropped += len(lines) - index
                break
            out.append(line)
            used += cost
    return out, dropped


def dashboard(*, now: Optional[float] = None, snapshots: Sequence[Snapshot] = (),
              round_state: Any = None, usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
              coordinator: str = "", lease: Optional[Snapshot] = None,
              limit: int = MAX_CHARS) -> str:
    """The pinned message: alerts, then a line per bot, then the round board.

    Anything that does not fit is dropped whole, alert lines last to go: a bot that is down
    is worth more than a round-board row, and the compact view is one button away.
    """
    moment = time.time() if now is None else float(now)
    head = header(now=moment, round_state=round_state, coordinator=coordinator,
                  usage=_sum_usage(usage), lease=lease or _coordinator_snapshot(
                      snapshots, coordinator))
    alert_lines = alerts(now=moment, snapshots=snapshots, round_state=round_state,
                         usage=usage)
    bots = bot_lines(now=moment, snapshots=snapshots, usage=usage)
    board_lines = board(round_state)
    sections = {
        "alerts": alert_lines,
        "bots": bots,
        "board": [f"round board ({len(board_lines)} items):"] + board_lines
        if board_lines else [],
    }
    text, dropped = _fill({"head": [head], **sections},
                          ("head", "alerts", "bots", "board"), max(64, int(limit)))
    if dropped:
        note = f"... {dropped} more line(s) hidden; detail is one button away"
        while text and len("\n".join(text + [note])) > limit:
            text.pop()
        text.append(note)
    return "\n".join(text)


def _coordinator_snapshot(snapshots: Sequence[Snapshot],
                          coordinator: str) -> Optional[Snapshot]:
    """The coordinator's own snapshot: that is where the lease term and its expiry live (#83)."""
    if not coordinator:
        return None
    wanted = coordinator.strip().lower()
    mine = [row for row in snapshots if str(row.alias).strip().lower() == wanted]
    return max(mine, key=lambda row: row.at) if mine else None


def _sum_usage(usage: Optional[Mapping[str, Mapping[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The swarm's own total: every bot's rows, with coverage summed the same way (#125)."""
    rows = [row for row in (usage or {}).values() if isinstance(row, Mapping)]
    if not rows:
        return None
    total: Dict[str, Any] = {"turns": 0, "tokens_in_known": 0, "tokens_out_known": 0,
                             "cost_known": 0}
    tokens_in: List[float] = []
    tokens_out: List[float] = []
    costs: List[float] = []
    for row in rows:
        total["turns"] += int(_num(row.get("turns")))
        total["tokens_in_known"] += int(_num(row.get("tokens_in_known")))
        total["tokens_out_known"] += int(_num(row.get("tokens_out_known")))
        total["cost_known"] += int(_num(row.get("cost_known")))
        if row.get("tokens_in") is not None:
            tokens_in.append(_num(row.get("tokens_in")))
        if row.get("tokens_out") is not None:
            tokens_out.append(_num(row.get("tokens_out")))
        if row.get("cost_usd") is not None:
            costs.append(_num(row.get("cost_usd")))
    total["tokens_in"] = sum(tokens_in) if tokens_in else None
    total["tokens_out"] = sum(tokens_out) if tokens_out else None
    total["cost_usd"] = round(sum(costs), 4) if costs else None
    return total
