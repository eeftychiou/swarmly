# Commands — one page

Every command below is answered by the bot's **transport**, never by a model turn, so none of
them costs tokens (the dashboard's Nudge is the one exception: it wakes a bot). Details live in
[`docs/telegram-channel.md`](../telegram-channel.md); this page is the overview.

**Who answers.** `/cmd@bot` is answered by that bot, and so is `/cmd @bot`, a command sent as a
reply to one of its messages, or a command in the admin's private chat with it. The command has
to come first: `@bot /status` is an ordinary message, and starts a turn. Most commands need the
bot named that way, and a bare `/status`, `/ping` or `/stop` in the group gets no answer.

A bare `/pause`, `/resume`, `/hold`, `/release`, `/topic`, `/admit`, `/remove`, `/sync` or
`/role @bot <role>` reaches **every** bot, and so does a bare `/bus` (every bot that has no bus
yet). Each bot applies it. The coordinator answers a bare `/pause`, `/resume`, `/hold`,
`/release`, `/topic` or role change, once for the swarm; after a long pause, when no lease is
live, the coordinator you named answers `/pause`, `/resume`, `/hold` and `/release`. `/sync` has
every bot post its peer list, `/admit` has every bot welcome the newcomer, and `/remove` gets no
answer. `/swarm` is answered by the coordinator, or by every bot while no coordinator is
recorded. The topic-registry commands (`/link`, `/unlink`, `/phase`) are answered by the bot
holding the state store's lease, which writes them. `/swarm`, `/admit`, `/remove`, `/sync`,
`/bus` and setting a role work in the home group only.

"Admin" means only `agent.admin_user_id` may use it. Any other member of the swarm (people in
`agent.allowed_users`, and peer bots) may use the rest; strangers are ignored unless
`limits.strangers` lets them in. Anyone else who names a bot with an admin command gets a
one-line "⛔ only the admin" refusal from it.

**The `/` menu adds the bot.** In a group with several bots, Telegram sends a command picked
from the menu as `/cmd@thatbot`. That is right for the commands below that name a bot, but
`/pause@bot` and `/resume@bot` reach only that bot: type a bare `/pause` to pause them all. The
menu lists each command once per bot, so it leaves the rarer ones out: `/where`, `/diag`,
`/storetest`, `/link`, `/unlink`, `/phase` and `/wakes` are typed.

## Stopping and starting work

These are easy to mix up. In short: **stop** cancels what runs now; **pause** stops one bot (or
all) taking turns; **hold** keeps everyone answering but starting nothing new.

| Command | Admin | What it does | Undo |
|---|---|---|---|
| `/stop@bot` | yes | Cancels the bot's running turn, ends its CLI process and drops the messages queued behind it: in a topic, that topic's only; in General or with `/stop@bot all`, everything. Messages held past the queue's limit still run afterwards. A paused bot ignores it. | — (one-off) |
| `/pause` / `/pause@bot` | yes | The bot (bare: every bot) finishes the turn in flight, then takes **no more turns**, from Telegram or the bus. `/pause@bot` (or `/pause @bot`) gets that bot's "paused" line; a bare `/pause` gets one line from the coordinator for the whole swarm. The messages queued behind that turn are dropped, and nothing sent while it is paused is kept for later: a person gets one 🧊 line saying it is paused. A bot out of quota is paused too, so it stays paused when its quota comes back. A paused coordinator stops renewing its lease, so another bot takes over coordinating. The pause survives a restart once the running turn has ended. | `/resume` / `/resume@bot` |
| `/resume` / `/resume@bot` | yes | Lifts a pause. Also ends a quota freeze early (or switch model with `/model@bot`). A bare `/resume` resumes every bot, and the coordinator answers once. | — |
| `/hold [reason]` | yes | Holds the **whole swarm** (`/hold@bot` only picks who answers). Bots still take turns for what they are asked, GitHub notices included, but every turn is told to start no new work, merge nothing, open no pull request and hand nothing off. The timers that wake bots stop: owed wakes, the stall ladder, hand-off reminders. Survives restarts, and reaches a bot that was down within one pulse. | `/release` |
| `/release` | yes | Lifts the hold, swarm-wide. The latest of hold/release always wins. | — |

## Looking at the swarm

Any member may use these.

| Command | What it shows |
|---|---|
| `/ping@bot` | That the bot answers (PONG). |
| `/status@bot` | Three lines. The bot's state, running revision, model, effort and peers (up/held/frozen/DOWN), with the messages it held or dropped. Then where it runs: the chat and topic ids, and the project, repository and working folder bound to this topic. Then transport facts for debugging: host, session, messages seen. |
| `/where@bot`, `/diag@bot` | Other names for `/status`, with the same answer. Not in the `/` menu. |
| `/swarm` | The bus, the coordinator, and every member with its role and whether it is online. Home group only. |
| `/usage@bot` (`/usage@bot all`) | What the bot spent today from its turn ledger: turns, tokens, cost where the CLI reports it; `all` lists every bot. |
| `/round@bot` | What the open round waits on. |
| `/owed@bot` | The open "owed" rows: who owes what to whom (reviews, merges, CI, hand-offs). Rows close themselves from facts (verdicts, CI, merges, GitHub's own state, a topic whose linked work has all finished). `/owed close [alias]` from the admin, inside a topic, closes the rows owed there (or only that bot's); the `@alias` form works the same way, a second name is ignored and the reply says so. The coordinator answers a bare `/owed` or `/owed close` — the one the admin named, while no lease is live. |
| `/drift@bot` | Rule violations the transport recorded for this bot, and its drift score (reminder at 3, session reset at 6). |
| `/state@bot` | The shared state store as this bot sees it: position, lease holder and term, snapshot, whether every bot agrees, owed rows in the store. |
| `/topics@bot` | The topic registry: topics, their links (work items with phase and assignees), linked bots and each bot's CLI session and turn count. |
| `/storetest@bot [-Alias]` (admin) | A one-minute check of the Telegram facts the state store relies on; `-Alias` leaves a bot out. Not in the `/` menu. |

## Per-topic settings

| Command | What it does |
|---|---|
| `/model@bot` (admin to set) | Show the model this bot runs in this topic; `/model@bot <name>` switches it from the next turn (`default` goes back). |
| `/reasoning@bot` (admin to set) | The same for reasoning effort (`low` … `max`, `default`). `/effort@bot` is the same command. |
| `/role@bot` | Show a bot's role and the team's. The admin sets one with `/role@bot <role>` (or `/role @bot <role>`); every bot applies it, and the bot named (or, for `/role @bot`, the coordinator) confirms it. `/role @bot coordinator` names a new coordinator, which announces itself, and the old one becomes a developer. |

## Topics and work items (the topic registry)

| Command | Admin | What it does |
|---|---|---|
| `/link @bot` | yes | Links a bot to this topic (with a binding from its profile). |
| `/link <ref> [as work\|tracking\|reference] [— title]` | yes | Links an issue, PR, document or path to this topic. A work item starts in phase `development`. `<ref>`: `#12`, a GitHub/GitLab/Drive URL, a local path, or `T-<topic>-<n>`. An issue or PR linked as work (the default) also lives in this topic from now on, as with `/topic`, even where the state store is off. |
| `/unlink @bot` | yes | Unlinks a bot (the coordinator is never unlinked). |
| `/phase <ref\|L1> <phase>` | yes | Moves a work item: `research`, `design`, `development`, `review`, `qa`, `done`, `blocked`. GitHub also moves linked items by itself (open PR → review, merged → done). |
| `/topic #N …` | yes | The same as `/link #N` for each item: these issues/PRs live in this topic, and replies about them are posted here. |

These work inside a topic, and only `/topic` is in the `/` menu. The coordinator records the
same things with lines in its replies: `LINK: …`, `ASSIGN: @bot <ref|L1> as <role>`,
`PHASE: <ref|L1> <phase>`.

## Membership and tools

| Command | Admin | What it does |
|---|---|---|
| `/admit @bot` | yes | Admits a bot to the swarm. A bot you made a group admin joins by itself. |
| `/remove @bot` | yes | Removes a bot from the swarm until you `/admit` it again. A bot listed under `peers:` in a config stays. |
| `/sync` | yes | Every bot re-introduces itself, so their peer lists match again. |
| `/bus <location>` | yes | Sets the bus of a bot that has none yet; `/bus@bot <location>` moves that bot to another bus. Bots set up by hand keep their configured bus. |
| `/cleanup` | yes | Deletes every finished topic at once and lists what it deleted and what it kept. Finished means: not Swarm pulse or a `limits.cleanup_keep` topic, no open work (an open registry work item, an open owed row, a turn running) and no activity in the last hour. The coordinator answers a bare `/cleanup`. Also `preview` (deletes nothing), `topic <id>`, `general` (General messages under 48h). |
| `/memory@bot` (`clear`) | yes | Shows (or empties) the bot's `memory.md` of lessons. |
| `/cli@bot <command>` | yes | Runs a native CLI command with no model turn: e.g. `/cli@bot --version`, `/cli@bot /cost`, `/cli@bot /context`. Commands that change something need `/cli!@bot`. |
| `/wakes` | — | Retired; see `/owed`. Not in the `/` menu. |

## Dashboard buttons

With `control:` set, the coordinator keeps a pinned dashboard in the chat it names (or in
📡 Swarm pulse). Its buttons are admin only, and some ask for a second tap. A per-bot button
names its bot: Pause, Resume, Stop turn and Restart travel as the `CONTROL` note that bot applies
to itself, and Model and Effort are forwarded to it as well — it applies them under its own rules,
and its card shows the new value once its own beat reports it. CLI and Memory act on the
coordinator only. A typed `ctl:` code reaches every bot, so the others answer that the
coordinator has the dashboard: pressing the button is quieter.

| Button | What it does |
|---|---|
| Pause, Resume | Pause is `/pause@bot` for the coordinator: a turn already running finishes and posts, then it holds, and its "paused" line goes out. Resume lifts it. |
| Stop turn | Stops the coordinator's turns and drops its queue, like `/stop@bot all`. |
| Restart | Restarts the coordinator now; a turn it cuts off is replayed when it starts again. The `swarmly --cli` launcher starts it again; `swarmly-telegram run` just exits. |
| Model, Effort | Picks the coordinator's model or effort, for the dashboard's own topic only. |
| CLI | Lists the `/cli` commands the coordinator may run; it runs nothing. |
| Memory | `clear`: empties the coordinator's own `memory.md`, after a second tap. It is that bot's own file, so a press aimed at another bot's card is refused. |
| Pause all | Pauses every bot, the way a bare `/pause` does, after a second tap. |
| Hold, Release | `/hold` and `/release`: held, every bot still answers but starts nothing. Both ask for a second tap, and the hold rides the pulse to the peers as a typed one does. |
| Hand lease, Fail back | Hands the coordinator lease to another bot, or back to the named coordinator. |
| Reassign role | Like `/role @bot <role>`, but it never gives or takes the coordinator role. |
| Cleanup | `/cleanup preview`: what `/cleanup` would delete; a press deletes nothing. |
| Sync | The coordinator re-posts its peer list. A bare `/sync` makes every bot do it. |
| Admit, Remove | `/admit @name` and `/remove @name`: admit adds a bot whose HELLO has already reached this one (the `@username` is typed), remove takes a bot out of the roster. Both ask for a second tap — Admit came back to the table in #345, wired for the first time, and a control that changes the roster is confirmed. |
| Nudge | On a round item's line, or typed as `ctl:item.nudge:<issue>`: wakes the bot that owes that item, which costs it a turn. |

The live page (#345) draws the same controls from the same table — a `bot.` button on that bot's
card, an `item.` button on its round item, the `swarm.` ones in its Controls panel — and each press
takes the same route and leaves the same `via=dashboard` line.
