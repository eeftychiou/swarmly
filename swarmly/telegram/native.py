"""Native CLI passthrough: ``/cli@bot COMMAND``, answered in code, no model turn (issue #116).

Swarmly should not reimplement every command each CLI already has. The admin sends
``/cli@claudebot /cost`` and the CLI's own answer comes back -- run as a subprocess or typed
into a short-lived interactive session, never as a prompt, so it costs no tokens at all.

Four rules shape this module.

**Nothing is guessed.** A command runs only if it is on the allowlist for *that* runtime.
Anything else -- a typo, a command nobody wrote down, a vendor's new one -- is refused with the
reason, because "run it and see" on a CLI that can also write files is not a passthrough.

**Three allowlist groups, because they fail differently.** Read-only commands run. Commands
that change something (``/clear``) need the explicit ``/cli@bot!`` form: the admin typed a ``!``
and meant it. Commands that would call the model (``/compact``, ``/review``, ``/init``, custom
commands) are refused outright -- spending tokens from a path that exists to spend none is the
one thing this feature must never do. Credentials (``/login``, ``/logout``, anything that moves
a token) are refused whatever else is true.

**Two mechanisms, one outcome.** One-shot subcommands are a plain subprocess. Interactive slash
commands need a terminal: the CLI resumes the discussion's own session, gets the command
written to it, and exits. No prompt is ever sent; a picker prints its options and the selection
stays with swarmly's ``/model`` and ``/reasoning``.

**Bounded.** Every run has a timeout and a character cap, the output is stripped of terminal
escapes and then redacted, and a run can be killed.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: The command runs as it is: it only reads.
RUN = "run"
#: It changes something: the admin has to say so with ``/cli@bot!``.
CONFIRM = "confirm"
#: It will not run, for the reason given.
REFUSE = "refuse"

#: A one-shot subprocess (``--version``, ``mcp list``).
EXEC = "exec"
#: A short-lived interactive session that gets one slash command typed into it.
SLASH = "slash"

#: A command with one of these words in it moves or tests a credential. Never passed through,
#: on any runtime, in any form: a bot that can run ``login`` can be made to authenticate as
#: whoever is talking to it.
CREDENTIAL_WORDS = frozenset({"login", "logout", "auth", "token", "tokens", "key", "keys",
                              "credential", "credentials", "oauth", "apikey", "api-key"})

#: Terminal escapes (CSI, OSC and the two-character forms) and the C0 controls that are not
#: whitespace: what a captured screen is made of and what a Telegram message must not be.
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: The CLI binary for each adapter type. Anything not here has no passthrough at all.
BINARY = {
    "claude": "claude", "claude_thread": "claude",
    "codex": "codex", "codex_thread": "codex",
    "antigravity": "agy", "antigravity_thread": "agy",
    "reasonix": "reasonix", "reasonix_thread": "reasonix",
    "hermes": "hermes",
}

#: How each runtime resumes the discussion's session for an interactive run. These mirror what
#: the runtime's own adapter passes on a turn (``claude_thread`` adds ``--resume`` itself, and
#: the others keep their session in the CLI's own store), so a passthrough lands in the same
#: conversation the turn does rather than starting a fresh one.
RESUME_FLAGS: Dict[str, Tuple[str, ...]] = {
    "claude": ("--resume", "{session}"),
    "codex": ("resume", "{session}"),
    "antigravity": ("--resume", "{session}"),
    "reasonix": ("--resume", "{session}"),
}


@dataclass(frozen=True)
class Native:
    """What one ``/cli`` request comes to, before anything runs."""

    kind: str                      # run | confirm | refuse
    mode: str = EXEC               # exec | slash
    argv: Tuple[str, ...] = ()     # what is spawned
    line: str = ""                 # what is typed into the session (slash only)
    reason: str = ""               # why a refusal, or what a confirm would do

    @property
    def runs(self) -> bool:
        return self.kind == RUN

    def describe(self) -> str:
        """One line for the channel, saying what will (or will not) happen."""
        command = " ".join(self.argv[1:]) if self.mode == EXEC else self.line
        if self.kind == REFUSE:
            return f"/cli refused: {self.reason}"
        if self.kind == CONFIRM:
            return (f"/cli {command} changes something ({self.reason}). "
                    f"Send `/cli! {command}` to allow it.")
        how = "one-shot" if self.mode == EXEC else "in the CLI's own session"
        return f"/cli {command} — running {how}, no model turn."


# ------------------------------------------------------------------------------------
# The allowlists
# ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Allowlist:
    """One runtime's commands, in the three groups the issue asks for."""

    one_shot: Tuple[Tuple[str, ...], ...] = ()      # (tokens) run as a subprocess
    read: Tuple[str, ...] = ()                      # slash commands that only read
    changes: Tuple[str, ...] = ()                   # slash commands that change something
    model: Tuple[str, ...] = ()                     # slash commands that would call the model

    def find_one_shot(self, tokens: Sequence[str]) -> bool:
        return tuple(tokens) in self.one_shot

    def find_slash(self, command: str) -> Optional[str]:
        if command in self.read:
            return RUN
        if command in self.changes:
            return CONFIRM
        if command in self.model:
            return REFUSE
        return None


