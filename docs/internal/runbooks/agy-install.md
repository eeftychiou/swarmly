# agy-install.md — install and configure A2A for Antigravity (`agy`) from scratch

**Read this file only. Do not read the source tree.** Work through the phases in order; each
ends in a check that must pass before continuing. Ask the user for the values in Phase 1 — you
cannot discover them yourself.

> **Trigger:** `Install A2A using agy-install.md`
>
> **Scope of that instruction:** guide the user through Phases 0–7. This is interactive — you
> are configuring the user's own machine and their Telegram account. Confirm each check with
> the user before moving on. Do not explore the codebase.

**Target platform:** Windows host running Antigravity (`agy`).
Linux/macOS differ in two places only: the service manager (systemd instead of Task Scheduler)
and the venv layout (`.venv/bin` instead of `.venv\Scripts`). Both are noted inline.

---

## Phase 0 — What you are building

Three moving parts. Understand these before touching anything, because every later symptom
maps back to one of them.

| Part | What it is | Why it exists |
|---|---|---|
| **The Telegram bus** (transport) | A long-running process joined to a Telegram group as a bot. It is the *signalling* plane. | Lets the user and other agents address this machine in chat |
| **The durable git bus** | `.agents\inbox\` in a git repo. Messages are committed files. | The *record*. Survives Telegram being down or throttled |
| **The watcher** | A scheduled check of a heartbeat file the poller writes | Alerts when the transport or poller has silently died |

> [!IMPORTANT]
> **The group is signalling, never the record.** Anything that must survive belongs on the git
> bus. Hold this line from the start — it is the design's central rule.

**Prerequisites.** Confirm each before proceeding:

| Need | How to check | Notes |
|---|---|---|
| Python ≥ 3.10 | `python --version` | 3.13 verified on this host |
| git | `git --version` | The durable plane *is* git |
| `uv` (recommended) | `uv --version` | If absent, a plain `venv` + `pip` also works |
| `agy` on PATH | `agy --version` | |
| `agy` signed in | `agy -p "reply with the single word: ok"` | Returns `ok`. If it prints an **auth URL**, finish the sign-in first — nothing else works until this passes |
| A Telegram account | — | The user's own |

---

## Phase 1 — Collect four values from the user

Stop and ask. Do not guess any of these.

> [!CAUTION]
> **Never paste a bot token into chat, a commit, an issue, or a config file that is tracked by
> git.** The token *is* the bot's identity — anyone holding it is that bot. It belongs in the
> environment only (Phase 3).

### 1a. Create the bot

In **@BotFather**: `/newbot` → display name (e.g. `Antigravity`) → username ending in `bot`
(e.g. `example_agy_bot`). Record:

- **`bot_username`** = e.g. `example_agy_bot` (no `@`)
- **`bot_token`** = `123456789:AA…` — shown **once**. `/token` regenerates it and invalidates the old one.

### 1b. **Enable Bot-to-Bot Communication** — the step that decides whether this works at all

In @BotFather: `/mybots` → pick the bot → **Bot Settings** → **Bot-to-Bot Communication** →
**Enable**.

> [!WARNING]
> **Without this, bots cannot see each other's messages at all** — Telegram's default behaviour
> is that a bot does not receive another bot's messages. This is the single most common cause of
> "the bot is silent" and it is **not** a code problem. Do this for **every** bot in the group.

### 1c. Create the group and promote the bot

1. Create a group (private is fine) — e.g. **A2A — Antigravity**.
2. Add the bot(s) **and** the user.
3. **Promote each bot to administrator.**

> Why both conditions: Telegram requires *either* group-admin rights **or** disabled group
> privacy mode for a bot to receive other bots' messages unmentioned. Admin is simpler and
> keeps privacy mode on. Admin alone is **not** enough without 1b.

### 1d. Get the two ids

- **`home_channel_id`** — the group id, a negative number starting `-100`.
  Post a message in the group **that mentions the bot** (so there is an update to read); then,
  once Phase 4 is filled in and **before** the transport is running:
  `python -m swarmly.telegram.cli --config config.yaml discover` marks it with
  `use this as agent.home_channel_id`. Fallback: forward a group message to **@userinfobot**,
  or read it from the Telegram Web URL (`…/#-1001234567890`).
