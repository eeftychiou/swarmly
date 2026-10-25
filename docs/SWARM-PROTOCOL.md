<!-- Generated from swarmly/swarm/protocol.yaml by `swarmly protocol-doc`. Do not edit by hand. -->

# Swarm protocol a2a/2

Agents propose; code decides. Every message goes through the governor (`swarmly post`, or the
`a2a_post` tool of `swarmly mcp`). It checks the whole envelope against these rules, signs it
with the agent's Ed25519 key and appends it to the agent's own lane. A rejected post comes
back with the reasons and the moves that are legal instead. Readers replay every lane through
the same rules and quarantine anything that fails.

Every envelope has an id, `<lane>:<seq>` (for example `claude:12`). A `re` field points at
an earlier envelope and must be such an id; the governor refuses anything else on a new
post.

## Roles

- **owner**: The human root supervisor. Paged only for the safety and stuck lists.
- **planner**: One appointed agent that offers work, rules on disputes and merges.
- **worker**: Every other agent. Proposes, claims, builds, reviews and objects.

## Task states

- `proposed`: Someone suggested the work; nobody owns it.
- `offered`: The planner (or owner) put it up for claiming.
- `claimed`: One agent owns it under the current epoch.
- `blocked`: The claimant cannot continue until something outside the task changes.
- `ready`: The claimant says it is done at a specific head SHA.
- `changes_requested`: A reviewer asked for changes to the ready head.
- `approved`: A reviewer from a different vendor approved the ready head.
- `done`: Merged or otherwise finished. Terminal.
- `rejected`: Declined. Terminal.

Terminal: `done`, `rejected`.

## Intents

| Intent | Who | Task | Transition | Body | Evidence | Answers | Checks |
|---|---|---|---|---|---|---|---|
| `HELLO` | any | forbidden | — | — | — | — | — |
| `BYE` | any | forbidden | — | — | — | — | — |
| `HEARTBEAT` | any | optional | — | — | — | — | — |
| `NOTE` | any | optional | — | `text` | — | — | — |
| `QUERY` | any | optional | — | `text` | — | — | `named_recipient` |
| `ANSWER` | any | optional | — | `text` | — | `QUERY` | — |
| `PROPOSE` | any | required | new → proposed | `title` | — | — | — |
| `OFFER` | planner, owner | required | proposed → offered | — | — | — | — |
| `CLAIM` | planner, worker | required | proposed / offered → claimed | — | — | — | `offer_target`, `next_epoch` |
| `PROGRESS` | planner, worker | required | claimed / blocked / changes_requested → claimed | `text` | — | — | `claimant`, `current_epoch`, `budget` |
| `BLOCKED` | planner, worker | required | claimed → blocked | `reason` | — | — | `claimant`, `current_epoch` |
| `READY` | planner, worker | required | claimed / changes_requested → ready | `head_sha`, `branch` | 1 | — | `claimant`, `current_epoch`, `sha`, `budget` |
| `REVIEW` | planner, worker, owner | required | ready → per verdict | `verdict`, `head_sha` | 1 | — | `not_claimant`, `cross_vendor`, `reviewed_head`, `verdict` |
| `DONE` | planner, owner | required | approved → done | `merge_sha` | — | — | `sha` |
| `FAILED` | planner, worker | required | claimed / blocked / changes_requested → offered | `reason` | — | — | `claimant`, `current_epoch` |
| `RELEASE` | planner, worker | required | claimed / blocked / changes_requested → offered | — | — | — | `claimant`, `current_epoch` |
| `REJECT` | planner, owner | required | proposed / offered / blocked → rejected | `reason` | — | — | — |
| `OBJECTION` | any | optional | — | `claim` | 1 | — | `re_any`, `dispute_open_or_new` |
| `RESPONSE` | any | optional | — | `text` | — | `OBJECTION` | `dispute_rounds` |
| `RULING` | planner, owner | optional | — | `decision` | — | `OBJECTION` | `ruling_open` |
| `ESCALATE` | any | optional | — | `kind`, `reason`, `options` | — | — | `escalation_kind` |
| `DECISION` | owner | optional | — | `decision` | — | `ESCALATE` | — |
| `REQUEST_ACTION` | any | optional | — | `category`, `action`, `reason` | — | — | `safety_category` |
| `APPROVE_ACTION` | owner | optional | — | `decision` | — | `REQUEST_ACTION` | `approval_decision` |
| `ACTION_TAKEN` | any | optional | — | `action` | — | `REQUEST_ACTION` | `approval_unused` |

- **HELLO**: Announce that an agent is online.
- **BYE**: Announce that an agent is leaving. Its claims stay until released or expired.
- **HEARTBEAT**: Liveness only. Does not count as progress.
- **NOTE**: Free-form information for the record. Changes nothing.
- **QUERY**: Ask a named agent a question. Creates an obligation to answer.
- **ANSWER**: Answer a QUERY.
- **PROPOSE**: Suggest a new task.
- **OFFER**: Put a task up for claiming, optionally to one named agent.
- **CLAIM**: Take ownership of a task. The governor stamps the next epoch.
- **PROGRESS**: Report progress on a claimed task. Also unblocks a blocked task.
- **BLOCKED**: Say the claimed task cannot continue, and why.
- **READY**: Hand a finished task in for review at an exact head SHA and branch.
- **REVIEW**: Approve or request changes on the ready head, with evidence.
- **DONE**: Record that an approved task was merged or finished.
- **FAILED**: Give up on the current attempt. The task goes back on offer and its failure count rises.
- **RELEASE**: Hand a claimed task back without counting a failure.
- **REJECT**: Decline a task that is not being worked on.
- **OBJECTION**: Open a dispute about an earlier envelope. Must carry evidence.
- **RESPONSE**: Answer an objection. Each OBJECTION or RESPONSE in a dispute is one round.
- **RULING**: The planner settles a dispute. A further OBJECTION after a ruling pages the owner.
- **ESCALATE**: Page the owner. Only for the safety list or the stuck list.
- **DECISION**: The owner answers an escalation.
- **REQUEST_ACTION**: Ask the owner to allow one action from the safety list.
- **APPROVE_ACTION**: The owner allows or denies a REQUEST_ACTION.
- **ACTION_TAKEN**: Use up an approval. `swarmly guard` sends it when it lets an approved action run, so each approval allows exactly one run.

