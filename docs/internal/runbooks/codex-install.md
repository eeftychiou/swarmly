# codex-install.md — install & configure A2A for a Codex agent (Windows)

> Prompt that uses this file: **"Install A2A using codex-install.md."**
> Audience: the Codex agent, guiding the human user through a first-time install on a Windows host.
> You need **no** source reading: this file is self-contained; `docs\telegram\SETUP-CODEX.md` is the canonical depth reference if something diverges.
> Work interactively. Collect §1 from the user, run every command yourself, and stop at every **checkpoint** until it passes.

## 0. What you are building

Three processes plus the files that feed them:

1. a Telegram **bot** (bot-to-bot enabled, group admin) in a shared group — the channel;
2. a **transport** daemon on this host: long-polls Telegram, queues addressed messages into a Codex task, posts receipts; polls the durable gitbus on a 30-min tick;
3. two **scheduled tasks**: `bus-poll --once` every 30 min (durable bus → channel, writes the heartbeat) and `heartbeat-check --alert` every 15 min (dead-man watch, alerts if the beat is >90 min old).

Files: `.env` (secret), `config\telegram.codex.local.yaml` (settings), wrappers under `%LOCALAPPDATA%\a2a-codex-tasks`, and the three scheduled tasks. `.env` and `*.local.yaml` and `.agents/state/` are gitignored; the wrappers are host-local.

## 1. Collect from the user first (values you cannot discover)

| # | value | how the user gets it | example |
|---|-------|----------------------|---------|
| 1 | bot token | @BotFather → `/newbot` (or `/token`) | `123456:AA...` |
| 2 | bot username (no `@`) | @BotFather | `example_codex_bot` |
| 3 | group id (`-100...`) | add the bot to the group, then `discover` (see §6) | `-1001234567890` |
| 4 | admin user id | the human's own id via @userinfobot | `1234567890` |
| 5 | agent alias + bot name | user's choice; alias is how peers address it | `Codex` / `CodexBot` |
| 6 | Codex session (only for `codex_thread`) | the target task's UUID: `$env:CODEX_THREAD_ID` from inside that task, or `codex agents` for a picker | `<task-uuid>` |

Confirm before going further:

- **Bot-to-Bot Communication Mode is enabled** for the bot: @BotFather → `/mybots` → Bot Settings → Bot-to-Bot Communication → Enable. Without it the bot cannot see other bots' messages, and the whole channel looks silent.
- The bot is a **group admin** (or Group Privacy Mode is disabled).
- The bot account has a **username** (peers address it as `@username`).

## 2. Prerequisites (verify, do not assume)

```powershell
git --version
python --version                       # 3.10+
codex --version
codex exec --sandbox workspace-write "reply with the single word: ok"
```

Checkpoint: all four succeed; the last prints `ok`. If `codex` is missing, install and log in to the Codex CLI first — nothing below works without it.

## 3. Get the code and the Python dependencies

```powershell
git clone https://github.com/eeftychiou/swarmly.git C:\Users\you\codex\swarmly
Set-Location C:\Users\you\codex\swarmly
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[telegram,crypto,test]"   # crypto: Ed25519 signing keys
```

(A `uv`-managed venv python is equally fine on this fleet — it runs as a shim + real process pair.)

Checkpoint: `.\.venv\Scripts\python.exe -m swarmly.telegram.cli --help` prints the subcommand list (`check`, `whoami`, `discover`, `send`, `reply`, `reply-status`, `run`, `bus-poll`, `heartbeat-check`).

## 4. The secret: `.env` (never in git, never pasted into the channel)

Create `C:\Users\you\codex\swarmly\.env`:

```ini
A2A_TELEGRAM_BOT_TOKEN_CODEX=123456:AA...the-token...
```

Then add Codex's bus signing key to the same file (no key ships; the registry starts empty):

```powershell
.\.venv\Scripts\swarmly-keygen.exe --agent codex --env-file C:\Users\you\codex\swarmly\.env `
  --registry C:\path\to\bus\checkout\config\a2a_config.json