ALLOWLISTS: Dict[str, Allowlist] = {
    "claude": Allowlist(
        one_shot=(("--version",), ("-v",), ("--help",), ("doctor",),
                  ("mcp", "list"), ("config", "list")),
        read=("/cost", "/context", "/status", "/model", "/mcp", "/agents", "/permissions",
              "/doctor", "/help"),
        changes=("/clear",),
        model=("/compact", "/review", "/init"),
    ),
    "codex": Allowlist(
        one_shot=(("--version",), ("-v",), ("--help",), ("login", "status"),
                  ("mcp", "list")),
        read=("/status", "/mcp", "/model", "/cost", "/help"),
        changes=("/new",),
        model=("/init", "/review", "/compact"),
    ),
    "antigravity": Allowlist(
        one_shot=(("--version",), ("-v",), ("--help",), ("models",)),
        read=("/status", "/model", "/help"),
        changes=("/clear",),
        model=("/init", "/review"),
    ),
    "reasonix": Allowlist(
        one_shot=(("--version",), ("-v",), ("--help",), ("models",)),
        read=("/status", "/model", "/context", "/cost", "/help"),
        changes=("/clear", "/new"),
        model=("/compact", "/review", "/init"),
    ),
    "hermes": Allowlist(
        one_shot=(("--version",), ("-v",), ("--help",)),
        read=("/status", "/help"),
        changes=(),
        model=(),
    ),
}


def binary_for(adapter_type: str, *, command: Optional[str] = None) -> Optional[str]:
    """The CLI binary this bot's adapter runs: its configured command's first word, else the
    runtime's own binary. ``None`` when the adapter has no CLI (echo, a custom script)."""
    if command:
        try:
            tokens = shlex.split(command, posix=(os.name != "nt"))
        except ValueError:
            tokens = []
        if tokens:
            return tokens[0]
    return BINARY.get(str(adapter_type or "").strip().lower())


def allowlist_for(adapter_type: str) -> Optional[Allowlist]:
    kind = str(adapter_type or "").strip().lower()
    return ALLOWLISTS.get(kind) or ALLOWLISTS.get(kind.replace("_thread", ""))


def plan(adapter_type: str, text: str, *, confirmed: bool = False,
         command: Optional[str] = None, session: Optional[str] = None) -> Native:
    """What ``/cli`` *text* comes to on this runtime. Never runs anything."""
    binary = binary_for(adapter_type, command=command)
    table = allowlist_for(adapter_type)
    if binary is None or table is None:
        return Native(REFUSE, reason="this bot has no CLI to pass a command to")
    raw = str(text or "").strip()
    if not raw:
        return Native(REFUSE, reason="say which command, e.g. `/cli /cost`")
    try:
        tokens = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError as exc:
        return Native(REFUSE, reason=f"that command could not be parsed ({exc})")
    if not tokens:
        return Native(REFUSE, reason="say which command, e.g. `/cli /cost`")
    lowered = [token.strip().lower() for token in tokens]
    if len(tokens) > 8:
        return Native(REFUSE, reason="that command has too many words to be a native command")

    def credential(word: str) -> bool:
        return word.strip().lstrip("/-").lower() in CREDENTIAL_WORDS

    if tokens[0].startswith("/"):
        name = tokens[0].lower()
        if len(tokens) > 1:
            return Native(REFUSE, reason=f"{name} does not take arguments here")
        if credential(name):
            return Native(REFUSE, reason="credentials are never passed through to the CLI")
        verdict = table.find_slash(name)
        if verdict is None:
            return Native(REFUSE, reason=f"{name} is not on the allowlist for this CLI")
        if verdict == REFUSE:
            return Native(REFUSE, reason=f"{name} would call the model; that costs tokens")
        argv = _session_argv(binary, adapter_type, session)
        if verdict == CONFIRM and not confirmed:
            return Native(CONFIRM, mode=SLASH, argv=argv, line=name,
                          reason=f"{name} changes the session")
        return Native(RUN, mode=SLASH, argv=argv, line=name)

    if table.find_one_shot(tokens):
        # An allowlisted one-shot wins over the credential rule: `login status` is a read that
        # says whether this CLI is logged in, and the issue asks for it by name. Nothing that
        # *changes* credentials is ever allowlisted, so this cannot open the door.
        return Native(RUN, mode=EXEC, argv=(binary, *tokens))
    if any(credential(word) for word in lowered):
        return Native(REFUSE, reason="credentials are never passed through to the CLI")
    return Native(REFUSE, reason=f"`{' '.join(tokens)}` is not on the allowlist "
                                 f"for this CLI")


