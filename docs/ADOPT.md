# Adopting Swarmly in your project

This is the walkthrough for the audience the README does not serve yet: you have **two or more
agent runtimes on different machines, from different vendors**, and you want them to work on one
project together with a durable record and a human in charge. If your agents all live in one
process, a framework like AutoGen is simpler — Swarmly earns its keep across machines, vendors,
and trust boundaries.

## The mental model (read PROTOCOL.md §1 for the normative version)

- **The chat group is signalling.** Small, fast, lossy. Pings, ACKs, blockers, handoffs.
- **The repo is the record.** Everything that matters lands in git: issues, PRs, commits, bus
  files. Chat points at it; the record wins on conflict.
- **The bus is a mailbox, not a push.** Each peer's poller picks messages up on its own
  schedule. Exactly-once delivery comes from a seen-ledger, not from a socket.
- **Rule 0**: post to chat only to answer a peer or the human, or to hand something over.
  Narrating progress that the record already holds is pollution.

## A minimal adoption, end to end

1. **Create the project repo** (private is fine). Add an `AGENTS.md` at the root describing
   who is in the swarm, their roles, the review flow, and the merge rules — this file is the
   project constitution every agent reads first.
2. **Give the repo a bus**: `.agents/inbox/` (created on first send; commit the directory).
   Every project owns its own bus; do not share one bus across projects.
3. **Configure each agent** (one `config.yaml` per agent, per machine — see
   `docs/GETTING-STARTED.md` §3). Point `adapter.cwd` at the project checkout on that machine.
4. **Schedule each poller**: `bus-poll --once` on cron / Task Scheduler, every 10-30 minutes.
   Tokenless, silent on success, and it leaves a heartbeat file you can watchdog.
5. **Agree the envelope discipline** (`docs/ENVELOPES.md`): envelope on line 1, real mentions,
   `ref=` pointers, closure by mention, receipts are not completions.
6. **Run your first handoff** (below), then let the process grow: reviews before merges, one
   merge per PR, upgrades canaried one host at a time.

## First handoff (5 minutes)

The runnable version is `examples/minimal-bus-demo.sh` — it clones the repo to a temp dir,
posts a message from `alice` to `bob` with the real CLI and shows the envelope file.

`scripts/a2a_bus.py` always writes to the bus next to it: `.agents/inbox/` in the checkout
that contains the script, not your current directory. For a hand-run handoff on your own
project's bus, use a shared folder, which works from anywhere:

```bash
# alice's key: private key into .env, public key into the registry (template at config/a2a_config.json)
swarmly-keygen --agent alice --env-file .env --registry config/a2a_config.json
set -a; . ./.env; set +a          # nothing is sent without A2A_KEY_ALICE
python scripts/a2a_bus.py --transport folder --folder /path/to/shared/bus send \
  --sender alice --recipient bob \
  --intent PR_REVIEW_REQUEST --text "PR #7 ready for review; see the diff."
python scripts/a2a_bus.py --transport folder --folder /path/to/shared/bus inbox
```

On a git bus, the transports read your project's repository once `bus.repo` (or
`fallback.gitbus_root`) points at it, and `swarmly-telegram --config config.yaml post` writes there.
With `scripts/a2a_bus.py` on the git transport, add `--push` to commit and push the message;
without it the file is only written.

On bob's machine the poller delivers it into bob's runtime; bob's answer (a review, or a
receipt and a promise) goes back through the same bus, or to the chat group if the swarm has
one.

## Process that actually worked in production

- **One writer per artefact.** Issues and PRs have a single owner at a time; handoffs are
  explicit (`TASK_HANDOFF`).
- **Reviews before merges, verdicts in writing.** "MERGE"/"FIX-FIRST" with the head sha; a
  moved head invalidates the verdict.
- **Receipt != completion.** Distinguish "seen" from "done" in your own tooling — it is what
  catches a silent peer (see `docs/telegram/PROTOCOL.md`).
- **Liveness is two-layered**: a local watchdog for a stalled poller (heartbeat file), and a
  cross-host detector for a dead host (unacknowledged-ping watch).
- **Upgrades are canaried** and revert in one command (`docs/telegram/UPGRADE.md`).
- **Attribution is per agent** in commits and comments (`alias@domain`), never the human's name.

## Layout of a project that drinks its own champagne

```
project/
  AGENTS.md                 # constitution: roles, review flow, merge rules
  .agents/
    inbox/                  # the bus: committed envelopes (msg-*.md)
    state/                  # seen-ledgers, heartbeats (typically gitignored)
  config.yaml               # THIS agent's config (not committed)
  docs/                     # durable narrative
  src/ tests/               # the work
```

## Where to read more

- `docs/telegram/PROTOCOL.md` — channel protocol v1.1, normative
- `docs/ENVELOPES.md` — envelope shapes, both planes
- `docs/GETTING-STARTED.md` — bring a node up
- `docs/telegram/UPGRADE.md` — safe upgrades
- `examples/` — runnable demo
