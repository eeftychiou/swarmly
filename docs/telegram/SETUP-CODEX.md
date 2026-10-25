# SETUP — OpenAI Codex on the home channel

Everything here runs **on the machine that hosts Codex** (for example, a workstation you keep for builds). It never calls
another agent's CLI, and nothing about it depends on the other agents' machines.

---

## 1. Prerequisites

| need | check |
|---|---|
| Codex CLI installed and logged in | `codex --version`, then a one-shot smoke test below |
| one-shot mode works | `codex exec --sandbox workspace-write "reply with the single word: ok"` |
| BotFather bot with Bot-to-Bot Communication enabled | `SETUP-ADMIN.md` §2 |
| bot added to the group **as admin** | `SETUP-ADMIN.md` §3 |
| the three values: token, group id, your user id | `SETUP-ADMIN.md` §4–5 |

`codex exec` is the integration point — non-interactive, no TUI, final response on stdout.

**Sandbox levels** (`--sandbox`):

| level | meaning | when |
|---|---|---|
| `read-only` | proposes changes, writes nothing | reviews, second opinions |
| `workspace-write` | edits inside the workspace | `strict` and `standard` below |
| `danger-full-access` | no confinement | **the default here** (`full`) |

The bot picks the flags from `adapter.permissions` (ignored when `adapter.command` is set):

| `adapter.permissions` | flags |
|---|---|
| `strict` | `--sandbox workspace-write` (no network: `git push`/`pull` fail) |
| `standard` | `--sandbox workspace-write -c sandbox_workspace_write.network_access=true` (`.git` read-only: no `git pull`, `commit` or `push`) |
| `full` (default) | `--dangerously-bypass-approvals-and-sandbox` |

Every runtime defaults to `full`; for Codex it is also the only level that works with git,
because its sandbox keeps `.git` read-only, so a sandboxed Codex can neither pull, commit nor
push. Keep the group and the bus repository private. `full` has no sandbox at all: anyone who can address the bot
can make it run anything as the transport's user, anywhere that user can write. Run agents as
a separate low-privilege user, or in a container or VM. Set `adapter.permissions: standard`
to keep the sandbox; the transport then fetches for Codex before each turn
(`adapter.git_sync`).

Useful variants, if you want them:

```bash
# continue the previous session instead of starting cold
codex exec --sandbox workspace-write resume --last "<prompt>"   # exec options before resume

# machine-readable event stream, if you later want to parse output yourself
codex exec --json --sandbox workspace-write "<prompt>"

# write the final message to a file as well as stdout
codex exec -o /tmp/last.txt --sandbox workspace-write "<prompt>"
```

## 2. Install the channel

```bash
git clone https://github.com/eeftychiou/swarmly.git ~/swarmly
cd ~/swarmly
python3 -m venv .venv
.venv/bin/pip install .                  # everything, incl. Ed25519 signing keys
```

## 3. Put the token in the environment, not the file

```bash
# ~/.a2a-telegram.env   (chmod 600, never committed)
A2A_TELEGRAM_BOT_TOKEN_CODEX=123456789:AA...codex-bot-token...
```

Then create this agent's bus signing key. No key ships with the repository, and without one
every durable write (`post`, escalations) is refused. On this agent's host:

```bash
# Ed25519: the private key goes into the env file (owner-only, never printed); only the
# public key is registered, in config/a2a_config.json of the bus checkout (bus.repo).
.venv/bin/swarmly-keygen --agent codex --env-file ~/.a2a-telegram.env \
  --registry /path/to/bus/checkout/config/a2a_config.json
```

Commit and push that registry change in the bus checkout, so every peer can verify `codex`.
Each peer verifies against its own checkout: until a peer has pulled this commit it rejects
`codex`'s messages as from an unregistered sender, so pull the bus checkout on every peer
(`git -C /path/to/bus/checkout pull`) before `codex` posts.
`check` (below) reports a missing signing key. Details: GETTING-STARTED §3.

