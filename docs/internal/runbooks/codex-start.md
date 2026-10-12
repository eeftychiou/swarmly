# codex-start.md — start the A2A stack on the Codex host (Windows)

> Prompt that uses this file: **"Start A2A using codex-start.md."**
> Audience: the Codex agent on your Windows host.
> Goal: all three A2A processes running and verified in ~2 minutes, **without reading `swarmly/`**.
> Replace each placeholder (`C:\path\to\...`, `<codex-task-uuid>`) with your host's value once, then keep this file next to the checkout.

## 0. What "the A2A bus" is here — three pieces

| # | piece | mechanism | job |
|---|-------|-----------|-----|
| 1 | **Telegram bus** (transport) | hidden daemon: `swarmly.telegram.cli run` | long-polls Telegram, queues addressed messages into the Codex task, posts receipts; also polls the gitbus on its own 30-min tick |
| 2 | **gitbus** (durable bus poller) | scheduled task `a2a-codex-buspoll`, every 30 min | `bus-poll --once`: git-fetches, reads `.agents/inbox/` from `origin/main`, delivers to the channel, writes the heartbeat |
| 3 | **watcher** (dead-man watch) | scheduled task `a2a-codex-watchdog`, every 15 min | `heartbeat-check --alert`: alerts the channel when the beat is older than 90 min (3 x 30) |

Heartbeat: `.agents/state/codex.heartbeat.json` — advanced by either poll path (scheduler or in-transport tick). It is the canonical liveness signal; the `gitbus: poll cycle complete` line is diagnostics only.

## 1. Host facts — the only map you need

