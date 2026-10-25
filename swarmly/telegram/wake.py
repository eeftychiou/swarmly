"""Self-wake timers (issue #81): a bot schedules its own check and acts on its exit code.

Round 3 stalled at the finish line because the only thing that would have ended it was an
external signal — a GitHub CI wake — and that signal was off. A bot needs its own timer, and
the timer has to be one the transport can fire without a model turn: nobody can ask a model to
remember to wake up.

A reply arms one with a machine line, stripped before posting like the other directives::

    WAKE in=10m every=5m until=120m check="gh pr checks 75 --required" then="merge it"

The transport runs ``check`` itself and decides on the **exit code**, which is what makes the
cost one subprocess instead of one turn per poll:

* **0, ready** — one turn carrying ``then`` and the output tail. The timer is done.
* **pending** — re-arm at ``every``, silently, until ``expires_at``.
* **anything else, or a timeout** — one turn saying the check itself failed, with the tail, so
  the bot decides. It is *not* re-armed: a broken check must not loop.
* **past ``expires_at``** — one turn saying the wait expired, so the bot escalates or re-arms.
  A timer never fires silently forever.

Four constraints shape the code, because ``check`` is untrusted model output running beside a
turn for the first time after it:

**An exact argv prefix, not a first token.** ``gh`` and ``git`` can change the world — a
``gh pr merge``, an ``api -X POST``, a ``push --force`` — so the allowlist is a list of
read-only argv prefixes (``ALLOWED_ARGV``), matched argv-to-argv, and the line is parsed with
:mod:`shlex` rather than a shell. Shell metacharacters are refused outright.

**Exit codes per command.** ``gh pr checks`` exits 0 when green, 8 while pending and 1 when
failing; mapping every non-zero to "not yet" is how a *failing* check would re-arm forever. Each
allowed prefix documents the codes it may return (:data:`EXIT_MEANING`), and a code outside that
map is an error turn.

**A fire is claimed, not assumed.** Two bots can see the same bus-mirrored timer. A firing is
claimed by writing an append-only record on the bus *before* the turn starts; the claims for one
firing are folded to exactly one winner (lowest alias), so a restart or a takeover can neither
fire twice nor lose the fire.

**Absolute times and a stored attempt count.** ``next_at`` and ``expires_at`` are persisted as
absolute times, so a restart resumes the schedule instead of re-deriving it (``in=10m`` on every
start would re-arm forever), and ``attempts`` bounds the chain even if a clock misbehaves.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from swarmly.transports.base import WAKES_RELPATH, is_bus_document

logger = logging.getLogger("swarmly.telegram.wake")

#: The marker a reply uses. One per line, stripped from the posted text in every case.
WAKE_MARKER = "WAKE"

#: One line, bounded: it is carried in a reply and parsed by the transport.
MAX_LINE_CHARS = 600

#: Read-only argv prefixes a check may start with (issue #81 review, blocker 1). Matched
#: argv-to-argv, so ``gh pr checks`` cannot be stretched into ``gh pr merge``.
ALLOWED_ARGV: Tuple[Tuple[str, ...], ...] = (
    ("gh", "pr", "checks"),
    ("gh", "pr", "view"),
    ("gh", "pr", "list"),
    ("gh", "pr", "status"),
    ("gh", "pr", "diff"),
    ("gh", "run", "view"),
    ("gh", "run", "list"),
    ("gh", "api"),
    ("gh", "issue", "view"),
    ("gh", "search"),
    ("git", "status"),
    ("git", "log"),
    ("git", "diff"),
    ("git", "show"),
    ("git", "rev-parse"),
)
#: Deliberately absent: ``git fetch`` and ``git ls-remote``. Both take a repository
#: argument that accepts ``ext::<command>``, which git runs as a remote helper -- arbitrary
#: command execution that no shell metacharacter check can see (PR #92 review, B1/Codex 1).

#: Flags that turn a read into a write, as canonical flag names. Matched after
#: normalization, because gh accepts an attached value: ``-XPOST`` is ``-X POST`` and
#: ``--field=body=x`` is ``--field body=x`` (PR #92 review, blocker 1).
WRITE_FLAGS = ("-X", "--method", "-f", "--field", "-F", "--input", "--raw-field",
               "-o", "--output")

#: What ``gh api`` may carry besides the path: reads only. Anything else is refused, so a
#: mutation (``-XPOST``, ``-f body=…``) cannot be composed at all.
GH_API_FLAGS = ("-q", "--jq", "--paginate", "--slurp", "-i", "--include", "-H", "--header")

#: Canonical flag name of a token: ``--field=x`` -> ``--field``, ``-XPOST`` -> ``-X``.
_SHORT_FLAGS = tuple(flag for flag in WRITE_FLAGS if len(flag) == 2)

#: Characters that only mean something to a shell. A check needs none of them, and one that
#: wants ``$`` or a pipe is asking for the thing this allowlist exists to prevent.
SHELL_METACHARACTERS = set("|&;<>`$()\n\r\\*?[]{}~!")

#: What each allowed prefix's exit codes mean (issue #81 review, blocker 2). ``gh pr checks``
#: exits 8 while checks are pending; anything else non-zero is a failure, not a "not yet".
EXIT_MEANING: Dict[Tuple[str, ...], Dict[int, str]] = {
    ("gh", "pr", "checks"): {0: "ready", 8: "pending", 1: "error"},
}

#: Default verdict for an allowed command with no documented map: 0 is ready, else the check
#: itself failed. Never "pending" by default — silence is not readiness.
DEFAULT_MEANING: Dict[int, str] = {0: "ready"}

READY, PENDING, ERROR = "ready", "pending", "error"

#: A check's own wall-clock budget. Past it the process group is killed.
CHECK_TIMEOUT_SECONDS = 120.0

#: The lines of output a wake turn carries.
TAIL_LINES = 20

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
_KV = re.compile(r'([a-z]+)=("[^"]*"|\'[^\']*\'|\S+)')


class WakeError(ValueError):
    """A WAKE line, a timer or a check that is refused."""


def parse_duration(text: str) -> float:
    """``90``, ``90s``, ``10m``, ``2h``, ``1d`` -> seconds."""
    value = str(text or "").strip()
    match = _DURATION.match(value)
    if not match:
        raise WakeError(f"not a duration: {text!r} (use 30s, 10m, 2h or 1d)")
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * factor
    if seconds <= 0:
        raise WakeError(f"a duration must be positive: {text!r}")
    return seconds


def flag_name(token: str) -> Optional[str]:
    """The canonical flag a token starts with: ``--field=x`` -> ``--field``, ``-XPOST`` -> ``-X``.

    ``None`` for a bare ``-``/``--`` and for anything that is not a flag.
    """
    if not token.startswith("-") or token in ("-", "--"):
        return None
    if token.startswith("--"):
        return token.split("=", 1)[0]
    short = token[:2]
    return short if short in _SHORT_FLAGS else token


def validate_check(command: str) -> Tuple[str, ...]:
    """The argv of a check, or :class:`WakeError` saying why it is refused.

    Parsed with :mod:`shlex` and matched against :data:`ALLOWED_ARGV` argv-to-argv; every
    token that is a flag is then normalized, so an attached value cannot smuggle a write in
    (``-XPOST``, ``--field=body=x``, ``--output=/tmp/x``).
    """
    text = str(command or "").strip()
    if not text:
        raise WakeError("a check needs a command")
    bad = sorted({char for char in text if char in SHELL_METACHARACTERS})
    if bad:
        raise WakeError(f"the check contains shell metacharacters ({''.join(bad)}); "
                        "it is run without a shell, so it needs none")
    try:
        argv = tuple(shlex.split(text, posix=True))
    except ValueError as exc:
        raise WakeError(f"the check could not be parsed: {exc}") from None
    if not argv:
        raise WakeError("a check needs a command")
    if not any(argv[:len(prefix)] == prefix for prefix in ALLOWED_ARGV):
        allowed = ", ".join(" ".join(prefix) for prefix in ALLOWED_ARGV[:6])
        raise WakeError(f"the check must start with one of: {allowed}, … "
                        f"(got {' '.join(argv)})")
    for token in argv:
        flag = flag_name(token)
        if flag is None:
            continue
        if flag in WRITE_FLAGS:
            raise WakeError(f"the check asks for a write ({token}); a wake check is read-only")
        if argv[:2] == ("gh", "api") and flag not in GH_API_FLAGS:
            raise WakeError(
                f"gh api may only carry {', '.join(GH_API_FLAGS)} (got {token}); "
                "a wake check reads, it does not write")
    binary = argv[0]
    if shutil.which(binary) is None:
        raise WakeError(f"{binary} is not on PATH on this host")
    return argv


@dataclass(frozen=True)
class WakeSpec:
    """One parsed ``WAKE`` line."""

    argv: Tuple[str, ...]
    then: str
    in_seconds: float
    every_seconds: float
    until_seconds: float
    #: Whether ``until=`` was **written**, not merely derived: `parse_line` fills
    #: ``until_seconds`` from ``in=`` when the line names none, and a reader that refuses an
    #: explicit `until=` must not refuse every line (#213 review).
    until_explicit: bool = False

    @property
    def check(self) -> str:
        return " ".join(self.argv)


def parse_line(line: str, *, now: Optional[float] = None) -> WakeSpec:
    """One ``WAKE ...`` line -> :class:`WakeSpec`, or :class:`WakeError`."""
    text = str(line or "").strip()
    if not text.upper().startswith(WAKE_MARKER):
        raise WakeError(f"not a {WAKE_MARKER} line")
    rest = text[len(WAKE_MARKER):].strip()
    if len(rest) > MAX_LINE_CHARS:
        raise WakeError(f"a WAKE line must be at most {MAX_LINE_CHARS} characters")
    fields: Dict[str, str] = {}
    for match in _KV.finditer(rest):
        value = match.group(2)
        quoted = value[:1] in ("'", '"') and value[-1:] == value[:1]
        if quoted:
            value = value[1:-1]
        name = match.group(1)
        if name in ("check", "then") and not quoted:
            # `then=merge it if review holds` would parse as "merge" and silently drop the
            # rest, so the two free-text fields must be quoted (PR #92 review, S1).
            raise WakeError(f"{name}= must be quoted: {name}=\"…\"")
        fields[name] = value
    unknown = sorted(set(fields) - {"in", "every", "until", "check", "then"})
    if unknown:
        raise WakeError(f"unknown WAKE field(s): {', '.join(unknown)}")
    for required in ("in", "check"):
        if not fields.get(required):
            raise WakeError(f"a WAKE line needs {required}=…")
    if not fields.get("then"):
        raise WakeError("a WAKE line needs then=\"…\": what the bot should do when the "
                        "check says it is ready")
    argv = validate_check(fields["check"])
    in_seconds = parse_duration(fields["in"])
    every_seconds = parse_duration(fields["every"]) if fields.get("every") else 0.0
    until_seconds = (parse_duration(fields["until"]) if fields.get("until")
                     else max(in_seconds, every_seconds or in_seconds))
    if until_seconds < in_seconds:
        raise WakeError("until= must not be shorter than in=")
    return WakeSpec(argv=argv, then=fields["then"].strip(), in_seconds=in_seconds,
                    every_seconds=every_seconds, until_seconds=until_seconds,
                    until_explicit=bool(fields.get("until")))


def extract_wakes(text: str, *, now: Optional[float] = None
                  ) -> Tuple[str, List[WakeSpec], List[str]]:
    """``(text, specs, reasons)``: every ``WAKE`` line removed from *text*.

    A line is stripped in every case — a refused one must not be posted as prose — and every
    refusal comes back in *reasons* so the caller can say why.
    """
    if not text or WAKE_MARKER not in text.upper():
        return text, [], []
    specs: List[WakeSpec] = []
    reasons: List[str] = []
    kept: List[str] = []
    for line in text.splitlines():
        if not line.strip().upper().startswith(WAKE_MARKER):
            kept.append(line)
            continue
        try:
            specs.append(parse_line(line, now=now))
        except WakeError as exc:
            reasons.append(str(exc))
    return "\n".join(kept).strip(), specs, reasons


# ------------------------------------------------------------------------------------
# Timers
# ------------------------------------------------------------------------------------


_VOLATILE = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b(?:about |almost |over |less than )?(?:an?|\d+) "
               r"(?:second|minute|hour|day|week|month|year)s? ago\b", re.IGNORECASE),
    re.compile(r"\b(?:\d+h)?(?:\d+m)?\d+(?:\.\d+)?s\b|\b\d+h\d+m\b|\b\d+m\b"),
)


_NUMBER = re.compile(r"^#?(\d+)$")
_API_SUBJECT = re.compile(r"(?:^|/)repos/([^/]+/[^/]+)/(pulls|issues)/(\d+)(?:/|$)")


def subject_of(argv: Tuple[str, ...]) -> Optional[Tuple[str, str, str]]:
    """``("pr", "173", repo)`` or ``("issue", "120", repo)`` when a check is about one PR or
    issue, else None. ``repo`` is the ``-R/--repo`` value, empty for the checkout's own."""
    words = list(argv)
    if len(words) < 3 or words[0] != "gh":
        return None
    repo = ""
    rest: List[str] = []
    skip = False
    for index, word in enumerate(words[1:], start=1):
        if skip:
            skip = False
            continue
        if word in ("-R", "--repo") and index + 1 < len(words):
            repo, skip = words[index + 1], True
            continue
        if word.startswith("--repo="):
            repo = word.split("=", 1)[1]
            continue
        rest.append(word)
    if rest[:1] == ["api"]:
        for word in rest[1:]:
            match = _API_SUBJECT.search(word)
            if match:
                kind = "pr" if match.group(2) == "pulls" else "issue"
                return kind, match.group(3), match.group(1)
        return None
    if len(rest) >= 3 and rest[0] in ("pr", "issue") and rest[1] in (
            "view", "checks", "diff", "status"):
        for word in rest[2:]:
            if word.startswith("-"):
                continue
            match = _NUMBER.match(word)
            if match:
                return rest[0], match.group(1), repo
            break
    return None


