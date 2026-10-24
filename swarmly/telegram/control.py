"""The command centre's pinned dashboard -- the text (round 5 item 9, issue #117).

The coordinator's transport keeps one message pinned in a control topic and edits it in
place on every heartbeat. This module is the *text* half of that: alerts first, a swarm
header, the round board, and one line per bot. It is built from what the transport already
holds -- the #124 snapshots, the #125 ledger, the #86 round state -- so rendering it calls
no adapter and no network, and never costs a model turn.

Three rules decide the shape.

**One row per bot, and the alerts still survive.** The card opens with a header line -- the
round, the swarm's spend today and when that line was refreshed -- then one row per bot in a
fixed order: a status icon, the alias, its role, what it is doing and for how long, model and
effort, today's turns, tokens and cost (#241). "Who is doing what" is the question a card in a
chat answers first. Under the rows come the lease, then the alerts, then the round's open
items. When the message runs out of room it is the *detail* that is dropped -- the round board
first, then the lease -- never an alert.

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

#: What survives a full message, best first. `_fit` walks this order and stops taking lines
#: when the next one would not fit: the header and the alerts come before detail, and the
#: round board is the first thing to go (#241).
_FILL = ("head", "alerts", "bots", "lease", "board")

#: Where those sections are drawn: one row per bot first, then the lease, then the alerts,
#: then the round's open items (#241).
_SHOW = ("head", "bots", "lease", "alerts", "board")

#: One icon per state, so a row is recognised before it is read (#241).
_ICONS = {BUSY: "🔵", IDLE: "🟢", PAUSED: "⏸", FROZEN: "🧊", STUCK: "⚠️", DOWN: "🔴"}

#: What the row says the bot is doing, in the same order as :data:`_ICONS`.
_WORDS = {BUSY: "working", IDLE: "idle", PAUSED: "paused", FROZEN: "frozen",
          STUCK: "stuck", DOWN: "DOWN"}


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
    Control("swarm.cleanup", "Cleanup", report=True,
            note="dry run: nothing is deleted"),
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


#: The controls that take a **picked value**: pressing one with no value offers the choices,
#: pressing one with a value sets it after the second tap (#153). The list is short on purpose
#: -- a control joins it when its choices can actually be read in code, with no model turn.
PICKERS: Tuple[str, ...] = ("bot.model", "bot.effort", "bot.wake", "swarm.role",
                            "swarm.lease")


def is_picker(action: str) -> bool:
    """Whether a control takes a value from a list rather than being a plain button."""
    return str(action) in PICKERS


def picker(action: str, choices: Iterable[Tuple[str, str]],
           target: str = "", as_target: bool = False) -> List[List[Dict[str, str]]]:
    """The keyboard that carries a picked value: one button per choice, three to a row.

    The value rides the button's own callback data (``ctl:<action>:<target>:<value>``), not a
    conversation state, so a press made after a restart still means what it said, and nothing
    is kept on disk. ``choices`` are ``(label, value)`` pairs read from a live list.
    """
    from swarmly.telegram.control_press import MAX_VALUE_CHARS, callback_data
    buttons = []
    for label, value in choices:
        value = str(value)[:MAX_VALUE_CHARS]
        # ``as_target`` is for the controls that need *two* values (reassign a role: which bot,
        # then which role): the first choice goes in the target slot, and the second press
        # fills the value next to it, so the whole pick still arrives in one callback (#153).
        buttons.append({"text": str(label),
                        "callback_data": (callback_data(action, value)
                                          if as_target else
                                          callback_data(action, target, value))})
    return [buttons[index:index + PER_ROW] for index in range(0, len(buttons), PER_ROW)]


def picker_text(action: str, alias: str = "", target: str = "") -> str:
    """What a picker asks, in words -- a fixed line, so asking costs no model turn.

    It names the bot the press will *change*: the target of a two-value pick when there is one,
    and otherwise this bot. The role pick's first step changes nobody yet, so it must not name
    itself there -- a list of bots under "Reassign role for Hermes" reads as if Hermes were
    already chosen (review, #153).
    """
    found = control(action)
    label = found.label if found else action
    target = str(target or "").strip()
    if target:
        return (f"{label} for {target}: pick one below. "
                "Nothing changes until you tap again to confirm.")
    if action == "swarm.role":
        return f"{label}: which bot? pick one below, then its role."
    if action == "bot.wake":
        # The one thing a timer pick can do is cancel it: say so, rather than "WAKE" over a
        # list of ids that reads as arming one (review, #153).
        return f"{label}{f' for {alias}' if alias else ''}: pick the timer to cancel below."
    if action == "swarm.lease":
        # The hand-over names the *new* holder; nothing is given away until it is confirmed.
        return (f"{label}: which bot should coordinate? Nothing moves until you tap again "
                "to confirm.")
    who = f" for {alias}" if alias else ""
    return f"{label}{who}: pick one below. Nothing changes until you tap again to confirm."


def confirm_keyboard(action: str, code: str, value: str = "") -> List[List[Dict[str, str]]]:
    """The second tap, as a button: the only thing that can send ``ctl:confirm:<code>`` back.

    A confirmation the presser cannot deliver is not a confirmation. The words under a pick
    say "tap again", so the tap has to exist as a button whose data carries the minted code --
    pressing the *first* button again only mints a new one (review, #153).
    """
    from swarmly.telegram.control_press import confirm_data
    found = control(action)
    label = found.label if found else action
    said = f"Confirm {label}"
    if value:
        said += f" → {str(value)[:24]}"
    return [[{"text": said[:48], "callback_data": confirm_data(code)}]]


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
        return "?"
    value = _num(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def _cost(value: Any, known: Any = None, turns: Any = None) -> str:
    """Today's cost for a card: ``$22.33``, or ``?`` when the runtime never said."""
    if value is None:
        return "?"
    text = f"${_num(value):.2f}"
    if _partial(known, turns):
        text += f" ({int(_num(known))}/{int(_num(turns))})"
    return text


def _partial(known: Any, turns: Any) -> bool:
    """Whether a total is short of the turns it covers: it then says how short (#125)."""
    turns, known = int(_num(turns)), int(_num(known))
    return bool(turns and known < turns)


def _token_cell(usage: Optional[Mapping[str, Any]]) -> str:
    """A bot's tokens today: ``19.0M``, ``900+?`` when one direction is unreported, or ``?"""
    if not usage or (usage.get("tokens_in") is None and usage.get("tokens_out") is None):
        return "?"
    tokens_in, tokens_out = usage.get("tokens_in"), usage.get("tokens_out")
    if tokens_in is None or tokens_out is None:
        # One direction was reported: showing it is the point, and the ``?`` beside it keeps
        # the cell from reading as a whole total (#125).
        return f"{_count(tokens_in)}+{_count(tokens_out)}"
    text = _count(_num(tokens_in) + _num(tokens_out))
    known = min(_num(usage.get("tokens_in_known")), _num(usage.get("tokens_out_known")))
    if _partial(known, usage.get("turns")):
        text += f" ({int(known)}/{int(_num(usage.get('turns')))})"
    return text


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
           usage: Optional[Mapping[str, Any]] = None) -> str:
    """``SWARM · round=5 · items=6/9 · today 12 turns · 19.0M tok · $22.33 · refreshed 12:04Z``"""
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
    parts.append(_spend(usage))
    parts.append("refreshed " + time.strftime("%H:%MZ", time.gmtime(moment)))
    return " · ".join(parts)


def _spend(usage: Optional[Mapping[str, Any]]) -> str:
    """The swarm's today, in one phrase: turns, tokens and cost, each ``?`` when unreported."""
    usage = usage or {}
    turns = usage.get("turns")
    turns_text = "?" if turns is None else str(int(_num(turns)))
    return (f"today {turns_text} turns · {_token_cell(usage)} tok"
            f" · {_cost(usage.get('cost_usd'), usage.get('cost_known'), turns)}")


def lease_line(*, now: Optional[float] = None, coordinator: str = "",
               lease: Optional[Snapshot] = None) -> str:
    """``lease: Claude · term=4 · 10m left`` -- who coordinates, and how long that holds."""
    moment = time.time() if now is None else float(now)
    holder = str(coordinator or getattr(lease, "alias", "") or "").strip()
    if not holder:
        return "lease: ?"
    term = int(_num(getattr(lease, "term", 0)))
    parts = [f"lease: {holder}", f"term={term or '?'}"]
    ends = _num(getattr(lease, "lease_ends", 0.0))
    if ends > moment:
        parts.append(f"{_dur(ends - moment)} left")
    return " · ".join(parts)


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


def bot_row(row: Snapshot, *, now: float, usage: Optional[Mapping[str, Any]] = None,
            role: str = "") -> str:
    """One fixed row: icon, alias, role, what it is doing, model/effort, turns, tokens, cost.

    The order never varies, so a reader learns where to look once (#241). A number the
    runtime did not report is ``?``, in the row as everywhere else: a made-up ``0`` would read
    as a bot that spent nothing, which is a different fact from one that said nothing.
    """
    moment = float(now)
    # A bot that stopped beating is DOWN however it last described itself (#124): its own
    # record cannot say it died, so the age of the record says it instead.
    state = DOWN if (row.state == DOWN or row.is_stale(moment)) else row.state
    since = _num(getattr(row, "since", 0.0))
    if state == DOWN:
        took = _dur(row.age(moment)) if row.at else "?"
        activity = f"{_WORDS[state]} {took}"
    elif since and since <= moment:
        activity = f"{_words(state)} {_dur(moment - since)}"
    else:
        activity = f"{_words(state)} ?"
    model = str(getattr(row, "model", "") or "").strip() or "?"
    effort = str(getattr(row, "effort", "") or "").strip()
    usage = usage or {}
    turns = usage.get("turns")
    return " · ".join([
        f"{_icon(state)} {row.alias}",
        str(role or "").strip() or "?",
        activity,
        f"{model}/{effort}" if effort else model,
        "? turns" if turns is None else f"{int(_num(turns))} turns",
        f"{_token_cell(usage)} tok",
        _cost(usage.get("cost_usd"), usage.get("cost_known"), turns),
    ])


def _icon(state: str) -> str:
    return _ICONS.get(state, "?")


def _words(state: str) -> str:
    return _WORDS.get(state, "?")


def bot_lines(*, now: Optional[float] = None, snapshots: Sequence[Snapshot] = (),
              usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
              roles: Optional[Mapping[str, str]] = None) -> List[str]:
    """One row per bot, the newest record for each, in alias order: a fixed card (#241)."""
    moment = time.time() if now is None else float(now)
    usage = usage or {}
    roles = roles or {}
    newest: Dict[str, Snapshot] = {}
    for row in snapshots:
        key = str(row.alias).strip().lower()
        if key not in newest or row.at >= newest[key].at:
            newest[key] = row
    lines: List[str] = []
    for key in sorted(newest):
        row = newest[key]
        mine = usage.get(row.alias) or {}
        role = roles.get(row.alias) or roles.get(key) or ""
        lines.append(bot_row(row, now=moment, usage=mine, role=role))
    return lines


# ------------------------------------------------------------------------------------
# The message
# ------------------------------------------------------------------------------------


def _fit(sections: Mapping[str, Sequence[str]], order: Iterable[str],
         limit: int) -> Tuple[Dict[str, List[str]], int]:
    """Take whole lines in priority order until the message is full. Returns (kept, dropped)."""
    kept: Dict[str, List[str]] = {}
    used = 0
    dropped = 0
    for name in order:
        lines = list(sections.get(name) or ())
        if not lines:
            continue
        room: List[str] = []
        for index, line in enumerate(lines):
            # A newline inside a section, and a blank line before a section that follows one.
            cost = len(line) + (1 if room else (2 if kept else 0))
            if used + cost > limit:
                dropped += len(lines) - index
                break
            room.append(line)
            used += cost
        if room:
            kept[name] = room
    return kept, dropped


def _join(kept: Mapping[str, Sequence[str]], order: Iterable[str]) -> str:
    return "\n\n".join("\n".join(kept[name]) for name in order if kept.get(name))


def _render(kept: Dict[str, List[str]], dropped: int, limit: int) -> str:
    """The sections in reading order, plus how many lines did not fit (#117, #241)."""
    if not dropped:
        return _join(kept, _SHOW)
    note = f"... {dropped} more line(s) hidden; detail is one button away"
    while True:
        text = _join(kept, _SHOW)
        if text and len(text) + 1 + len(note) <= limit:
            return text + "\n" + note
        # Make room for the note by dropping the least important line still drawn: the round
        # board first, then the lease, then a bot row -- an alert is the last thing to go.
        for name in ("board", "lease", "bots"):
            rows = kept.get(name) or []
            if rows:
                rows.pop()
                if not rows:
                    del kept[name]
                break
        else:
            return note[:limit]


def dashboard(*, now: Optional[float] = None, snapshots: Sequence[Snapshot] = (),
              round_state: Any = None, usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
              coordinator: str = "", lease: Optional[Snapshot] = None,
              roles: Optional[Mapping[str, str]] = None,
              limit: int = MAX_CHARS) -> str:
    """The pinned card: a header, one row per bot, the lease, alerts, then round items.

    Anything that does not fit is dropped whole, the round board first and the alerts last:
    a bot that is down is worth more than a round-board row, and the compact view is one
    button away.
    """
    moment = time.time() if now is None else float(now)
    held = lease or _coordinator_snapshot(snapshots, coordinator)
    board_lines = board(round_state)
    sections = {
        "head": [header(now=moment, round_state=round_state, usage=_sum_usage(usage))],
        "alerts": alerts(now=moment, snapshots=snapshots, round_state=round_state,
                         usage=usage),
        "bots": bot_lines(now=moment, snapshots=snapshots, usage=usage, roles=roles),
        "lease": [lease_line(now=moment, coordinator=coordinator, lease=held)],
        "board": [f"round board ({len(board_lines)} items):"] + board_lines
        if board_lines else [],
    }
    kept, dropped = _fit(sections, _FILL, max(64, int(limit)))
    return _render(kept, dropped, max(64, int(limit)))


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
