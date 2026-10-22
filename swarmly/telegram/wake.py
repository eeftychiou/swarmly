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


class Claim(NamedTuple):
    """One claim on one firing, as the bus places it."""

    #: Sort key, ascending: earlier claims first (a higher arrival rank is a lower key).
    order: float
    #: When the claim became visible, when the transport can say (epoch seconds), else None.
    seen_at: Optional[float]
    alias: str


class WakePushError(WakeError):
    """The timer state was written here but the bus did not take it."""


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


def format_duration(seconds: float) -> str:
    for unit, factor in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= factor and seconds % factor == 0:
            return f"{int(seconds // factor)}{unit}"
    return f"{int(seconds)}s"


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


def classify(argv: Iterable[str], returncode: int) -> str:
    """``ready``, ``pending`` or ``error`` for *argv*'s exit code."""
    argv = tuple(argv)
    for prefix, meaning in EXIT_MEANING.items():
        if argv[:len(prefix)] == prefix:
            verdict = meaning.get(returncode)
            if verdict is None:
                return ERROR if returncode != 0 else READY
            return verdict
    return DEFAULT_MEANING.get(returncode, ERROR)


# ------------------------------------------------------------------------------------
# The line in the reply
# ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeSpec:
    """One parsed ``WAKE`` line."""

    argv: Tuple[str, ...]
    then: str
    in_seconds: float
    every_seconds: float
    until_seconds: float

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
                    every_seconds=every_seconds, until_seconds=until_seconds)


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


@dataclass
class WakeTimer:
    """A persisted timer. Times are absolute, so a restart resumes the schedule."""

    argv: Tuple[str, ...]
    then: str
    next_at: float
    expires_at: float
    every_seconds: float
    chat_id: int
    alias: str = ""
    thread_id: Optional[int] = None
    topic: str = ""
    id: str = ""
    created_at: float = 0.0
    attempts: int = 0
    fires: int = 0
    last_exit: Optional[int] = None
    last_tail: str = ""
    #: The turn a ready firing was about to start. Persisted *before* it starts, so a crash
    #: in that window replays the turn on the next tick instead of losing it.
    pending: str = ""
    #: How many times an interrupted turn has already been replayed.
    replays: int = 0
    #: armed | firing (turn written, not yet started) | done | expired | failed | cancelled
    state: str = "armed"
    #: The bot this copy was adopted from, empty for the bot's own timer. An adopted copy is
    #: a stand-in for a dead peer's promise, not one of this bot's own.
    adopted_from: str = ""

    def __post_init__(self) -> None:
        self.argv = tuple(self.argv)
        if not self.id:
            self.id = uuid.uuid4().hex[:8]
        if not self.created_at:
            self.created_at = time.time()

    @property
    def check(self) -> str:
        return " ".join(self.argv)

    @property
    def live(self) -> bool:
        return self.state in ("armed", "firing")

    @property
    def in_flight(self) -> bool:
        """A ready firing whose turn was written down but has not been started yet."""
        return self.state == "firing" and bool(self.pending)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "alias": self.alias, "argv": list(self.argv), "then": self.then,
            "next_at": self.next_at, "expires_at": self.expires_at,
            "every_seconds": self.every_seconds, "chat_id": self.chat_id,
            "thread_id": self.thread_id, "topic": self.topic, "created_at": self.created_at,
            "attempts": self.attempts, "fires": self.fires, "last_exit": self.last_exit,
            "last_tail": self.last_tail, "pending": self.pending, "replays": self.replays,
            "state": self.state, "adopted_from": self.adopted_from,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "WakeTimer":
        if not isinstance(data, dict):
            raise WakeError(f"a timer must be an object, got {type(data).__name__}")
        argv = data.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise WakeError("a timer needs a non-empty 'argv' list of strings")
        for name in ("then",):
            if not isinstance(data.get(name, ""), str):
                raise WakeError(f"a timer's {name!r} must be a string")
        for name in ("next_at", "expires_at", "every_seconds"):
            if not isinstance(data.get(name), (int, float)) or isinstance(
                    data.get(name), bool):
                raise WakeError(f"a timer's {name!r} must be a number")
        if not isinstance(data.get("chat_id"), int) or isinstance(data.get("chat_id"), bool):
            raise WakeError("a timer needs an integer 'chat_id'")
        return cls(
            argv=tuple(argv), then=str(data.get("then") or ""),
            next_at=float(data["next_at"]), expires_at=float(data["expires_at"]),
            every_seconds=float(data["every_seconds"]), chat_id=int(data["chat_id"]),
            alias=str(data.get("alias") or ""),
            thread_id=(int(data["thread_id"]) if isinstance(data.get("thread_id"), int)
                       and not isinstance(data.get("thread_id"), bool) else None),
            topic=str(data.get("topic") or ""), id=str(data.get("id") or ""),
            created_at=float(data.get("created_at") or 0.0),
            attempts=int(data.get("attempts") or 0), fires=int(data.get("fires") or 0),
            last_exit=(int(data["last_exit"]) if isinstance(data.get("last_exit"), int)
                       and not isinstance(data.get("last_exit"), bool) else None),
            last_tail=str(data.get("last_tail") or ""),
            pending=str(data.get("pending") or ""),
            replays=int(data.get("replays") or 0),
            state=str(data.get("state") or "armed"),
            adopted_from=str(data.get("adopted_from") or ""),
        )

    def ack(self, *, now: float) -> str:
        """One line for the topic: what will run, when, and in which checkout."""
        return (f"⏰ wake {self.id}: `{self.check}` at "
                f"{time.strftime('%H:%M:%SZ', time.gmtime(self.next_at))}"
                + (f", every {format_duration(self.every_seconds)}" if self.every_seconds else "")
                + f", until {time.strftime('%H:%M:%SZ', time.gmtime(self.expires_at))}")