## 4. Fill in the config

Copy `config/telegram.codex.example.yaml` → `config/telegram.codex.local.yaml` (same checkout; `*.local.yaml` is gitignored):

```yaml
agent:
  name: CodexBot
  alias: Codex                                 # addressed as "Codex, ..."
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_CODEX
  home_channel_id: -1001234567890              # the shared group
  admin_user_id: 123456789                     # you

peers:
  Hermes: HermesBot
  Antigravity: AntigravityBot

adapter:
  type: codex_thread
  cwd: /home/codex/your-project     # the checkout Codex works on, ON THIS MACHINE
  conversation: <CODEX_SESSION_UUID_OR_EXACT_SESSION_NAME>
  timeout_seconds: 30                           # queue acknowledgement timeout

# Optional: route Telegram forum topics to separate project checkouts (see docs/PROJECTS.md)
# projects:
#   webapp:
#     repo: example-org/webapp
#     cwd: /home/codex/webapp
#     topics: [12, 15]
#     worktrees: true
# github:
#   login: codex-bot
#   poll_minutes: 5
```

For multi-project configurations where different Telegram forum topics route to separate repositories, see [PROJECTS.md](../PROJECTS.md).

`cwd` matters more than anything else here: it is the working directory of the Codex task, so
point it at this machine's own clone. The `conversation` value identifies the already-open task
that should inherit messages from Telegram.

### Route into the currently open Codex task

The `codex` adapter keeps one Codex session per forum topic (a message in General starts a
fresh one), so a topic's messages share context. If Telegram messages must instead go into
an already-open Codex task, use the Codex-only bridge:

```yaml
adapter:
  type: codex_thread
  cwd: /home/codex/your-project
  conversation: 0190f000-0000-7000-8000-000000000001  # UUID or exact session name
  timeout_seconds: 30
```