def _session_argv(binary: str, adapter_type: str, session: Optional[str]) -> Tuple[str, ...]:
    """The interactive CLI, resuming the discussion's session when one is known."""
    kind = str(adapter_type or "").strip().lower().replace("_thread", "")
    flags = RESUME_FLAGS.get(kind, ())
    if not flags or not session:
        return (binary,)
    return (binary,) + tuple(part.replace("{session}", session) for part in flags)


# ------------------------------------------------------------------------------------
# Cleaning what came back
# ------------------------------------------------------------------------------------

@dataclass
class Outcome:
    """What one passthrough produced."""

    ok: bool
    text: str
    exit_code: Optional[int] = None
    timed_out: bool = False
    truncated: bool = False
    #: ``/stop`` ended it: distinct from a timeout, which is the bot's own clock.
    killed: bool = False

    def line(self, native: Native, seconds: float) -> str:
        """The one-line verdict the channel posts above the output."""
        command = native.line if native.mode == SLASH else " ".join(native.argv[1:])
        if self.killed:
            return f"/cli {command}: stopped on request"
        if self.timed_out:
            return f"/cli {command}: killed after {seconds:.0f}s"
        code = "" if self.exit_code is None else f" (exit {self.exit_code})"
        tail = ", output truncated" if self.truncated else ""
        return f"/cli {command}: {'ok' if self.ok else 'failed'}{code}{tail}"


def clean(text: str, *, max_chars: int = 3500) -> Tuple[str, bool]:
    """Strip terminal escapes and controls, and cap the size. ``(text, truncated)``."""
    body = _ESCAPES.sub("", str(text or ""))
    body = _CONTROLS.sub("", body).replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if max_chars and len(body) > max_chars:
        return body[:max_chars].rstrip() + "\n… (truncated)", True
    return body, False


# ------------------------------------------------------------------------------------
# Running it
# ------------------------------------------------------------------------------------

class SessionError(RuntimeError):
    """An interactive session could not be opened or produced nothing."""


