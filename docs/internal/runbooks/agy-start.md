# agy-start.md — start, check and stop A2A on this Windows host

**Read this file only. Do not read the source tree.** Everything needed to start A2A is
here, including every path, expected output and known failure mode.

> **Trigger:** `Start A2A using agy-start.md`
>
> **Scope of that instruction:** run §1, confirm §2, report. Nothing else. Do not explore the
> codebase, do not run the test suite, do not edit config.

---

## 0. What "A2A" is on this host

Three Windows Task Scheduler tasks. That is the whole stack.

| Task | Wrapper | Cadence | Job |
|---|---|---|---|
| `a2a-agy-transport` | `scripts\run_antigravity_telegram.ps1` | 30 min | **The Telegram bus.** Long-running `cli run` in the foreground; each tick is a restart-on-failure probe. |
| `a2a-agy-bus-poll` | `scripts\run_antigravity_bus_poll.ps1` | 30 min | **The durable git bus.** One poll cycle of `.agents\inbox\`; writes the heartbeat. |
| `a2a-agy-heartbeat` | `scripts\run_antigravity_heartbeat.ps1` | 15 min | **The watcher.** Reads the heartbeat, alerts to Telegram when it is stale. |

Repo root: `C:\path\to\swarmly`
Config: `config.yaml` · Secrets: `.env` (both gitignored)

---

## 1. Start it — one command

```powershell
cd C:\path\to\swarmly
.\scripts\start_antigravity.ps1
```

That registers (if needed), enables and starts all three tasks in dependency order, then
**verifies the transport actually came up** before reporting success.

**It is idempotent and safe to re-run.** A task whose definition already matches is left
alone; running it while the transport is already up is a no-op, not an error.

### Before you start: point the transport at the right conversation

Do this at the start of **every new conversation** — the pointer does not follow you.

```powershell
cd C:\path\to\swarmly
Set-Content .active_conversation "<this conversation's UUID>" -NoNewline -Encoding ascii
Get-Content  .active_conversation
```

`auto` (the current setting) means "most recently modified conversation for this workspace",
which is not always the one you are typing in. A bare UUID is unambiguous. Full rationale,
including why a wrong value fails silently, is in §6. No restart is needed — the pointer is
re-read on every turn.

### Read the exit code — not the scroll

| Exit | Meaning | Action |
|---|---|---|
| `0` | Started and verified. Look for the literal line `started and verified.` | Done — go to §2 |
| `2` | Pre-flight failed. **Nothing was touched.** | Read the `FATAL:` lines it printed; they name the missing file and the flag that fixes it |
| `3` | Registered and enabled, but no transport process appeared (`NOT STARTED`) | It printed the log paths. Read `antigravity.transport.host.log` first, then §5 |

> [!TIP]
> **Unsure, or want to see the plan first?** Run `-DryRun`. It prints exactly what it would do
> and changes nothing. Use it when you want to confirm the tasks would only be *enabled and
> started* rather than re-registered — the healthy case reads
> `definition already matches; leaving it as it is`.

---

## 2. Confirm it — the four receipts

Do not trust the launcher's word alone; confirm these four. Each has an exact expected shape.

```powershell
cd C:\path\to\swarmly

# 1. Task states — transport must be Running
Get-ScheduledTask -TaskName 'a2a-agy-*' | Select-Object TaskName,State

# 2. The telegram bus is authenticated and announced itself
Select-String -Path .agents\state\antigravity.transport.err.log -Pattern 'connected as|A2A_ONLINE_NOTICE' |
  Select-Object -Last 2 | ForEach-Object { $_.Line }

# 3. The durable bus wrote a fresh heartbeat
Get-Content .agents\state\antigravity.heartbeat.json -Raw

