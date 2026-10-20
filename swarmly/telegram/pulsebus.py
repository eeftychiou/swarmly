"""The durable half of the heartbeat: a pulse on the bot's own git ref (issue #82).

The design review settled the shape, and the round-4 review settled the storage:

* the pulse must be **written on the interval**, must **survive a restart** and must **skip
  Telegram while the bus is on** -- so it cannot live only in a process's memory;
* but a commit on the bus branch every two minutes is ~180 commits an hour into the project's
  history, forever, every one a push race with the real bus traffic.

So each bot **force-updates its own ref**, ``refs/pulse/<alias>``, to a **parentless** commit
carrying one blob. The ref is a mailbox, not a history: no parents, no growth on the bus
branch, no race with anybody (the only writer is its owner), and a reader fetches exactly the
refs it needs. ``main`` never learns a pulse happened.

Telegram keeps the fallback role it had: when the bus is off -- or this beat's push failed --
the pulse goes out as the ephemeral ``STATUS intent=PULSE`` line instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from swarmly.telegram.presence import Pulse

logger = logging.getLogger("swarmly.telegram.pulsebus")

#: Where a pulse lives: one ref per bot, holding one file.
PULSE_REF_PREFIX = "refs/pulse/"
PULSE_PATH = "pulse.json"
#: A git call in the heartbeat path must never hang the pulse loop.
GIT_TIMEOUT_SECONDS = 30.0
#: How many times a push may lose to the remote's tip before the beat falls back to Telegram.
PUSH_RETRIES = 2


class PulseBus:
    """Publishes this bot's pulse to ``refs/pulse/<alias>`` and reads everybody else's."""

    def __init__(self, root: Path, alias: str, *, remote: str = "origin",
                 timeout: float = GIT_TIMEOUT_SECONDS,
                 runner: Optional[Callable[..., Any]] = None):
        self.root = Path(root)
        self.alias = alias
        self.remote = remote
        self.timeout = timeout
        self._run = runner or subprocess.run

    # -- plumbing ----------------------------------------------------------------------
    def ref(self, alias: Optional[str] = None) -> str:
        return f"{PULSE_REF_PREFIX}{self._safe(alias or self.alias)}"

    @staticmethod
    def _safe(alias: str) -> str:
        """A git-ref-safe form of an alias (refs cannot hold spaces or a few odd characters)."""
        cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in alias.strip())
        return cleaned.strip("._") or "agent"

    def available(self) -> bool:
        """Whether there is a git checkout to hold pulses at all."""
        try:
            return (self.root / ".git").exists()
        except OSError:
            return False

    def _git(self, *args: str, stdin: Optional[bytes] = None) -> subprocess.CompletedProcess:
        """Run git with **bytes** stdin, deliberately.

        Text mode would translate the newlines in the objects we hand git, and on Windows
        that writes a carriage return into a tree entry's file name -- the kind of bug one OS
        finds and the other ships.
        """
        return self._run(["git", "-C", str(self.root), *args], input=stdin, capture_output=True,
                         timeout=self.timeout, check=False)

    @staticmethod
    def _text(stream: Any) -> str:
        if stream is None:
            return ""
        return stream.decode("utf-8", "replace") if isinstance(stream, bytes) else str(stream)

    def _ok(self, *args: str, stdin: Optional[bytes] = None) -> Optional[str]:
        """Run one git command; its stdout when it succeeded, else None (logged)."""
        try:
            result = self._git(*args, stdin=stdin)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("pulsebus: git %s failed: %s", args[0], exc)
            return None
        if result.returncode != 0:
            logger.warning("pulsebus: git %s exited %s: %s", args[0], result.returncode,
                           self._text(result.stderr).strip()[:200])
            return None
        return self._text(result.stdout)

    # -- writing -----------------------------------------------------------------------
    def publish(self, pulse: Pulse) -> bool:
        """Write *pulse* to this bot's ref and push it. False means "use the Telegram line".

        The commit is parentless: the previous pulse is replaced, not appended to, so the ref
        never grows a history and the push never conflicts with anybody.
        """
        if not self.available():
            return False
        body = json.dumps(pulse.to_dict(), sort_keys=True).encode("utf-8")
        blob = self._ok("hash-object", "-w", "--stdin", stdin=body)
        if not blob:
            return False
        # `-z`: NUL-terminated entries, so no newline translation can reach a name.
        entry = f"100644 blob {blob.strip()}\t{PULSE_PATH}"
        tree = self._ok("mktree", "-z", stdin=(entry + "\0").encode("utf-8"))
        if not tree:
            return False
        commit = self._ok("commit-tree", tree.strip(), "-m", f"pulse {self.alias}")
        if not commit:
            return False
        ref = self.ref()
        if self._ok("update-ref", ref, commit.strip()) is None:
            return False
        # The push is the part the swarm can see. A lost race is retried; anything else means
        # this beat goes out on Telegram instead (the fallback the acceptance criteria allow).
        for attempt in range(PUSH_RETRIES):
            try:
                pushed = self._git("push", "--quiet", "--force", self.remote,
                                   f"{ref}:{ref}")
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("pulsebus: push of %s failed: %s", ref, exc)
                continue
            if pushed.returncode == 0:
                return True
            logger.warning("pulsebus: push of %s failed (attempt %d): %s", ref, attempt + 1,
                           self._text(pushed.stderr).strip()[:200])
        return False

    # -- reading -----------------------------------------------------------------------
    def _fetch(self) -> bool:
        """Fetch every bot's pulse ref. ``False`` means the refs may be stale or missing."""
        try:
            result = self._git("fetch", "--quiet", self.remote,
                               f"+{PULSE_REF_PREFIX}*:{PULSE_REF_PREFIX}*")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("pulsebus: fetch failed: %s", exc)
            return False
        if result.returncode != 0:
            # No pulse refs upstream yet is the ordinary first-run answer, not an error.
            logger.debug("pulsebus: no pulse refs fetched: %s",
                         self._text(result.stderr).strip()[:200])
            return False
        return True

    def read(self) -> Dict[str, Dict[str, Any]]:
        """Everybody's pulse, keyed by alias, as last published.

        A ref that cannot be read is skipped: one unreadable pulse must not cost the swarm the
        rest of its liveness. `Pulse.from_dict` decides what a well-formed pulse is.
        """
        if not self.available():
            return {}
        self._fetch()
        listing = self._ok("for-each-ref", "--format=%(refname)", PULSE_REF_PREFIX)
        if listing is None:
            return {}
        pulses: Dict[str, Dict[str, Any]] = {}
        for line in listing.splitlines():
            ref = line.strip()
            if not ref:
                continue
            body = self._ok("show", f"{ref}:{PULSE_PATH}")
            if not body:
                continue
            try:
                pulse = Pulse.from_dict(json.loads(body))
            except (ValueError, TypeError):
                logger.warning("pulsebus: unreadable pulse on %s; skipping it", ref)
                continue
            pulses[pulse.alias] = pulse.to_dict()
        return pulses

    def aliases(self) -> List[str]:
        """Which refs exist right now (for a diagnostic line)."""
        listing = self._ok("for-each-ref", "--format=%(refname)", PULSE_REF_PREFIX) or ""
        return [ref[len(PULSE_REF_PREFIX):] for ref in listing.split()
                if ref.startswith(PULSE_REF_PREFIX)]

    def describe(self) -> str:
        return f"pulse refs at {self.root} ({PULSE_REF_PREFIX}*)"

    def __repr__(self) -> str:                       # pragma: no cover - debugging aid
        return f"PulseBus({self.root!s}, alias={self.alias!r})"


def notes_from(pulses: Dict[str, Any], table: Any, *, now: float,
               skip: Iterable[str] = ()) -> int:
    """Feed read pulses into a :class:`~swarmly.telegram.presence.Presence` table.

    `seen_at` is *now*, not the pulse's own stamp, for the same reason a Telegram pulse uses the
    receiver's clock: a peer's clock is not evidence about the time. Returns how many counted.
    """
    skip_lower = {str(name).strip().lower() for name in skip}
    counted = 0
    for alias, record in pulses.items():
        if alias.strip().lower() in skip_lower:
            continue
        try:
            pulse = Pulse.from_dict(record)
        except (ValueError, TypeError):
            continue
        table.note(pulse, seen_at=now)
        counted += 1
    return counted
