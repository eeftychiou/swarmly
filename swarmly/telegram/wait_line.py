"""``WAIT``: a reply says what it waits on, and the transport turns it into a row (#165).

Round 5.5 item 6 is the migration: the model stops arming timers and running standing checks,
and says what it is waiting for instead. That line is the whole interface --

    WAIT review #133 head 38f1977 from Reasonix
    WAIT ci #173 head 9fb2f21
    WAIT merge #173 head b40f3ae from Claudebot
    WAIT progress #166 from Antigravity
    WAIT answer on the Drive credentials from admin

-- and everything else is the transport's: the row is opened (``owed.Row``), the deadline and
the ladder come from #164's guard, and the wake comes from #163's ``owed_wake``. Nothing here
runs a check, reads a clock into a timer, or trusts a model about a commit: a head is a sha or
the line is refused.

**A refused line is still removed from the reply** and said back into it, the way a refused
``WAKE`` is: a promise the bot thinks it made is worse than a line that never appeared.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from swarmly.telegram.owed import ADMIN, CI, OwedError, Row

#: The marker, at the start of a line (case-insensitive), like ``WAKE``.
WAIT_MARKER = "WAIT"

#: What a bot may wait on. Each maps to the row kind that models it: a verdict is owed by a
#: bot on a head, CI by the transport, a merge by the coordinator, progress by an owner, and an
#: answer by the admin.
KINDS = {
    "review": "verdict",
    "qa": "verdict",
    "ci": "ci",
    "merge": "merge",
    "progress": "progress",
    "handoff": "handoff",
    "answer": "admin",
    "admin": "admin",
}

#: The kinds whose row is bound to one head: a verdict or a result on older code is not one.
HEAD_BOUND_KINDS = ("review", "qa", "ci", "merge")


class WaitError(ValueError):
    """A line that cannot become a row: said back to the bot, never silently dropped."""


def _words(line: str) -> List[str]:
    return str(line or "").strip().split()


def _head(words: Sequence[str]) -> str:
    """The ``head <sha>`` token, or "" when the line names none."""
    for index, word in enumerate(words):
        if word.lower() == "head" and index + 1 < len(words):
            return words[index + 1]
    return ""


def _from(words: Sequence[str]) -> str:
    """The ``from <alias>`` token, or "" when the line names none."""
    for index, word in enumerate(words):
        if word.lower() == "from" and index + 1 < len(words):
            return words[index + 1].lstrip("@")
    return ""


def _item(words: Sequence[str]) -> str:
    """The first ``#N``/``#owner/repo#N`` token, as rows write it (``#173``)."""
    for word in words:
        if word.startswith("#"):
            return "#" + word.lstrip("#").split("#")[-1]
    return ""


def _owes(kind: str, words: Sequence[str], head: str, item: str) -> str:
    """What the row says is owed, in the bot's own words when it gave any."""
    said = " ".join(word for word in words[2:]
                    if word.lower() not in ("head", "from") and word not in (head, _from(words))
                    and not word.startswith("#"))
    said = said.strip(" ,.;:")
    if said and said.lower() not in ("on", "at"):
        return said if said.lower().startswith(("on ", "at ")) else f"on {said}"
    return {
        "review": "a review verdict", "qa": "a QA verdict", "ci": "a result",
        "merge": "a merge", "progress": "progress", "handoff": "an answer",
        "answer": "an answer", "admin": "an answer",
    }.get(kind, "something")


def _question_key(owes: str) -> str:
    """The question, keyed the way ``owed.admin_row`` keys it: two questions, two debts.

    A row's id is ``<kind>:<item>[@<head>][@<key_extra>]:<debtor>``, and an admin row usually
    has no item -- so without a key two questions to the admin in one topic write the same id
    and the second is never owed (#209 review). The question is what differs, so it is what
    keys the row; the same question asked again updates the one row.
    """
    return hashlib.sha1(owes.encode("utf-8")).hexdigest()[:7]