# 4. A poll cycle ran
Get-Content .agents\state\antigravity.bus-poll.err.log -Tail 5
```

| # | Receipt | Expected |
|---|---|---|
| 1 | Task state | `a2a-agy-transport` = **`Running`**; the other two = `Ready` between ticks |
| 2 | Online notice | `A2A_ONLINE_NOTICE rev=<sha> alias=Antigravity chat=-1001234567890` — **the `rev:` is the receipt** |
| 3 | Heartbeat | `{"at":<recent epoch>, ... "delivered":N, ...}` — `at` within the last ~30 min |
| 4 | Poll cycle | No `FATAL`; ideally a completed cycle line |

Or use the built-in inspector, which prints **all** of the above plus log paths in one shot:

```powershell
.\scripts\start_antigravity.ps1 -Status
```

> [!NOTE]
> **`a2a-agy-transport` sitting in `Running` continuously is the design working**, not a hang.
> Its job is to notice when the transport dies. Likewise `-Status` showing **two** pids for one
> transport is normal — the venv python is a trampoline. `Last Result` `267009` means "still
> running"; that is the steady state.

---

## 3. Day-to-day commands

```powershell
.\scripts\start_antigravity.ps1              # start (idempotent)
.\scripts\start_antigravity.ps1 -Status      # states, pids, heartbeat age, log paths
.\scripts\start_antigravity.ps1 -Stop        # disable all three, then kill the transport tree
.\scripts\start_antigravity.ps1 -Restart     # stop then start
.\scripts\start_antigravity.ps1 -DryRun      # print the plan, change nothing
```

Overrides — only needed if the checkout or config lives elsewhere:

```powershell
.\scripts\start_antigravity.ps1 -RepoDir D:\a2a -Config C:\agents\antigravity\config.yaml
```

Other flags: `-EnvFile` (default: repo `.env`), `-PythonExe` (default: repo `.venv`),
`-WaitSeconds` (default `60`, the verify window), `-Force` (re-register even if the definition
matches — **not** normally needed).

---

## 4. Where the evidence lives

Everything is under `.agents\state\`. On a scheduled task there is no console, so logs are the
only record.

| File | What it tells you |
|---|---|
| `antigravity.transport.err.log` | **The Telegram bus.** `connected as`, `A2A_ONLINE_NOTICE rev=`, inbound/outbound routing decisions |
| `antigravity.transport.host.log` | The launcher's own transcript — read this first when a start fails |
| `antigravity.bus-poll.err.log` | One line per durable-bus poll cycle |
| `antigravity.heartbeat-check.err.log` | What the watcher decided, and any stale alert it posted |
| `antigravity.heartbeat.json` | `at` = liveness; `delivered`/`deferred`/`escalations` = diagnostics |
| `antigravity.run.lock` | The transport's single-instance lock, holding the live pid |

---

## 5. Troubleshooting — failure modes actually observed on this host

| Symptom | Cause | Fix |
|---|---|---|
| `checkout` / `git checkout` refuses: *"untracked working tree files would be overwritten"* | `core.autocrlf=true` plus an untracked file that `main` also tracks (historical: `scripts\run_hidden.vbs`) | **Do not delete it.** The blob may be identical — confirm with `git hash-object <file>` vs `git rev-parse origin/main:<file>`. Move it aside, then proceed |
| Launcher exits `3`, tasks are enabled, no transport | The transport process never appeared | Read `antigravity.transport.host.log` — it records the launcher's own reason |
| Transport log says `python not found` | Task registered against a different checkout, or the venv moved | Re-run the script; it prints every resolved path. Use `-RepoDir` |
| The whole stack is silent after a reboot | Tasks `Disabled` (a deliberate `-Stop`), or the battery defaults | `-Status` shows `enabled=` per task; a plain start re-enables |
| `-Stop` leaves a process behind | A launcher outlived its task | `-Stop` sweeps by argv and prints `WARNING: still alive: pid(s) …`; kill those pids |
| A **false "beat is stale" alert** right after a cold start | The watcher ticked before the first poll cycle had written a fresh beat | Harmless and self-correcting once the cycle completes. If it persists past one poll interval, the poller is genuinely wedged — check receipt #4 |
| The **first** reply after a transport start is slow (~2–3 s), later ones are instant | Endpoint + credential recovery: the port and the per-session CSRF token are read from the live processes once, then cached for the run | **Not a fault.** Recovery runs off the transport's event loop, so heartbeats, `/ping`, `/status` and outbound sends keep working while it happens. Only investigate if *every* reply is slow |
| Every message fails fast with *"could not reach the live session endpoint … fix `ANTIGRAVITY_LS_ADDRESS` or the IDE session, then re-send"*, and the log repeats `missing CSRF token` / `invalid CSRF token` | The transport could not recover the session endpoint **or** its per-session credential. Since the fix this is reported in ~1 s and the blocking `agy -p` route is **not** taken, so the reply is an error rather than a delayed answer | See §6. Not a start failure — do not restart-loop over it |
| Replies land in an unexpected conversation | The adapter honours `.active_conversation` in `adapter.cwd` **over** `conversation: auto`, and the file is sticky — a new chat does not move it | See §6. Set it at the start of every new conversation |
| `discover` says *"No chats seen yet"* although the bot is in the group | **Not a fault.** `discover` makes one bare `getUpdates` call, and the running transport has already consumed the pending updates | Expected while the transport is up. Use `-Status` or @userinfobot to read the group id; run `discover` only with the transport stopped |

---

## 6. Three known conditions (not start failures)

> [!IMPORTANT]
> **The `agentapi` fast path needs TWO things, and the transport recovers both by itself.**
> `swarmly/telegram/endpoint.py` resolves the IDE's HTTP port **and** the per-session
> `ANTIGRAVITY_CSRF_TOKEN` that `language_server.exe agentapi` additionally requires —
> recovering the credential from the live process's own command line, because that is the
> only place it exists (nothing writes it to a file, and it rotates on every IDE start).
> Both are injected into the `agentapi` child process, so a Task Scheduler-launched
> transport reaches the fast path with **no `.env` change at all**.
>
> The port is found two ways, and a healthy host uses both: the `language_server` log's own
> startup banner, and the listener ports of the live `language_server.exe` / `agy.exe`
> processes. The second matters because the CLI (`agy`) hosts its own session too, and its
> ports are owned by a different image.
>
> **Cost:** recovery is paid **once per transport run**, not per message — measured 2.5 s
> cold, then **0 ms** from cache. It runs **off the transport's event loop**, so heartbeats,
> `/ping`, `/status` and outbound sends keep working while it happens — the transport stays
> responsive rather than frozen. It does **not** change turn semantics: an addressed message
> arriving mid-turn is still dropped, because recovery is awaited inside the adapter's turn.
> A slow *first* reply after a start is normal, and only a slow *every* reply is worth
> investigating.
>
> **Do not pin `ANTIGRAVITY_CSRF_TOKEN` in `.env`.** A pinned value goes stale as soon as
> the IDE restarts and then fails with `invalid CSRF token` — replacing an automatic
> recovery with a manual one that silently rots.
>
> **Do not pin `ANTIGRAVITY_LS_ADDRESS` either**, for the same reason: it is a per-process
> port that changes on every IDE start.
>
> If you do see `missing CSRF token` / `invalid CSRF token` in the log, the cause is a
> session that is genuinely gone (IDE closed, or the process restarted mid-dispatch), not a
> configuration gap. Delivery is refused in ~1 s with an explicit error; the transport does
> **not** sit on the 1500 s blocking path.

> [!IMPORTANT]
> **Conversation pinning — set it at the start of every NEW conversation.**
> `resolve_conversation()` prefers a `.active_conversation` file in `adapter.cwd` over
> `adapter.conversation: auto`, and that file is **sticky**: it keeps winning until you change
> it. Starting a new chat does **not** move the transport off the old one.
>
> A stale pointer fails **silently** — an unknown value is passed through rather than rejected,
> and a stale UUID that still names a real conversation simply keeps working, pointing at
> *that* conversation. Replies then land somewhere you did not intend, with no error in the log
> and no error in chat.
>
> ```powershell
> cd C:\path\to\swarmly
> Set-Content .active_conversation "auto" -NoNewline -Encoding ascii   # or the new UUID
> Get-Content  .active_conversation                                    # confirm
> ```
>
> `auto` tracks the most recently modified conversation for this workspace; a bare UUID is the
> safer choice when more than one conversation touches it. Get ids from the store with:
>
> ```powershell
> @'
> import sqlite3, pathlib
> db = pathlib.Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
> for cid, title in sqlite3.connect(db).execute(
>         "SELECT conversation_id, title FROM conversation_summaries "
>         "ORDER BY last_modified_time DESC LIMIT 5"):
>     print(cid, "|", title)
> '@ | .\.venv\Scripts\python.exe -
> ```
>
> `.active_conversation` is **untracked and not gitignored** — it appears as
> `?? .active_conversation` in `git status`. Never commit it.
>
> In chat the admin can also post `switch context to auto` or
> `switch context to "<Title or UUID>"`. **It must be the whole message** — the adapter matches
> from the start of the text, so prefixing the agent's own alias
> (`Antigravity, switch context to auto`) does not match and is dispatched as an ordinary turn
> instead.

---

## 7. Updating the code (optional — *not* part of a normal start)

Only when you specifically want the checkout refreshed before starting.

```powershell
cd C:\path\to\swarmly
git fetch origin
git log --oneline -1 origin/main          # note the sha