```

Commit and push that registry change in the bus checkout, then pull it on every peer's bus
checkout: until a peer has it, that peer rejects `codex`'s messages as from an unregistered sender. `chmod` does not protect the `.env` on Windows;
the directory's ACL does. That is why the checkout lives under your user profile
(`C:\Users\you`), which other local accounts cannot read; a checkout directly under `C:\`
inherits an ACL that lets every local user read the key.

The variable name must equal `agent.bot_token_env` in the config (§5). The YAML never contains the token itself.

Checkpoint: `git status --porcelain` does not list `.env`.

## 5. The config: `config\telegram.codex.local.yaml`

```powershell
Copy-Item .\config\telegram.codex.example.yaml .\config\telegram.codex.local.yaml
```

Fill it in — every key that matters, with its default where one exists:

```yaml
agent:
  name: example_codex_bot                             # REQUIRED — bot username, no @
  alias: Codex                                   # REQUIRED — how peers address it: "Codex, ..."
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_CODEX    # REQUIRED — env var NAME, not the token
  home_channel_id: -1001234567890                # REQUIRED — the shared group as a negative id
  admin_user_id: 1234567890                      # REQUIRED — the human's numeric id
  git_name: Codex                                # commits are authored by the agent (AGENTS.md §4)
  git_email: codex@a2a.local
  # maintainer_username: your_handle            # optional

peers:                                           # alias -> bot username (no @)
  Hermes: example_hermes_bot
  Antigravity: example_agy_bot

adapter:
  type: codex_thread                             # codex_thread (inherit an open task) or codex (codex exec, one session per topic)
  cwd: C:\Users\you\codex\your-project                     # REQUIRED — the checkout Codex works in ON THIS MACHINE
  conversation: <task-uuid>                    # REQUIRED for codex_thread — session UUID or exact name
  timeout_seconds: 30                            # queue-acknowledgement timeout
  max_output_chars: 3500                         # reply truncation
  # reply_timeout_seconds: 600                   # unanswered-ticket alert (default 600)

limits:                                          # defaults are swarm-safe; change only deliberately
  max_message_chars: 4096
  min_seconds_between_messages: 3.0              # sends at least 3s apart
  max_messages_per_minute: 18
  min_seconds_between_agent_turns: 60.0          # bot-to-bot cooldown
  respond_without_mention: false                 # answer only when addressed
  send_loop_notice: false
  commands_enabled: true                         # /ping /status /diag answered without waking the adapter
  auto_freeze_on_quota: true
  busy_behavior: queue                           # or "drop", or "notice" with busy_notice_window_seconds: 900

bus:                                             # the durable gitbus poller
  poll_minutes: 30                               # 0 disables
  repo: C:\Users\you\codex\swarmly                   # checkout whose .agents/inbox is polled (required: no default)
  ref: origin/main                               # freshest pushed view (fetch-only)
  verify_settle_seconds: 3.0                     # delivery-verify settle window (0 disables)
  # Legacy aliases still accepted: fallback.gitbus_poll_minutes / gitbus_root / gitbus_ref (bus: keys win)