def parse_line(line: str, *, topic: str = "", place: str = "",
               at: Optional[float] = None) -> Row:
    """One ``WAIT`` line as a row. Raises :class:`WaitError` with the reason it cannot be one."""
    moment = time.time() if at is None else float(at)
    words = _words(line)
    if not words or words[0].upper() != WAIT_MARKER:
        raise WaitError("not a WAIT line")
    if len(words) < 2:
        raise WaitError("WAIT needs a kind: WAIT review #133 head 38f1977 from Reasonix")
    kind = words[1].strip().lower()
    if kind not in KINDS:
        raise WaitError(f"WAIT does not know {kind!r}: use one of "
                        f"{', '.join(sorted(KINDS))}")
    head = _head(words)
    debtor = _from(words)
    item = _item(words)
    if kind in HEAD_BOUND_KINDS and not head:
        raise WaitError(f"WAIT {kind} needs the head it is about: WAIT {kind} "
                        f"{item or '#N'} head <sha>")
    if KINDS[kind] in ("verdict", "merge", "progress", "handoff") and not debtor:
        raise WaitError(f"WAIT {kind} needs the bot that owes it: WAIT {kind} "
                        f"{item or '#N'} from <alias>")
    if KINDS[kind] == "ci" and debtor:
        raise WaitError("WAIT ci is owed by the transport, not a bot: drop "
                        f"'from {debtor}'")
    owes = _owes(kind, words, head, item)
    row_kind = KINDS[kind]
    if row_kind == "handoff":
        extra = place
    elif row_kind == "admin":
        extra = f"{topic or '-'}#q{_question_key(owes)}"
    else:
        extra = ""
    try:
        return Row(kind=row_kind, item=item, debtor=debtor or (CI if row_kind == "ci" else ADMIN),
                   owes=f"a {kind} {owes}" if kind in ("review", "qa") else owes,
                   head=head, topic=topic, key_extra=extra,
                   opened_by=f"WAIT line in {topic or 'a discussion'}",
                   created_at=moment, updated_at=moment)
    except OwedError as exc:
        raise WaitError(str(exc)) from exc


def from_wake(spec: Any, *, alias: str, topic: str = "", at: Optional[float] = None) -> Row:
    """A retired ``WAKE`` line as the row that replaces it (#165).

    ``WAKE in=10m check="gh pr checks 209" then="merge it"`` was a promise to *look again* at
    something in ten minutes. The step the migration removes is the timer, not the promise, so
    the promise becomes a row: the bot owes itself progress on the item the check named, with
    the check and the action written down, and the guard's ladder (#164) and the wakes (#163)
    act on it. A check that names no item (``git status``) is progress with no item.

    The check is part of the row's key: two checks on one PR are two promises, and re-arming
    the same check updates the row instead of opening a second one -- which is what the old
    "one standing check per PR or issue" rule did by replacing the timer.
    """
    moment = time.time() if at is None else float(at)
    from swarmly.telegram.wake import subject_of

    argv = tuple(getattr(spec, "argv", ()) or ())
    check = " ".join(argv) or str(getattr(spec, "check", "") or "").strip()
    subject = subject_of(argv) if argv else None
    kind, number, repo = subject if subject else ("", "", "")
    item = f"#{number}" if number else ""
    then = str(getattr(spec, "then", "") or "").strip()
    in_seconds = float(getattr(spec, "in_seconds", 0.0) or 0.0)
    where = f" in {repo}" if repo else ""
    opened = (f"a WAKE line that checked `{check}`{where}"
              + (f" then \"{then[:100]}\"" if then else ""))
    return Row(kind="progress", item=item, debtor=str(alias or "").strip(),
               owes=f"a look at `{check}`{where}",
               key_extra=f"wake#{_question_key(check)}", topic=topic,
               opened_by=opened[:200], deadline=(moment + in_seconds) if in_seconds else 0.0,
               created_at=moment, updated_at=moment)


def extract_waits(text: str, *, topic: str = "", place: str = "",
                  at: Optional[float] = None) -> Tuple[str, List[Row], List[str]]:
    """``(text, rows, reasons)``: every ``WAIT`` line turned into a row and removed.

    A line is stripped in every case -- a refused one is said back into the reply instead of
    being posted as prose -- and every refusal comes back in *reasons*.
    """
    if not text or WAIT_MARKER not in text.upper():
        return text, [], []
    rows: List[Row] = []
    reasons: List[str] = []
    kept: List[str] = []
    for line in str(text).splitlines():
        if not line.strip().upper().startswith(WAIT_MARKER):
            kept.append(line)
            continue
        if line.strip().upper() == WAIT_MARKER or not line.strip().upper()[
                len(WAIT_MARKER):].startswith((" ", "\t")):
            kept.append(line)           # "WAITING for the bus" is prose, not a line
            continue
        try:
            rows.append(parse_line(line, topic=topic, place=place, at=at))
        except WaitError as exc:
            reasons.append(str(exc))
    return "\n".join(kept).strip(), rows, reasons


def ack(row: Row) -> str:
    """What the bot is told its own line became: one short line, not a paragraph."""
    head = f" at {row.head}" if row.head else ""
    return f"WAIT {row.kind} {row.subject}{head} recorded: {row.debtor} owes {row.owes}"


def line_for(rows: Iterable[Row]) -> List[str]:
    """One ack per row, for a caller that wants them as a list."""
    return [ack(row) for row in rows or ()]