This invokes `codex queue --thread <conversation> --message <prompt>`. The message appears as
a new user message in that Codex task, so the task retains its existing context. Successful
queueing posts a bounded receipt, not completion. The prompt carries a durable reply ticket.
The task must submit its answer or blocker via `reply ID --text ... --wait 45` using the
config and working directory supplied in the prompt. Only `reply-status ID` reporting
`sent` (with Telegram message ID) proves delivery. Pure ACK/status-only messages use
`reply ID --skip "reason"` without posting. See [reply lifecycle](../telegram-channel.md#codex-task-bridge--type-codex_thread).
Receipt-send failures are tracked and alerted independently from task-deadline expiry, with
the persisted failure detail included so a receipt warning cannot hide a later overdue task.
Structured `REQUEST` messages use a protocol ACK after queue acceptance and do not receive
a second receipt. `codex_thread` is opt-in; other adapters keep their execution model.

## 5. Verify, in this order

```bash
set -a; . ~/.a2a-telegram.env; set +a

.venv/bin/python -m swarmly.telegram.cli --config config/telegram.codex.local.yaml whoami
.venv/bin/python -m swarmly.telegram.cli --config config/telegram.codex.local.yaml discover
.venv/bin/python -m swarmly.telegram.cli --config config/telegram.codex.local.yaml check
#   -> for type: codex, prints:
#      codex exec --sandbox workspace-write {prompt}
#   -> for type: codex_thread, validates the configured existing Codex session

.venv/bin/python -m swarmly.telegram.cli --config config/telegram.codex.local.yaml send "Codex online"
```

Before letting the channel drive it, prove the runtime works on its own:

```bash
cd /home/codex/your-project
codex exec --sandbox workspace-write "reply with the single word: ok"
```

If that hangs or errors, the channel will faithfully report the error — fix the CLI first.

### Recommended foreground launchers

From the Swarmly checkout, the checked-in launchers select the local virtualenv,
optionally load an operator-owned env file, validate the config, and then run the daemon:

```powershell
.\scripts\run_codex_telegram.ps1 -Config .\config\telegram.codex.local.yaml -EnvFile ..\your-project\.env
```

```bash
./scripts/run_codex_telegram.sh /path/to/codex/config.yaml /path/to/codex/.a2a-telegram.env
```

Use the same Python executable for validation and launch. Do not invoke the daemon with a
different system Python after installing Swarmly into a virtualenv.

Both launchers resolve the Codex CLI — from `PATH`, then the standard install locations —
pin its directory on the daemon's `PATH`, and print which binary they chose, so a minimal
service environment cannot start a daemon that fails every message with `[WinError 2]`.
`check`, which both run first, reports a missing CLI as a problem and exits non-zero.

## 6. Run it as a service

`~/.config/systemd/user/a2a-codex.service`:

```ini
[Unit]
Description=Swarmly Telegram home channel (Codex)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/codex/swarmly
EnvironmentFile=%h/.a2a-telegram.env
# The daemon spawns `codex` by name; give the unit the directory that holds the CLI
# (or run the launcher, which resolves and pins it for you).
Environment=PATH=/home/codex/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/codex/swarmly/.venv/bin/python -m swarmly.telegram.cli \
    --config /home/codex/swarmly/config/telegram.codex.local.yaml run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now a2a-codex
journalctl --user -u a2a-codex -f
```

If that machine has no systemd, use `tmux new -s a2a '… run'` or `nohup … &`.

### Windows: Task Scheduler (three user-level tasks)

Windows has no systemd; the equivalent is Task Scheduler. All three run without
admin rights and keep the transport observable, not just alive.

**Launch every task through `scripts\run_hidden.vbs`.** A task action of
`powershell -File …` / `pwsh -Command …` allocates a *visible console* on every tick: Task
Scheduler's "Hidden" flag hides the task from the UI, and `-WindowStyle Hidden` only hides a
window that already exists. `wscript.exe` is GUI-subsystem, so it allocates no console of its
own; the shim starts the child with window style 0 (created hidden) and **waits, returning the
child's exit code**, so `Last Result` and the skip-while-running repetition semantics are
unchanged. No transport restart is needed after pulling the shim — each tick re-reads it.

The bus-poll and watchdog tasks invoke Python directly, so give each a one-line wrapper and
point the task at the shim:

```powershell
$repo  = "C:\Users\you\codex\swarmly"
$cfg   = "$repo\config\telegram.codex.local.yaml"   # the same path as section 4
$envf  = "$repo\.env"
$py    = "C:\Users\you\codex\swarmly\.venv\Scripts\python.exe"
$tasks = "$env:LOCALAPPDATA\a2a-codex-tasks"
$state = "$repo\.agents\state"
New-Item -ItemType Directory -Force -Path $tasks, $state | Out-Null
Set-Content "$tasks\buspoll.ps1" @"
`$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath "$repo"
# cmd /c does the redirection (the checked-in AG wrappers' pattern): PowerShell's own
# `*>> log` capture form enters the PS 5.1 EAP trap (PowerShell#4002) and a failing
# cycle would leave no output anywhere.
& cmd /c "`"$py`" -m swarmly.telegram.cli --config `"$cfg`" bus-poll --once --quiet 1>`"$state\codex.bus-poll.out.log`" 2>`"$state\codex.bus-poll.err.log`""
exit `$LASTEXITCODE
"@
Set-Content "$tasks\watchdog.ps1" @"
`$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath "$repo"
# Loader parity with the checked-in launchers (run_codex_telegram.ps1:28-41): strip surrounding
# quotes and fail LOUDLY when the env file is missing -- the token is needed exactly on the
# stale path this watchdog exists for.
if (-not (Test-Path -LiteralPath "$envf")) { Write-Output "env file not found: $envf"; exit 2 }
Get-Content -LiteralPath "$envf" | ForEach-Object {
    if (`$_ -match '^\s*([^#=\s]+)\s*=\s*(.*)`$') {
        Set-Item -Path "Env:`$(`$matches[1])" -Value (`$matches[2].Trim().Trim('"').Trim("'"))
    }
}
# cmd /c performs the redirection -- same reason as the bus-poll wrapper above.
& cmd /c "`"$py`" -m swarmly.telegram.cli --config `"$cfg`" heartbeat-check --alert 1>`"$state\codex.watchdog.out.log`" 2>`"$state\codex.watchdog.err.log`""
exit `$LASTEXITCODE
"@
```

```powershell
$repo  = "C:\Users\you\codex\swarmly"
$cfg   = "$repo\config\telegram.codex.local.yaml"
$envf  = "$repo\.env"
$py    = "C:\Users\you\codex\swarmly\.venv\Scripts\python.exe"   # or the interpreter that has the deps
$shim  = "$repo\scripts\run_hidden.vbs"

# 1. Transport: the launcher. `onlogon` alone only STARTS it once per logon -- restart-on-failure
#    needs a REPEATING trigger, which `schtasks /create` cannot attach to an onlogon trigger.
#    Create the base task, then apply the repetition and durability settings from the Antigravity
#    runbook (SETUP-ANTIGRAVITY.md:171-180) via one XML export/edit/re-register:
schtasks /create /tn a2a-codex-transport /sc onlogon /f `
  /tr "wscript.exe //B \"$shim\" \"$repo\scripts\run_codex_telegram.ps1\" -Config \"$cfg\" -EnvFile \"$envf\""
schtasks /query /tn a2a-codex-transport /xml ONE > transport.xml
#    edit transport.xml, then re-register:
#      <Triggers> -> <Repetition><Interval>PT30M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
#      <Settings> -> <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
#                    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
#                    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
schtasks /create /tn a2a-codex-transport /xml transport.xml /f

# 2. Bus poller: the tokenless one-shot, every 30 minutes.
schtasks /create /tn a2a-codex-buspoll /sc minute /mo 30 /f `
  /tr "wscript.exe //B \"$shim\" \"$tasks\buspoll.ps1\""

# 3. Watchdog: the canonical liveness beat, every 15 minutes.
#    Exit 0 = fresh; exit 3 = stale, with a channel alert naming the peers.
schtasks /create /tn a2a-codex-watchdog /sc minute /mo 15 /f `
  /tr "wscript.exe //B \"$shim\" \"$tasks\watchdog.ps1\""
```

`run` now guards itself against a restart race: it takes
`<repo>\.agents\state\<alias>.run.lock` and exits 4 (not 3 — that is heartbeat-check's stale
code) — one line naming the live holder's pid
and start time — while a transport already owns it; a killed transport leaves a stale marker
the next repetition takes over automatically.

The canonical liveness signal is the file
`<repo>\.agents\state\<alias>.heartbeat.json` — `at` (and the file mtime)
advances on every completed poll cycle, from either the scheduled `bus-poll
--once` or the in-transport tick. The per-cycle `gitbus: poll cycle complete`
log line is diagnostics only. The watchdog flags a beat older than
`3 × interval` (`--factor` changes it).

The watchdog's `--alert` needs the bot token, so the scheduled wrapper must load
the env file the way the launcher does. **Without the token, `heartbeat-check`
still exits 3 (stale) — but the alert cannot be posted** and the failure is only
visible in the wrapper's output: check the scheduled task's last result, not just
the channel.

## 7. How you use it day to day

**Addressing.** Write `Codex, <task>` — the alias or `@CodexBot`, a reply to one of its
messages, or anything from the admin routes to this bot. Mentioning `Hermes` instead leaves
Codex silent on purpose.

**What happens on a message.** With `type: codex`, the original mentions are preserved, the text becomes
the prompt for `codex exec --sandbox workspace-write` run in `adapter.cwd`, and the final
response is posted back as `[Codex] …`. With `type: codex_thread`, the intact message is queued
into the configured existing Codex task and a bounded receipt is posted. The task submits a
separate, explicit Telegram answer against its ticket; ordinary task chat is not forwarded.
Unanswered tickets alert after `reply_timeout_seconds` (default 600); the SQLite inbox/outbox
survives bridge restarts. A request embedded in a status update still requires action or a blocker.

The `codex_thread` route does not add a model, sandbox, or other execution override: the
destination Codex task governs its own execution. Ambient and alias-only chatter remains
suppressed, but every exact `@example_codex_bot` mention is queued into this task, including messages
that also contain status or acknowledgment wording.

**Hours-long jobs.** `timeout_seconds: 1800` is the cap; a run that exceeds it is reported as a
timeout rather than left hanging. If you routinely need longer, raise it *and* keep the group
paced — a bot that posts nothing for 30 minutes is indistinguishable from a broken bot, so ask
for a short acknowledgement first (`Codex, confirm and then run it`).

**Typical uses on this machine**

| you want | say |
|---|---|
| reproduce a failure | `Codex, run the failing test on this box and report the traceback` |
| a long run | `Codex, run the full suite three times and report any flaky tests` |
| a review | `Codex, review this diff for regressions, read-only please` *(then set sandbox to read-only if you prefer)* |
| a branch, not a merge | `Codex, fix it on a branch, open a PR and post the link` |

**What this agent will not do.** Merging and deploying stay human decisions, and `main` is
off-limits for the channel. Branches and pull requests are fine.

## 8. Commits are authored by the agent

Work Codex does is committed by **Codex**, never by you. Two layers enforce it:

1. **Automatic.** Every run the channel starts carries
   `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` = `Codex <codex@a2a.local>`
   (or whatever `agent.git_name`/`agent.git_email` say). Those variables override the
   repository's `user.name`/`user.email` for that process only — including when Codex shells
   out to `git commit` mid-task.
2. **Belt and braces**, for sessions you start by hand as well:

   ```bash
   git -C /home/codex/your-project config user.name  "Codex"
   git -C /home/codex/your-project config user.email "codex@a2a.local"
   ```

Verify at any time:

```bash
git -C <repo> log -1 --format='%an <%ae>'
```

**To make commits link to a GitHub machine account** (avatar, profile, contribution graph),
create the account and use its noreply address — the numeric id is on that account's
*Settings → Emails* page:

```yaml
agent:
  git_name: Codex
  git_email: 1234567+codexbot@users.noreply.github.com
```

Without a machine account the commits are still correctly attributed by name — GitHub simply
shows them as authored by an unlinked identity rather than by your account.

**One author per commit, no impersonation.** Never set an agent's identity to a human's address,
including yours: a commit should answer "which agent did this" at a glance. If two agents work
the same repository, they get two identities — that is the point.

## 9. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `could not start: [Errno 2]` / `queue could not start: … [WinError 2]` | the daemon's PATH has no `codex` | restart via `scripts/run_codex_telegram.*` (resolves the CLI and pins its directory) or put `codex` on PATH; for `type: codex` an absolute path in `adapter.command` also works; `check` reports it first |
| every reply is `Codex failed: …` | the CLI errored; the last stderr line is shown | run the same prompt by hand in `cwd` |
| edits refused | `--sandbox read-only` left in the template | set `workspace-write` |
| silent bot | Bot-to-Bot mode / admin rights | `SETUP-ADMIN.md` §2–3 |
| answers truncated | `max_output_chars` | raise it, or have Codex write a file and post the path |
| two Codex runs collide in one repo | each message starts a process | ask for one task per message; use git worktrees for parallel work |
| a commit is attributed to the wrong agent | a stale `GIT_AUTHOR_*`/`GIT_COMMITTER_*` is exported in that shell, and it beats the config | `unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL`, then re-check with `git log -1 --format='%an <%ae>'` |
