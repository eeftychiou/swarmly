# Agent roles

A role says what an agent is for: who plans, who builds, who checks. Give each agent one, and
every turn it takes carries the role's instructions, while its peers know whom to hand work to.

Roles follow the shape of [Claude Code subagents](https://docs.claude.com/en/docs/claude-code/sub-agents):
one Markdown file per role, YAML front matter with a `name` and a one-line `description` of
what the role is for, and the role's standing instructions as the body. The practices behind
the built-in roles are the ones that make subagents work well: one clear responsibility per
role, an explicit workflow, a definition of done, and stated boundaries (what the role must
not do), so agents check each other instead of overlapping.

## Built-in roles

| Role | What it does | Must not |
|---|---|---|
| `coordinator` | turns a goal into a tracking issue and small issues with acceptance criteria, assigns them, keeps status, is the only one who nudges a stalled peer (once, before reassigning or escalating), merges PRs once approved, QA-passed and green in the same turn the last missing piece arrives, posts the round-end report in the round's topic when the tracking issue closes (`github.tracking_label`), escalates to the human | write product code, approve PRs, merge before review and QA, decide what only the human can |
| `developer` | implements assigned issues as tested pull requests, answers review | merge its own PR, widen scope, skip tests |
| `reviewer` | reviews pull requests in one pass with every blocker listed, severities, and a **merge** (Approve) / **fix-first** (Request changes) verdict | push to the author's branch, approve its own work, report blockers one review at a time |
| `qa-tester` | tests a PR against its acceptance criteria, reports **pass** / **fail** with reproduction steps, files bugs | fix product code, report checks it did not run |
| `researcher` | answers technical questions with sources and a recommendation on the issue that asked | change the project's code |

The full text of each is in [`swarmly/telegram/roles/`](../swarmly/telegram/roles/).

Two cross-role rules. A reviewer reads the whole diff and lists **every** blocker in one pass,
so the author fixes them in one push instead of a round per finding. And only the coordinator
nudges a peer that has gone quiet: it asks once, then reassigns or escalates. Every other role
reports the stall to the coordinator rather than chasing the peer itself, so a stalled agent is
not pinged by three bots at once.

Chasing a stalled owner is not the same thing as retrying a delivery. The escalation ladder in
[PROTOCOL.md](telegram/PROTOCOL.md#escalation-ladder) — at `ttl`, re-sending the REQUEST with
`/ping`, mirroring it to the bus, escalating to the human — belongs to whoever sent that
REQUEST, whichever role it has: a retry is not a nudge.

## How an agent gets its role

Roles are not written in any config. An agent started with `swarmly --cli ...` asks the swarm
for one when it joins ([JOIN.md](JOIN.md)): the coordinator's agent picks the role that fills
the team's gap and answers `ROLE: @bot <role>`, which every agent records. The first agent
of a swarm coordinates. The maintainer overrides any role at any time:

```
/role @codex_bot reviewer
```

The role's instructions reach the agent at once: its next turn introduces the role in full
and asks it to request its first task from the coordinator, and from then on they ride in
every turn. Every agent also knows the team's roles, so it knows whom to hand work to.

A config written by hand can still start an agent with a role and list its peers' roles
(`swarmly-telegram init --role developer` writes the `role:` line):

```yaml
agent:
  alias: Codex
  role: developer                  # where it starts; a role the swarm gives later wins

peers:
  Antigravity: {username: agy_bot, role: qa-tester}
```

`check` prints the resolved role and the peers' roles, and fails on a role it cannot find.

## Project roles

A project overrides a built-in role, or adds its own, with a file in
`.agents/roles/<name>.md` of its bus checkout (`bus.repo`), looked up before the built-ins:

```markdown
---
name: data-engineer
description: Owns the ingestion pipelines and their tests.
---
You are the swarm's data engineer. ...

Boundaries: ...
```

The name must match the file name, use lowercase letters, digits, `-` or `_`, and the body
stays under 6000 characters, since it rides in every turn.

**Trust.** Project role files are read only from the bus checkout (or from an explicit path
in `agent.role`), never from a per-topic worktree or a separate `adapter.cwd`: agents edit
those and check out pull-request branches there, and a branch must not be able to rewrite
the reviewer's instructions. That protection holds only when the agent works somewhere other
than the bus checkout. `swarmly-telegram init --bus-repo X` without `--cwd` makes the bus
checkout the working directory too; then a branch checked out there can change the roles,
just as it can change the `AGENTS.md` or `CLAUDE.md` the CLI loads, and `check` says so.
For reviewer and QA agents, give the agent its own clone or worktree (`--cwd`). Swarmly never
moves the bus checkout forward on its own, so a role change takes effect when you pull it
there; the role is read when an adapter is built, so restart the agent to be sure every
turn uses it. If a role file breaks after startup, the agent logs a
warning and runs without its role rather than failing every turn; `check` names the
problem.

## Where the role shows

- **Every turn:** the role's instructions and the team's roles follow the channel rules, in
  the system prompt for CLIs that take one (Claude Code, Codex) and in the message otherwise.
- **`/role`** answers with this agent's role and the team's; **`/status`** includes
  `role=<name>`; the online notice says `role: <name>`.

- **Every turn's framing line** ends with a short card that repeats the role in one line, so
  it holds deep into a long session.

**The role guard.** Instructions fade over a long session, so the transport also enforces a
built-in role's hard limits. A `gh` wrapper goes first on the agent's `PATH`, and a git
`pre-push` hook is set through `GIT_CONFIG_*` in the agent's environment, so it holds
whichever `git` runs (Git Bash puts its own first on `PATH`). Both live in
`.agents/state/guard-<alias>/`; the repository's own hooks still run. A refusal comes with a
one-line reason the agent reads back:

| Role | Refused |
|---|---|
| coordinator | nothing |
| developer, qa-tester | `gh pr merge`; a push to `main`/`master` |
| reviewer | `gh pr merge`; any push |
| researcher | `gh pr merge`; `gh pr create`; any push |

The guard keeps an agent from drifting out of its role; it is not a security boundary (an
agent can call a program by its full path). Limit what an agent may run with
`adapter.permissions` (for example `strict` for a researcher) and, for GitHub, with what its
own account may do.