def subject_state(subject: Tuple[str, str, str], *, cwd: Optional[Path],
                  timeout: float = 30.0) -> Optional[Tuple[str, str]]:
    """``(state, head)`` of a PR or issue (``("OPEN", "8356114…")``, ``("MERGED", …)``,
    ``("CLOSED", "")``), or None when it cannot be read. Read-only, one ``gh`` call."""
    kind, number, repo = subject
    fields = "state,headRefOid" if kind == "pr" else "state"
    argv: List[str] = ["gh", kind, "view", number, "--json", fields]
    if repo:
        argv += ["--repo", repo]
    if shutil.which("gh") is None:
        return None
    try:
        process = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                                 text=True, timeout=timeout, check=False,
                                 env=dict(os.environ, GH_PROMPT_DISABLED="1", LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if process.returncode != 0:
        return None
    try:
        data = json.loads(process.stdout or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
        return None
    return data["state"].upper(), str(data.get("headRefOid") or "")


async def subject_state_async(subject: Tuple[str, str, str], *, cwd: Optional[Path],
                              timeout: float = 30.0) -> Optional[Tuple[str, str]]:
    """:func:`subject_state`, off the event loop."""
    return await asyncio.to_thread(subject_state, subject, cwd=cwd, timeout=timeout)


# ------------------------------------------------------------------------------------
# Where the state went
# ------------------------------------------------------------------------------------

#: The directory a bot's state lives in. It held the WAKE timers until #215 retired them; it now
#: holds the expectations table, the head freeze and the round's wake budget, so nothing moves
#: on disk and an upgraded bot finds its state where it left it.
WAKE_RELPATH = WAKES_RELPATH


def wake_dir_for(config: Any) -> Optional[Path]:
    """The state directory a bot keeps its timers in (beside the gitbus ledger)."""
    from swarmly.telegram.poll import transport_state_root
    try:
        return transport_state_root(config) / ".agents" / "state"
    except Exception:                                  # noqa: BLE001 - optional
        return None