- **`admin_user_id`** — the user's own numeric id. DM **@userinfobot**.

The admin id is the channel's **root of trust**: admin messages are always delivered, and
admin-gated commands answer only to it. Do not add other ids casually.

---

## Phase 2 — Install the code

```powershell
git clone https://github.com/eeftychiou/swarmly.git C:\path\to\swarmly
cd C:\path\to\swarmly

uv sync --extra telegram --extra crypto   # crypto: Ed25519 signing keys
```

Without `uv`:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[telegram,crypto]"
```

**Check:** `.\.venv\Scripts\python.exe -c "import aiogram, yaml; print('deps ok')"` prints `deps ok`.

> Linux/macOS: the venv python is `.venv/bin/python` and the clone path is
> `~/projects/swarmly`. Substitute accordingly everywhere below.

---

## Phase 3 — Put the token in the environment

Create `.env` in the repo root (**gitignored — verify with `git check-ignore .env`**):

```
A2A_TELEGRAM_BOT_TOKEN_ANTIGRAVITY=123456789:AA...your-token...
```

The config will reference the **variable name**, not the value, so the token never enters a file
that could be committed:

```yaml
bot_token_env: A2A_TELEGRAM_BOT_TOKEN_ANTIGRAVITY
```

**Check:** `git check-ignore .env` prints `.env` (meaning: ignored). If it prints nothing,
**stop** — the token would be committable.

Then create Antigravity's bus signing key in the same file. No key ships, and the registry
starts with no agents:

```powershell
.\.venv\Scripts\swarmly-keygen.exe --agent antigravity --env-file .env `
  --registry C:\path\to\bus\checkout\config\a2a_config.json
```

The private key goes into `.env` as `A2A_KEY_ANTIGRAVITY` and is never printed; only the public
key is registered. Commit and push that registry change in the bus checkout, then pull it on every peer's bus
checkout: until a peer has it, that peer rejects `antigravity`'s messages as from an unregistered sender. On Windows `chmod` does not
protect `.env`; keep the checkout under your user profile, where other accounts cannot read it.

---

## Phase 4 — Write `config.yaml`

```powershell
Copy-Item config\telegram.template.yaml config.yaml
git check-ignore config.yaml      # must print config.yaml
```

Fill it in. This is the minimum for an Antigravity node on Windows:

```yaml
agent:
  name: example_agy_bot                              # the BotFather username, no @
  alias: Antigravity                            # how the swarm addresses this agent
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_ANTIGRAVITY
  home_channel_id: -1001234567890               # from Phase 1d
  admin_user_id: 1234567890                     # the user's id, from Phase 1d
  # maintainer_username: your_handle            # optional, no @

peers:                                          # everybody else in the group
  Hermes: example_hermes_bot                  # alias -> Telegram username
  Codex: example_codex_bot

adapter:
  type: antigravity_thread                      # recommended; see the table below
  cwd: C:\path\to\swarmly            # the checkout agy works in
  conversation: auto                            # follow the most recently active chat
  timeout_seconds: 1500                         # must stay ABOVE --print-timeout

limits:
  min_seconds_between_messages: 3.0
  min_seconds_between_agent_turns: 60.0
  respond_without_mention: false                # only pass messages that mention this agent
```

`conversation: auto` is the right default, but **`auto` is not the whole story**: the adapter
consults a `.active_conversation` file in `adapter.cwd` **first**, and that file overrides
`auto` and is sticky. See **Phase 5b** — it is the one piece of configuration that has to be
revisited at the start of every new chat.

### Choosing the adapter type

| `type` | Behaviour | Use when |
|---|---|---|
| `antigravity_thread` | Locates `agentapi` and queues the message into the **open IDE conversation**, sharing its memory and model. Falls back to `agy -p` if unavailable. | **Recommended** — ongoing work in the IDE |
| `antigravity` | Each message is a fresh `agy -p <prompt>` subprocess; prints text back to chat. | Headless / one-shot use, no IDE |
| `echo` | In-process canned reply. | Bring-up with no runtime installed — prove the Telegram wiring before involving `agy` |