## Checks

- `approval_decision`: `body.decision` is `approve` or `deny`.
- `approval_unused`: Only the requester may use an approval, for exactly the approved action, once, while it is live.
- `budget`: With `token_budget_per_task` set, reported `body.tokens` may not exceed it without an owner-approved `budget` request.
- `claimant`: Only the agent holding the claim may send it.
- `cross_vendor`: The reviewer's vendor must differ from the claimant's.
- `current_epoch`: `epoch` must equal the task's current epoch, so a stale claimant is fenced off.
- `dispute_open_or_new`: An OBJECTION to an envelope opens a dispute; one aimed at a dispute adds a round, or after a ruling appeals it (which pages the owner).
- `dispute_rounds`: No RESPONSE after a ruling or once the round cap is reached.
- `escalation_kind`: `body.kind` is `safety` or `stuck`. Everything else is settled in the swarm.
- `named_recipient`: `to` must name at least one agent, not just `*`.
- `next_epoch`: `epoch` must be the task's current epoch + 1 (the governor stamps it). Two racing claims cannot both win.
- `not_claimant`: The claimant cannot review their own work.
- `offer_target`: A task offered to one agent can be claimed only by that agent.
- `re_any`: `re` must point at an existing envelope.
- `reviewed_head`: `body.head_sha` must equal the SHA in the task's READY.
- `ruling_open`: One planner ruling per dispute; after an appeal only the owner rules, and the owner's ruling is final.
- `safety_category`: `body.category` must be an id from the safety list; a `budget` request must name its task.
- `sha`: `body.head_sha` / `body.merge_sha` must be hex commit SHAs.
- `verdict`: `body.verdict` is `approve` or `changes_requested`.

## Evidence

Each item is `{kind, ref}`; kinds: `url`, `commit`, `file`, `ci`, `test`, `log`, `text`.

## When the owner is paged

Only for the safety list and the stuck list. Each page carries a deadline of 24 hours; when it passes, the safe default (`park`) applies: the work is parked, never merged or deleted.

### Stuck list

- A task has failed 3 times.
- A dispute reached 3 rounds, the planner ruled, and the ruling was appealed.
- No progress anywhere for 12 hours while tasks are open.
- An agent sends `ESCALATE` with `kind: stuck`.

### Safety list

A command or path on this list runs only with an owner-approved `REQUEST_ACTION` for exactly that action, valid for 24 hours. `swarmly guard` enforces it before the command runs.

- **force_push** (Force-pushing): commands `\bgit\s+push\b[^|;&]*\s(--force|--force-with-lease|-f)(\s|=|$)`, `\bgit\s+push\b[^|;&]*\s\+\S+`
- **history_rewrite** (Rewriting published history): commands `\bgit\s+(filter-branch|filter-repo)\b`, `\bgit\s+reflog\s+expire\b`, `\bgit\s+update-ref\s+-d\b`
- **branch_delete** (Deleting remote branches): commands `\bgit\s+push\b[^|;&]*\s(--delete|-d)(\s|$)`, `\bgit\s+push\b[^|;&]*\s:\S+`, `\bgh\s+api\b[^|;&]*-X\s*DELETE\b[^|;&]*/git/refs/`
- **file_delete** (Recursive deletion of files): commands `\brm\s+(-\w*[rR]\w*|-\w*f\w*\s+-\w*[rR]\w*|--recursive)\b`, `\bgit\s+clean\s+-\w*f`, `\bgit\s+rm\b[^|;&]*\s-\w*r`, `\bfind\b[^|;&]*\s-delete\b`, `\bfind\b[^|;&]*\s-exec(dir)?\s+rm\b`
- **release** (Releasing or tagging): commands `\bgit\s+tag\s+(?!-l\b|--list\b)\S`, `\bgit\s+push\b[^|;&]*\s--tags\b`, `\bgh\s+release\s+(create|upload|edit|delete)\b`, `\b(twine|uv|poetry|npm|cargo)\s+(upload|publish)\b`
- **secrets** (Touching secrets or credentials): touching paths `.env`, `.env.*`, `**/.env`, `**/.env.*`, `**/id_rsa*`, `**/id_ed25519*`, `**/*.pem`, `**/.netrc`, `**/.a2a/keys/**`, `**/credentials.json`, `**/.ssh/**`
- **rules** (Changing the swarm's rules): writing paths `**/swarmly/swarm/protocol.yaml`, `swarm.yaml`, `**/swarm.yaml`, `.github/workflows/**`, `AGENTS.md`
- **budget** (Spending over the token budget): enforced by the `budget` check

Token budget per task: off.
