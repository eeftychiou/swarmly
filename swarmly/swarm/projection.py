"""Fold every lane into one view: task states, disputes, obligations, legal moves, escalations.

The projection is deterministic. It checks each lane's signatures and hash chain, merges
the lanes in ``(ts, lane, seq)`` order and replays every envelope through the same rules
the governor applies on write. Anything that fails is quarantined and listed in
``violations``; it never changes state. The governor uses the same code to validate a
draft, so a write is legal exactly when the replay would accept it.
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .envelope import GENESIS, SHA_RE, Envelope, parse_ts
from .protocol import IntentRule, Swarm

log = logging.getLogger("swarmly.swarm.projection")

# Intents that show the agent is alive but not that work is moving.
NOT_PROGRESS = {"HELLO", "BYE", "HEARTBEAT"}
VERDICTS = ("approve", "changes_requested")


@dataclass
class Task:
    id: str
    title: str
    context_id: str
    state: str
    created_by: str
    created_at: str
    claimant: Optional[str] = None
    epoch: int = 0
    offered_to: Optional[str] = None
    failures: int = 0
    head_sha: Optional[str] = None
    branch: Optional[str] = None
    tokens: int = 0
    reason: Optional[str] = None
    merge_sha: Optional[str] = None
    reviews: List[Dict[str, Any]] = field(default_factory=list)
    history: List[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Dispute:
    id: str                      # the root OBJECTION's envelope id
    target: Optional[str]        # the envelope objected to
    target_author: Optional[str]
    task_id: Optional[str]
    opened_by: str
    opened_at: str
    rounds: int = 1
    last_speaker: str = ""
    ruling: Optional[str] = None
    appealed: Optional[str] = None
    final: bool = False
    parties: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.__dict__)
        data["parties"] = sorted(self.parties)
        return data


@dataclass
class Violation:
    id: str
    lane: str
    errors: List[str]


class Projection:
    def __init__(self, swarm: Swarm) -> None:
        self.swarm = swarm
        self.protocol = swarm.protocol
        self.tasks: Dict[str, Task] = {}
        self.disputes: Dict[str, Dispute] = {}
        self.dispute_of: Dict[str, str] = {}     # envelope id in a dispute -> root
        self.dispute_by_target: Dict[str, str] = {}  # envelope objected to -> root
        self.by_id: Dict[str, Envelope] = {}
        self.log: List[Envelope] = []
        self.tips: Dict[str, Envelope] = {}        # last chain-verified envelope per lane
        self.processed: Dict[str, int] = {}        # last replayed seq per lane
        self.verified: Dict[str, Envelope] = {}    # every chain-verified envelope by id
        self.queries: Dict[str, Envelope] = {}
        self.escalated: Dict[str, Envelope] = {}
        self.decisions: Dict[str, Envelope] = {}
        self.requests: Dict[str, Envelope] = {}
        self.approvals: Dict[str, Envelope] = {}
        self.used: Dict[str, Envelope] = {}
        self.last_seen: Dict[str, str] = {}
        self.last_progress: Optional[str] = None
        self.violations: List[Violation] = []

    # -- building ---------------------------------------------------------------------

    @classmethod
    def build(cls, swarm: Swarm, lanes: Dict[str, List[Envelope]],
              problems: Iterable[Tuple[str, str]] = ()) -> "Projection":
        """Replay every lane in causal order.

        An envelope comes after its own lane's previous envelope and after every
        envelope it lists in ``seen``, and its ts may not be earlier than any of those.
        Envelopes that are concurrent (neither saw the other) are ordered by
        ``(ts, lane, seq)``. So an agent cannot win a race it had already seen by
        backdating its ts.
        """
        proj = cls(swarm)
        for lane, problem in problems:
            proj.violations.append(Violation(f"{lane}:*", lane, [problem]))
        chains = {lane: proj._check_lane(lane, envs) for lane, envs in sorted(lanes.items())}
        avail: Dict[Tuple[str, int], Envelope] = {}
        for lane, envs in chains.items():
            for env in envs:
                avail[(lane, env.seq)] = env
                proj.verified[env.id] = env
            if envs:
                proj.tips[lane] = envs[-1]
        waiting: Dict[Tuple[str, int], List[Envelope]] = {}
        missing: Dict[str, int] = {}
        heap: List[Tuple[str, str, int, Envelope]] = []
        for (lane, seq), env in avail.items():
            deps = [(lane, seq - 1)] if seq > 1 else []
            # A seen envelope this reader lacks (not synced yet, or quarantined) imposes
            # no order: one damaged lane must not take down everyone who read it.
            deps += [(dl, ds) for dl, ds in env.seen.items() if (dl, ds) in avail]
            missing[env.id] = len(deps)
            for dep in deps:
                waiting.setdefault(dep, []).append(env)
            if not deps:
                heapq.heappush(heap, (env.ts, lane, seq, env))
        done: Set[str] = set()
        while heap:
            _, lane, seq, env = heapq.heappop(heap)
            done.add(env.id)
            proj.processed[lane] = seq
            late = [f"{dl}:{ds}" for dl, ds in env.seen.items()
                    if (dl, ds) in avail and avail[(dl, ds)].ts > env.ts]
            errors = [f"ts is earlier than {', '.join(late)}, which it had seen"] if late \
                else proj.validate(env)
            if errors:
                proj.violations.append(Violation(env.id, env.lane, errors))
            else:
                proj.apply(env)
            for nxt in waiting.get((lane, seq), []):
                missing[nxt.id] -= 1
                if missing[nxt.id] == 0:
                    heapq.heappush(heap, (nxt.ts, nxt.lane, nxt.seq, nxt))
        for (lane, seq), env in sorted(avail.items()):
            if env.id not in done:
                proj.violations.append(Violation(env.id, lane, [
                    "its causal history forms a cycle"]))
        log.debug("replayed %d envelopes from %d lanes; %d quarantined", len(proj.log),
                  len(chains), len(proj.violations))
        for v in proj.violations:
            log.debug("quarantined %s: %s", v.id, "; ".join(v.errors))
        return proj

    def _check_lane(self, lane: str, envs: List[Envelope]) -> List[Envelope]:
        """Return the trusted prefix of a lane; quarantine everything after a break."""
        good: List[Envelope] = []
        prev, last_ts = GENESIS, ""
        key = self.swarm.public_key(lane)
        for i, env in enumerate(envs):
            err = None
            if lane not in self.swarm.agents:
                err = f"lane {lane!r} is not in the roster"
            elif env.lane != lane or env.sender != lane:
                err = f"envelope from {env.sender!r} sits in lane {lane!r}"
            elif env.seq != i + 1:
                err = f"seq {env.seq} where {i + 1} was expected"
            elif env.prev != prev:
                err = "prev does not match the previous envelope's digest"
            elif env.ts < last_ts:
                err = "ts goes backwards within the lane"
            elif not env.verify(key):
                err = "signature does not verify against the roster key"
            if err:
                for rest in envs[i:]:
                    self.violations.append(Violation(rest.id, lane, [err if rest is env else
                                                     f"after a broken chain at {env.id}"]))
                break
            good.append(env)
            prev, last_ts = env.digest(), env.ts
        return good

    # -- validation -------------------------------------------------------------------

    def rule(self, intent: str) -> Optional[IntentRule]:
        return self.protocol.intents.get(intent)

    def validate(self, env: Envelope, *, structural: bool = False) -> List[str]:
        """Every reason ``env`` is illegal now. ``structural`` skips body, evidence and re."""
        errors: List[str] = []
        rule = self.rule(env.intent)
        if rule is None:
            return [f"unknown intent {env.intent!r}; legal intents: "
                    f"{', '.join(sorted(self.protocol.intents))}"]
        if env.sender not in self.swarm.agents:
            return [f"{env.sender!r} is not in the swarm roster"]
        role = self.swarm.role_of(env.sender)
        if not rule.allows_role(role):
            errors.append(f"{env.intent} is for {'/'.join(rule.roles)}; {env.sender} is {role}")
        for rcpt in env.to:
            if rcpt != "*" and rcpt not in self.swarm.agents:
                errors.append(f"recipient {rcpt!r} is not in the roster")
        task = self.tasks.get(env.task_id) if env.task_id else None
        if rule.task == "required" and not env.task_id:
            errors.append(f"{env.intent} needs a task_id")
        if rule.task == "forbidden" and env.task_id:
            errors.append(f"{env.intent} does not take a task_id")
        if env.task_id and rule.task != "forbidden":
            if rule.from_states is not None:
                state = task.state if task else None
                if state not in rule.from_states:
                    if task is None:
                        errors.append(f"unknown task {env.task_id!r}")
                    elif None in rule.from_states and len(rule.from_states) == 1:
                        errors.append(f"task {env.task_id!r} already exists")
                    else:
                        errors.append(f"task {env.task_id} is {state}; {env.intent} needs "
                                      f"{' or '.join(str(s) for s in rule.from_states)}")
            elif task is None:
                errors.append(f"unknown task {env.task_id!r}")
        if not structural:
            for name in rule.body:
                value = env.body.get(name)
                if value is None or (isinstance(value, str) and not value.strip()) \
                        or value == [] or value == {}:
                    errors.append(f"{env.intent} needs body.{name}")
            if len(env.evidence) < rule.evidence:
                errors.append(f"{env.intent} needs at least {rule.evidence} evidence item(s)")
            for item in env.evidence:
                if item.get("kind") not in self.protocol.evidence_kinds:
                    errors.append(f"evidence kind {item.get('kind')!r} is not one of "
                                  f"{', '.join(self.protocol.evidence_kinds)}")
            if rule.re:
                target = self.by_id.get(env.re or "")
                if target is None:
                    errors.append(f"{env.intent} must answer a {rule.re} (re: <envelope id>)")
                elif target.intent != rule.re:
                    errors.append(f"{env.intent} answers a {rule.re}; {env.re} is a "
                                  f"{target.intent}")
        for check in rule.checks:
            if structural and check in _BODY_CHECKS:
                continue
            msg = getattr(self, f"_check_{check}")(env, task)
            if msg:
                errors.append(msg)
        return errors

    # Each check returns an error string or None.

    def _check_named_recipient(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if all(r == "*" for r in env.to):
            return f"{env.intent} must name its recipient(s)"
        return None

    def _check_offer_target(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if task and task.state == "offered" and task.offered_to \
                and task.offered_to != env.sender:
            return f"task {task.id} is offered to {task.offered_to}"
        return None

    def _check_next_epoch(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        want = (task.epoch if task else 0) + 1
        if env.epoch != want:
            return f"CLAIM must carry epoch {want} (got {env.epoch}); someone claimed first"
        return None

    def _check_claimant(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if task and task.claimant != env.sender:
            return f"only the claimant ({task.claimant or 'nobody'}) may send {env.intent}"
        return None

    def _check_not_claimant(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if task and task.claimant == env.sender:
            return "the claimant cannot review their own work"
        return None

    def _check_cross_vendor(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if task and task.claimant:
            mine = self.swarm.agent(env.sender).vendor
            theirs = self.swarm.agent(task.claimant).vendor
            if mine == theirs:
                return f"a reviewer must be from a different vendor than {task.claimant} ({mine})"
        return None

    def _check_current_epoch(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if task and env.epoch != task.epoch:
            return f"stale epoch {env.epoch}; the task is at epoch {task.epoch}"
        return None

    def _check_sha(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        for key in ("head_sha", "merge_sha"):
            if key in env.body and not SHA_RE.match(str(env.body[key])):
                return f"body.{key} must be a hex commit SHA"
        return None

    def _check_reviewed_head(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if task and env.body.get("head_sha") != task.head_sha:
            return f"the review is for {env.body.get('head_sha')}; READY was {task.head_sha}"
        return None

    def _check_verdict(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if env.body.get("verdict") not in VERDICTS:
            return f"body.verdict must be one of {', '.join(VERDICTS)}"
        return None

    def _check_budget(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        budget = int(self.protocol.policy.get("token_budget_per_task") or 0)
        if not task or budget <= 0:
            return None
        try:
            spent = task.tokens + int(env.body.get("tokens") or 0)
        except (TypeError, ValueError):
            return "body.tokens must be an integer"
        if spent > budget and not self.approved(env.sender, "budget", task_id=task.id,
                                                 at=env.ts):
            return (f"task {task.id} would spend {spent} tokens, over the budget of {budget}; "
                    "send REQUEST_ACTION category=budget and wait for the owner")
        return None

    def _check_re_any(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if not env.re or env.re not in self.by_id:
            return f"{env.intent} must point at an existing envelope (re: <envelope id>)"
        return None

    def _dispute_root(self, ref: Optional[str]) -> Optional[str]:
        """The dispute an envelope id belongs to, or the one already about that envelope."""
        return self.dispute_of.get(ref or "") or self.dispute_by_target.get(ref or "")

    def _check_dispute_open_or_new(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        root = self._dispute_root(env.re)
        if root is None:
            return None
        dispute = self.disputes[root]
        if dispute.final:
            return f"dispute {root} is closed by the owner's ruling"
        if dispute.ruling:
            return None  # an appeal: allowed, and it pages the owner
        return self._rounds_left(dispute)

    def _check_dispute_rounds(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        root = self.dispute_of.get(env.re or "")
        if root is None:
            return None  # the re check already failed
        dispute = self.disputes[root]
        if dispute.ruling or dispute.final:
            return f"dispute {root} has a ruling; object to it (it pages the owner) or move on"
        return self._rounds_left(dispute)

    def _rounds_left(self, dispute: Dispute) -> Optional[str]:
        cap = int(self.protocol.stuck("max_dispute_rounds"))
        if cap and dispute.rounds >= cap:
            return (f"dispute {dispute.id} has had {dispute.rounds} rounds (the cap is {cap}); "
                    f"only a RULING by the planner ({self.swarm.planner}) is legal now")
        return None

    def _check_ruling_open(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        root = self.dispute_of.get(env.re or "")
        if root is None:
            return None
        dispute = self.disputes[root]
        if dispute.final:
            return f"dispute {root} already has the owner's final ruling"
        if dispute.ruling and not dispute.appealed:
            return f"dispute {root} already has a ruling ({dispute.ruling})"
        if dispute.appealed and self.swarm.role_of(env.sender) != "owner":
            return f"the ruling on {root} was appealed; only the owner can rule now"
        return None

    def _check_escalation_kind(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if env.body.get("kind") not in ("safety", "stuck"):
            return "ESCALATE is only for body.kind safety or stuck; settle everything else " \
                   "in the swarm (OBJECTION, RULING)"
        return None

    def _check_safety_category(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        cat = env.body.get("category")
        if cat == "budget" and not env.task_id:
            return "a budget request must name its task_id"
        if self.protocol.safety_category(str(cat)) is None:
            return (f"body.category {cat!r} is not on the safety list: "
                    f"{', '.join(c.id for c in self.protocol.safety)}")
        return None

    def _check_approval_unused(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        req = self.by_id.get(env.re or "")
        if req is None:
            return None  # the re check already failed
        if req.sender != env.sender:
            return "only the agent that asked may use the approval"
        if str(req.body.get("action", "")).strip() != str(env.body.get("action", "")).strip():
            return "body.action must be exactly the approved action"
        if req.id in self.used:
            return f"the approval for {req.id} was already used"
        if not self.approved(env.sender, str(req.body.get("category")),
                             action=str(req.body.get("action", "")), at=env.ts):
            return f"{req.id} has no live owner approval"
        return None

    def _check_approval_decision(self, env: Envelope, task: Optional[Task]) -> Optional[str]:
        if env.body.get("decision") not in ("approve", "deny"):
            return "body.decision must be approve or deny"
        return None

    # -- applying ---------------------------------------------------------------------

    def apply(self, env: Envelope) -> None:
        self.by_id[env.id] = env
        self.log.append(env)
        self.last_seen[env.sender] = env.ts
        if env.intent not in NOT_PROGRESS:
            self.last_progress = env.ts
        rule = self.rule(env.intent)
        assert rule is not None
        task = self.tasks.get(env.task_id) if env.task_id else None
        if env.intent == "PROPOSE":
            task = Task(id=str(env.task_id), title=str(env.body.get("title")),
                        context_id=env.context_id, state="proposed", created_by=env.sender,
                        created_at=env.ts)
            self.tasks[task.id] = task
        if task is not None:
            task.history.append(env.id)
            task.updated_at = env.ts
            self._apply_task(env, task, rule)
        handler = getattr(self, f"_apply_{env.intent.lower()}", None)
        if handler:
            handler(env)

    def _apply_task(self, env: Envelope, task: Task, rule: IntentRule) -> None:
        intent = env.intent
        if rule.to_state == "@verdict":
            verdict = env.body["verdict"]
            task.state = "approved" if verdict == "approve" else "changes_requested"
            task.reviews.append({"id": env.id, "by": env.sender, "verdict": verdict,
                                 "head_sha": env.body.get("head_sha")})
        elif rule.to_state:
            task.state = rule.to_state
        if intent == "OFFER":
            task.offered_to = env.body.get("assignee") or None
        elif intent == "CLAIM":
            task.claimant, task.epoch, task.offered_to = env.sender, int(env.epoch or 0), None
        elif intent in ("PROGRESS", "READY"):
            try:
                task.tokens += int(env.body.get("tokens") or 0)
            except (TypeError, ValueError):
                pass
            if intent == "READY":
                task.head_sha, task.branch = env.body["head_sha"], env.body["branch"]
        elif intent == "BLOCKED":
            task.reason = env.body.get("reason")
        elif intent in ("FAILED", "RELEASE"):
            if intent == "FAILED":
                task.failures += 1
            task.reason = env.body.get("reason")
            task.claimant, task.offered_to, task.head_sha = None, None, None
        elif intent == "REJECT":
            task.reason = env.body.get("reason")
        elif intent == "DONE":
            task.merge_sha = env.body.get("merge_sha")

    def _apply_query(self, env: Envelope) -> None:
        self.queries[env.id] = env

    def _apply_answer(self, env: Envelope) -> None:
        self.queries.pop(env.re or "", None)

    def _apply_objection(self, env: Envelope) -> None:
        root = self._dispute_root(env.re)
        if root is None:
            target = self.by_id.get(env.re or "")
            dispute = Dispute(id=env.id, target=env.re,
                              target_author=target.sender if target else None,
                              task_id=env.task_id or (target.task_id if target else None),
                              opened_by=env.sender, opened_at=env.ts, last_speaker=env.sender,
                              parties={env.sender} | ({target.sender} if target else set()))
            self.disputes[env.id] = dispute
            self.dispute_of[env.id] = env.id
            if env.re:
                self.dispute_by_target[env.re] = env.id
            return
        dispute = self.disputes[root]
        self.dispute_of[env.id] = root
        dispute.parties.add(env.sender)
        dispute.last_speaker = env.sender
        if dispute.ruling:
            dispute.appealed = env.id
        else:
            dispute.rounds += 1

    def _apply_response(self, env: Envelope) -> None:
        root = self.dispute_of[env.re or ""]
        dispute = self.disputes[root]
        self.dispute_of[env.id] = root
        dispute.rounds += 1
        dispute.parties.add(env.sender)
        dispute.last_speaker = env.sender

    def _apply_ruling(self, env: Envelope) -> None:
        root = self.dispute_of[env.re or ""]
        dispute = self.disputes[root]
        self.dispute_of[env.id] = root  # objecting to the ruling itself is an appeal
        dispute.last_speaker = env.sender
        if self.swarm.role_of(env.sender) == "owner":
            dispute.final = True
        dispute.ruling = env.id
        dispute.appealed = None

    def _apply_escalate(self, env: Envelope) -> None:
        self.escalated[env.id] = env

    def _apply_decision(self, env: Envelope) -> None:
        self.decisions[env.re or ""] = env

    def _apply_request_action(self, env: Envelope) -> None:
        self.requests[env.id] = env

    def _apply_approve_action(self, env: Envelope) -> None:
        self.approvals[env.re or ""] = env

    def _apply_action_taken(self, env: Envelope) -> None:
        self.used[env.re or ""] = env

    # -- queries ----------------------------------------------------------------------

    def approved(self, agent: str, category: str, *, action: Optional[str] = None,
                 task_id: Optional[str] = None, at: Optional[str] = None) -> Optional[str]:
        """The id of a live, unused owner approval for ``agent`` to do ``category``.

        Approvals for actions are single-use (``ACTION_TAKEN`` uses one up). A ``budget``
        approval is not an action: it lifts the cap for its own task until it expires.
        """
        ttl = timedelta(hours=float(self.protocol.policy.get("approval_ttl_hours") or 0))
        now = parse_ts(at) if at else datetime.now(timezone.utc)
        for req_id, req in self.requests.items():
            if req.sender != agent or req.body.get("category") != category:
                continue
            if category == "budget":
                if task_id is None or req.task_id != task_id:
                    continue
            elif req_id in self.used:
                continue
            if action is not None and str(req.body.get("action", "")).strip() != action.strip():
                continue
            ok = self.approvals.get(req_id)
            if not ok or ok.body.get("decision") != "approve":
                continue
            if ttl and now - parse_ts(ok.ts) > ttl:
                continue
            return req_id
        return None

    def inbox(self, agent: str, limit: int = 20) -> List[Envelope]:
        mine = [e for e in self.log if e.sender != agent and ("*" in e.to or agent in e.to)]
        return mine[-limit:]

    def obligations(self, agent: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        role = self.swarm.role_of(agent)
        for q in self.queries.values():
            if agent in q.to:
                out.append({"do": "ANSWER", "re": q.id, "why": f"{q.sender} asked: "
                            f"{_short(q.body.get('text'))}"})
        for d in self.disputes.values():
            if d.final:
                continue
            cap = int(self.protocol.stuck("max_dispute_rounds"))
            if not d.ruling and d.rounds >= cap and role == "planner":
                out.append({"do": "RULING", "re": d.id,
                            "why": f"dispute {d.id} reached {d.rounds} rounds"})
            elif not d.ruling and agent in d.parties | {d.target_author} \
                    and d.last_speaker != agent and d.rounds < cap:
                out.append({"do": "RESPONSE", "re": d.id,
                            "why": f"{d.last_speaker} objected; round {d.rounds} of {cap}"})
            if d.appealed and role == "owner":
                out.append({"do": "RULING", "re": d.id,
                            "why": f"the planner's ruling on {d.id} was appealed"})
        for t in self.tasks.values():
            if t.state == "ready" and t.claimant and t.claimant != agent \
                    and self.swarm.agent(t.claimant).vendor != self.swarm.agent(agent).vendor:
                out.append({"do": "REVIEW", "task_id": t.id,
                            "why": f"{t.claimant} marked {t.id} ready at {t.head_sha}"})
            if t.claimant == agent and t.state == "changes_requested":
                out.append({"do": "PROGRESS", "task_id": t.id, "why": "changes were requested"})
            if t.claimant == agent and t.state in ("claimed", "blocked"):
                out.append({"do": "PROGRESS", "task_id": t.id,
                            "why": f"you hold {t.id} (epoch {t.epoch})"})
            if t.state == "offered" and t.offered_to == agent:
                out.append({"do": "CLAIM", "task_id": t.id, "why": "it was offered to you"})
            if role == "planner" and t.state == "proposed":
                out.append({"do": "OFFER", "task_id": t.id,
                            "why": f"{t.created_by} proposed {t.title!r}"})
            if role == "planner" and t.state == "approved":
                out.append({"do": "DONE", "task_id": t.id, "why": "approved; merge it"})
        if role == "owner":
            for esc in self.escalations():
                out.append({"do": esc["answer_with"], "re": esc.get("id"), "why": esc["reason"]})
        return out

    def legal_moves(self, agent: str) -> List[Dict[str, Any]]:
        """What ``agent`` may send now, with the body fields each move needs."""
        moves: List[Dict[str, Any]] = []
        role = self.swarm.role_of(agent)

        def add(intent: str, **extra: Any) -> None:
            rule = self.protocol.intents[intent]
            move = {"intent": intent, **{k: v for k, v in extra.items() if v is not None},
                    "body": list(rule.body), "summary": rule.summary}
            if rule.evidence:
                move["evidence"] = rule.evidence
            moves.append(move)

        for name, rule in self.protocol.intents.items():
            if not rule.allows_role(role):
                continue
            if rule.re:
                continue  # listed below, against concrete targets
            if rule.task == "required" and rule.from_states and None in rule.from_states:
                add(name, task_id="<new>")
                continue
            if rule.from_states is None:
                if name != "OBJECTION":
                    add(name)
                continue
            for task in self.tasks.values():
                if task.state in self.protocol.terminal_states:
                    continue
                draft = self._draft(agent, name, task)
                if not self.validate(draft, structural=True):
                    add(name, task_id=task.id, epoch=draft.epoch)
        for ob in self.obligations(agent):
            ob_rule = self.protocol.intents.get(ob["do"])
            if ob.get("re") and ob_rule and ob_rule.allows_role(role) and ob_rule.re:
                add(ob["do"], re=ob["re"])
        if self.protocol.intents["OBJECTION"].allows_role(role):
            add("OBJECTION", re="<envelope id>")
        return moves

    def _draft(self, agent: str, intent: str, task: Task) -> Envelope:
        rule = self.protocol.intents[intent]
        epoch = task.epoch + 1 if "next_epoch" in rule.checks else task.epoch
        return Envelope(lane=agent, seq=1, prev=GENESIS, ts="", sender=agent, to=["*"],
                        intent=intent, context_id=task.context_id, task_id=task.id,
                        epoch=epoch)

    def escalations(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Everything that needs the owner: the safety list and the stuck list."""
        now = now or datetime.now(timezone.utc)
        hours = float(self.protocol.escalation("deadline_hours") or 24)
        default = self.protocol.escalation("safe_default") or "park"
        out: List[Dict[str, Any]] = []

        def item(kind: str, since: str, reason: str, answer_with: str, **extra: Any) -> None:
            deadline = parse_ts(since) + timedelta(hours=hours)
            out.append({"kind": kind, "reason": reason, "since": since,
                        "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "overdue": now > deadline, "safe_default": default,
                        "answer_with": answer_with, **extra})

        for req_id, req in self.requests.items():
            if req_id not in self.approvals:
                cat = self.protocol.safety_category(str(req.body.get("category")))
                item("safety", req.ts, f"{req.sender} asks to {_short(req.body.get('action'))} "
                     f"({cat.summary if cat else req.body.get('category')}): "
                     f"{_short(req.body.get('reason'))}", "APPROVE_ACTION", id=req_id,
                     task_id=req.task_id)
        for esc_id, esc in self.escalated.items():
            if esc_id not in self.decisions:
                item(str(esc.body.get("kind")), esc.ts, f"{esc.sender}: "
                     f"{_short(esc.body.get('reason'))}", "DECISION", id=esc_id,
                     options=esc.body.get("options"), task_id=esc.task_id)
        max_fail = int(self.protocol.stuck("max_task_failures"))
        for t in self.tasks.values():
            if max_fail and t.failures >= max_fail and \
                    t.state not in self.protocol.terminal_states:
                item("stuck", t.updated_at, f"task {t.id} has failed {t.failures} times "
                     f"(last: {_short(t.reason)})", "REJECT", task_id=t.id,
                     options=["re-offer with new guidance", "REJECT the task"])
        for d in self.disputes.values():
            if d.appealed and not d.final:
                item("stuck", self.by_id[d.appealed].ts,
                     f"dispute {d.id} is still contested after the planner's ruling",
                     "RULING", id=d.id, task_id=d.task_id)
        idle = self.protocol.stuck("swarm_idle_hours")
        open_tasks = [t for t in self.tasks.values()
                      if t.state not in self.protocol.terminal_states]
        if idle and open_tasks and self.last_progress:
            last = parse_ts(self.last_progress)
            if now - last > timedelta(hours=idle):
                item("stuck", self.last_progress, f"no progress anywhere for "
                     f"{int((now - last).total_seconds() // 3600)}h with {len(open_tasks)} "
                     "open task(s)", "NOTE")
        return out


_BODY_CHECKS = {"reviewed_head", "verdict", "sha", "re_any", "dispute_open_or_new",
                "dispute_rounds", "ruling_open", "escalation_kind", "safety_category",
                "approval_decision", "named_recipient"}


def _short(text: Any, n: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def iter_tasks(proj: Projection, states: Optional[Iterable[str]] = None) -> List[Task]:
    wanted = set(states) if states else None
    return [t for t in proj.tasks.values() if wanted is None or t.state in wanted]
