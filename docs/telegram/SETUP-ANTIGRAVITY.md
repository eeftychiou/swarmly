# SETUP — Antigravity CLI (`agy`) on the home channel

Everything here runs **on the machine that hosts Antigravity**. It never calls another agent's CLI.

> **Windows host, in a hurry?** This is the short version — the reasoning behind it is in
> [§6a](#6a-windows-task-scheduler-instead-of-systemd). One script starts, stops and inspects
> the whole local stack (three scheduled tasks over three wrapper scripts):
>
> ```powershell
> .\scripts\start_antigravity.ps1            # start everything, then verify it really came up
> .\scripts\start_antigravity.ps1 -Status    # what is registered, enabled and running
> .\scripts\start_antigravity.ps1 -Stop      # stop everything
> .\scripts\start_antigravity.ps1 -DryRun    # print the plan, change nothing
> ```
>
> It is safe to re-run: a task whose definition already matches is left alone. Linux/macOS
> hosts use the systemd unit in §6 instead.

---

## 1. Prerequisites

| need | check |
|---|---|
| `agy` on PATH | `command -v agy && agy --version` |
| signed in | `agy -p "reply with the single word: ok"` returns text (auth is via the OS keyring / browser sign-in; over SSH it prints an auth URL) |
| BotFather bot with Bot-to-Bot Communication enabled | `SETUP-ADMIN.md` §2 |
| bot added to the group **as admin** | `SETUP-ADMIN.md` §3 |
| the three values: token, group id, your user id | `SETUP-ADMIN.md` §4–5 |

`agy -p` (print mode) is the integration point: non-interactive, **plain text only** — there is
no JSON envelope and no `--max-turns`. A print run is bounded by **`--print-timeout`** (default
`5m`, raised to `20m` in the default template).

Useful flags:

| flag | purpose |
|---|---|
| `--print-timeout 30m` | bound a long run (must stay under `timeout_seconds`) |
| `--model 'Gemini 3.1 Pro (High)'` | pick the engine — `agy models` lists the exact strings |
| `--add-dir /path` | extra context roots, repeatable |
| `--sandbox` | confine the run |
| `--dangerously-skip-permissions` | skip approvals; the default command includes it (section 7) |

## 2. Install the channel

```bash
git clone https://github.com/eeftychiou/swarmly.git ~/projects/swarmly
cd ~/projects/swarmly
python3 -m venv .venv
.venv/bin/pip install .                  # everything, incl. Ed25519 signing keys
```

## 3. Put the token in the environment, not the file

```bash
# ~/.a2a-telegram.env   (chmod 600, never committed)
A2A_TELEGRAM_BOT_TOKEN_ANTIGRAVITY=123456789:AA...antigravity-bot-token...
```

Then create this agent's bus signing key. No key ships with the repository, and without one
every durable write (`post`, escalations) is refused. On this agent's host:

```bash
# Ed25519: the private key goes into the env file (owner-only, never printed); only the
# public key is registered, in config/a2a_config.json of the bus checkout (bus.repo).
.venv/bin/swarmly-keygen --agent antigravity --env-file ~/.a2a-telegram.env \
  --registry /path/to/bus/checkout/config/a2a_config.json
```

Commit and push that registry change in the bus checkout, so every peer can verify `antigravity`.
Each peer verifies against its own checkout: until a peer has pulled this commit it rejects
`antigravity`'s messages as from an unregistered sender, so pull the bus checkout on every peer
(`git -C /path/to/bus/checkout pull`) before `antigravity` posts.
`check` (below) reports a missing signing key. Details: GETTING-STARTED §3.

## 4. Fill in the config

Copy `config/telegram.antigravity.example.yaml` → `~/agents/antigravity/config.yaml`:

```yaml
agent:
  name: AntigravityBot
  alias: Antigravity                           # addressed as "Antigravity, ..."
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_ANTIGRAVITY
  home_channel_id: -1001234567890              # the shared group
  admin_user_id: 123456789                     # you

peers:
  Hermes: HermesBot
  Codex: CodexBot

adapter:
  type: antigravity
  cwd: /home/user/your-project      # the checkout `agy` works in
  timeout_seconds: 1500                        # keep this ABOVE --print-timeout
  # Optional: tether runs to an active development conversation by ID or Title:
  # conversation: "Your Project"

# Optional: route Telegram forum topics to separate project checkouts (see docs/PROJECTS.md)
# projects:
#   webapp:
#     repo: example-org/webapp
#     cwd: /home/user/webapp
#     topics: [12, 15]
#     worktrees: true
# github:
#   login: antigravity-bot
#   poll_minutes: 5
```

Keep the two timeouts consistent: if `--print-timeout` is `30m`, set
`timeout_seconds: 2100`. A print run cut off by the outer timeout loses its output entirely.
For multi-project environments where different Telegram forum topics route to separate repositories, see [PROJECTS.md](../PROJECTS.md).

## 5. Verify, in this order

```bash
set -a; . ~/.a2a-telegram.env; set +a

.venv/bin/python -m swarmly.telegram.cli --config ~/agents/antigravity/config.yaml whoami
.venv/bin/python -m swarmly.telegram.cli --config ~/agents/antigravity/config.yaml discover
.venv/bin/python -m swarmly.telegram.cli --config ~/agents/antigravity/config.yaml check
#   -> prints the exact command it will run:
#      agy -p {prompt} --print-timeout 20m

.venv/bin/python -m swarmly.telegram.cli --config ~/agents/antigravity/config.yaml send "Antigravity online"
```

Prove the runtime independently first:

```bash
cd /home/user/your-project
agy -p "reply with the single word: ok"
```

If that returns an auth URL instead of text, finish the sign-in before starting the service.

## 6. Run it as a service

`~/.config/systemd/user/a2a-antigravity.service`:

```ini
[Unit]
Description=Swarmly Telegram home channel (Antigravity)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/user/projects/swarmly
EnvironmentFile=%h/.a2a-telegram.env
ExecStart=/home/user/projects/swarmly/.venv/bin/python -m swarmly.telegram.cli \
    --config /home/user/agents/antigravity/config.yaml run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now a2a-antigravity
journalctl --user -u a2a-antigravity -f
```

Antigravity keeps its own state under `~/.gemini/antigravity-cli/` (settings, conversations,
logs). If the service behaves oddly, read the newest file in `~/.gemini/antigravity-cli/log/`
— that is where the CLI records why a print run failed.

### 6a. Windows: Task Scheduler instead of systemd

There is no systemd on Windows, so supervision is three scheduled tasks over three wrapper
scripts. The wrappers host the transport in the **foreground** and refuse to start a second
copy, so "repetition" doubles as restart-on-failure: while the transport runs each repetition
is skipped (`IgnoreNew`); when it dies the next repetition starts it.

The transport refuses a second instance on its own as well: `run` holds
`<repo>\.agents\state\<alias>.run.lock` and exits 4 (not 3 — that is heartbeat-check's stale
code), naming the live holder's pid, so a
repetition can never race the daemon into two `getUpdates` consumers on one token.

| task | cadence | wrapper | why |
|---|---|---|---|
| `a2a-agy-transport` | 30 min | `scripts\run_antigravity_telegram.ps1` | restart-on-failure for `cli run` |
| `a2a-agy-bus-poll` | 30 min | `scripts\run_antigravity_bus_poll.ps1` | durable fallback; writes the heartbeat the watchdog reads |
| `a2a-agy-heartbeat` | 15 min | `scripts\run_antigravity_heartbeat.ps1` | dead-man watch; rc 3 = stale and alerted |

The transport task is a **supervisor tick, not a worker**: `cli run` starts as a foreground
long-running process and the task's only job is to notice when it has died. While the transport
is healthy each tick is a fast "already running" no-op. The cadence is therefore the
worst-case **restart delay**, not a polling rate — 30 minutes means a dead transport is
restarted within half an hour. Keep it comfortably below the heartbeat staleness threshold
below if you want a death to be restarted before it is alerted on.

Build them with the repo's own script, from the repo root. It registers all three, enables
them and starts them in dependency order (transport → poller → watchdog), then **checks** that
the transport is really up before reporting success:

```powershell
.\scripts\start_antigravity.ps1              # start (idempotent: matching tasks are left alone)
.\scripts\start_antigravity.ps1 -Status      # task states, live transport pids, heartbeat age
.\scripts\start_antigravity.ps1 -Stop        # disable all three, then kill the transport tree
.\scripts\start_antigravity.ps1 -Restart
.\scripts\start_antigravity.ps1 -DryRun      # print the plan and change nothing
```

Exit codes: `0` success, `2` pre-flight failed (nothing touched — the reason is printed), `3`
registered and enabled but the transport was not observed running (the logs are printed).

The script encodes exactly the settings a reliable supervisor needs — the two battery defaults
off, `IgnoreNew`, `PT0S` for the transport and `PT10M` for the two one-shot wrappers (the rationale
for each is below) — and it starts everything through `scripts\run_hidden.vbs`, not directly
through `powershell` (see the trap below). Override the defaults only if your checkout or config
lives elsewhere:

```powershell
.\scripts\start_antigravity.ps1 -RepoDir D:\a2a -Config C:\agents\antigravity\config.yaml
```

> The script is the source of truth for the three tasks and their cadences, and
> `tests/test_start_script_antigravity.py` fails if this table and the script ever disagree.
> Hand-rolling the tasks with `schtasks` is still possible — that is how this host was first
> built — but the script does the fiddly parts (the SID principal, `InteractiveToken`, the
> battery defaults, the `PT0S` cap, CRT quoting of the shim arguments) so you do not have to
> reproduce them from memory during an outage.

`DisallowStartIfOnBatteries` defaults to `true`, which silently stops both the transport and
the dead-man watch exactly when the host is unattended — the failure is then invisible,
because the thing that would report it is the thing that stopped.

Two traps worth knowing, both of which cost real debugging time:

- **The transport logs to stderr, and PowerShell 5.1 promotes a native command's stderr to a
  *terminating* error when `$ErrorActionPreference = "Stop"`.** A wrapper written as
  `& python ... *>> $log` therefore kills the transport at its first log line: the task
  reports `1` while the transport log stays empty, and it looks like a scheduling fault.
  The wrappers have cmd.exe do the redirection and wait with
  `Process.WaitForExit()`. **Do not `Start-Process -Wait` for this**: that form returns only
  when the redirected *handles* close, and the transport's own adapter spawns `agy` children
  that inherit them — so when the transport dies with a reply in flight, the launcher blocks
  inside `Start-Process` for as long as the orphaned child lives. The task then stays
  `Running`, `IgnoreNew` refuses every later start, and the transport is never restarted.
- **Never `exit` a negative child status.** A transport that was killed reports `-1`
  (`0xFFFFFFFF`); PowerShell 5.1 refuses a negative exit code and raises `System error.`,
  leaving the task's `Last Result` at `-1` — which reads as if the *launcher* crashed. The
  wrappers map it to `1` with a `was terminated` line instead. `Last Result` `-1` from an
  older run means "the transport was killed", not "the wrapper is broken".
- **A task action of `powershell -File …` allocates a console window.** With
  `LogonType=Interactive` the task runs in your session, PowerShell is a console-subsystem
  binary, and Windows therefore paints a window every time the task fires, however brief the
  work. Two plausible cures do **not** work:
  - **The `Hidden` checkbox** (`Settings.Hidden`) is a UI flag: it hides the task from the Task
    Scheduler list and does nothing to a window a started program paints.
  - **`-WindowStyle Hidden` alone** acts on a window that already exists, so it can still leave
    a brief flash.
  What works is not being a console process *yourself*: `wscript.exe` is GUI-subsystem (PE
  subsystem 2), so it allocates no console of its own, and window style 0 makes the child's
  console **created hidden** (`SW_HIDE` in `STARTUPINFO`) rather than hidden after the fact.
  The distinction is worth keeping straight: the child PowerShell **does** still get a
  console, it is simply never painted — and painting a visible window is exactly what you
  see as the flash. That is why the task action names `wscript.exe` rather than `powershell`
  (the script builds that action for you). The shim waits for the
  child and returns its exit code, so `Last Result` and the `IgnoreNew` restart semantics are
  unchanged.
- **The shim quotes arguments with the canonical CreateProcess (CRT) rules**, and this is
  load-bearing rather than cosmetic. `WScript.Shell.Run` does no parsing — it hands the string
  to `CreateProcess`, and the *target* parses it, so the rules that matter are those of
  `powershell.exe`. Naive wrapping in quotes is therefore not enough:

  ```
  -Path "C:\dir with space\"     arrives as  C:\dir with space"   (backslash ate the quote)
  -Path "C:\dir with space\\"    arrives as  C:\dir with space\   (correct)
  ```

  A backslash run is doubled when it precedes a quote, and an embedded `"` is escaped with
  `2n+1` backslashes. A plain argument is still passed through unquoted, so the live task
  actions stay byte-identical to what they name. **An empty argument is the boundary case, and
  the layer that drops it is the script host, not the shim**: `QuoteArg` renders an empty value
  as `""`, but both `cscript` and `wscript` drop an empty argument at the `WScript.Arguments`
  boundary *before* the shim ever sees it. The empty-value guarantee therefore holds at the
  function boundary only — pass empty options as an explicit placeholder, or give the script a
  default. (Getting this wrong twice, in opposite
  directions, is why the shim now records *why* the rules are CRT rules and not VBScript
  ones: the same string `C:\dir\` is literal to `wscript.exe` but escaped by `powershell.exe`.)
- **A scheduled task has no usable console for logging**: whatever it prints is gone when the
  run ends (until a human is watching the flash). Each wrapper logs to `.agents\state\`; the
  transport also keeps a `Start-Transcript` log (`antigravity.transport.host.log`). Read those
  before guessing.

Each wrapper takes `-Config` (default `config.yaml`), `-EnvFile` (default: the repo's `.env`
if present) and `-PythonExe` (default: the repo venv). Load the env file explicitly: the CLI
reads a `.env` only from the config file's folder and `~/.a2a-telegram.env` / `~/.env`, never
from its working directory, and the dead-man watch most needs its token precisely on the stale
path, when something has already gone wrong.

Expected results: `.\scripts\start_antigravity.ps1` ends with `started and verified.` only once it
has seen both the transport task `Running` **and** a live `python -m swarmly.telegram.cli … run`
process; anything less prints `NOT STARTED (rc 3)` with the log paths and exits `3`, so a
supervisor's silence is never mistaken for success. The transport task then sits in `Running`
continuously (that is the foreground design working), and running it while the transport is
already up returns `SCHED_E_ALREADY_RUNNING` (`Last Result` `-2147020576`) rather than starting a
second poller. A `Last Result` of `267009` means the task is *still running*, which for this task
is the normal steady state. `heartbeat-check` exits `0` on a fresh beat and `3` when stale.

`-Status` prints the task states, the live transport pids, the resolved bus root, the age of the
heartbeat and the path of every log — enough to diagnose "the bot is quiet" without running
anything else. The venv python is a trampoline, so one healthy transport normally shows **two**
matching pids; that is not a double run.

## 7. How you use it day to day

**Addressing.** Write `Antigravity, <task>`. Alias or `@AntigravityBot`, replies to its
messages, and anything from the admin all route here; messages addressed to another alias leave
this bot quiet.

**What happens on a message.**
- With **`type: antigravity_thread`** (recommended): The adapter locates `agentapi` (Antigravity's `language_server.exe agentapi`, found directly or through the path inside `~/.gemini/antigravity/bin/agentapi.bat`; the batch file itself is never run, because cmd.exe would parse the message) and queues the message directly into the active IDE conversation session as a `<SYSTEM_MESSAGE>` (`priority=MESSAGE_PRIORITY_HIGH`). The active session wakes up, echoes the message directly into the IDE chat window, and reacts with full conversation context and the active session model (e.g. Gemini 3.8 Flash High). If `agentapi` is unavailable, it gracefully falls back to `agy -p`.
- With **`type: antigravity`**: The mention is stripped and the text becomes the prompt for a subprocess `agy -p` in `adapter.cwd`; whatever it prints is posted back as `[Antigravity] …`. Each forum topic gets its own Antigravity conversation: the topic's first turn starts one, the adapter notes the id of the conversation agy stored for it (`~/.gemini/antigravity-cli/conversations/<id>.db`; agy prints no id), and later turns there run `agy -p … --conversation <id>`. The ids live in `antigravity-sessions-<alias>.json` beside the transport's other state. agy does not fail on an unknown id (it silently starts an empty conversation), so the adapter checks that the file still exists before resuming and starts a new conversation when it is gone. This is off when a conversation is configured (`adapter.conversation`, `.active_conversation` or `--conversation` in the command).

**Zero-Config Context Tracking & Ongoing Conversations.**
1. **Auto-Context (`conversation: auto`)**: By default, `antigravity_thread` automatically detects the most recently active conversation for `adapter.cwd` by inspecting `~/.gemini/antigravity/conversation_summaries.db`. You never need to manually update `config.yaml` when switching chats.
2. **Pinned Conversation**: Set `adapter.conversation: "<UUID or Chat Title>"` in `config.yaml` or write it to `.active_conversation` in `adapter.cwd`.
3. **In-Chat Switching (Admin)**: Post in Telegram:
   ```text
   Antigravity, switch context to "Your New Chat Title"
   ```
   (or `switch context to auto`). The adapter updates `.active_conversation` and confirms in the chat.
4. **Bi-directional SQLite Sync (Fallback Mode)**: When running in `agy -p` fallback mode, turns from the IDE (`~/.gemini/antigravity/conversations/`) are synced to the CLI (`~/.gemini/antigravity-cli/conversations/`) via atomic SQLite backups (`backup()`), and synced back after execution.


**Typical uses**

| you want | say |
|---|---|
| a third opinion | `Antigravity, review Codex's plan for the fix and say what it missed` |
| a Gemini-family cross-check | `Antigravity, independently reproduce the benchmark result and report only if you disagree` |
| a long bounded run | `Antigravity, run the test matrix and report; you have 20 minutes` |

**Bounding.** Because print mode has no turn limit, the only controls are `--print-timeout` and
`timeout_seconds`. Keep both generous but finite, and prefer asking for a short answer rather
than a long transcript.

**Approvals.** Print mode cannot ask for approval, so the default command (no
`adapter.command`) runs `agy -p ... --dangerously-skip-permissions`: every tool call
Antigravity makes runs unapproved. That is code execution for anyone who can address the bot,
so run it as a separate low-privilege user, or in a container or VM. To keep approvals, set
`adapter.command` without the flag (`--sandbox` confines the run as well). The channel adds
nothing to an explicit command, and a tool call that needs approval is then refused.
`adapter.permissions` has no effect on Antigravity: even `strict` runs the default command.

With `type: antigravity_thread` (the recommended type) a message normally goes into the open
IDE conversation through agentapi, and there the IDE's own approval settings apply, not this
command. Only when agentapi is unavailable does the channel fall back to `agy -p`; that run
takes from `adapter.command` just the skip flag and `--sandbox`, and nothing else.

**What this agent will not do.** No uploads and no pushes to `main`. Those are instructions
in its rules, not a sandbox.

## 8. Commits are authored by the agent

Work Antigravity does is committed by **Antigravity**, never by you. Two layers enforce it:

1. **Automatic.** Every run the channel starts carries
   `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` = `Antigravity <antigravity@a2a.local>`
   (or whatever `agent.git_name`/`agent.git_email` say). Those variables override the
   repository's `user.name`/`user.email` for that process only.
2. **Belt and braces**, for sessions you start by hand as well:

   ```bash
   git -C /home/user/your-project config user.name  "Antigravity"
   git -C /home/user/your-project config user.email "antigravity@a2a.local"
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
  git_name: Antigravity
  git_email: 1234567+antigravitybot@users.noreply.github.com
```

Without a machine account the commits are still correctly attributed by name — GitHub simply
shows them as authored by an unlinked identity rather than by your account.

**One author per commit, no impersonation.** Never set an agent's identity to a human's address,
including yours: a commit should answer "which agent did this" at a glance.

## 9. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| reply is an auth URL | not signed in on that machine | complete the browser/keyring sign-in |
| `timed out after 1500s (generic)` | print run exceeded the outer cap, with no attributable cause (was `timed out after 1500s`) | raise `timeout_seconds` above `--print-timeout` |
| `exceeded 1500s on the slow path` | `agentapi` resolved but the session endpoint was unreachable, so every message took the `agy -p` fallback; the bracket names agentapi's own reason | fix the endpoint, not the timeout. On current code an unreachable endpoint fails **fast**, so a 1500s block means the tree predates that guard |
| `could not reach the live session endpoint … then re-send` (reply arrives in ~1s) | the endpoint guard fired: no port, no credential, or agentapi refused with an endpoint cause | expected when the IDE session is genuinely gone. Check the IDE is up; the credential (`ANTIGRAVITY_CSRF_TOKEN`) is recovered from the live process automatically and must **not** be pinned in `.env`, since it rotates per spawn |
| empty reply | print run produced no text (killed, or model refused) | run the same prompt by hand; check `~/.gemini/antigravity-cli/log/` |
| `could not start: [Errno 2]` | `agy` not on the service's PATH | use an absolute path in `command:` |
| silent bot | Bot-to-Bot mode / admin rights | `SETUP-ADMIN.md` §2–3 |
| answers truncated | `max_output_chars` | raise it, or ask Antigravity to write a file and post the path |
| responses lack context | workspace identity follows `cwd` and the `.antigravitycli` marker | point `cwd` at the project directory |
| a commit is attributed to the wrong agent | a stale `GIT_AUTHOR_*`/`GIT_COMMITTER_*` is exported in that shell, and it beats the config | `unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL`, then re-check with `git log -1 --format='%an <%ae>'` |
| IDE agent appears dormant between Telegram turns | IDE session is event-driven and dormant between user prompts | Register a recurring 5-minute cron using `schedule(CronExpression="*/5 * * * *")` or `/schedule` in the IDE session |
| `start_antigravity.ps1` exits `2` | pre-flight found a missing file — no task was touched | read the `FATAL:` lines it printed; they name every problem and the flag that fixes it |
| `start_antigravity.ps1` exits `3` (`NOT STARTED`) | the tasks are registered and enabled but no transport process appeared | read `.agents\state\antigravity.transport.host.log` first — it records the launcher's own reason |
| task exists but nothing runs; transport log says `python not found` | the task was registered against a different checkout, or the venv moved | re-run the script; it prints every path it resolved, and `-RepoDir` overrides the checkout |
| the whole stack is silent after a reboot | the tasks are `Disabled` (a deliberate `-Stop`), or `DisallowStartIfOnBatteries` | `.\scripts\start_antigravity.ps1 -Status` shows `enabled=` per task; plain `start_antigravity.ps1` re-enables |
| `-Stop` leaves a transport process behind | a launcher outlived the task it belonged to | `-Stop` sweeps by argv and prints `WARNING: still alive: pid(s) …`; kill those pids and report it |
