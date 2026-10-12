"""The governor: the only write path into the swarm.

``post`` builds the whole envelope (seq, prev, ts, epoch), validates it against the
protocol and the current projection, signs it with the agent's own key and appends it to
the agent's lane. A rejected post raises :class:`Rejected` with every reason and the
moves that are legal instead, so an agent that has forgotten the protocol is told what
it may do rather than silently coerced.

``guard`` checks a shell command or a file path against the safety list before an agent
runs it, and allows it only with a live owner approval for exactly that action.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import keys
from .envelope import GENESIS, ID_RE, Envelope, EnvelopeError, now_iso, parse_ts
from .lanes import LaneConflict, LaneStore
from .projection import Projection
from .protocol import Swarm, SwarmConfigError

log = logging.getLogger("swarmly.swarm.governor")

WRITE_RE = re.compile(
    r"(>>?|\btee\b|\bsed\s+(-\w*\s+)*-i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bchmod\b|\bln\b|"
    r"\bgit\s+(add|rm|mv|commit|checkout|restore)\b|\bpatch\b|\bapply\b|\bdd\b|\binstall\b)")


class Rejected(Exception):
    """The governor refused a post. ``errors`` says why; ``legal`` says what is allowed."""

    def __init__(self, errors: List[str], legal: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.legal = legal or []

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "errors": self.errors, "legal_moves": self.legal}


@dataclass
class GuardResult:
    allowed: bool
    hits: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "hits": self.hits}

    def message(self) -> str:
        if self.allowed:
            approved = [h for h in self.hits if h.get("approved_by")]
            if approved:
                return "allowed by owner approval " + ", ".join(h["approved_by"] for h in approved)
            return "allowed"
        lines = []
        for h in self.hits:
            if h.get("approved_by"):
                continue
            lines.append(f"blocked: {h['summary']} ({h['category']}) needs the owner's approval.")
            lines.append(f"  ask: swarmly post REQUEST_ACTION --body category={h['category']} "
                         f"--body action={shlex.quote(h['action'])} --body reason='<why>'")
        return "\n".join(lines)


class Governor:
    def __init__(self, swarm: Swarm, store: LaneStore, agent: str, *,
                 clock: Optional[Callable[[], str]] = None,
                 doorbell: Optional[Callable[[Envelope], Any]] = None) -> None:
        if not agent:
            raise SwarmConfigError("no agent identity: pass --as <agent> or set A2A_AGENT")
        swarm.agent(agent)  # raises when not in the roster
        self.swarm = swarm
        self.store = store
        self.agent = agent
        self.clock = clock or now_iso
        #: Rung after every append (see doorbell.py). It never decides anything.
        self.doorbell = doorbell
        self._priv: Any = None

    # -- identity ---------------------------------------------------------------------

    def private_key(self) -> Any:
        if self._priv is None:
            priv = keys.load_private(self.agent)
            roster = self.swarm.public_key(self.agent)
            if keys.public_key_b64(priv) != roster:
                raise SwarmConfigError(
                    f"the key in {keys.key_path(self.agent)} does not match {self.agent}'s "
                    "public key in swarm.yaml")
            self._priv = priv
        return self._priv

    # -- reading ----------------------------------------------------------------------

    def view(self, *, sync: bool = True) -> Projection:
        if sync:
            self.store.sync()
        lanes, problems = self.store.read_all_checked()
        return Projection.build(self.swarm, lanes, problems)

    def next(self, proj: Optional[Projection] = None) -> Dict[str, Any]:
        proj = proj or self.view()
        role = self.swarm.role_of(self.agent)
        out: Dict[str, Any] = {
            "agent": self.agent, "role": role,
            "obligations": proj.obligations(self.agent),
            "legal_moves": proj.legal_moves(self.agent),
            "my_tasks": [t.id for t in proj.tasks.values() if t.claimant == self.agent
                         and t.state not in self.swarm.protocol.terminal_states],
        }
        own = [v.__dict__ for v in proj.violations if v.lane == self.agent]
        if own:
            out["my_quarantined"] = own
        if role in ("owner", "planner"):
            out["escalations"] = proj.escalations()
        return out

    # -- writing ----------------------------------------------------------------------

    def draft(self, proj: Projection, intent: str, *, to: Optional[Iterable[str]] = None,
              task_id: Optional[str] = None, body: Optional[Dict[str, Any]] = None,
              evidence: Optional[List[Dict[str, str]]] = None, re: Optional[str] = None,
              context_id: Optional[str] = None, epoch: Optional[int] = None) -> Envelope:
        # Build on the verified tip of our own lane, never on unverified files.
        tip = proj.tips.get(self.agent)
        rule = self.swarm.protocol.intents.get(intent)
        task = proj.tasks.get(task_id) if task_id else None
        if epoch is None and rule is not None and task_id:
            if "next_epoch" in rule.checks:
                epoch = (task.epoch if task else 0) + 1
            elif task is not None:
                epoch = task.epoch
        # Causal context: the last replayed envelope of every other lane.
        seen = {lane: seq for lane, seq in proj.processed.items() if lane != self.agent}
        floor = [proj.verified[f"{lane}:{seq}"].ts for lane, seq in seen.items()]
        if tip:
            floor.append(tip.ts)
        ts = self.clock()
        latest = max(floor) if floor else ""
        if ts <= latest:
            # Never earlier than anything we saw, even when this host's clock is behind.
            ts = (parse_ts(latest) + timedelta(microseconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")
        if context_id is None:
            context_id = task.context_id if task else (task_id or "swarm")
        env = Envelope(lane=self.agent, seq=(tip.seq + 1) if tip else 1,
                       prev=tip.digest() if tip else GENESIS, ts=ts, sender=self.agent,
                       to=list(to) if to else ["*"], intent=intent, context_id=context_id,
                       task_id=task_id, epoch=epoch, re=re, body=dict(body or {}),
                       evidence=list(evidence or []), seen=seen)
        return env

    def check(self, proj: Projection, env: Envelope) -> List[str]:
        try:
            env.check_shape()
        except EnvelopeError as exc:
            return [str(exc)]
        if env.re is not None and not ID_RE.match(env.re):
            # New posts only; older lanes may hold free text here and still replay.
            return [f"re must be an envelope id (lane:seq), not {env.re!r}"]
        return proj.validate(env)

    def post(self, intent: str, *, retries: int = 3, **fields: Any) -> Envelope:
        """Validate, sign and append. Raises :class:`Rejected` or :class:`LaneConflict`."""
        priv = self.private_key()
        last: Optional[Exception] = None
        for _ in range(max(1, retries)):
            proj = self.view()
            env = self.draft(proj, intent, **fields)
            errors = self.check(proj, env)
            if errors:
                # Rejections are how protocol drift shows up: keep every one.
                log.warning("REJECTED %s from %s (task %s, re %s): %s; body %s", intent,
                            self.agent, env.task_id, env.re, "; ".join(errors), env.body)
                raise Rejected(errors, proj.legal_moves(self.agent))
            env.sign_with(priv)
            try:
                own = proj.tips.get(self.agent)
                self.store.append(env, expected_prev=own.digest() if own else GENESIS)
                log.info("posted %s %s (task %s, epoch %s, re %s)", env.id, intent,
                         env.task_id, env.epoch, env.re)
                log.debug("posted envelope:\n%s", env.to_json())
            except LaneConflict as exc:
                log.warning("lane conflict on %s %s, retrying: %s", env.id, intent, exc)
                last = exc
                continue
            self._ring(env)
            return env
        raise LaneConflict(f"could not append after {retries} tries: {last}")

    def _ring(self, env: Envelope) -> None:
        if self.doorbell is None:
            return
        try:
            self.doorbell(env)
        except Exception:                              # noqa: BLE001 - the post stands
            log.warning("doorbell failed for %s", env.id, exc_info=True)

    # -- safety -----------------------------------------------------------------------

    def safety_hits(self, command: Optional[str] = None, paths: Iterable[str] = (),
                    *, writes: bool = True) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        protocol = self.swarm.protocol
        forms = command_forms(command) if command else []
        for cat in protocol.safety:
            if command and any(cat.matches_command(f) for f in forms):
                hits.append({"category": cat.id, "summary": cat.summary, "action": command})
                continue
            if command and cat.paths:
                tokens = _tokens(command)
                if any(cat.matches_path(t) for t in tokens) and \
                        (cat.touch == "any" or WRITE_RE.search(command)):
                    hits.append({"category": cat.id, "summary": cat.summary,
                                 "action": command})
                    continue
            for path in paths:
                if cat.matches_path(path) and (cat.touch == "any" or writes):
                    hits.append({"category": cat.id, "summary": cat.summary, "action": path})
                    break
        return hits

    def guard(self, command: Optional[str] = None, paths: Iterable[str] = (), *,
              writes: bool = True, sync: bool = True) -> GuardResult:
        """Allow ``command``/``paths`` unless they hit the safety list without approval.

        Fails closed: when the lanes cannot be read, a hit stays blocked.
        """
        hits = self.safety_hits(command, list(paths), writes=writes)
        if not hits:
            log.debug("guard allowed %r %s", command, list(paths))
            return GuardResult(True)
        result = self._guard_hits(hits, sync)
        log.log(logging.INFO if result.allowed else logging.WARNING, "guard %s for %s: %s",
                "ALLOWED" if result.allowed else "BLOCKED", self.agent,
                [(h["category"], h["action"], h.get("approved_by") or h.get("error"))
                 for h in hits])
        return result

    def _guard_hits(self, hits: List[Dict[str, Any]], sync: bool) -> GuardResult:
        try:
            proj = self.view(sync=sync)
        except Exception:  # noqa: BLE001 - unreadable bus: fail closed
            try:
                proj = self.view(sync=False)
            except Exception:  # noqa: BLE001
                return GuardResult(False, hits)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for hit in hits:
            req = proj.approved(self.agent, hit["category"], action=hit["action"], at=now)
            if req:
                hit["approved_by"] = req
        if not all(h.get("approved_by") for h in hits):
            return GuardResult(False, hits)
        # Use each approval up before allowing the action; if that fails, block.
        for req_id in sorted({h["approved_by"] for h in hits}):
            try:
                self.post("ACTION_TAKEN", re=req_id,
                          body={"action": proj.by_id[req_id].body.get("action")})
            except Exception as exc:  # noqa: BLE001 - fail closed
                for h in hits:
                    h.pop("approved_by", None)
                    h["error"] = f"could not use approval {req_id}: {exc}"
                return GuardResult(False, hits)
        return GuardResult(True, hits)


# Words that run the rest of the line as a command.
_WRAPPERS = {"sudo", "doas", "command", "exec", "nohup", "time", "nice", "builtin", "xargs"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
# git's global options, and which of them take a separate value.
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                   "--super-prefix", "--config-env", "--list-cmds"}
_SEGMENT_RE = re.compile(r"\|\||&&|[;|&\n]|\$\(|`|\)")


def command_forms(command: str, _depth: int = 0) -> List[str]:
    """The raw command plus a normalized form of each simple command inside it.

    Normalizing removes what would let a regex miss the real intent: wrappers such as
    ``sudo`` and ``env X=1``, git's global options (``git -C . push`` is ``git push``),
    split ``rm`` flags (``rm -v -r`` is ``rm -r``) and ``sh -c '...'`` bodies. This is a
    speed bump for forgetful agents, not a sandbox against hostile ones.
    """
    forms = [command]
    for segment in _SEGMENT_RE.split(command):
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        while words and (words[0] in _WRAPPERS or words[0] == "env"
                         or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])):
            if words[0] == "env":
                words = words[1:]
                while words and (words[0].startswith("-") or "=" in words[0]):
                    words = words[1:]
                continue
            words = words[1:]
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        if prog in _SHELLS and _depth < 3:
            for i, w in enumerate(words[1:], 1):
                if w.startswith("-") and "c" in w.lstrip("-") and i + 1 < len(words):
                    forms.extend(command_forms(words[i + 1], _depth + 1))
                    break
            continue
        if prog == "git":
            rest = words[1:]
            while rest and rest[0].startswith("-"):
                opt = rest[0]
                rest = rest[1:]
                if opt in _GIT_VALUE_OPTS and rest:
                    rest = rest[1:]
            forms.append(" ".join(["git", *rest]))
        elif prog == "rm":
            flags = [w for w in words[1:] if w.startswith("-") and w != "-"]
            args = [w for w in words[1:] if not w.startswith("-") or w == "-"]
            recursive = any(f == "--recursive" or (not f.startswith("--") and
                                                   ("r" in f or "R" in f)) for f in flags)
            forms.append(" ".join(["rm", *(["-r"] if recursive else []), *args]))
        else:
            forms.append(" ".join([prog, *words[1:]]))
    return forms


def _tokens(command: str) -> List[str]:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    out = []
    for part in parts:
        # `>file`, `--out=file` and `a=b` style arguments carry paths too.
        for piece in re.split(r"[=<>|;&]", part):
            if piece:
                out.append(piece)
    return out