def make_timer(spec: WakeSpec, *, alias: str, chat_id: int, thread_id: Optional[int],
               topic: str, now: float) -> WakeTimer:
    """The timer a parsed line becomes, with absolute times."""
    return WakeTimer(
        argv=spec.argv, then=spec.then, alias=alias,
        next_at=now + spec.in_seconds,
        expires_at=now + spec.until_seconds,
        every_seconds=spec.every_seconds, chat_id=chat_id, thread_id=thread_id, topic=topic,
        created_at=now,
    )


@dataclass
class WakeAction:
    """What the transport should do with a fired timer."""

    kind: str                      # fire | rearm | expire | error
    timer: WakeTimer
    reason: str = ""
    tail: str = ""


def decide(timer: WakeTimer, returncode: Optional[int], *, now: float,
           max_attempts: int = 0) -> WakeAction:
    """The action for a timer that is due, given its check's exit code (or ``None``)."""
    timer.attempts += 1
    if max_attempts and timer.attempts >= max_attempts:
        timer.state = "failed"
        return WakeAction("expire", timer,
                          reason=f"gave up after {max_attempts} attempts")
    if returncode is None:                       # timed out, or could not run at all
        timer.state = "failed"
        return WakeAction("error", timer, reason="the check did not finish")
    verdict = classify(timer.argv, returncode)
    if verdict == READY:
        # Not "done" yet: the turn text is written to the record first, so a crash between
        # here and the turn replays it once instead of losing it (PR #92 review, blocker 3).
        timer.state = "firing"
        timer.fires += 1
        return WakeAction("fire", timer)
    if verdict == PENDING and timer.every_seconds:
        nxt = now + timer.every_seconds
        if nxt > timer.expires_at:
            timer.state = "expired"
            return WakeAction("expire", timer,
                              reason="the wait expired before the check said ready")
        timer.next_at = nxt
        return WakeAction("rearm", timer)
    if verdict == PENDING:                        # pending, but nothing to re-arm on
        timer.state = "expired"
        return WakeAction("expire", timer, reason="still pending, and the timer has no every=")
    timer.state = "failed"
    return WakeAction("error", timer, reason=f"the check exited {returncode}")


