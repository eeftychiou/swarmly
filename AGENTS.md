# AGENTS.md — Maintainer process

The standing process for whoever acts as **maintainer** of this repository (admin-stated;
amendments via PR). It governs how changes reach `main`, and how the swarm learns about
them. Also applies to the maintainer's own PRs: the same verification, the same notice.

## 1. Reviewing a pull request

Before the maintainer finishes their own review:

1. **Impartial review first.** Launch independent subagent review(s) of the PR — reviewers
   that did not write the change and are not asked to confirm it. They report findings with
   severity and an explicit **merge / fix-first** verdict.
2. **Ask the swarm to comment.** Post a **pointer** to the PR in the Telegram channel —
   pointer, not payload — and ask the peers (Codex, Antigravity, DeepSeek) to comment: code,
   tests, docs, risks.
3. **Then the maintainer's own review.** Read the diff, verify claims against the PR head
   (run the suite locally, check the CI run), reconcile the subagent and swarm findings,
   and decide.

Only then merge — `--no-ff` merge commits authored by the reviewing maintainer, keeping
every contributor's authorship intact — or reject with a written reason.

## 2. After merging to main

**Inform the swarm — an unrolled-out fix is not a fix.** Every merge gets a notice to the
Telegram channel addressing all peers (Codex, Antigravity, DeepSeek): what landed — the PR
and the merge commit, pointers, not payloads.

**The admin deploys; agents do not.** That notice says what landed and nothing more. Agents
never tell each other to install, upgrade, pull or switch the swarmly they run on, and no
peer is asked to run a rollout. The safe upgrade and revert path in
`docs/telegram/UPGRADE.md` is the **admin's** to run:

- `scripts/upgrade_transport.sh verify <ref>` — run the suite **on the host** at the target
  revision before switching anything;
- `scripts/upgrade_transport.sh switch <ref> --service <unit>` — switch with automatic
  rollback when the restart fails; `rollback` reverts in seconds;
- **canary order** — one peer upgrades first, the rest after a clean window;
- **receipt** — the `rev:` field in the transport's online notice.

When a merge cannot take effect until the transport is deployed, the notice says so to the
admin and the swarm waits; it is never turned into a task for a peer.

## 3. Durable-first coordination

Substantive coordination lands on the durable plane first (gitbus commits, GitHub issues),
and only then does Telegram carry a short alert with pointers (issue numbers, commit shas).
Telegram remains for direct, low-latency coordination.

**One bus per project (admin directive, 2026-09-19).** Every project owns a dedicated
gitbus in its own repository (`.agents/inbox/` on that repository's default branch — the ref
transports actually read; default `origin/main`). This
repository's bus carries Swarmly development only — other projects consume the Swarmly
*library* and operate their own bus. Every transport must set `bus.repo` (or the older
`fallback.gitbus_root`) to the repository whose bus it reads: there is no default, and
without it nothing is polled.

**Topic-to-project routing.** Agents configure `projects:` in `config.yaml` to bind
Telegram forum topics (by numeric topic id) to project repositories and checkouts (with
optional git worktrees). Turns in a bound topic run in that project's workspace with
isolated session states. Diagnostic commands `/where` and `/status` identify the bound project.

**GitHub durable plane and notifications backstop.** GitHub issues and PRs serve as the
default durable coordination record for project topics. A background notifications backstop
(`github.poll_minutes`) catches unread mentions, review requests, and assignments if Telegram
delivery fails or is delayed, while silently skipping items already handled in recent topic turns.

## 4. Attribution (admin directive, 2026-09-19)

Every agent commits and comments under its **own identity** — never the human owner's
name or email — so the swarm's actions are distinguishable.

- **Commits:** repo-local git identity `user.name` / `user.email` = agent name +
  `agent@a2a.local` (`Claude <claude@a2a.local>`, `Codex <codex@a2a.local>`,
  `Antigravity <antigravity@a2a.local>`). Verify before pushing:
  `git log -1 --format='%an <%ae>'`. Beware `GIT_AUTHOR_*` / `GIT_COMMITTER_*`
  env overrides shadowing repo config.
- **Merges:** local `git merge --no-ff` + push only — GitHub web/API merges always land
  under the owner's account and are not permitted; the merge commit carries the merging
  agent's identity.
- **Comments and issue bodies:** sign with the agent name (e.g. `— Codex`).
- **History:** historical commits are not re-attributed (no rewriting of `main`);
  the rule is fix-forward.


## 5. Consultation before escalation (admin directive, 2026-09-21)

Agents **consult each other** whenever they are uncertain about a question that appears to
need approval, and **escalate to the admin only when strictly necessary**.

1. **Consult the swarm first.** Put the question where the work lives (PR/issue comment,
   or in-channel with a durable pointer): the decision needed, the options, each agent's
   read. Asking the admin for approval is a last resort, not a default.
2. **Converged readings carry.** When the agents agree on a reading, act on it and record
   the decision on the durable plane (the PR/issue). Questions the swarm can settle itself
   are not the admin's to arbitrate.
3. **Escalate only when strictly necessary** — genuinely owner-scope matters (spending or
   quota posture, identity or sensitive data, cross-project policy) or readings that
   conflict and cannot converge. A necessary escalation states the question, the options,
   and the default the swarm would take.

## 6. Related documents

- `docs/telegram/PROTOCOL.md` — channel protocol v1.1 (delivery semantics, escalation ladder).
- `docs/telegram/UPGRADE.md` — safe upgrade + revert procedure.
- `docs/PROJECTS.md` — multi-project coordination, topic routing, and GitHub durable plane.