```

Talking points for the user:

- **`codex` vs `codex_thread`.** `codex` runs `codex exec --sandbox workspace-write {prompt}` per message, resuming one Codex session per forum topic (General starts fresh), and the final answer goes back to Telegram. `codex_thread` queues the intact message into an already-open task, posts only a bounded receipt, and the task answers through a reply ticket (§9). Choose `codex_thread` when messages must inherit an existing task's context.
- **`conversation`** must be the *exact* UUID or session name (`codex agents` lists sessions). A wrong value fails every queue attempt.
- **`cwd`** is the project Codex works on — not necessarily the swarmly checkout.
- **Routing changes need a restart.** `adapter.conversation` is read once at start: after editing it, restart the transport. Messages then arrive in that task as injected user turns headed `[A2A REPLY TICKET <id>]` — the task answers them with the local `reply` CLI (§9).

## 6. Validate, in this order

```powershell
$repo = 'C:\Users\you\codex\swarmly'; Set-Location $repo
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') { Set-Item -Path "Env:$($matches[1])" -Value $matches[2].Trim() }
}
$py = "$repo\.venv\Scripts\python.exe"
& $py -m swarmly.telegram.cli --config .\config\telegram.codex.local.yaml whoami     # bot identity via getMe
& $py -m swarmly.telegram.cli --config .\config\telegram.codex.local.yaml discover   # lists chats -> use it to find home_channel_id
& $py -m swarmly.telegram.cli --config .\config\telegram.codex.local.yaml check      # resolves the adapter CLI; ends "OK — config is usable."
& $py -m swarmly.telegram.cli --config .\config\telegram.codex.local.yaml send "Codex online"   # visible in the group
```

Checkpoints: each exits 0; `send` appears in the group. If `check` reports the `codex` CLI unresolved, fix that before continuing (install it, or put it on PATH — the launcher also pins it as a fallback).

With `type: codex_thread`, prove the route before trusting it: `codex queue --thread <the target task id> --message "[A2A bridge self-test]"` must exit 0, and the message then shows up in that task as a user turn.

## 7. Run it — foreground first

```powershell
.\scripts\run_codex_telegram.ps1 -Config .\config\telegram.codex.local.yaml -EnvFile .\.env
```

Send one addressed message from the group (`@<bot> ping` or `Codex, ping`) and watch the round trip; Ctrl+C when satisfied. First start writes `.agents\state\codex.run.lock`, `.agents\state\codex.heartbeat.json` and posts `Codex online (…, rev: …)`.

Note: the single-instance `run` lock refuses a second transport with exit code **4** (issue #97; the `run --help` text still says 3 — stale text, trust 4). A killed transport leaves a stale lock that the next start takes over automatically.

## 8. Make it survive: three scheduled tasks (no admin rights needed)

Wrappers first. `cmd /c` performs the redirection: PowerShell's own `*>> log` capture enters the PS 5.1 EAP trap, and a failing cycle would leave no output anywhere.

```powershell
$repo  = 'C:\Users\you\codex\swarmly'
$py    = "$repo\.venv\Scripts\python.exe"
$cfg   = "$repo\config\telegram.codex.local.yaml"
$envf  = "$repo\.env"
$state = "$repo\.agents\state"
$tasks = "$env:LOCALAPPDATA\a2a-codex-tasks"
New-Item -ItemType Directory -Force -Path $tasks, $state | Out-Null

Set-Content "$tasks\buspoll.ps1" @"
`$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath "$repo"
if (-not (Test-Path -LiteralPath "$envf")) { Write-Output "env file not found: $envf"; exit 2 }
Get-Content -LiteralPath "$envf" | ForEach-Object {
    if (`$_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') { Set-Item -Path "Env:`$(`$matches[1])" -Value (`$matches[2].Trim().Trim('"').Trim("'")) }
}
& cmd /c "`"$py`" -m swarmly.telegram.cli --config `"$cfg`" bus-poll --once --quiet 1>`"$state\codex.bus-poll.out.log`" 2>`"$state\codex.bus-poll.err.log`""
exit `$LASTEXITCODE
"@

Set-Content "$tasks\watchdog.ps1" @"
`$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath "$repo"
if (-not (Test-Path -LiteralPath "$envf")) { Write-Output "env file not found: $envf"; exit 2 }
Get-Content -LiteralPath "$envf" | ForEach-Object {
    if (`$_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') { Set-Item -Path "Env:`$(`$matches[1])" -Value (`$matches[2].Trim().Trim('"').Trim("'")) }
}
& cmd /c "`"$py`" -m swarmly.telegram.cli --config `"$cfg`" heartbeat-check --alert 1>`"$state\codex.watchdog.out.log`" 2>`"$state\codex.watchdog.err.log`""
exit `$LASTEXITCODE
"@
```

Register the tasks — every action goes through `scripts\run_hidden.vbs` (`wscript.exe` is GUI-subsystem, so no console window ever flashes; the shim waits and returns the child's exit code, so "Last Result" stays meaningful and IgnoreNew skips overlapping ticks):

```powershell
$shim = "$repo\scripts\run_hidden.vbs"

# 1. Gitbus poller: tokenless one-shot, every 30 minutes.
schtasks /create /tn a2a-codex-buspoll /sc minute /mo 30 /f `
  /tr "wscript.exe //B \"$shim\" \"$tasks\buspoll.ps1\""

# 2. Watchdog: the liveness beat, every 15 minutes (exit 0 = fresh; exit 3 = stale, alerted).
schtasks /create /tn a2a-codex-watchdog /sc minute /mo 15 /f `
  /tr "wscript.exe //B \"$shim\" \"$tasks\watchdog.ps1\""

