# Commands — one page

Every command below is answered by the bot's **transport**, never by a model turn, so none of
them costs tokens (except where a command asks a model to work). Details live in
[`docs/telegram-channel.md`](../telegram-channel.md); this page is the overview.

**Who answers.** `/cmd@bot` is answered by that bot. A bare `/cmd` is answered by the bot it
mentions, or by every bot for the swarm-wide controls (`/pause`, `/resume`, `/stop`, `/hold`,
`/release`, `/ping`), or by the **coordinator** (the lease holder) for the topic-registry
commands. "Admin" means only `agent.admin_user_id` may use it; anyone in the swarm may use
the rest.

## Stopping and starting work

These are easy to mix up. In short: **stop** cancels what runs now; **pause** stops one bot (or
all) taking turns; **hold** keeps everyone answering but starting nothing new.

| Command | Admin | What it does | Undo |
|---|---|---|---|
| `/stop@bot` | yes | Cancels the bot's running turn, ends its CLI process and clears its queue (in a topic: that topic only; `/stop@bot all`: everything). | — (one-off) |
| `/pause` / `/pause@bot` | yes | The bot (bare: every bot) finishes the turn in flight, then takes **no more turns** — from Telegram or the bus — and says "paused". Survives restarts. Use it to stop a bot at its budget. | `/resume` / `/resume@bot` |
| `/resume` / `/resume@bot` | yes | Lifts a pause. Also ends a quota freeze early (or switch model with `/model@bot`). | — |
| `/hold [reason]` | yes | Holds the **whole swarm** (`/hold@bot` only picks who answers). Bots still **answer** what they are asked, but **start nothing**: no reminders, no stall ladder, no hand-offs, no merges, no PRs. Survives restarts and reaches a bot that was down within one pulse. | `/release` |
| `/release` | yes | Lifts the hold, swarm-wide. The latest of hold/release always wins. | — |

## Looking at the swarm

| Command | What it shows |
|---|---|
| `/ping@bot` | That the bot answers (PONG). |
| `/status@bot` | The bot's state, running revision, model, effort, peers (up/held/frozen/DOWN). |
| `/where@bot` | Where the bot runs (host, working directory). |
| `/swarm@bot` | The bus, the coordinator and the members. |
| `/usage@bot` (`/usage@bot all`) | What the bot spent today from its turn ledger: turns, tokens, cost where the CLI reports it; `all` lists every bot. |
| `/round@bot` | What the open round waits on. |
| `/owed@bot` | The open "owed" rows: who owes what to whom (reviews, merges, CI, hand-offs). Rows close themselves from facts (verdicts, CI, merges, GitHub's own state). |
| `/drift@bot` | Rule violations the transport recorded for this bot, and its drift score (reminder at 3, session reset at 6). |
| `/state@bot` | The shared state store as this bot sees it: position, lease holder and term, snapshot, whether every bot agrees, owed rows in the store. |
| `/topics@bot` | The topic registry: topics, their links (work items with phase and assignees), linked bots and each bot's CLI session and turn count. |
| `/storetest@bot [-Alias]` (admin) | A one-minute check of the Telegram facts the state store relies on; `-Alias` leaves a bot out. |

## Per-topic settings

| Command | What it does |
|---|---|
| `/model@bot` (admin to set) | Show the model this bot runs in this topic; `/model@bot <name>` switches it from the next turn (`default` goes back). |
| `/reasoning@bot` (admin to set) | The same for reasoning effort (`low` … `max`, `default`). |
| `/role@bot` | Show a bot's role; the admin sets one with `/role@bot <role>`. The coordinator role follows the lease. |

## Topics and work items (the topic registry)

| Command | Admin | What it does |
|---|---|---|
| `/link @bot` | yes | Links a bot to this topic (with a binding from its profile). |
| `/link <ref> [as work\|tracking\|reference] [— title]` | yes | Links an issue, PR, document or path to this topic. A work item starts in phase `development`. `<ref>`: `#12`, a GitHub/GitLab/Drive URL, a local path, or `T-<topic>-<n>`. |
| `/unlink @bot` | yes | Unlinks a bot (the coordinator is never unlinked). |
| `/phase <ref\|L1> <phase>` | yes | Moves a work item: `research`, `design`, `development`, `review`, `qa`, `done`, `blocked`. GitHub also moves linked items by itself (open PR → review, merged → done). |
| `/topic #N …` | yes | Says that these issues/PRs live in this topic (today's routing). |
| `/cleanup@bot …` | yes | Tidies the group: a dry run without arguments; `topics done`, `topic <id>`, `topics all`, `general`; deletions need `confirm <code>`. |

The coordinator records the same things with lines in its replies: `LINK: …`, `ASSIGN: @bot
<ref|L1> as <role>`, `PHASE: <ref|L1> <phase>`.

## Membership and tools

| Command | Admin | What it does |
|---|---|---|
| `/admit @bot` | yes | Admits a bot to the swarm. |
| `/remove @bot` | yes | Removes a bot from the swarm. |
| `/sync@bot` | yes | Re-syncs the roster. |
| `/memory@bot` (`clear`) | yes | Shows (or empties) the bot's `memory.md` of lessons. |
| `/cli@bot <command>` | yes | Runs a native CLI command with no model turn: e.g. `/cli@bot --version`, `/cli@bot /cost`, `/cli@bot /context`. Commands that change something need `/cli!@bot`. |
| `/wakes` | — | Retired; see `/owed`. |