# Confirm nothing local is at risk, then fast-forward
git status --porcelain                     # expect no tracked modifications
git checkout main
git merge --ff-only origin/main
```

The `.venv` install is **editable**, so a checkout needs no reinstall — new code takes effect on
the next process start. If `pyproject.toml` or `uv.lock` did change, then run `uv sync --extra telegram`.

Then start normally (§1). The `rev=` in the online notice (§2, receipt 2) is how you confirm the
running transport is actually the revision you just pulled.

For a **gated** upgrade with automatic rollback, use `scripts/upgrade_transport.sh` — see
`docs/telegram/UPGRADE.md`.

> [!CAUTION]
> Never merge to `main` with a web/API merge. Per `AGENTS.md`: merges are local
> `git merge --no-ff` by the reviewing maintainer, commits are authored as the agent
> (`Antigravity <antigravity@a2a.local>`), and `main` history is never rewritten.

---

## 8. If you must restart from scratch

```powershell
# stop everything
.\scripts\start_antigravity.ps1 -Stop

# clear only the transient state (locks and heartbeat) — NOT the inbox or outbox
Remove-Item .agents\state\antigravity.run.lock -ErrorAction SilentlyContinue
Remove-Item .agents\state\antigravity.heartbeat.json -ErrorAction SilentlyContinue

# start again
.\scripts\start_antigravity.ps1
```

**Never delete `.agents\inbox\` or `.agents\outbox\`** — that is the durable bus, not state.
A stale lock does not block a start: the transport takes over a lock whose holder is gone, and
logs `took over the instance lock`.