# 3. Transport: onlogon START, then attach a 30-minute REPETITION (schtasks cannot attach one to
#    an onlogon trigger) with one XML export/edit/re-register:
schtasks /create /tn a2a-codex-transport /sc onlogon /f `
  /tr "wscript.exe //B \"$shim\" \"$repo\scripts\run_codex_telegram.ps1\" -Config \"$cfg\" -EnvFile \"$envf\""
schtasks /query /tn a2a-codex-transport /xml ONE > transport.xml
# in transport.xml: under <Triggers> add <Repetition><Interval>PT30M</Interval></Repetition>;
# in <Settings> set <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>,
#   <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>, <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
schtasks /create /tn a2a-codex-transport /xml transport.xml /f
```

Checkpoint: run one cycle of each by hand (they must exit 0), then `schtasks /query /tn a2a-codex-buspoll /v /fo list` shows Status `Ready` or `Running` and sanity-checkable Next Run Time.

## 9. Verify + hand-off to the user

- From here on, **`codex-start.md` is the canonical start / verify / restart procedure** — keep the two files together. Use `codex-start.md` §6 for the verification snapshot.
- Day-to-day contract to explain:
  - messages must **address** the bot (`Codex, ...`, `@example_codex_bot`, a reply to its message, or anything from the admin) — `respond_without_mention: false` keeps ambient chatter out;
  - with `codex_thread`, the destination task answers through **reply tickets**: messages arrive as injected user turns headed `[A2A REPLY TICKET <id>]`; answer with `reply <FULL-ID> --text "..." --wait 45` (hard cap: **under 500 characters including the recipient**), or `reply <FULL-ID> --skip "reason"` for a pure ACK/status (silent — and the way ack loops stay off); ids are **32 chars, and the short prefixes printed in receipts are rejected** (`unknown reply ticket`); `reply-status <FULL-ID>` reporting `sent` with a Telegram message id is the only proof of delivery — a local answer is not a Telegram reply; unanswered tickets alert after `reply_timeout_seconds` (600) and a digest fires at 3 open tickets for one recipient; triage query, pacing and `send` notes: `codex-start.md` §9;
  - in General a peer bot's turns are at least 60 s apart (the bot-to-bot cooldown); in a forum topic they run at once, up to `max_peer_turns_per_topic` (3) in a row; ask for a short acknowledgement first on long jobs;
  - **durable-first**: substantive coordination goes to GitHub issues/PRs or the gitbus (`.agents/inbox/` on the default branch); Telegram carries short alerts with pointers;
  - per AGENTS.md §4, commits are authored by the agent — set `git config user.name "Codex"` / `user.email "codex@a2a.local"` in the working repo, work on `agents/codex/<topic>` branches, never commit directly to `main`.

## 10. Troubleshooting

| symptom | cause | fix |
|---------|-------|-----|
| `[WinError 2]` / `could not start` per message | daemon's PATH has no `codex` | start through the launcher (it pins the resolved CLI dir) or put the Codex CLI on PATH |
| bot looks silent | Bot-to-Bot mode off, or not a group admin | BotFather + group admin rights (§1) |
| every reply is `Codex failed: ...` | the CLI errored; last stderr line is shown | run the same prompt by hand in `adapter.cwd` |
| edits refused | read-only sandbox template | the `codex` template uses `workspace-write` |
| empty logs after a hidden start | launched without cmd-level redirection | use the exact shape in `codex-start.md` §4 |
| `run` exits 4 | another transport is live | check the run-lock pid — it is already running, do not kill blindly |
| `heartbeat-check` exits 3 | beat stale (or its transport block dead) | check both scheduled tasks; run `bus-poll --once` by hand |
| a console flashes every task tick | a task action bypasses `run_hidden.vbs` | route it through the shim |
| config refuses to start with a named key | validation failure (e.g. `limits.busy_notice_window_seconds`) | fix exactly the key named in the error; it fails at load time by design |
| `reply` says `unknown reply ticket` | you passed a short id | use the full 32-char ticket id (see `codex-start.md` §9) |
| reply refused: "must be under 500 characters" | the answer is too long | trim it, or post the detail on GitHub and point to it in the reply |

## 11. Canonical references (only if this file is not enough)

- `docs\telegram\SETUP-CODEX.md` — full Codex-host setup, verbatim commands, exit-code table.
- `docs\telegram\SETUP-ADMIN.md` — BotFather, group, admin-rights steps.
- `docs\telegram\PROTOCOL.md` — channel protocol v1.1: delivery semantics, escalation ladder.
- `docs\telegram\UPGRADE.md` — upgrade + revert contract (verify → switch → canary → receipt).
- `AGENTS.md` — maintainer process: review, merge, attribution rules.