def tail_of(text: str, lines: int = TAIL_LINES) -> str:
    """The last *lines* lines of a check's output."""
    rows = [row for row in str(text or "").splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


# ------------------------------------------------------------------------------------
# Running the check
# ------------------------------------------------------------------------------------


def run_check(argv: Tuple[str, ...], *, cwd: Optional[Path],
              timeout: float = CHECK_TIMEOUT_SECONDS) -> Tuple[Optional[int], str]:
    """Run one check in its own process group. ``(returncode, output_tail)``.

    ``None`` as the code means it timed out: the whole process group is killed, so a check
    that spawned children cannot outlive it (issue #81 review, should-fix 9).
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", LC_ALL="C")
    try:
        process = subprocess.Popen(
            list(argv), cwd=str(cwd) if cwd else None, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        )
    except OSError as exc:
        return None, f"could not run {argv[0]}: {exc}"
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        out, _ = process.communicate()
        return None, tail_of(f"(timed out after {timeout:.0f}s)\n{out or ''}")
    return process.returncode, tail_of(out or "")


def _kill_group(process: "subprocess.Popen[str]") -> None:
    """Kill the check and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, text=True, check=False)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:                                  # noqa: BLE001 - best effort
        logger.warning("wake: could not kill %s", process.pid, exc_info=True)
    try:
        process.kill()
    except Exception:                                  # noqa: BLE001
        pass


async def run_check_async(argv: Tuple[str, ...], *, cwd: Optional[Path],
                          timeout: float = CHECK_TIMEOUT_SECONDS) -> Tuple[Optional[int], str]:
    """One check, off the event loop."""
    return await asyncio.to_thread(run_check, argv, cwd=cwd, timeout=timeout)


# ------------------------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------------------------