class InteractiveSession:
    """A short-lived interactive CLI: type one line, read the screen, exit.

    It is a terminal where the host has one (POSIX ``pty``), and a plain pipe where it does
    not (Windows without a ConPTY binding). Both are bounded the same way -- one write, read
    until quiet or the deadline, then terminate -- and neither ever sends a prompt.
    """

    def __init__(self, argv: Sequence[str], *, cwd: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None, quiet_seconds: float = 1.0,
                 deadline: float = 20.0) -> None:
        self.argv = tuple(argv)
        self.cwd = cwd
        self.env = env
        self.quiet = float(quiet_seconds)
        self.deadline = float(deadline)
        self._proc: Optional[subprocess.Popen] = None
        self._master: Optional[int] = None
        self._buf: List[str] = []
        self._lock = threading.Lock()
        self._eof = threading.Event()

    # -- opening -------------------------------------------------------------------
    def open(self) -> "InteractiveSession":
        try:
            import pty                                        # POSIX only
        except ImportError:                                   # pragma: no cover - Windows
            pty = None                                        # type: ignore[assignment]
        if pty is not None:
            master, slave = pty.openpty()
            self._master = master
            self._proc = subprocess.Popen(
                list(self.argv), cwd=self.cwd, env=self.env, stdin=slave, stdout=slave,
                stderr=slave, close_fds=True)
            os.close(slave)
        else:                                                 # a pipe session: still no prompt
            self._proc = subprocess.Popen(
                list(self.argv), cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._read_loop, daemon=True).start()
        return self

    # -- reading -------------------------------------------------------------------
    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None:                                      # pragma: no cover - defensive
            return
        try:
            if self._master is not None:
                while True:
                    try:
                        chunk = os.read(self._master, 4096)
                    except OSError:                       # the slave side closed: the CLI exited
                        break
                    if not chunk:
                        break
                    with self._lock:
                        self._buf.append(chunk.decode("utf-8", "replace"))
            else:
                stream = proc.stdout
                assert stream is not None
                for line in stream:
                    with self._lock:
                        self._buf.append(line)
        finally:
            self._eof.set()

    def text(self) -> str:
        with self._lock:
            return "".join(self._buf)

    # -- driving it ----------------------------------------------------------------
    def send(self, line: str) -> None:
        proc = self._proc
        if proc is None:
            raise SessionError("the session is not open")
        payload = (line.rstrip("\n") + "\r") if self._master is not None \
            else (line.rstrip("\n") + "\n")
        try:
            if self._master is not None:
                os.write(self._master, payload.encode("utf-8"))
            elif proc.stdin is not None:
                proc.stdin.write(payload)
                proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise SessionError(f"the session would not take the command: {exc}") from exc

    def read_until_quiet(self, *, deadline: Optional[float] = None) -> str:
        """Read until the CLI has said nothing for ``quiet_seconds``, or the deadline."""
        limit = time.monotonic() + (self.deadline if deadline is None else float(deadline))
        last_len, last_change = -1, time.monotonic()
        while time.monotonic() < limit:
            size = len(self.text())
            if size != last_len:
                last_len, last_change = size, time.monotonic()
            elif self._eof.is_set() or (time.monotonic() - last_change) >= self.quiet:
                break
            time.sleep(0.05)
        return self.text()

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:              # pragma: no cover - stubborn CLI
                    proc.kill()
                    proc.wait(timeout=3)
        finally:
            if self._master is not None:
                try:
                    os.close(self._master)
                except OSError:                                # pragma: no cover
                    pass
                self._master = None
            self._proc = None


def _end(process: "subprocess.Popen[str]") -> None:
    """End a child process, without waiting on a pipe it may still hold."""
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
                process.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):     # pragma: no cover - stubborn
                pass


