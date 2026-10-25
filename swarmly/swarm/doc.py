"""Generate docs/SWARM-PROTOCOL.md from protocol.yaml, so the doc cannot drift from the code."""

from __future__ import annotations

from typing import List

from .protocol import Protocol, load_protocol

# What each named check in protocol.yaml enforces. A test fails when a check has no
# entry here or no implementation in projection.py.
CHECK_DOCS = {
    "named_recipient": "`to` must name at least one agent, not just `*`.",
    "offer_target": "A task offered to one agent can be claimed only by that agent.",
    "next_epoch": "`epoch` must be the task's current epoch + 1 (the governor stamps it). "
                  "Two racing claims cannot both win.",
    "claimant": "Only the agent holding the claim may send it.",
    "current_epoch": "`epoch` must equal the task's current epoch, so a stale claimant is "
                     "fenced off.",
    "not_claimant": "The claimant cannot review their own work.",
    "cross_vendor": "The reviewer's vendor must differ from the claimant's.",
    "reviewed_head": "`body.head_sha` must equal the SHA in the task's READY.",
    "verdict": "`body.verdict` is `approve` or `changes_requested`.",
    "sha": "`body.head_sha` / `body.merge_sha` must be hex commit SHAs.",
    "budget": "With `token_budget_per_task` set, reported `body.tokens` may not exceed it "
              "without an owner-approved `budget` request.",
    "re_any": "`re` must point at an existing envelope.",
    "dispute_open_or_new": "An OBJECTION to an envelope opens a dispute; one aimed at a dispute "
                           "adds a round, or after a ruling appeals it (which pages the owner).",
    "dispute_rounds": "No RESPONSE after a ruling or once the round cap is reached.",
    "ruling_open": "One planner ruling per dispute; after an appeal only the owner rules, "
                   "and the owner's ruling is final.",
    "escalation_kind": "`body.kind` is `safety` or `stuck`. Everything else is settled in "
                       "the swarm.",
    "safety_category": "`body.category` must be an id from the safety list; a `budget` "
                       "request must name its task.",
    "approval_decision": "`body.decision` is `approve` or `deny`.",
    "approval_unused": "Only the requester may use an approval, for exactly the approved "
                       "action, once, while it is live.",
}

HEADER = """<!-- Generated from swarmly/swarm/protocol.yaml by `swarmly protocol-doc`. Do not edit by hand. -->

# Swarm protocol {name}

Agents propose; code decides. Every message goes through the governor (`swarmly post`, or the
`a2a_post` tool of `swarmly mcp`). It checks the whole envelope against these rules, signs it
with the agent's Ed25519 key and appends it to the agent's own lane. A rejected post comes
back with the reasons and the moves that are legal instead. Readers replay every lane through
the same rules and quarantine anything that fails.

Every envelope has an id, `<lane>:<seq>` (for example `claude:12`). A `re` field points at
an earlier envelope and must be such an id; the governor refuses anything else on a new
post.
"""


def _cell(items: List[str]) -> str:
    return ", ".join(f"`{i}`" for i in items) if items else "—"


def render(protocol: Protocol) -> str:
    out = [HEADER.format(name=protocol.name)]
    out.append("## Roles\n")
    out += [f"- **{k}**: {v}" for k, v in protocol.roles.items()]
    out.append("\n## Task states\n")
    out += [f"- `{k}`: {v}" for k, v in protocol.states.items()]
    out.append(f"\nTerminal: {_cell(protocol.terminal_states)}.\n")
    out.append("## Intents\n")
    out.append("| Intent | Who | Task | Transition | Body | Evidence | Answers | Checks |")
    out.append("|---|---|---|---|---|---|---|---|")
    for name, r in protocol.intents.items():
        if r.from_states is not None:
            src = " / ".join("new" if s is None else s for s in r.from_states)
            dst = "per verdict" if r.to_state == "@verdict" else str(r.to_state)
            trans = f"{src} → {dst}"
        else:
            trans = "—"
        answers = f"`{r.re}`" if r.re else "—"
        out.append(f"| `{name}` | {', '.join(r.roles)} | {r.task} | {trans} | "
                   f"{_cell(r.body)} | {r.evidence or '—'} | {answers} | {_cell(r.checks)} |")
    out.append("")
    out += [f"- **{name}**: {r.summary}" for name, r in protocol.intents.items()]
    out.append("\n## Checks\n")
    used = sorted({c for r in protocol.intents.values() for c in r.checks})
    out += [f"- `{c}`: {CHECK_DOCS.get(c, '(undocumented)')}" for c in used]
    out.append("\n## Evidence\n\nEach item is `{kind, ref}`; kinds: "
               f"{_cell(protocol.evidence_kinds)}.\n")
    pol = protocol.policy
    stuck = pol.get("stuck") or {}
    esc = pol.get("escalation") or {}
    out.append("## When the owner is paged\n")
    out.append("Only for the safety list and the stuck list. Each page carries a deadline of "
               f"{esc.get('deadline_hours')} hours; when it passes, the safe default "
               f"(`{esc.get('safe_default')}`) applies: the work is parked, never merged or "
               "deleted.\n")
    out.append("### Stuck list\n")
    out.append(f"- A task has failed {stuck.get('max_task_failures')} times.")
    out.append(f"- A dispute reached {stuck.get('max_dispute_rounds')} rounds, the planner "
               "ruled, and the ruling was appealed.")
    out.append(f"- No progress anywhere for {stuck.get('swarm_idle_hours')} hours while tasks "
               "are open.")
    out.append("- An agent sends `ESCALATE` with `kind: stuck`.\n")
    out.append("### Safety list\n")
    out.append("A command or path on this list runs only with an owner-approved "
               "`REQUEST_ACTION` for exactly that action, valid for "
               f"{pol.get('approval_ttl_hours')} hours. `swarmly guard` enforces it before the "
               "command runs.\n")
    for cat in protocol.safety:
        bits = []
        if cat.commands:
            bits.append("commands " + ", ".join(f"`{p.pattern}`" for p in cat.commands))
        if cat.paths:
            bits.append(("writing " if cat.touch == "write" else "touching ") + "paths " +
                        ", ".join(f"`{p}`" for p in cat.paths))
        if cat.id == "budget":
            bits.append("enforced by the `budget` check")
        out.append(f"- **{cat.id}** ({cat.summary}): {'; '.join(bits) or '—'}")
    budget = pol.get("token_budget_per_task")
    out.append(f"\nToken budget per task: {budget or 'off'}.\n")
    return "\n".join(out).rstrip() + "\n"


def render_default() -> str:
    return render(load_protocol())