| thing | value |
|-------|-------|
| repo | `C:\path\to\swarmly` (branch `main`, remote `origin`) |
| interpreter | `C:\path\to\venv\Scripts\python.exe` (= `python` on PATH). It is a uv venv shim: every start shows **two** pids (shim + real interpreter) |
| config | `C:\path\to\swarmly\config\telegram.codex.local.yaml` (gitignored) |
| env file | `C:\path\to\swarmly\.env` (holds `A2A_TELEGRAM_BOT_TOKEN_CODEX`; gitignored) |
| launcher | `scripts\run_codex_telegram.ps1 -Config <cfg> -EnvFile <env>` — runs `check`, then `run` (foreground) |
| hidden shim | `scripts\run_hidden.vbs` — how every scheduled task launches without a console window |
| state dir | `.agents\state\` → `codex.run.lock`, `codex.heartbeat.json`, `codex.transport.out.log`, `codex.transport.err.log` |
| scheduled tasks | `a2a-codex-buspoll` (30 min), `a2a-codex-watchdog` (15 min); both launch via `wscript.exe //B "<repo>\scripts\run_hidden.vbs" "C:\Users\<you>\.codex\tasks\<wrapper>.ps1"` |
| identity | alias `Codex`, bot `@example_codex_bot`, chat `-1001234567890` |
| adapter | `codex_thread` → queues into Codex task `<codex-task-uuid>` (the open Codex task that receives the channel's messages) |

## 2. Session setup + pre-flight (once per conversation)

```powershell
$repo  = 'C:\path\to\swarmly'
$py    = 'C:\path\to\venv\Scripts\python.exe'
$cfg   = "$repo\config\telegram.codex.local.yaml"
$envf  = "$repo\.env"
$state = "$repo\.agents\state"
Set-Location -LiteralPath $repo
Get-Content -LiteralPath $envf | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
        Set-Item -Path "Env:$($matches[1])" -Value $matches[2].Trim().Trim('"').Trim("'")
    }
}
& $py -m swarmly.telegram.cli --config $cfg check     # must end with: OK — config is usable.
```

If `check` fails: stop, show the user its output (the `token` line reads `present`/`absent`). Do not read the code to debug — see the table in §10.

## 3. Already running?

```powershell
if (Test-Path "$state\codex.run.lock") {
    $lock = Get-Content "$state\codex.run.lock" | ConvertFrom-Json
    "lock pid: $($lock.pid)"
    Get-Process -Id $lock.pid -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime
}
```

- pid alive → the Telegram bus is up; skip to §5.
- No lock, or pid gone → the lock is stale; the next start takes it over automatically (it logs `run: took over the instance lock (pid N is gone)`). There is no cleanup step and none is needed.

## 4. Start the Telegram bus (transport)

```powershell
$psLine = '{0} -NoProfile -ExecutionPolicy Bypass -File {1}\scripts\run_codex_telegram.ps1 -Config {1}\config\telegram.codex.local.yaml -EnvFile {1}\.env 1>>{2}\codex.transport.out.log 2>>{2}\codex.transport.err.log' -f "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe", $repo, $state
Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $psLine -WindowStyle Hidden -WorkingDirectory $repo
```

Why exactly this shape — do not improvise around it:

- **cmd /c performs the redirection** at the process level. Without it, the hidden process's console swallows everything and `.agents\state\codex.transport.*.log` stays empty — you lose all observability.
- In-script `2>>` / `*>>` is not an option: in PS 5.1 a native command's stderr becomes a terminating error under `$ErrorActionPreference = 'Stop'` (which the launcher sets).
- `Start-Process -RedirectStandardOutput/-Error` is not an option: it returns only when the redirected handles close, which a long-running daemon never does.
- Wait ~25 s, then verify in §6. The first start posts `Codex online (…, rev: …)` to the channel — expected, that is the receipt.

## 5. Gitbus + watcher (scheduled tasks)

```powershell
Enable-ScheduledTask -TaskName a2a-codex-buspoll  -ErrorAction Stop
Enable-ScheduledTask -TaskName a2a-codex-watchdog -ErrorAction Stop
```

Prove both paths once, by hand:

```powershell
& $py -m swarmly.telegram.cli --config $cfg bus-poll --once
#   expect: gitbus: poll cycle complete (delivered=… deferred=… …)   rc=0
& $py -m swarmly.telegram.cli --config $cfg heartbeat-check --alert
#   expect: heartbeat: fresh beat, age=…s   rc=0
```

If a task does not exist (not merely disabled): do not invent it — register it from `codex-install.md` §8.

## 6. Verify, then report

```powershell
Get-Content "$state\codex.transport.err.log" -Tail 10
(Get-Content "$state\codex.run.lock" | ConvertFrom-Json).pid
Get-Content "$state\codex.heartbeat.json"
foreach ($t in 'a2a-codex-buspoll','a2a-codex-watchdog') {
    Get-ScheduledTask -TaskName $t | Select-Object TaskName,State
    Get-ScheduledTaskInfo -TaskName $t | Select-Object LastRunTime,LastTaskResult,NextRunTime
}
```

A fresh start logs, in order: `run: took over the instance lock (pid …)` → `connected as @example_codex_bot (alias Codex) in chat -1001234567890` → `A2A_ONLINE_NOTICE rev=<short-sha>` → `Telegram accepted outbound message id=…` → `aiogram.dispatcher: Start polling`.

`rev=` is the running revision — it doubles as the upgrade receipt. Compare with `git -C $repo rev-parse --short HEAD`.

Report: status of the three pieces, running `rev`, poll summary (`delivered=…`), heartbeat age. Then stop.

## 7. Restart onto the latest code (the "pull and start" request)

```powershell
git -C $repo pull --ff-only
(Get-Content "$state\codex.run.lock" | ConvertFrom-Json).pid | ForEach-Object { Stop-Process -Id $_ -Force }
Start-Sleep -Seconds 4          # the cmd/powershell/shim chain exits by itself
# optional, recommended before switching: & $py -m pytest -q   (see §11 for the one known host failure)
# then §4 (start) and §6 (verify): the new ONLINE notice must show the new rev.
```

The same pull → kill → start → verify loop is required after any **config edit** (`adapter.conversation`, limits, …): the config is read once at start. When you change `conversation`, update the adapter row in §1 too.

## 8. Stop (only when asked)

```powershell
Stop-Process -Id (Get-Content "$state\codex.run.lock" | ConvertFrom-Json).pid -Force
```

Leave the scheduled tasks enabled unless the user says otherwise — they are the supervisor and the dead-man watch.

## 9. Messages that arrive from the channel (reply tickets)

When the transport queues a Telegram message for this task, it arrives as an injected user turn headed `[A2A REPLY TICKET <full-id>]`. The rules that matter:

- **Every routed message is a ticket you must resolve** — with an answer or an explicit skip. A transport receipt only means "received"; it is never completion.
- **Ticket ids are 32 chars — always use the FULL id.** Receipts print a short prefix (`ticket 402e7c64`), but `reply` / `reply-status` reject it with `unknown reply ticket`.
- Answer: `reply <FULL-ID> --text "..." --wait 45`. The reply is capped at **500 characters including the recipient** — longer is refused.
- Pure ACK / status / sign-off with no ask: `reply <FULL-ID> --skip "reason"` — resolves the ticket silently (nothing is posted), which is also how ack loops are kept off.
- Delivery proof: `reply-status <FULL-ID>` shows `state: sent` with a `telegram_message_id`. `queued`/`ready` is not sent; `--wait 45` usually gets there, and send pacing (at least 3 s apart, at most 18 per minute by default) may push a burst later than that.
- Standalone channel post (record corrections, hand-offs): `send "text"` — also paced.
- Address peers by exact **@username** (`@example_hermes_bot`, `@example_agy_bot`); an unmentioned bot ignores the message. Keep it concise and pointer-based.

Backlog triage (after an outage, tickets pile up):

```powershell
$env:PYTHONIOENCODING='utf-8'
python -c @"
import sqlite3
con = sqlite3.connect('file:C:/path/to/swarmly/config/telegram.codex.local.replies.sqlite3?mode=ro', uri=True)
con.row_factory = sqlite3.Row
q = "select id, recipient, state, created, original from replies where state not in ('sent','skipped') order by created"
for r in con.execute(q):
    print(r['id'], r['state'], '->', r['recipient'])
    print(r['original'][:300])
"@
```

States: `queued` → `overdue` (alerts capped at 2) → `sent`/`skipped`; 3 open for one recipient trigger a digest alert. Decide per message: answer what asks something; skip pure status — and say why in the skip reason, even for stale tickets.

After any `conversation` repoint, prove the route: `codex queue --thread <this task id> --message "[A2A bridge self-test]"` must exit 0, and the message then surfaces here as a user turn.

## 10. Exit codes and gotchas (learned the hard way)

| symptom | meaning / fix |
|---------|----------------|
| `run` exits **4** | another live transport holds the lock — it is already running; check §3, do not kill blindly |
| `heartbeat-check` exits **3** | beat stale (or the beat's transport block dead/degraded) — the watchdog's alert path; `--alert` needs the token env loaded (§2) |
| `run --help` still prints "exits 3" | stale help text in this revision; the real constant is **4** (`RUN_LOCK_EXIT_CODE`, issue #97) |
| logs empty after a start | you launched without the cmd-level redirection (§4) |
| two python pids per start | uv venv shim + real interpreter — normal; the run-lock pid is the real one |
| `Get-ScheduledTaskInfo -TaskName a,b` errors | PS 5.1 accepts one task name, not an array — loop |
| launcher warns "codex CLI not found" | the adapters cannot run on that start; install/repair the Codex CLI or add it to PATH |
| anything about config schema / adapters | read only `docs\telegram\SETUP-CODEX.md` (canonical) or `docs\telegram\UPGRADE.md` (upgrade + revert contract) |

## 11. Known non-blocking test failure on this host

`python -m pytest -q` reports one failure on this host:
`tests/test_telegram_channel.py::TestAdapterCommands::test_antigravity_thread_runs_without_echoing_to_telegram`
(Antigravity-adapter endpoint probe; reproduced at earlier revisions too — pre-existing, unrelated to the Codex path). Representative run at rev `798f0f4`: 448 passed, 37 skipped, that 1 failed. Counts move; the constant is the one named failure.

## 12. Do not

- Do not read `swarmly/`, `tests/`, or `scripts/` to answer start-up questions — this file is complete for them.
- Do not edit the config, `.env`, or the scheduled tasks during a *start* — that is install work (`codex-install.md`).
- Do not commit anything; starting A2A changes no tracked files.
- Do not post to the channel by any path other than the bus.