class WakeStore:
    """The timers of one bot, persisted locally and claimed on the bus.

    The local file is where a running transport keeps its schedule; the bus carries the
    **fire claims** (append-only, one file per firing), which is what makes a takeover safe:
    two bots can see the same timer, and the claims decide who fires it.
    """

    def __init__(self, alias: str, *, state_dir: Optional[Path] = None,
                 transport: Any = None, root: Optional[Path] = None,
                 clock: Any = None):
        self.alias = alias
        safe = re.sub(r"[^a-z0-9_-]", "", alias.strip().lower()) or "agent"
        self.safe_alias = safe
        self.state_dir = Path(state_dir) if state_dir else None
        self.transport = transport
        self.root = Path(root or getattr(transport, "root", None) or ".")
        self.clock = clock or time.time
        #: firing -> when this bot's claim for it was *confirmed on the bus*. The settle window
        #: runs from here, not from when the file was written, so a slow push cannot let a peer
        #: fire while our claim is still in flight (PR #92 review, Codex/Reasonix).
        self._confirmed: Dict[int, float] = {}

    # -- local ----------------------------------------------------------------------
    @property
    def directory(self) -> Optional[Path]:
        """One file per timer, so two parallel turns never rewrite each other's schedule."""
        return (self.state_dir / f"wake-{self.safe_alias}") if self.state_dir else None

    @property
    def path(self) -> Optional[Path]:
        return self.directory

    def _timer_path(self, timer_id: str) -> Optional[Path]:
        directory = self.directory
        return (directory / f"{timer_id}.json") if directory else None

    def load(self) -> List[WakeTimer]:
        directory = self.directory
        if directory is None or not directory.is_dir():
            return []
        timers: List[WakeTimer] = []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                timers.append(WakeTimer.from_dict(data))
            except Exception as exc:                   # noqa: BLE001 - one bad file only
                logger.warning("wake: %s is not usable (%s); skipping it", path.name, exc)
        return timers

    def save(self, timers: Iterable[WakeTimer]) -> None:
        for timer in timers:
            self.write(timer)

    def write(self, timer: WakeTimer) -> None:
        path = self._timer_path(timer.id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:6]}")
        tmp.write_text(json.dumps(timer.to_dict(), indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def add(self, timer: WakeTimer, *, max_timers: int) -> bool:
        # The cap is on this bot's own promises: copies it holds for dead peers must not crowd
        # out the WAKE it needs to keep its own word (live, round 5: ten stale copies refused it).
        live = [row for row in self.load() if row.live and not row.adopted_from]
        if len(live) >= max_timers:
            raise WakeError(f"{len(live)} timers are already armed (limits.wake_max={max_timers})")
        self.write(timer)
        return self.publish_state(timer)

    def cancel(self, timer_id: str) -> bool:
        found = False
        for timer in self.load():
            if timer.live and timer.id.lower().startswith(timer_id.lower()):
                timer.state = "cancelled"
                self.write(timer)
                self.publish_state(timer)
                found = True
        return found

    def due(self, now: Optional[float] = None) -> List[WakeTimer]:
        moment = now if now is not None else self.clock()
        return sorted((timer for timer in self.load() if timer.live and timer.next_at <= moment),
                      key=lambda timer: timer.next_at)

    def replace(self, timer: WakeTimer) -> None:
        """Persist a timer, and publish its state when it is not simply "armed".

        A failed publication raises :class:`WakePushError`: a transition that never reached the
        bus leaves a stale `armed` record behind, which a successor would adopt and re-fire.
        """
        previous = next((row for row in self.load() if row.id == timer.id), None)
        self.write(timer)
        moved = previous is not None and previous.next_at != timer.next_at
        if (previous is None or previous.state != timer.state or moved) or not timer.live:
            # A re-arm moves next_at while staying "armed": without publishing it, a successor
            # adopts the old schedule and the timer is stranded.
            self.publish_state(timer)

    def pending_firings(self) -> List[WakeTimer]:
        """Timers whose turn was written but not started: replayed once after a restart."""
        return [timer for timer in self.load() if timer.in_flight]

    def publish_state(self, timer: WakeTimer) -> bool:
        """Record this timer's *state* on the bus, so a successor knows what it inherits.

        One `<id>-state.json` per timer per alias, overwritten on every transition: a
        successor reads the newest state for an id and adopts it only if it is still armed.
        Without this a claim (which carries the pre-firing snapshot) would resurrect a
        finished timer, once per firing it ever had (PR #92 review, B2).
        """
        transport = self.transport
        if transport is None or not getattr(transport, "ref", None):
            return True                    # no bus: the checkout is the plane
        directory = self.root / WAKE_RELPATH / self.safe_alias
        directory.mkdir(parents=True, exist_ok=True)
        relpath = f"{WAKE_RELPATH}/{self.safe_alias}/{timer.id}-state.json"
        if not is_bus_document(relpath):
            raise WakeError(f"refusing to write outside {WAKE_RELPATH}: {relpath!r}")
        payload = timer.to_dict()
        payload["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        (self.root / relpath).write_text(json.dumps(payload), encoding="utf-8")
        if hasattr(transport, "commit_and_push"):
            try:
                transport.commit_and_push(f"wakes: {timer.id} {timer.state}", paths=[relpath])
            except Exception as exc:                   # noqa: BLE001 - reported, not swallowed
                logger.warning("wake: state %s was written but not pushed: %s", relpath, exc)
                raise WakePushError(
                    f"the timer state is local only: {exc}") from exc
        return True

    def adopt(self, *, max_timers: int, allow: Iterable[str] = (),
              skip: Iterable[str] = ()) -> List[WakeTimer]:
        """Take over armed timers this checkout does not have, from the bus state records.

        A state record carries the whole timer, so a successor can rebuild a dead bot's
        schedule instead of watching the promise die with it. Adopted timers are **untrusted
        input**: anyone who can push to the bus can write one, so the argv is put through the
        same allowlist a WAKE line goes through, and through ``allow`` (the bot's
        ``limits.wake_check_allow``) as well, before it is written or run (PR #92 review,
        ``adopt`` blocker).
        """
        allowed_binaries = {str(word) for word in allow}
        alive = {str(name).strip().lower() for name in skip}
        transport = self.transport
        if transport is None or not getattr(transport, "ref", None):
            return []
        self.refresh()
        try:
            names = transport.list_at_ref(WAKE_RELPATH)
        except Exception:                              # noqa: BLE001 - adoption is optional
            return []
        if not names:
            return []
        mine = {timer.id for timer in self.load()}
        # Only state records are adopted, and only the newest one per timer: a firing claim
        # is an audit record of a moment, not the timer's current state.
        newest: Dict[str, Tuple[str, WakeTimer]] = {}
        for name in names:
            base = name.rsplit("/", 1)[-1]
            if not base.endswith("-state.json"):
                continue
            owner = name.split("/", 1)[0]
            if owner == self.safe_alias or owner.lower() in alive:
                continue
            try:
                payload = json.loads(transport.read_at_ref(f"{WAKE_RELPATH}/{name}") or "{}")
                timer = WakeTimer.from_dict(payload)
            except Exception:                          # noqa: BLE001 - one bad record, skip
                continue
            stamp = str(payload.get("at") or "")
            current = newest.get(timer.id)
            if current is None or (stamp, owner) > (current[0], current[1].alias.lower()):
                newest[timer.id] = (stamp, timer)
        taken: List[WakeTimer] = []
        for timer_id in sorted(newest):
            timer = newest[timer_id][1]
            if not timer.live or timer_id in mine or timer.alias == self.alias \
                    or timer.alias.strip().lower() in alive:
                continue                      # finished, ours, or its owner is still up
            try:
                checked = validate_check(timer.check)
            except WakeError as exc:
                logger.warning("wake: adopted timer %s refused: %s", timer_id, exc)
                continue
            if allowed_binaries and checked[0] not in allowed_binaries:
                logger.warning("wake: adopted timer %s refused: %s is not in "
                               "limits.wake_check_allow", timer_id, checked[0])
                continue
            timer.argv = checked
            if len(taken) + self.live_count >= max_timers:
                break
            timer.adopted_from = timer.adopted_from or timer.alias or owner
            timer.alias = self.alias          # this bot now holds the promise
            self.write(timer)
            taken.append(timer)
        return taken

    @property
    def live_count(self) -> int:
        return len([timer for timer in self.load() if timer.live])

    # -- bus claims -----------------------------------------------------------------
    @staticmethod
    def firing_of(timer: WakeTimer) -> int:
        """The firing this claim is about: the time the timer is due, agreed by every bot.

        Keyed on the *time*, not on a local attempt counter: two bots holding the same timer
        would count differently, and a stale claim from an earlier firing must not stop the
        next one (PR #92 review, blocker 2).
        """
        return int(timer.next_at)

    def refresh(self) -> bool:
        """Fetch the bus ref, so a claim written seconds ago by a peer is visible.

        Without this the claim reads whatever the last poll fetched (30 minutes by default)
        and two bots can both believe they are alone (PR #92 review, R1). ``False`` means the
        ref is stale, and a claim must not be arbitrated on it.
        """
        transport = self.transport
        if transport is None or not getattr(transport, "ref", None):
            return True
        if not hasattr(transport, "refresh"):
            return True
        try:
            result = transport.refresh()
        except Exception:                              # noqa: BLE001 - a failed fetch is a no
            logger.warning("wake: the bus ref could not be refreshed", exc_info=True)
            return False
        return result is not False

    def _claim_records(self, timer_id: str, firing: int) -> Optional[List[Claim]]:
        """Every claim on this firing, ordered by when the **bus** got it, or ``None``.

        Order comes from the bus, never from the claim's own contents: a stamp inside the file
        is written before its own push, and so is a commit time, so either could carry an
        earlier claim than the one it overtook. The transport's arrival rank (how many commits
        landed after the one that added the file) is what a writer cannot forge. Where the
        transport cannot say -- a folder bus, an in-memory one -- the file's own stamp is the
        fallback, and the lowest alias breaks ties.
        """
        transport = self.transport
        if transport is None or not getattr(transport, "ref", None):
            return []
        try:
            names = transport.list_at_ref(WAKE_RELPATH)
        except Exception:                              # noqa: BLE001 - reported, not swallowed
            logger.warning("wake: the claim list could not be read", exc_info=True)
            return None
        if names is None:
            return None
        prefix = f"{timer_id}-{firing}-"
        claims: List[Claim] = []
        for name in names:
            base = name.rsplit("/", 1)[-1]
            if not (base.startswith(prefix) and base.endswith(".json")):
                continue
            alias = base[len(prefix):-len(".json")]
            full = f"{WAKE_RELPATH}/{name}"
            arrival = getattr(transport, "arrival_rank", None)
            if arrival is not None:
                # This bus *can* say where a claim sits, so a claim it cannot place is not a
                # claim: demoting it to the stamp inside the file would put the sender-set key
                # back in charge, which is the double fire this exists to prevent.
                try:
                    rank = arrival(full)
                except Exception:                      # noqa: BLE001 - unranked: not a claim
                    logger.warning("wake: %s could not be ranked; not counted", name,
                                   exc_info=True)
                    continue
                if rank is None:
                    logger.warning("wake: %s has no place in the bus history; not counted", name)
                    continue
                claims.append(Claim(order=-float(rank), seen_at=None, alias=alias))
                continue
            stamp = 0.0
            reader = getattr(transport, "read_at_ref", None)
            if reader is not None:
                try:
                    payload = json.loads(reader(full) or "{}")
                    stamp = float(payload.get("written_at") or 0.0)
                except Exception:                      # noqa: BLE001 - no usable time
                    stamp = 0.0
            if stamp <= 0:
                continue
            claims.append(Claim(order=stamp, seen_at=stamp, alias=alias))
        return sorted(claims)

    def _claims(self, timer_id: str, firing: int) -> Optional[List[str]]:
        """Aliases that have claimed this firing, first written first."""
        records = self._claim_records(timer_id, firing)
        return None if records is None else [claim.alias for claim in records]

    def claim(self, timer: WakeTimer, *, settle_seconds: float = 0.0) -> bool:
        """True when this bot may fire now. See :meth:`claim_state` for the why."""
        return self.claim_state(timer, settle_seconds=settle_seconds) == "won"

    def claim_state(self, timer: WakeTimer, *, settle_seconds: float = 0.0) -> str:
        """Claim this firing: ``won`` (fire now), ``wait`` (ours is written, let it settle),
        ``taken`` (another bot's claim holds it) or ``lost`` (the bus could not be read or
        written, so nobody here can say).

        The claim is an append-only file, one per timer per firing, and the winner is the
        lowest alias among the claims *for that firing*. Two pushes can overlap, so a claim
        is not honoured the moment it is written: with a settle window, the writer fires on a
        later tick, by which time both claims are visible and every bot folds the same
        winner. A claim that cannot be read or pushed never fires.
        """
        transport = self.transport
        if transport is None or not getattr(transport, "ref", None):
            return "won"                      # no bus to race on: this transport is alone
        firing = self.firing_of(timer)
        if not self.refresh():                # a peer's claim from a minute ago must be visible
            return "lost"                     # a stale ref is a reason not to fire
        before = self._claim_records(timer.id, firing)
        if before is None:                    # a broken bus is not "I am the only one"
            return "lost"
        mine = any(claim.alias == self.safe_alias for claim in before)
        if not mine:
            # Someone claimed it before we did: the earliest claim holds the firing.
            if before:
                return "taken"
            stamp = self._write_claim(timer, firing)
            if stamp is None:
                return "lost"                 # it did not reach the bus: do not fire on it
            self._confirmed[firing] = stamp   # the window starts when the push returns
            if settle_seconds > 0:
                return "wait"                 # confirmed now; fire once peers can see it
        elif settle_seconds > 0:
            # The window runs from when the *bus* first saw our claim, which a restart keeps
            # (the local confirmation is only a fallback for a bus that cannot say).
            seen = next((claim.seen_at for claim in before
                         if claim.alias == self.safe_alias), None)
            confirmed = seen if seen is not None else self._confirmed.get(firing)
            if confirmed is None:
                # A restart (or another process): wait one more window rather than firing on a
                # claim whose visibility we cannot vouch for.
                self._confirmed[firing] = self.clock()
                return "wait"
            if (self.clock() - confirmed) < settle_seconds:
                return "wait"                 # not visible to peers for long enough yet
        if not self.refresh():                # the post-read must come from a fresh ref too
            return "lost"
        after = self._claim_records(timer.id, firing)
        if after is None or not any(claim.alias == self.safe_alias for claim in after):
            # The post-read must show this bot's own claim: an empty list means the bus did
            # not confirm it, not that this bot won.
            return "lost"
        if len(after) == 1:
            return "won"                      # only ours is on the bus: it holds the firing
        # Several are there: the one the bus got first holds the firing, ties by lowest alias.
        # A claim that arrives after that decision finds one already there and loses.
        return "won" if after[0].alias == self.safe_alias else "taken"

    def _write_claim(self, timer: WakeTimer, firing: int) -> Optional[float]:
        """Write this bot's claim for one firing, and push it once.

        Nothing in the file decides who wins: the bus's history does (see
        :meth:`_claim_records`). That is the only way to close the last window — a stamp is
        written before its own push, so a slow push would carry an earlier time than the write
        it overtook (PR #92 review, B4).
        """
        directory = self.root / WAKE_RELPATH / self.safe_alias
        directory.mkdir(parents=True, exist_ok=True)
        relpath = f"{WAKE_RELPATH}/{self.safe_alias}/{timer.id}-{firing}-{self.safe_alias}.json"
        if not is_bus_document(relpath):
            raise WakeError(f"refusing to write outside {WAKE_RELPATH}: {relpath!r}")
        stamp = self.clock()
        (self.root / relpath).write_text(json.dumps(
            {"id": timer.id, "alias": self.safe_alias, "firing": firing,
             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
             # Advisory only: claims are ordered by when the bus saw them, not by this.
             "written_at": stamp, "check": timer.check, "timer": timer.to_dict()}),
            encoding="utf-8")
        transport = self.transport
        if transport is not None and hasattr(transport, "commit_and_push"):
            try:
                transport.commit_and_push(f"wakes: {timer.id} firing {firing}",
                                          paths=[relpath])
            except Exception as exc:                   # noqa: BLE001 - never fire on it
                logger.warning("wake: claim %s was written but not pushed: %s", relpath, exc)
                return None
        return stamp


#: Where the fire claims live on the bus (see ``swarmly.transports.base``).
WAKE_RELPATH = WAKES_RELPATH


def wake_dir_for(config: Any) -> Optional[Path]:
    """The state directory a bot keeps its timers in (beside the gitbus ledger)."""
    from swarmly.telegram.poll import transport_state_root
    try:
        return transport_state_root(config) / ".agents" / "state"
    except Exception:                                  # noqa: BLE001 - optional
        return None