def sanitized_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The host environment minus the variables a runtime must never inherit.

    The same denylist a turn's subprocess gets (``adapters.base.SECRET_ENV``), so a
    passthrough cannot hand a bot token to a CLI that would happily print it.
    """
    from swarmly.telegram.adapters.base import is_secret_env

    env = {name: value for name, value in os.environ.items() if not is_secret_env(name)}
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GH_PROMPT_DISABLED", "1")
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


class Runner:
    """One passthrough run, with the timeout, the cap and the kill the issue asks for."""

    def __init__(self, *, cwd: Optional[str] = None, timeout: float = 20.0,
                 max_chars: int = 3500, spawn: Optional[Callable[..., Any]] = None,
                 env: Optional[Dict[str, str]] = None,
                 session_factory: Optional[Callable[..., InteractiveSession]] = None,
                 redact: Optional[Callable[[str], str]] = None,
                 quiet_seconds: float = 1.0) -> None:
        self.cwd = cwd
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)
        #: How a one-shot command is started: ``subprocess.Popen``, injectable for tests. It is
        #: a *process*, not ``subprocess.run``, because `/stop` has to be able to end one.
        self._spawn = spawn or subprocess.Popen
        self.env = env if env is not None else sanitized_env()
        self.session_factory = session_factory or InteractiveSession
        self.redact = redact
        self.quiet_seconds = quiet_seconds
        #: The session of the run in flight, so `/stop` can end it.
        self.session: Optional[InteractiveSession] = None
        #: The one-shot process of the run in flight, for the same reason. `/stop` has to be
        #: able to end a subprocess too: a command that hangs (a CLI waiting on a lock, a
        #: network read) is exactly the one somebody stops.
        self.process: Optional[subprocess.Popen] = None
        #: Set by `/stop`, read by both mechanisms. A stop can land in the window between "the
        #: run is registered" and "the process exists", and it must still end the command rather
        #: than be missed by a few microseconds.
        self.killed = False
        self.running = False
        #: Set by the channel before the run starts, so a `/stop` that lands in the window
        #: between "this run exists" and "run() has begun" is honoured rather than missed.
        self.armed = False
        self._lock = threading.Lock()

    def arm(self) -> "Runner":
        """Announce that a run is about to start, so a stop cannot fall in the gap."""
        with self._lock:
            self.armed = True
            self.killed = False
        return self

    # -- the two mechanisms ---------------------------------------------------------
    def _exec(self, native: Native) -> Outcome:
        try:
            process = self._spawn(
                list(native.argv), cwd=self.cwd, env=self.env, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            return Outcome(False, str(exc))
        with self._lock:
            self.process = process
            killed_early = self.killed
        if killed_early:                             # `/stop` beat the spawn
            _end(process)
            return Outcome(False, "", exit_code=process.returncode, killed=True)
        try:
            try:
                out, err = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                _end(process)
                out, err = process.communicate()
                return Outcome(False, "", timed_out=True)
            if self.killed:
                return Outcome(False, "", exit_code=process.returncode, killed=True)
        finally:
            with self._lock:
                self.process = None
        body = "\n".join(part for part in (out, err) if part)
        text, truncated = clean(body, max_chars=self.max_chars)
        return Outcome(process.returncode == 0, text, exit_code=process.returncode,
                       truncated=truncated)

    def _slash(self, native: Native) -> Outcome:
        try:
            session = self.session_factory(native.argv, cwd=self.cwd, env=self.env,
                                           quiet_seconds=self.quiet_seconds,
                                           deadline=self.timeout)
            with self._lock:
                self.session = session
            session.open()
            with self._lock:
                killed_early = self.killed
            if killed_early:                         # `/stop` beat the session
                return Outcome(False, "", killed=True)
            session.send(native.line)
            body = session.read_until_quiet()
        except (SessionError, OSError) as exc:
            return Outcome(False, str(exc))
        except subprocess.TimeoutExpired:
            return Outcome(False, "", timed_out=True)
        finally:
            with self._lock:
                session, self.session = self.session, None
            if session is not None:
                session.close()
        text, truncated = clean(body, max_chars=self.max_chars)
        return Outcome(True, text, truncated=truncated)

    def stop(self) -> bool:
        """Kill the run in flight, whichever mechanism it is using.

        ``False`` only when nothing was running: a one-shot subprocess is as stoppable as an
        interactive session, so a hanging ``--version`` is not something the admin has to wait
        out.
        """
        with self._lock:
            session, process = self.session, self.process
            waiting = self.armed or self.running
            if session is None and process is None and not waiting:
                return False
            self.killed = True
        if session is not None:
            session.close()
        if process is not None:
            _end(process)
        return True

    def run(self, native: Native) -> Outcome:
        """Run an allowlisted plan. A plan that is not ``run`` is never executed."""
        if not native.runs:
            return Outcome(False, native.describe())
        with self._lock:
            if self.killed:                          # stopped before it began
                self.armed = False
                return Outcome(False, "", killed=True)
            self.running = True
        try:
            outcome = self._exec(native) if native.mode == EXEC else self._slash(native)
        finally:
            with self._lock:
                self.session = None
                self.process = None
                self.running = False
                self.armed = False
        if self.redact is not None:
            outcome.text = self.redact(outcome.text or "")
        return outcome


def available(adapter_type: str, *, command: Optional[str] = None) -> str:
    """Why a passthrough is not possible on this host, or ``""`` when it is."""
    binary = binary_for(adapter_type, command=command)
    if binary is None:
        return "this bot has no CLI to pass a command to"
    if shutil.which(binary) is None:
        return f"`{binary}` is not installed on this host"
    return ""


def parse_request(text: str) -> Optional[Tuple[bool, str]]:
    """``(confirmed, command)`` for ``/cli[!] COMMAND``, or ``None`` when it is not a request.

    The ``!`` is the whole difference between "read that" and "change that", so it is read
    from the command word itself rather than from a second argument nobody remembers.
    """
    match = re.match(r"^/cli(?P<bang>!)?(?:@(?P<target>[A-Za-z0-9_]+))?\s*(?P<rest>.*)$",
                     str(text or "").strip(), re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return bool(match.group("bang")), (match.group("rest") or "").strip()


def target_of(text: str) -> Optional[str]:
    """The bot a ``/cli@bot`` request names, if any."""
    match = re.match(r"^/cli!?(?:@(?P<target>[A-Za-z0-9_]+))?", str(text or "").strip(),
                     re.IGNORECASE)
    return match.group("target") if match else None


def commands_for(adapter_type: str) -> List[str]:
    """Every command this runtime allows, for the usage line."""
    table = allowlist_for(adapter_type)
    if table is None:
        return []
    return [" ".join(tokens) for tokens in table.one_shot] + list(table.read) \
        + list(table.changes)