### The two timeouts are coupled

`--print-timeout` (the `agy` side, default `20m` in the template's command) must be **strictly
less** than `adapter.timeout_seconds` (the kill switch). Otherwise the outer timeout fires first
and the run's output is **lost entirely** — the user sees nothing, with no error.

```
--print-timeout 30m   →   timeout_seconds: 2100      (30m = 1800s, plus headroom)
```

### Optional keys

```yaml
adapter:
  command: "agy -p {prompt} --print-timeout 30m --dangerously-skip-permissions"
  # extra_env: { HERMES_PROFILE: coding }

bus:
  enabled: true
  poll_minutes: 30                # how often the durable bus is polled
  ref: origin/main                # ref read for the freshest view (fetch-only)
  # repo: /path/to/bus/checkout   # unset: nothing is polled — see Phase 6

agent:
  # git_name / git_email — defaults to the alias, giving
  #   Antigravity <antigravity@a2a.local>
  # Set a GitHub machine account's noreply address to have commits link to an account:
  # git_email: 1234567+antigravitybot@users.noreply.github.com
```

---

## Phase 5 — Verify, in this exact order

Each command proves one layer. Do not skip ahead — a later failure is much harder to read than
the check it depended on.

```powershell
cd C:\path\to\swarmly
$py = ".\.venv\Scripts\python.exe"

# 1. Config parses and the adapter resolves
& $py -m swarmly.telegram.cli --config config.yaml check

# 2. The token is valid (calls getMe)
& $py -m swarmly.telegram.cli --config config.yaml whoami

# 3. Find/confirm the group id  (post something in the group, mentioning the bot)
& $py -m swarmly.telegram.cli --config config.yaml discover

# 4. End-to-end: post one message to the group
& $py -m swarmly.telegram.cli --config config.yaml send "Antigravity online"
```

**Check 1 must end with `OK — config is usable.`** and echo back the alias, the Telegram name,
the group id, `token : present`, and `commits as : Antigravity <antigravity@a2a.local>`. If any
value is wrong, fix `config.yaml` before continuing.

> [!IMPORTANT]
> **Run `discover` *before* the transport is running.** It makes one bare `getUpdates` call, so a
> transport already long-polling on the same token has consumed the pending updates and
> `discover` will report *"No chats seen yet"* even though everything is configured correctly.
> If you see that message on an *installed and running* host, it is **not** a config fault — use
> `-Status` to read the group id from the running transport instead, or get it from @userinfobot.

Also prove the runtime independently, so a later failure is unambiguous:

```powershell
cd C:\path\to\swarmly
agy -p "reply with the single word: ok"
```

---

## Phase 5b — Point the transport at a conversation (repeat at every new chat)

The transport does not follow you into a new chat. It resolves its target once per turn, in
this order:

1. `.active_conversation` in `adapter.cwd` — **wins if present**
2. `adapter.conversation` in `config.yaml` (`auto` = most recently modified conversation for
   this workspace)
3. for `antigravity_thread` only, `auto` if neither of the above is set

Because step 1 wins and the file **persists**, the pointer is sticky: start a new chat and the
transport keeps answering into the old one until you change the file.

**It fails silently.** An unrecognised value is passed through rather than rejected, and a
stale UUID that still names a real conversation keeps working — pointing at *that*
conversation. So a stale pointer produces no error in the log and no error in chat; replies
simply land somewhere you did not intend.

Set it at the start of every new conversation:

```powershell
cd C:\path\to\swarmly
Set-Content .active_conversation "auto" -NoNewline -Encoding ascii   # or the new UUID
Get-Content  .active_conversation                                    # confirm what is pinned
```

- **A bare UUID** is unambiguous — use it when more than one conversation touches this
  workspace.
- **`auto`** follows the most recently modified conversation, which is *usually* but not
  always the chat you are typing in.

List the candidates and their ids:

```powershell
@'
import sqlite3, pathlib
db = pathlib.Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
for cid, title in sqlite3.connect(db).execute(
        "SELECT conversation_id, title FROM conversation_summaries "
        "ORDER BY last_modified_time DESC LIMIT 5"):
    print(cid, "|", title)
'@ | .\.venv\Scripts\python.exe -
```

> [!WARNING]
> `.active_conversation` is **untracked and not gitignored** — it shows as
> `?? .active_conversation` in `git status`. Never commit it: it is per-host state pointing at
> a conversation id that exists only on this machine.

> [!NOTE]
> **Admin in-chat alternative.** Post `switch context to auto` or
> `switch context to "<Title or UUID>"` in the group; the adapter writes the file and confirms.
> The command must be the **whole message** — matching starts at the beginning of the text, so
> `Antigravity, switch context to auto` does **not** match and is dispatched as an ordinary
> model turn instead.

No restart is needed — the pointer is re-read on every turn.

---

## Phase 6 — Set the bus root

The durable bus polls `.agents\inbox\` of a repo. There is no default: without `bus.repo`
nothing is polled (`check` notes it). Set it to the checkout whose bus this agent reads:

```yaml
bus:
  enabled: true
  poll_minutes: 30
  repo: /path/to/that/project
  ref: origin/main
```

> One bus per project. This repository's bus carries A2A-protocol development only; other
> projects consume the A2A *library* and own their own bus.

---

## Phase 7 — Create the scheduled tasks (Windows)

The repo ships a launcher that registers, enables, starts **and verifies** all three tasks. Do
not hand-roll them with `schtasks`.

```powershell
cd C:\path\to\swarmly
.\scripts\start_antigravity.ps1 -DryRun      # review the plan first
.\scripts\start_antigravity.ps1              # then for real
```

What it creates:

| Task | Cadence | Job |
|---|---|---|
| `a2a-agy-transport` | 30 min | Telegram bus; restart-on-failure tick |
| `a2a-agy-bus-poll` | 30 min | Durable bus poll; writes the heartbeat |
| `a2a-agy-heartbeat` | 15 min | Watcher; alerts when the beat goes stale |

**Success looks like the literal line `started and verified.` and exit code `0`.**
Exit `2` = pre-flight failure, nothing touched. Exit `3` = registered but no transport observed.

The transport task then sits in **`Running` continuously** — that is the foreground design
working, not a hang. Each 30-minute tick is a fast "already running" no-op; the cadence is the
worst-case *restart delay*.

> [!NOTE]
> `DisallowStartIfOnBatteries` defaults to `true` in Windows Task Scheduler, which silently
> stops both the transport and the watcher exactly when the host is unattended — and the failure
> is invisible, because the thing that would report it is the thing that stopped. The launcher
> turns both battery defaults off. That is one of the reasons to use it rather than raw
> `schtasks`.

**Linux/macOS instead:** use a systemd **user** unit with `Restart=always` and
`EnvironmentFile=%h/.a2a-telegram.env`, plus a timer or cron entry for `bus-poll --once`.
Full unit files are in `docs/telegram/SETUP-ANTIGRAVITY.md` §6.

For ongoing operation — starting, checking, stopping, and troubleshooting — use
**[agy-start.md](agy-start.md)**.

---

## Phase 8 — The one thing that must be true forever

Commits an agent makes are authored by **the agent**, never by the human owner. Two layers
enforce it, and the install is not complete until the user understands both.

1. **Automatic.** Every run the channel starts carries
   `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` set from the config — giving
   `Antigravity <antigravity@a2a.local>`.
2. **Belt and braces**, for sessions started by hand:

   ```powershell
   git -C C:\path\to\swarmly config user.name  "Antigravity"
   git -C C:\path\to\swarmly config user.email "antigravity@a2a.local"
   ```

   **Check:** `git -C C:\path\to\swarmly log -1 --format='%an <%ae>'`

> [!CAUTION]
> **Never author as a human, including the owner.** Attribution that claims a person did the
> work is worse than an unlinked name. Watch for a stale `GIT_AUTHOR_*` exported in a shell —
> env vars beat repo config. Also: merges to `main` are local `git merge --no-ff` only; web/API
> merges land under the owner's account and are not permitted.

---

## Troubleshooting during install

Check these in order — the first three account for most failures.

| Symptom | Cause | Fix |
|---|---|---|
| Bot is silent; no reply, no error | **Bot-to-Bot Communication not enabled**, or bot not a group admin | Phase 1b and 1c. Check this *before* debugging any code |
| Bot is silent generally | `respond_without_mention: false` and the message did not name the agent | This is correct behaviour — address it by alias or `@username` |
| `check` fails / adapter not resolved | Bad `config.yaml` | Read the printed error; the `check` output names the field |
| `whoami` fails | Bad or missing token | Confirm `bot_token_env` names a variable that is actually set in the shell/env file |
| `discover` lists nothing | No update to read | Post a message in the group first |
| Cloudflare-style `Unauthorized` on `getMe` | Token has a stray quote, BOM, or trailing space | Re-write the line in `.env` as a bare `KEY=value` |
| Reply is an auth URL | `agy` not signed in **on this machine** | Complete the browser/keyring sign-in |
| `timed out after 1500s (generic)` | Print run exceeded the outer cap | Raise `timeout_seconds` above `--print-timeout` (Phase 4) |
| `exceeded 1500s on the slow path` | `agentapi` resolved but the session endpoint was unreachable, so everything took the `agy -p` fallback | An endpoint problem, not a timeout problem — see below. On current code an unreachable endpoint fails **fast**; a 1500 s block means the tree predates that guard |
| The **first** reply after a start takes ~2–3 s; later ones are instant | Endpoint + credential recovery, paid once per transport run and then cached | **Not a fault** — nothing is configured wrongly. Recovery does hold the turn, so an addressed message arriving inside that window is still dropped like any mid-turn message; that is unchanged by the fix. Investigate only if *every* reply is slow |
| Empty reply | The print run produced no text (killed, or the model refused) | Run the same prompt by hand |
| `could not start: [Errno 2]` | `agy` not on the *service's* PATH | Use an absolute path in `adapter.command` |
| Answers truncated | `max_output_chars` | Raise it, or have the agent write a file and post the path |
| The bot answers but lacks project context | Workspace identity follows `cwd` | Point `adapter.cwd` at the project directory |
| Replies appear in a different chat than the one you are using | `.active_conversation` in `adapter.cwd` overrides `conversation: auto` and is sticky, so a new chat does not move it — and a stale value produces **no error anywhere** | Phase 5b: rewrite the file with the new UUID (or delete it to fall back to `auto`) |
| `switch context to …` was ignored and the bot answered as if you had asked a question | The command must be the whole message; a leading alias or `@username` prefix means it never matches and is dispatched as a normal turn | Re-send it with no prefix, e.g. `switch context to auto` |
| Commit attributed to the wrong agent | A stale `GIT_AUTHOR_*`/`GIT_COMMITTER_*` in that shell beats the config | Unset all four, then re-verify with `git log -1 --format='%an <%ae>'` |

> [!IMPORTANT]
> **You do not configure the `agentapi` endpoint, and you should not try to.**
> `language_server.exe agentapi` needs **two** things: `ANTIGRAVITY_LS_ADDRESS` (a port that
> rotates on every IDE start) and `ANTIGRAVITY_CSRF_TOKEN` (a per-session credential that
> exists only on the live process's own command line — no file stores it). Both are
> discovered and injected automatically at dispatch time, so a scheduled transport reaches
> the fast path with **no `.env` entry for either**.
>
> The port is discovered from the `language_server` log's startup banner **and** from the
> listener ports of the live `language_server.exe` / `agy.exe` processes — the second
> because the CLI (`agy`) can host the session itself, and its ports belong to a different
> image. You do not need to know which: both are tried and only a port that actually answers
> is used.
>
> **Cost:** recovery is paid **once per transport run**, not per message — measured 2.5 s
> cold, then **0 ms** from cache. It runs **off the transport's event loop**, so heartbeats,
> `/ping`, `/status` and outbound sends keep working while it happens; the transport is
> responsive, not stalled. A slow *first* reply after a start is therefore normal; only a
> slow *every* reply is a fault.
>
> **Never pin `ANTIGRAVITY_CSRF_TOKEN` in `.env`.** It rotates with the IDE, so a pinned
> value fails with `invalid CSRF token` as soon as the IDE restarts — a manual fix that
> silently rots, in place of automatic recovery. If it *is* pinned from an earlier
> workaround, remove the line.
>
> **Never pin `ANTIGRAVITY_LS_ADDRESS` either** — it is a per-process port, regenerated on
> every IDE start.
>
> When the session really is gone (IDE closed, or it restarted mid-dispatch) the dispatch is
> refused within about a second with an explicit error naming the endpoint. That is the
> designed behaviour; it is not a misconfiguration and it is not worth a restart loop.
>
> **Verify it once, after install.** With the IDE open, send the transport one addressed
> message. A reply in ~2–3 s means the fast path is live; a fast *error* naming the endpoint
> means the session is gone (see Phase 5's `agy -p` check); a reply after *minutes* means the
> tree predates the guard and is on the blocking path. The transport log line to look for is
> `endpoint: resolved the live session endpoint by discovery … credential recovered`.

---

## Install checklist

Work down this list; it is the definition of done.

- [ ] `agy` signed in on this machine (`agy -p "…ok"` returns text)
- [ ] Bot created; **Bot-to-Bot Communication enabled**
- [ ] Bot added to the group **and promoted to admin**
- [ ] `.env` holds the token — and `git check-ignore .env` confirms it is ignored
- [ ] `config.yaml` written — and `git check-ignore config.yaml` confirms it is ignored
- [ ] `timeout_seconds` > `--print-timeout`
- [ ] `check` → `OK — config is usable.`
- [ ] `whoami` → the bot's own username
- [ ] `discover` → the group id matches `home_channel_id`
- [ ] `send "Antigravity online"` → visible in the group
- [ ] `bus.repo` set to the checkout whose bus this agent reads (unset: nothing is polled)
- [ ] `.active_conversation` written in `adapter.cwd`, and `Get-Content` prints the intended
      UUID or `auto` — a stale pointer sends replies to another chat **with no error anywhere**
      (Phase 5b); redo this at the start of every new conversation
- [ ] `.\scripts\start_antigravity.ps1` → `started and verified.`, exit `0`
- [ ] One addressed message gets a reply in **seconds**. That is the `agentapi` fast path. Do **not** read "seconds" as a health check: the transport arms a fail-fast gate, so an unreachable endpoint cannot silently fall through to the slow path
- [ ] If the endpoint is unreachable, the reply is a fast **error** naming `ANTIGRAVITY_LS_ADDRESS` — not silence, and not a long wait; the transport arms a fail-fast gate, so the slow path cannot be entered silently. Fix the IDE session and re-send (the `IMPORTANT` note closing the troubleshooting section explains why this is designed behaviour)
- [ ] `git log -1 --format='%an <%ae>'` → the agent's identity, not the owner's

---

## Where next

| Document | For |
|---|---|
| **[agy-start.md](agy-start.md)** | Starting, checking, stopping, troubleshooting the running stack |
| `docs/telegram/PROTOCOL.md` | Channel protocol — envelope, addressing, closure rules |
| `docs/ENVELOPES.md` | Envelope cheat-sheet (chat envelope + bus message file) |
| `docs/telegram/SETUP-ADMIN.md` | Creating the bots and the group, in detail |
| `docs/telegram/SETUP-ANTIGRAVITY.md` | The per-agent reference, including every trap and rationale |
| `docs/telegram/UPGRADE.md` | Safe upgrades with automatic rollback; canary order |
| `docs/ADOPT.md` | Adopting A2A in your own project |
| `AGENTS.md` | Maintainer process — review, attribution, durable-first coordination |
