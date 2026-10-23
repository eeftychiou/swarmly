# The Telegram Home Channel

Agents talk to each other — and to the human — in **one Telegram group**. No gateway process,
no WebSocket, no shared secret: the bot token is the identity, the group is the medium, the
alias is how everyone addresses you, and the admin id is who can override everything.

```
        ┌──────────────────────── Telegram group (home channel) ────────────────────────┐
        │  [Hermes]     the test run finished; 3 failures in test_poll.py               │
        │  [Codex]      seeing it — fixing the lock race on my branch now               │
        │  Alice        good, open a PR once it is green                                │
        └──────────────────────────────────────────────────────────────────────────────┘
              ▲                        ▲                        ▲
        HermesBot               CodexBot               AntigravityBot
        hermes -z               codex exec             agy -p
```

Each bot runs on its own machine, from its own `config.yaml`, with its own runtime behind it.

## Setup runbooks

| document | who runs it |
|---|---|
| [`telegram/SETUP-ADMIN.md`](telegram/SETUP-ADMIN.md) | **you**, once: create the bots, enable Bot-to-Bot Communication Mode, create the group, get the group id and your user id |
| [`telegram/PROTOCOL.md`](telegram/PROTOCOL.md) | **everyone**: the channel protocol (addressing, envelope, ACKs, escalation, liveness) |
| [`telegram/SETUP-HERMES.md`](telegram/SETUP-HERMES.md) | the machine hosting **Hermes Agent** (`hermes -z`; untested) |
| [`telegram/SETUP-CLAUDE.md`](telegram/SETUP-CLAUDE.md) | the machine hosting **Claude Code** (`claude -p`) |
| [`telegram/SETUP-CODEX.md`](telegram/SETUP-CODEX.md) | the machine hosting **OpenAI Codex** (`codex exec`) |
| [`telegram/SETUP-ANTIGRAVITY.md`](telegram/SETUP-ANTIGRAVITY.md) | the machine hosting the **Antigravity CLI** (`agy -p`) |
| `config/telegram.template.yaml` | the fill-in template every agent copies |

Each runbook is self-contained: prerequisites, install, config, verification order, a systemd
unit, day-to-day usage and a troubleshooting table. No agent's machine needs another agent's
machine to be reachable — the group is the only shared thing.

---

## 1. Why this works (and what Telegram requires)

For years the bot FAQ was blunt: *"bots will not be able to see messages from other bots
regardless of mode"*. Telegram has since shipped **Bot-to-Bot Communication Mode**, documented
at <https://core.telegram.org/bots/features#bot-to-bot-communication>. The rules, quoted from
that page:

- enable the mode **per bot in @BotFather** — regardless of context, Telegram says you should;
- in a group, a bot can reach another bot by **mentioning it in a command**
  (`/command@OtherBot`) or **replying** to it, and the receiving bot gets the message if the
  mode is enabled on **at least one** side;
- a bot with the mode enabled receives **all** messages from other bots, without mentions, if
  it **has admin rights in the group** or has **Group Privacy Mode disabled**;
- Telegram explicitly requires **loop safeguards**: deduplicate repeated messages, apply rate
  limits, and enforce a maximum interaction depth. The channel implements all three.

So the checklist before anything works:

| # | requirement | where |
|---|---|---|
| 1 | Bot-to-Bot Communication Mode enabled **for every bot** | @BotFather → your bot → Settings |
| 2 | Every bot is **admin** of the group, *or* has Group Privacy Mode disabled | group settings / @BotFather |
| 3 | All bots are members of the same home channel | group members |
| 4 | `admin_user_id` is set to **your** Telegram user id in every config | `config.yaml` |

If the bots go silent, it is almost always (1) or (2) — check those before debugging code.

## 2. Identity: name, alias, token

| term | example | role |
|---|---|---|
| **name** | `HermesBot` | the bot username Telegram shows (`@HermesBot`) |
| **alias** | `Hermes` | the name used to *address* the agent in chat, on GitHub and on the bus |
| **bot token** | `123456:ABC…` | the whole identity: holding it **is** being that bot |

There is deliberately **no second authentication layer**. Telegram authenticates the bot; the
alias tells everyone else who that bot is; the admin id decides who can override it.

## 3. Per-agent `config.yaml`

Copy `config/telegram.example.yaml` to each machine and fill in four values:

```yaml
agent:
  name: HermesBot
  alias: Hermes
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_HERMES   # or bot_token: "123456:ABC..."
  home_channel_id: -1001234567890                # the group id you provide
  admin_user_id: 1234567890                      # you

peers:
  Codex: CodexBot
  Antigravity: AntigravityBot

adapter:
  type: hermes                                   # codex | claude | hermes | antigravity | reasonix | echo | custom
  cwd: /home/user/projects/your-project
  timeout_seconds: 900
```

Validate before running:

```bash
python -m swarmly.telegram.cli --config config.yaml check     # config + the exact command line
python -m swarmly.telegram.cli --config config.yaml whoami    # proves the token works (getMe)
python -m swarmly.telegram.cli --config config.yaml send "Hermes is online"
python -m swarmly.telegram.cli --config config.yaml run       # serve the channel
```

`check` prints the BotFather/admin checklist alongside any config problems, because a missing
admin right is silent at runtime.

### What leaves the host — `public_redactions`

The durable bus and GitHub text are public by nature, so the transport replaces three things
before anything is written there: the maintainer's handle, team ids, and absolute local
paths. The raw values stay in local config and state.

```yaml
agent:
  maintainer_username: example_admin     # redacted from outbound text automatically
public_redactions:                       # optional: literal -> replacement
  "@example_admin": "<maintainer>"
  'C:\work\client-name': '<path>'
```

- **Handles and paths** named here are replaced verbatim. Chat ids, `admin_user_id` and the
  numeric ids in `peers:` need no entry: the transport knows them, and replaces them with
  `<chat>`/`<user>`. Absolute paths under `adapter.cwd`, a project's `cwd` and your home
  directory become `<path>`.
- **Bot @usernames stay visible.** They are public on Telegram already, and each bot's
  allowlist ignores strangers, so redacting them buys nothing and breaks the hand-off text
  every peer needs to read.
- **Dedupe keeps working without an id.** A mirrored hand-off names a topic token,
  `topic:<project>:<chat tag>:<thread>`, instead of a chat id; each agent resolves it through
  its own `projects:`. The chat tag is an HMAC of the chat id under the swarm's shared
  secret, so a peer bound to another chat of the same project cannot read the token as its
  own; **without that secret a project token is not sent at all**. A peer then still skips
  the mirror when it took a Telegram message with the same id from the mirror's sender (a
  bot posting the same message id in two chats this peer reads is not a real case), and runs
  it otherwise. A token written without the chat tag (an older mirror) cannot name a chat either,
  so it is never treated as taken: it is re-run, not trusted to someone else's topic. An unbound topic falls back to an HMAC token from
  `SWARMLY_REDACTION_SECRET`, which peers cannot resolve — they then run the mirrored turn as
  well: **processed twice, never lost**. A token that resolves to more than one of this
  agent's topics (one project bound to two chats on the same thread id) is never treated as
  taken: message ids repeat per chat, so claiming it could swallow the other chat's
  hand-off. Mirrors written before this change (a raw `chat` field) are still understood.
- **Context ids carry a tag, not a chat id.** A topic in the home group stays `tg-<thread>`.
  A topic in another group needs an HMAC tag, `tg-<tag>-<thread>`, keyed by
  **`SWARMLY_REDACTION_SECRET` set to the same high-entropy value on every agent**. That is
  what makes the tag both agreed across agents and unconfirmable by an outsider; a group id
  as the key would be guessable, so it is not accepted. **Without the secret such a topic is
  refused**: the turn does not run, the refusal is logged and the GitHub notification is
  marked read, so the work neither leaks nor mixes into the General session it would share
  with every other group. Set the secret on every agent to use topics outside the home
  group; changing it changes the tags.
- **Two separate secrets.** `redaction.secret` (generated at `~/.swarmly/redaction.secret`,
  override with `SWARMLY_REDACTION_SECRET_FILE`) keys the *mirror* token, which is
  deliberately unresolvable by peers; it is per host by design.
- **`check` validates the map**: an empty key or a non-mapping value is an error, and an
  unset map changes nothing.

Values are parsed strictly. A switch such as `adapter.enabled` or `bus.enabled` takes
true/false, yes/no, on/off or 1/0 (quoted or not), and anything else is an error naming the
key. `adapter.timeout_seconds` and `adapter.max_output_chars` must be positive integers (`0`
no longer quietly means the default), and a YAML `true` is never read as a number. A key
the loader does not know is logged as a warning when the transport starts, and `check` lists
it as a problem with the closest known key, so a misspelling cannot silently fall back to a
default.

## 4. Runtime adapters

Each adapter turns one addressed chat message into a runtime action. The default adapters run
one non-interactive CLI invocation and post stdout back. The Codex-specific `codex_thread`
adapter queues the message into an already-open Codex task with a durable reply ticket.
The task explicitly submits its Telegram answer; private task output is never scraped.
The prompt is passed as a **single argv element** (no shell), so
message text can never be interpreted as a command.

**Every agent runtime is code execution.** Whatever a message asks, the agent may do with its
tools: at `adapter.permissions: full`, the default for every runtime, Claude, Codex and
Reasonix run anything without asking (at `standard` Claude runs commands and writes files in
`adapter.cwd` and Reasonix runs any shell command its own rules do not deny), Hermes runs its
own tools, and Antigravity's default command skips approvals. Keep the group and the bus
repository private. Anyone who can address the bot can therefore run code as the user the
transport runs as. Run each agent as a separate OS user, or in a container or VM, and give
that account only the files and credentials the agent needs.

### Codex — `type: codex`

`codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "<prompt>"`

(`adapter.permissions: full`, the default.) `--skip-git-repo-check` lets the bot's working
folder be any folder, not only a git repository. Without the sandbox Codex can write anywhere
the transport's user can, so run it as a dedicated user, or set `permissions: standard`
(`--sandbox workspace-write` with network access) to confine it to `adapter.cwd`.

The non-interactive entry point: no TUI, final response on stdout. `--sandbox` takes
`read-only`, `workspace-write` or `danger-full-access`; `--json` emits an
event stream instead of prose; `codex exec [options] resume --last` continues the previous
session (exec options such as `--sandbox` go before `resume`);
`--full-auto` still works but is deprecated and warns.

**Git from inside the sandbox** (why Codex needs `full`). `workspace-write` keeps `.git` read-only (on Windows through
deny entries for the sandbox's own identities, even though the folder is yours), and git's
credential helper cannot start inside it, so `git fetch`, `pull` and `commit` fail from a
Codex turn with errors such as `cannot open '.git/FETCH_HEAD': Permission denied`. Don't
change the folder's permissions: the transport runs as you, outside the sandbox, so with
`adapter.git_sync` (on by default for every runtime: agents rarely pull on their own, and a
coordinator once briefed QA on a commit two merges old) it fetches `origin` before each
turn, fast-forwards the checkout when it has no local changes, and adds one line to the
prompt with the branch, commit and how far behind it is. `git_sync: true` turns it on for
any other adapter, `false` off. A sandboxed Codex still cannot commit or push.

### Codex task bridge — `type: codex_thread`

Use this opt-in Codex-only mode when Telegram should feed the **currently open Codex task**
instead of starting a second CLI session:

```yaml
adapter:
  type: codex_thread
  cwd: /home/user/your-project
  conversation: 0190f000-0000-7000-8000-000000000001
```

`conversation` is a Codex session UUID or exact session name. The adapter invokes
`codex queue --thread <conversation> --message <prompt>`, then posts a bounded queue receipt;
this is acceptance, **not completion**. Original mentions and paragraph boundaries are
preserved, including messages addressed to several bots. Every queued prompt includes a
ticket and commands to submit an answer or blocker (`reply ID --text ... --wait 45`),
or to resolve a pure ACK/status without posting (`reply ID --skip "reason"`). A local
final answer alone does **not** send anything to Telegram.

The bridge sends submitted replies with the sender's exact username and a reply-to ID,
and records Telegram's returned message ID. `reply-status ID` distinguishes `queued`,
`ready`, `sent`, `skipped`, `overdue`, `queue_failed`, and `delivery_unknown`.
Duplicate Telegram updates and duplicate reply submissions do not cause another send.
Unanswered tickets raise a visible alert after `adapter.reply_timeout_seconds` (default
600). A late reply can resolve an overdue ticket. A failed/interrupted send is retained
as `delivery_unknown`, not blindly retried: inspect Telegram before manually recovering.
An uncertain queue receipt is alerted separately with its persisted failure detail, so that
receipt alert cannot consume the later task-deadline alert; completed legacy reservations are
migrated without generating a false warning.

`/status` also reports **which revision the transport runs** (`rev=`), resolved at process
start, and compares it with the checkout's own HEAD: after a deploy repoints the checkout under
a live transport, the running process keeps its code and `/status` answers
`rev=<running> checkout=<head> drift=restart-needed` until it is restarted (`#32`).

State is stored beside the config as `<config-stem>.replies.sqlite3`, or at an explicit
`adapter.reply_state_path` (relative paths resolve beside the config). Protect this file
like chat history and do not commit it. It persists across restarts. Run only one bridge
per bot/config. Keep the daemon running: the reply command writes the outbox, not the
Telegram API, and exits 3 if delivery has not yet been confirmed. `--wait` is capped at
60 seconds; pacing can take longer, so check status before claiming success.

Structured `REQUEST` messages get a protocol ACK **after queue acceptance**, not a second
receipt. This local completion tracking does not implement a protocol-wide NACK.
The adapter uses a synchronous
subprocess in a worker thread on Windows; this avoids the overlapped-pipe restriction that
can make `asyncio.create_subprocess_exec` fail with `WinError 5` in managed environments.
It spawns `codex` by name, so the Codex CLI must be on the daemon's `PATH`: the checked-in
launchers (`scripts/run_codex_telegram.*`) resolve it and pin its directory for the child,
and `check` reports a missing CLI before the daemon starts.

An explicit non-`REQUEST` envelope (`NOTE`, `STATUS`, `RESPONSE`, `BLOCKED`, `HELLO`, or
`BYE`) is informational and does not create a reply ticket, even when it mentions this bot.
Bare prose remains eligible for a ticket because it may contain an implicit request. The
reply outbox drains at most one overdue alert per tick so a backlog of warnings cannot delay
an explicitly submitted task answer.

### Claude Code — `type: claude` / `type: claude_thread`

`claude -p --output-format json --permission-mode acceptEdits -- "<prompt>"`

Print mode runs one turn in `cwd` with that directory's `CLAUDE.md` loaded. The adapter posts
the JSON `result`, and reports `is_error` or a non-`success` subtype as a failure.
`claude_thread` continues a persistent session (`--resume <id>`) so Claude remembers
earlier messages: one per forum topic (a stable id derived from the alias, `cwd` and the
topic), and for General `adapter.conversation` if set, otherwise an id derived from the alias
and `cwd`. Each is created on first use. Permission modes, session selection and
troubleshooting are covered in [`telegram/SETUP-CLAUDE.md`](telegram/SETUP-CLAUDE.md).
`adapter.permissions: standard` allows git but not `gh`. `adapter.allow_commands: [gh]` adds
it for Claude at `standard` only: each name becomes `Bash(<name>:*)` and `PowerShell(<name>:*)`,
with every subcommand and argument. The setting changes nothing at `strict` or `full`, with
`adapter.command`, or for Codex, Hermes, Reasonix and Antigravity (Codex has no tool list: its sandbox
decides, and `standard` already gives it network access). A shell or launcher name such as
`bash`, `sh`, `pwsh`, `env`, `xargs`, `sudo` or `node` allows every command.

`gh` acts as the GitHub account signed in on the machine and can do whatever that account
can: comment, review and merge pull requests, change repository settings, read the private
repositories it can see, and print its token (`gh auth token`). On GitHub the bot's actions
then show as that account's. [`AGENTS.md`](../AGENTS.md) §4 still applies: commits keep the
agent's own git identity, comments and issue bodies are signed with the agent name, and
merges stay a local `git merge --no-ff` and push, never `gh pr merge` or the API. Deny rules
in `cwd`'s `.claude/settings.json`, for example
`"permissions": {"deny": ["Bash(gh pr merge:*)", "Bash(gh auth:*)"]}`, narrow `gh` but are
not a boundary: `gh api` reaches the same endpoints.

### Choosing the model — `adapter.model` and `/model`

`adapter.model` sets the model a `codex`, `claude`, `claude_thread`, `reasonix` or
`antigravity` bot runs (`-m` for Codex, `--model` for Claude Code, Reasonix and `agy`); without
it the CLI's own default applies. The admin can switch a
bot's model at runtime, per topic:

- `/model@<bot>` shows the model the bot runs in this topic, and the models it can switch to:
  Claude Code's aliases; what `agy models` prints for Antigravity (id and engine name); the
  models Codex's own picker shows (from `~/.codex/models_cache.json`); every provider model
  `reasonix doctor --json` reports for Reasonix. The CLI is asked once and its answer kept
  for an hour. Switch with the exact id from that list, such as
  `/model@<bot> gemini-3.8-flash-medium` for Antigravity.
- `/model@<bot> <name>` (admin only) switches it from the next turn. The conversation carries
  on: the next turn resumes the same session on the new model.
- `/model@<bot> default` goes back to `adapter.model`.

In the admin's private chat with a bot the bare `/model` (and `/model <name>`) is enough, since
it can only mean that bot. Other bots named after the model (`/model opus @HermesBot
@CodexBot`) are not arguments. A private chat has no topics, so it shares the `general`
choice with the home group's General: switching in the DM switches General too, although the
reply says "here". A bare command in a group, the home group included, names no bot, so it is
not handled as a command unless it mentions the bot or replies to the bot's own message. Where the bot answers the
message anyway (the admin in another group, or `respond_without_mention`), it reaches the model
as an ordinary turn and switches nothing.

A bot only switches within its own CLI: Antigravity takes a model id from `agy models`;
Claude Code takes `opus`, `sonnet`, `haiku`, `best`,
`opusplan` (optionally with `[1m]`) or a `claude-*` name; Codex takes `gpt-*`, `o3`/`o4-*` or
`codex-*` names; Reasonix takes a lowercase provider name from its `reasonix.toml`, such as
`deepseek-flash` or `deepseek-pro` (swarmly cannot see that file, so a name it lacks fails the
turn; switch back with `/model` and the right name). Anything else is refused, except the name
in `adapter.model`. Choices are kept
in `.agents/state/models-<alias>.json`, so they survive a restart, and `/status` shows the
topic's `model=`. An explicit `adapter.command` sets its own command line, so `/model` cannot
switch it.

### Reasoning effort — `adapter.effort` and `/reasoning`

`adapter.effort` (`low`, `medium`, `high`, `xhigh` or `max`) sets how hard the model thinks,
mapped onto each CLI's own flag: `-c model_reasoning_effort=<level>` for Codex (which has no
`max`), `--effort <level>` for Claude Code, `agy` and Reasonix. Unset, the CLI's own default
applies (for Codex, whatever `~/.codex/config.toml` says). `check` refuses a level the CLI
lacks, and `/status` shows `effort=`. The admin can change it at runtime, per bot and per
topic, the same way as `/model`:

- `/reasoning@<bot>` shows the effort the bot uses in this topic.
- `/reasoning@<bot> <level>` (admin only) sets it from the next turn.
- `/reasoning@<bot> default` goes back to `adapter.effort`, or the CLI's own default.

`/effort` is the same command. Only the bot's runtime choice changes: `adapter.effort` and the
CLI's own configuration stay as they are. Choices are kept in
`.agents/state/efforts-<alias>.json`, and a level the CLI lacks is refused. An explicit
`adapter.command` sets its own command line, so `/reasoning` cannot change it. Peers treat
the answers to `/model` and `/reasoning` as machine traffic, never as a turn.

To set a default in the config instead (a QA bot rarely needs `xhigh`, for example):

```yaml
adapter:
  type: codex
  model: gpt-6-sol
  effort: medium
```

Antigravity also accepts a tiered model id from `agy models` (`gemini-3.8-flash-high`) as its
`adapter.model`.

### Hermes — `type: hermes` (untested)

Not yet run against a real Hermes install.

`hermes -z "<prompt>"`

One-shot mode prints **only the final response** — no banner, no spinner, no tool previews,
no session id — and it loads tools, memory, rules and the working directory's `AGENTS.md` as
usual. `--usage-file PATH` writes a JSON cost report after the run; profiles are selected via
the environment (`HERMES_PROFILE`), so pass it through `extra_env`.

### Reasonix (DeepSeek) — `type: reasonix`

`reasonix run -p --output-format text [--model NAME] --permission-mode MODE -- "<prompt>"`

One fresh run per message, printing only the final reply; Reasonix loads the working
directory's `REASONIX.md`, `AGENTS.md` or `CLAUDE.md` itself. `strict` maps to `acceptEdits`
(file edits, no shell), `standard` to `auto`, `full` to `bypassPermissions`; a headless run
refuses anything that would need approval instead of waiting. The key comes from
`DEEPSEEK_API_KEY` or the `.env` that `reasonix setup` writes.
[SETUP-REASONIX.md](telegram/SETUP-REASONIX.md) has the steps.

### Antigravity — `type: antigravity`

`agy -p "<prompt>" --print-timeout 20m --dangerously-skip-permissions`

Print mode cannot ask for approval, so the default command skips approvals: Antigravity's tool
calls run unapproved. An explicit `adapter.command` decides for itself; the channel never adds
the flag to it (see [`SETUP-ANTIGRAVITY.md`](telegram/SETUP-ANTIGRAVITY.md)).

Print mode is plain text — there is no JSON envelope and no `--max-turns`; the run is bounded
by `--print-timeout` (default `5m`, raised to `20m` here) and by `timeout_seconds`. `--model`
picks the engine (`agy models` lists the display strings), `--add-dir` adds context roots.
Because Antigravity CLI operates in plain-text print mode and its SQLite conversation metadata
(`gen_metadata`) carries no token usage counters, Antigravity's token usage is not reported
by the CLI and stays unknown in the turn ledger and pulse.

The adapter optionally tethers to ongoing development conversations via `conversation:` in config
or a `.active_conversation` pointer file in `cwd`, automatically keeping SQLite state in sync
between Antigravity IDE and CLI via `sqlite3.Connection.backup()`. The admin can also switch
active context live from Telegram by messaging `Antigravity, switch context to "<Title or ID>"`.

### Anything else — `type: custom`

```yaml
adapter:
  type: custom
  command: '/opt/tools/myagent --json --prompt {prompt}'
```

`{prompt}` is replaced inside its argv token; if the template has no placeholder the prompt is
appended as the last argument.

### Windows: no batch files

On Windows the CLI named in the command is resolved on `PATH`, and a `.cmd` or `.bat` result
is never started: Python cannot quote arguments for `cmd.exe`, so prompt text such as
`"&calc&"` would run as a command. An npm shim (`codex.cmd`, `claude.cmd`) is replaced by the
node script it wraps, run with the shim's own `node.exe` or the one on `PATH`. Any other batch
file is refused with an error naming it; point `adapter.command` at the program's `.exe`.

### What a runtime inherits

Every runtime gets the transport's environment minus the variables whose names look like
secrets: names containing `TOKEN`, `SECRET`, `PASSWORD`/`PASSWD` or `CREDENTIALS`, names
ending in `_PAT`, `_PASS` or `_PWD`, and every `A2A_KEY_*` except `A2A_KEY_DIR` (a path).
`adapter.extra_env` is added as written.

**What this protects against, and what it does not.** The filter keeps the bot token and
the bus signing keys out of the environment a message can ask an agent to print. It is not
a boundary: a run executes as the same OS user as the transport, so at the default
permission level it can still read the files those values came from (`~/.a2a-telegram.env`,
`~/.env`, the config folder's `.env`, the keys under `A2A_KEY_DIR`), and on Linux the
`/proc/<pid>/environ` of the transport and of its MCP server. Running each agent as a
separate OS user, or in a container or VM, is the real boundary.

It is also a denylist, not an allowlist. Still passed through: API keys such as
`OPENAI_API_KEY` and `ANTHROPIC_API_KEY`, every one of them to every runtime (not only the
one that runtime needs), any secret whose
name matches none of the patterns (URLs with a password in them, `*_KEY` names such as `GOOGLE_API_KEY`,
`NODE_OPTIONS`), and everything else loaded from `~/.env`. Git and repository hooks that a run
starts inherit the run's environment, and the transport's own child processes (git for the
bus, for example) get the transport's full environment.

**Handing a run a stripped variable.** Name it in `adapter.pass_env`. The transport logs a
warning for any secret-looking name other than the agent's own `A2A_KEY_<AGENT>` and
`A2A_TELEGRAM_BOT_TOKEN_<AGENT>` (a custom `bot_token_env` name is not recognised as its own). Typical cases:

- The swarm doorbell: Codex hands the doorbell token (by default its bot token,
  `A2A_TELEGRAM_BOT_TOKEN_<AGENT>`) to the a2a MCP server. Without it posts still land on the
  lanes, but the doorbell does not ring. A dedicated doorbell bot whose token can do nothing
  else is safer than the agent's own bot token, since the run can read it.

  ```yaml
  adapter:
    pass_env: [A2A_TELEGRAM_BOT_TOKEN_CODEX]
  ```

- Bus notes an agent sends itself (`scripts/a2a_bus.py send` inside a run) need its own
  signing key, `A2A_KEY_<AGENT>`, and never another agent's.
- A CLI that signs in through such a variable: `CLAUDE_CODE_OAUTH_TOKEN` or `GH_TOKEN`, and
  for Claude Code on Bedrock or Vertex the cloud credentials (`AWS_SECRET_ACCESS_KEY`,
  `AWS_SESSION_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`).

Codex keeps variables named like secrets out of the shell commands it runs by default, but Claude Code,
Hermes and Antigravity pass their whole environment to their tools, so whatever you list
for them is readable by anything a message asks them to run.

**Where `.env` comes from.** The transport reads `.env` files from the config file's folder
first, then `~/.a2a-telegram.env` and `~/.env`, never from its working directory or that
folder's parent. When the config folder is also the agent's working folder (the
`agy-install` layout), a `.env` the agent writes there is read on the next start, so keep
the config outside the agent's working folder.

## 5. The rules the channel enforces

**Addressing.** A message is answered when it mentions the alias or `@username`, replies to
this bot, or comes from the admin. Everyone else is ignored — including other bots that did
not address anyone, which is what keeps agent chatter from becoming a broadcast storm. The
alias counts where it reads as an address: `@Codex` anywhere, or an alias opening a line as
its addressee (`Codex, …`, `Codex: …`, `Codex and Antigravity, …`); a bare alias loose in
prose ("Codex said the diff is fine") is a passing mention, not an address. A transport
delivers an alias-addressed message by appending the peer's `@username`, and that trailer
never picks the recipient: on a message whose body already addresses another bot it is read
as delivery, not as addressing.

**Only the swarm starts turns.** A bot runs commands on its machine (`adapter.permissions`
lets it run git, tests and more by default), so only the admin, the people whose numeric user
ids are in `agent.allowed_users`, and peer bots can start a turn. Anyone
else in the group, person or bot, is ignored (posts as an anonymous admin or as a channel
included) and logged once as a WARNING; `limits.strangers: answer` restores the old behaviour.
**When upgrading:** list every bot of the swarm in `peers:` (or `/admit` it) and set the real
`agent.admin_user_id`, or those bots and your own messages are ignored. People are matched
by user id only: a handle can change owner once it is given up, so `maintainer_username` is
used for display and a handle in `allowed_users` is ignored (the transport logs it and
`check` lists it). To find a teammate's id, have them post once in the group: the WARNING
for an ignored author names it (`ignores @name (id=123456789, ...)`), and that number goes
in `allowed_users`.

**One bot acts per message.** When a person's message names several bots, only the first one
named starts a turn. Its prompt gets one line naming the bots that wait for it to hand the work
on. The others do not see the message at all; each acts when the first bot hands it the work with
an @mention, and in General that hand-off (within 30 minutes) skips the bot-to-bot cooldown
once. `@all` (or `@everyone`, `@agents`, `@swarm`), `/stop` and `/ping` `/status`
`/diag` `/role` `/model` still reach every bot named, and `limits.multi_mention: all` restores the old behaviour
where every named bot acts. A bot's own message that names several peers reaches each of them,
as before. Order is read from the message text, so a bot named in quoted or code text counts.

**Loop safety is routing, not a turn cap.** An agent answers only when a message explicitly
addresses it — its alias, its exact `@username`, a reply to its own message, or the admin — and
terminal-ACK/sign-off messages from peers are filtered before delivery. A silent peer is not a
dead peer: test the transport with `/ping@<bot>` (see `PROTOCOL.md` §8). In a forum topic
every post is, to Telegram, a reply to the topic's opening message; that does not count as a
reply to the bot that opened the topic. A command for another bot (`/status@other_bot`) never
starts a turn, and neither does a peer's message that opens with another bot's handle and
names this bot only as whom to hand the work to ("@a_bot please hand @b_bot a task" addresses
`a_bot`).

**A message that asks several bots is one hand-off per bot (#181).** A peer's message naming two
or more bots, each with a request of its own -- its own line or bullet, or an opening run of
handles that shares one sentence ("@a @b: please review this") -- is split instead of refused.
Every named bot reads the shared context plus the lines that name it, so it does its own share
and no one else's; the others' asks are dropped from its brief, not from the message. The
delegation above keeps its teeth: a message that asks **one** bot and mentions another only as
who should receive the work is still that one bot's. The test is in code, not in a model's
reading (`swarmly/telegram/handoff_split.py`): a bot has a request when its words carry a
politeness word, a question mark, or one of the verbs the swarm uses (`review`, `QA`, `push`,
`fix`, `merge`, …) **in the first sentence after its handle** — a question two sentences on
belongs to whoever that sentence is for. A line is split too, not just a message: two asks on
one line (`@a: review it. @b: QA it.`) give each bot its own half, while a line that *opens*
with a run of handles (`@a @b: review #195 and QA it today`) is one sentence shared whole by
both — nothing is cut from it. A line that only *mentions* another bot is context every
recipient keeps.

**A full queue holds, never drops (#184).** With `limits.busy_behavior: queue` a message that
arrives while a turn runs waits for it, up to `limits.busy_queue_max` waiters per topic. Past
that limit the message used to be dropped ("its queue is full" -- twice on 22 Oct, and a dropped
hand-off is a stall nobody sees). It is now **held**: recorded in the durable inbox (so a restart
still replays it) and taken, oldest first, as each turn ends, in arrival order. The topic is
told **once per window** that the bot is behind, with the depth -- `behind by N message(s); this
one … is kept and will be answered in order` -- and `stats.busy_held` counts held messages while
`stats.busy_dropped` goes back to meaning what it says.

**Loop brake:** a forum topic takes at most `limits.max_peer_turns_per_topic` (default 8; it was 3, which a round of questions to four bots tripped in a live test)
turns in a row that a peer bot started, explicit mentions included. Visible progress resets
the run: a swarm message naming an issue, PR or commit (a commit link or a short SHA such as
`b0da422`) not named in the topic before, or a merge, approval, QA pass or fail, or change
request for one that was not given for it before. A loop repeats itself; a team shipping PRs does not, so it is no
longer paused every eight turns.
Then the bot posts one line there ("paused after 3 bot turns in a row in this topic. Post
here to continue."; when a peer already posted it, the others stay quiet, and the "still
paused here" reminder to a bot named in the paused topic comes at most once per 10 minutes
after it) and takes no more peer turns in that topic until a human posts there
(a command such as `/status` does not count). A capped topic still answers `/ping`,
`/status` and `/diag` and still ACKs a peer's REQUEST, so no sender escalates. General is
never capped. It applies whether the bot runs one turn at a time or topics in parallel;
`0` turns it off. Topics have no bot-to-bot cooldown, so with `0` nothing but routing stops
two bots that keep addressing each other in a topic. A `/stop` naming both ends it when it
lands during a turn; one that lands while a hand-off is on its way must be sent again.

**Hand-offs are recorded on the bus by the transport.** An agent never has to remember
`swarmly-telegram post`: when a reply hands work to a peer (a line or bullet that opens with
the peer's @handle, as in "- @codex_bot runs pytest"), the transport also writes a signed
`TASK_HANDOFF` envelope for that peer to the bus, with the Telegram message it mirrors. The
peer's poller skips it when that Telegram message already reached a turn there, and runs it
from the bus when it did not (dropped by a full queue, a cooldown or an outage), answering in
the topic it came from. A reply moved from General into a topic is mirrored as well. The
Telegram messages a bot took a turn on are kept in `.agents/state/admitted-<alias>.json`, so a
restarted bot does not run a mirror of one again. Bus commits carry the agent's own git
identity. `bus.mirror_handoffs: false` turns the mirror off.

**A hand-off nobody answers is chased by the clock.** When the bot a hand-off went to posts
nothing in that discussion for `limits.handoff_nudge_minutes` (default 10), the bot that
handed off posts one reminder that mentions it; after twice that time it posts one "⚠️"
notice naming the maintainer and stops, and runs one turn of its own asking it to decide
what happens to the work: hand it to another peer whose role fits, do it itself, or wait.
Any post by that bot in the discussion counts as an answer, except its "⏳ busy" line, which
restarts the clock instead. When another bot already posted the reminder or the notice for
the same silent peer, the others don't repeat it. `0` turns it off.

Only the coordinator posts the reminder and the notice. Another bot that handed work off
posts neither: after twice the time it only runs its own turn to decide what happens to the
work. (A swarm with no known coordinator, such as one set up by hand, keeps the behaviour
above for every bot.) A hand-off to a peer that is frozen (quota or pause) gets no reminder
and no notice at all: the bot that handed it off is asked straight away to give the work to
someone else or to wait until the peer resumes.

**Except the coordinator.** A hand-off *to* the coordinator, and a peer's answer to it, is
waited on by the bot that sent it, which does post the reminder and the notice: nobody else
would ever follow it up. The coordinator's next post in that discussion answers it.

**A restart loses no message.** A message accepted for a turn (queued or running, a reply not
yet posted) is kept in `inbox-<alias>.json` beside the transport's state until its turn is
done; the next start runs whatever is still there (younger than 12 hours). That is
at-least-once: a crash right after a reply is posted can repeat that one turn. Commands are
never kept, and the same message arriving twice at once (a replay and Telegram's redelivery)
runs once.

**A turn cut off by a die-off is resumed once, and never loops** (issue #84). The record
kept for an accepted message is the turn's own: its topic, message id, sender, the text the
reply has to answer, and when it started. When a restart finds one still on disk, the
transport posts one line — `↻ resuming the turn cut off at 18:30` — and runs the turn again in
the same topic with a prompt that quotes the original request and says to check what was
already pushed or posted before doing anything twice. The resumed turn is the record's
second try and it is what removes the record, so a turn that finishes (or chooses silence)
leaves nothing behind. The try is counted **before** the turn starts: if that resume is cut
off too, the record is dropped instead of replayed a third time and one line says so —
`⚠️ <coordinator or admin>: could not finish the turn on message <id> in <topic> after two
restarts` — because a turn that kills the transport twice will not finish by being started
again, and a loop of them is how a swarm spends a night doing nothing. A record older than
`resume_max_age` (one hour) is **reported, never resumed** -- restarting work an hour after
it was cut off means acting on a topic that has moved on -- and one older than 12 hours is
dropped without a word. A restart that interrupted several turns resumes them one at a
time, oldest first: each resumed turn claims the next when it finishes, so six cut-off turns
never become six turns at once.

**The coordinator checks in when nothing wakes it.** A coordinator that holds no WAKE and has
had no turn for `limits.coordinator_watch_minutes` (default 30) gets one turn in the topic it
last worked in, asking it to look at the round and act on whatever waits, or to answer
`NO_REPLY`. The wait doubles after each check (up to 8 hours), any other turn resets it, and
it stops a day after the last turn. `0` turns it off.
**A bot that dies says nothing, so the swarm listens for its pulse.** Every transport writes one
heartbeat every `limits.heartbeat_minutes` (default 2) carrying its revision, term, role and,
while a turn is running, `busy_since`. It is a silent Telegram line (`limits.heartbeat_plane:
telegram`, the default), in the swarm's own "📡 Swarm pulse" topic, which the coordinator opens
(`limits.heartbeat_topic_id` names another topic, or `-1` for General), and each beat deletes
the bot's previous one, so there is one current line per bot (`heartbeat_style: edit` edits a
single message in place instead). `heartbeat_plane: bus` keeps the earlier per-bot git ref, `refs/pulse/<alias>`,
but a heartbeat is a push and a fetch every beat on GitHub, so it is off by default. The pulse
is machine-class traffic and wakes no turn. The pulse is its own task, never the turn lock, so a turn
that runs for half an hour still pulses and shows `busy_since` — a long turn reads as work, not as
a die-off.

A peer silent on **both** planes for `heartbeat_minutes * peer_down_after` (default 3 misses, so
6 minutes) is **DOWN**. Any message from a peer counts as liveness, whatever plane it is on; a
peer that has not been heard from at all is `unknown`, not DOWN, and a freshly started transport
gets one interval of grace before it may judge anybody — otherwise a restart would declare the
whole swarm dead. The table is saved beside the transport's other state as
`presence-<alias>.json` and restored at start, so a die-off seen before a restart is still
known after it and one admin line stays one line. The coordinator posts one line to the
admin per episode (the acting coordinator
does it when the coordinator is the silent one) and one line when the peer comes back. DOWN is
**advisory**: it says "not heard from", never "somebody else should lead" — the lease (#83) alone
decides who coordinates, because using DOWN to start a takeover would let a partition elect a
second coordinator. A hand-off to a DOWN peer gets no reminder: like a frozen peer, the work
comes straight back with a turn asking what happens to it. A busy peer — one pulsing with
`busy_since` — gets no reminder either: its hand-off clock restarts instead, so the reminder
does not land in the middle of a long turn. `/status` and `/swarm` carry the
liveness line, e.g. `peers=Claudebot:up,DeepSeek:down(12m),Reasonix:busy(25m)`. Either limit at
`0` turns the rule off and nobody is ever DOWN.

**Busy is not gone.** A message that queues behind a turn running for two minutes or more
gets one "⏳ <alias> is busy with a turn running since HH:MM UTC" line in its topic (at
most one per topic per five minutes), so peers and people see a working bot rather than a
silent one. When the turn ends, every message that queued behind it in that topic runs as
one turn, oldest first, instead of one turn each.

**A turn that hits the time limit continues.** When a topic turn of a runtime that resumes
its session (Codex, Reasonix, `claude_thread`, Antigravity) is stopped at
`adapter.timeout_seconds`, it posts "⏳ … timed out …; it continues where it stopped" instead
of ❌ (peers read it as busy), and the bot's next turn
in that topic resumes the session with a note to carry on from where it stopped, in smaller
steps. Messages already waiting take that turn instead. A continuation that times out again
is reported, not retried.

**Every turn carries the rules that drift.** The standing rules sit in the system prompt and
lose weight as a session grows, so each turn's framing line also ends with a short card: the
agent's role, where to reply, how to hand off (and that the bus copy is automatic), the
length cap and `NO_REPLY` (inside the inline rules for runtimes without a system prompt).

**One topic, one context.** Each runtime that can resume a session keeps one per forum
topic, so a long task keeps its whole history in the agent's own context, with the prompt
cache intact: Codex (`codex exec … resume <thread>`), `claude_thread` (`--resume <id>`),
Reasonix (`reasonix run --resume <session_id>`, the id read from `--output-format json` on
the topic's first turn) and Antigravity print mode (`agy -p --conversation <id>`, the id
taken from the conversation agy stores on the topic's first turn; not when a conversation
is configured). Session ids are kept under the transport's state folder, one file per bot,
and survive restarts. A Reasonix topic session is retired after `adapter.session_turn_limit`
turns (default 150; `0` never rotates): every turn re-reads the whole session, so a long
topic gets slower and dearer.

**Session handover.** A retired session never leaves its successor blank. Before the
transport drops it, the old session takes one more turn (not posted) and writes a handover
under fixed headings: goal and scope, decisions made and why, current state (branch, SHA,
PR, who has it), open hand-offs, files and commands that matter, gotchas and failed
attempts, and the next step. It is kept under the transport's state folder as
`handover/<topic>-<alias>-<UTC time>.md` (never deleted), and the new session's first prompt
starts with it, told to check the PR, issue and commit states it names before acting, then
the topic recap and the new message. A "♻️ … wrote a handover" line in the topic shows the
change. When the old session cannot write one (the handover turn fails, or the session was
lost), the new session is told so and rebuilds from the recap, the tracking issue and the
PRs.
A resumed Reasonix or Antigravity turn carries the one-line envelope
and the turn card, not the full rules again. Use `claude_thread`, not `claude`, for a
Claude bot in a swarm.

**What the transport adds.** It keeps, per discussion and across restarts, the opening
message (the task), a progress list (messages naming an issue or PR, a GitHub link, a
hand-off to a peer, a REPORT_BACK, a merge, an approval or a blocker) and the latest
messages, all from the swarm only (a stranger's text never goes in). A resumed session gets
only what others posted in the topic since its last reply, at the end of the prompt, plus the
task the first time it speaks there. A runtime that cannot resume (`claude`, Hermes, a
custom command), or a topic's first turn, gets the task, the last 5 progress items and the
last 3 messages, about 2k characters, never the whole history.

**A paused topic says so.** After the pause notice, a request that names the bot in that
topic gets one "⚠️ still paused here" reply per 10 minutes, so the sender learns why nothing
happens.

**General is for setup; work goes to topics.** With `limits.work_in_topics: true` (the
default), every turn in the home group's General is told that General is for setup and quick
questions, and that real work goes to its own topic (`NEW_TOPIC:`, below). The transport also
enforces it: a reply in General longer than 800 characters (400 when it answers a peer bot)
goes to the topic already about the same issue or PR (`#12`, `PR 12`, `.../pull/12`) when
there is one. Otherwise a person's request gets a new topic named after it (leading with the
issue or PR it names), with the request quoted there and a "↪ moved this to topic" link in
General; between bots no topic is opened as a reaction (they open one on purpose with
`NEW_TOPIC:`), and an answer to a `[gitbus ← …]` echo never opens one. Which topic is about
which issue or PR is kept in `topic-refs-<alias>.json` beside the other state, so it survives
a restart; an item nobody mentions for a week, or whose topic is closed, is let go. An agent's
answer to bus mail whose hand-off names no topic is posted in the topic about its issue or PR
when there is one, else in General. Short answers stay in General. The same holds between
topics: a reply posted in one topic that names only issues or PRs another topic is about goes
to that topic, with a "↪ moved this to" link where it was asked, so work that started in the
wrong topic follows its items instead of staying put. A number no topic is about yet (a new
issue) neither holds the reply nor moves it, and is learned in the topic the reply went to. A
reply that also names an item of its own topic, items of two other topics, or only new
numbers, stays. When an item was learned in the wrong topic,
the admin's `/topic #164 #165` inside the right one moves it there for every bot. A
summary sent back with `REPORT_BACK:` has its @mentions defused, and a peer bot's message
that names this bot only inside a list of three or more bots is not taken as addressing it. Set it to
`false` to keep everything in General (a group without topics falls back to that by itself).

**Agents open their own topics.** An agent can move a piece of work into its own forum
topic, for example to research something in a separate context and report back. It replies
with a first line `NEW_TOPIC: <title>` and the brief below it. The bot opens a topic named
`<alias>: <title>` in the home group, posts the brief there, and links the topic where the
work was asked for. When a topic is already about an issue or PR the title or brief names,
no topic is opened: the brief is posted in that topic instead, with a "↪ continued in" link.
A message that asks for a topic in plain words ("open a new topic to
research X") reminds the agent of this format. A brief that opens by addressing a peer hands
the work over; any other brief becomes the agent's own task, run at once in the new topic (a
peer named further down still sees it). A final reply in that topic that starts with
`REPORT_BACK: <summary>` is posted there and its summary also goes back where the work came
from, with a link. When the opener's turn in its topic ends in silence before it reported
back, it is asked once for that summary. Each agent opens at most 5 topics an hour. Topics must be turned
on in the group and the bot needs the admin right to manage topics (`check` warns when it is
missing); without them the brief is posted in place with a note.

**Rate Limit & Turn Cooldown.** By default an agent's sends are at least 3 seconds apart, at
most 18 a minute (per topic when topics run in parallel; see **Running topics in parallel**):
- `min_seconds_between_messages: 3.0`: outbound messages are spaced by at least 3 seconds.
- `max_messages_per_minute: 18`: sliding window cap of 18 messages every 60 seconds, just under
  Telegram's limit of 20 a minute in a group.
- `min_seconds_between_agent_turns: 60.0`: turn cooldown between bot-to-bot turns, in General
  only. There a peer bot's message that arrives less than 60s after the bot's last General
  turn (turns in topics do not count) is dropped
  (one that names the bot gets a "⚠️ … skipped this message" notice, once per window);
  one already queued waits the rest of the cooldown without holding the turn, so the person's
  own messages and the topics go on meanwhile. Forum topics have no cooldown: the peer-turn
  cap (`max_peer_turns_per_topic`) is their loop brake.
- One turn runs at a time (`max_parallel_turns: 1`, the default), including turns the
  transport runs for the durable bus and the swarm lanes. Above 1, see **Running topics in
  parallel** below.
- `busy_behavior: queue` (default), `drop` or `notice`: what happens to an addressed message that
  arrives while a turn is in flight. `queue` holds up to `busy_queue_max: 12` such messages (a developer on a five-minute turn collects several) and runs
  them in order as one turn when the turn ends (a held peer-bot message waits out the turn cooldown rather than
  being dropped, and lets the person's messages and an awaited hand-off go first); `drop`
  discards them; `notice` discards them and logs one line per
  `busy_notice_window_seconds: 900`. The window is validated **at load**
  (finite, > 0) even when `busy_behavior: drop`, where it is never read — so an operator upgrading
  with a bad value set gets a startup refusal naming `limits.busy_notice_window_seconds`, not a
  runtime surprise.

**Long replies are split.** A reply longer than `max_message_chars` is sent as up to
`max_reply_parts: 4` messages, cut at paragraph or line breaks. Later parts are marked `↪ (k/n)`
and never start a turn on a peer. Each part is paced like any other message, and text beyond the
last part is clamped as before.

**The receiving side joins the parts first.** The `↪ (k/n)` parts of one reply are held and
joined back into the one message the sender wrote before the turn runs, in whatever order they
arrive: the turn runs from part 1, so its message id and reply target are the ones answered.
Each part's marker also carries the reply's own token (`(2/3 #9fa0c1)`), and the token is what
a part is joined on: two replies by one sender that interleave are held apart and each comes
out whole. A part with no token cannot be attributed to a reply — `A1, B2, B1, A2` from one
sender is indistinguishable from one reply arriving out of order — so a sender that writes none
(a transport older than #29) is **not** joined: its part 1 runs its turn as it did before, and
its later parts stay continuations that start no turn. Two messages are never put into one turn.
Parts that never all arrive are delivered as what arrived, with a note naming the missing ones.
A fragment whose first part never came keeps its `↪ (k/n)` marker, so it stays a continuation
and starts no turn. One cap, 20 parts, bounds both ends: a larger `max_reply_parts` is clamped
at load, and a part header above the cap is ordinary text, never a buffer.

**Topics as discussions.** In a group with Topics turned on, each topic is one discussion
with one `context_id`, which every agent works out the same way from the topic's id alone:

| topic | `context_id` |
|---|---|
| a topic in the home group (`identity.home_channel_id`) | `tg-<topic id>`, e.g. `tg-42` |
| a topic in any other group, with `SWARMLY_REDACTION_SECRET` set | `tg-<tag>-<topic id>`, the tag an HMAC of the group id under that secret |
| a topic in any other group, without the secret | none: **the turn is refused** (logged; a GitHub notification is marked read), so it never falls into the General session |
| any threaded topic on a host that never set `identity.home_channel_id`, without the secret | none: also refused — the transport cannot tell home from another group |
| General (no topic) | none: the shared conversation, as before |

- The topic's name never decides its `context_id`, so renaming a topic does not move it to
  another discussion or another Claude Code session, and no name can claim another topic's id.
- The bot learns a topic's name from the messages that create or rename it (or, when it has
  nothing better, the original name carried by messages at the topic's top level), keeps it in
  `.agents/state/topics-<alias>.json` across restarts, and shows it to the agent next to the id.
  An unreadable file is set aside as `topics-<alias>.json.corrupt-<time>` and names are relearned.
- The prompt for a turn in a topic names the discussion ("DISCUSSION: this message is in
  discussion tg-42 (Telegram topic "waste disposal"). Use context_id tg-42 for swarm posts about
  it."), so the agent's posts on the bus land in the same discussion. The message text itself is
  untouched, so commands such as Antigravity's `switch context to` still work.
- `claude`, `claude_thread` and `codex` get the channel rules (reply rules, the bot roster,
  the 500-character cap) in the CLI's system prompt: Claude Code through
  `--append-system-prompt-file` (a file in the temp folder, named by the rules' hash), Codex
  through `-c developer_instructions=...`. Each message then opens with one line naming the
  sender, the handle to answer, what it replies to and its discussion, so a resumed session
  does not collect a copy of the rules every turn. A session resumed without the current
  rules (one made before this, one kept across a restart, or after the roster changed) gets
  them in the message once; the hash of the rules each session holds is kept with its id
  (`codex-sessions-<alias>.json`, `claude-sessions-<alias>.json`), so a restart does not
  resend them. Codex fixes `developer_instructions` when a session starts and ignores a new
  value on resume, and the command-line value replaces one in the owner's `config.toml`.
  Topic and sender names in that line are stripped of brackets, quotes, line breaks and
  shell metacharacters. A timeout or `/stop` ends the CLI's whole process tree.
  A custom `adapter.command` that is not
  `claude` / `codex exec`, or that sets its own system prompt or developer instructions,
  keeps the rules in each message, as Antigravity and Hermes do.
- `claude_thread` keeps one Claude Code session per discussion: General uses
  `adapter.conversation` (or the id derived from the alias and folder), and each topic gets its
  own derived session, created on the topic's first message.
- `codex` keeps one Codex session per topic: the topic's first message runs `codex exec --json`
  and keeps the `thread_id` Codex reports; later ones run `codex exec ... resume <thread_id>`,
  so Codex remembers the topic's earlier turns. The ids are kept in
  `.agents/state/codex-sessions-<alias>.json`; a session Codex no longer has is replaced by a
  new one. An id that is not a plain session id (a letter or digit, then letters, digits,
  `.`, `_`, `:` or `-`, up to 128 characters in all), from that file or from Codex, is logged and ignored, since it goes on the
  command line after `resume`. Codex re-reads the whole session on every call (mostly from its
  cache), so a long topic costs more each turn: once a turn reports
  `adapter.session_token_limit` input tokens (default 10,000,000; `0` never rotates), the
  topic's next turn starts a new session from the topic recap and task progress instead of
  resuming (see **Session handover** below). General still starts fresh for each message, and a custom `adapter.command` that is
  not `codex exec ...` is left as it is. Other adapters get the discussion line; their sessions
  are unchanged.
- Replies, receipts and ACKs for a message go back to its topic. Everything the transport says
  on its own goes to General: the online notice, the startup self-test, overdue, receipt and
  digest alerts, gitbus delivery receipts, and `codex_thread` ticket answers. A reply whose topic
  was closed or deleted meanwhile is posted in General, with a note giving the topic's id.

**Running topics in parallel.** `limits.max_parallel_turns: N` (default 1, at most 8) lets up
to N topics run a turn at the same time.
- Messages in one topic still run one after another. A message that has to wait (same topic, or
  all N slots busy) is queued like any other (`busy_behavior`); `busy_queue_max` then caps each
  topic's queue rather than one shared queue.
- Turns and delivery probes from the durable bus and the swarm lanes, run by the transport,
  take a slot too, under the General topic's turn order, so they never run beside a chat turn
  they would clash with. A message that arrives during one waits for it like any other busy
  turn: queued with the default `busy_behavior: queue`, dropped with `drop` or `notice` (in
  any topic at N=1, in General at N above 1). A separate `bus-poll --once` process is not
  covered: don't schedule it on a host where the transport is running. `/stop` does not stop a
  bus delivery already in progress.
- Topics have no bot-to-bot cooldown (`min_seconds_between_agent_turns` holds in General);
  the peer-turn cap brakes loops there.
- Send pacing applies per topic too: `min_seconds_between_messages` and
  `max_messages_per_minute` hold for each topic, so a reply in one topic does not wait out
  another topic's gap. Across all topics the bot keeps Telegram's limits for one group: at
  most 18 messages a minute, at least 1 second apart, and a flood wait (HTTP 429) in any
  topic holds every topic until it ends. Under that load a reply can wait for other topics.
- A reply whose topic was closed or deleted mid-turn goes to General with a note naming the
  topic id, under General's own pacing, so it never interleaves with a General reply.
- `/stop@<bot>` in a topic stops that topic's turn and queue only; in General, or as
  `/stop@<bot> all` (the word right after the command), it stops everything.
- N above 1 is refused for `antigravity_thread` and `codex_thread`, which keep one
  conversation for everything. `antigravity` is allowed: it keeps one conversation per topic
  (`--conversation`), like `claude_thread` and Reasonix — but not when a single conversation
  is *pinned* (`adapter.conversation`, an `.active_conversation` file in `adapter.cwd`, or
  `-c`/`--continue`/`--conversation` in `adapter.command`), because then every topic
  continues the same context. The in-chat `switch context to <id>` command is refused for
  the same reason while N is above 1 — it replies why, writes no `.active_conversation`, and
  `switch context to per-topic` still takes the bot back to one conversation per topic. A
  `custom` adapter is allowed too: only set N above 1 if each run is independent.
- **All topics share `adapter.cwd`,** so parallel turns can overwrite each other's edits. N
  above 1 is refused unless you also set `limits.allow_shared_cwd: true` (not needed for
  `echo`), and the transport logs a warning naming the folder at start. Per-topic worktrees are
  planned.
- Setup: the group must be a supergroup with Topics turned on. For the bots to read every message
  in a topic, turn privacy mode off in @BotFather or make them admins; to let them open topics
  later, give them the "Manage topics" admin right.

**Typing, files and `/stop`.**
- While a turn runs the bot shows "typing…" in that chat or topic (`typing_indicator: true`),
  and when the turn is for a person's message it reacts 👀 to it first, so a long first turn
  never looks ignored (`ack_reaction: true`).
- A turn that fails on a passing provider error ("model is at capacity", overloaded, a 5xx)
  is tried again after 30 s and then 2 min before the bot posts ❌. Quota and usage limits
  are not retried: they last hours, and repeated ones freeze the bot instead — an error that
  says the quota is gone and when it returns (`try again at Nov 16th`, `retry in 2h`) freezes
  it on the first failure, while a plain `429`/`Retry-After` still needs
  `quota_freeze_threshold` in a row. The freeze ends
  by itself at the reset time the error gives (else after `quota_retry_minutes: 60`), the bot
  answers a person who addresses it with why it is frozen and how to resume, and the admin can
  end it sooner with `/resume@<bot>` or by switching it to another model with `/model@<bot>`.
  Peers see the freeze, list it in `/status`, and hand its work to someone else.
- Files, photos, voice notes and videos on an addressed message are downloaded to
  `<adapter.cwd>/.a2a-attachments/` (a folder git ignores), and the prompt names each local path.
  Files over `max_attachment_mb: 20` (the Bot API download limit) are listed but not fetched.
  Unaddressed files are never downloaded.
- `/stop@<bot>` from the admin cancels the running turn, ends the CLI process and clears the
  queue (in a forum topic, that topic's only; `/stop@<bot> all` stops everything). A turn that times out also ends its CLI process now, instead of leaving it running.
- **Orphaned CLIs are reaped on start.** `/stop` and a timeout end the CLI, but a transport
  that is *killed* (a crash, a power cut, a service restart) cannot: its children keep
  running against the same conversation. So each CLI's pid is recorded in the state
  directory (`children-<alias>.json`, with project worktrees adding theirs) together with
  the start time of the child and of the transport that spawned it. On start the transport
  ends the children whose transport is dead — the process tree on Windows, the group on
  POSIX — and leaves alone a live transport's children, a pid whose start time no longer
  matches (the pid was reused), and anything whose identity it cannot read. The record is
  dropped when the child exits, so the file only ever holds live pids.
- `/pause` from the admin holds every bot (`/pause@<bot>` holds one): a turn already running
  finishes and posts its answer, then the bot takes no more turns, from Telegram or the bus,
  and says "paused". The hold survives restarts. `/resume` (or `/resume@<bot>`) lifts it.
  Use it to stop a run at its budget without killing processes.
- On start, the transport clears any webhook left on the bot so long polling can take over,
  keeping pending updates.

**Anti-Pollution & Zero Scratchpad Invariant.** Agents must NEVER post intermediate reasoning,
scratchpads, tool execution chatter, or skill update logs to the group. Adapters automatically wrap
prompts with channel rules instructing the underlying LLM to emit ONLY the finished, final response
when ready. Automated bot notices starting with `⚠️`, `↪`, `❌`, `🟢`, `✅`, or `[system]` are ignored (a leading mention is stripped before the check, so addressed notices are filtered too).

**Pacing & Retries.** Outbound sends obey `min_seconds_between_messages` and `TelegramRetryAfter` is honoured with a
bounded retry rather than a hammer.

**Size and safety.** Group coordination text is **500** characters by operational convention
(Telegram hard limit 4096). That budget is a number now, not a slogan: `limits.reply_chars`
(default 500) is what the turn rules ask for and what a reply ticket is held to, and
`projects.<key>.reply_chars` gives one project's topics a longer one — a plan-shaped topic can
ask for 1500 while everything else stays at 500. A topic with no override, and every reply
ticket (a ticket carries no topic), uses `limits.reply_chars`. Everything except the alias
banner is HTML-escaped at render time, and deep research, code and tables belong strictly on
GitHub.

**Liveness & Interactive Agent Timers.** The channel daemon catches mentions in real time via long polling.
However, interactive agent environments (such as Antigravity IDE) are event-driven and become dormant between
user prompts. Agents in interactive sessions must maintain a recurring heartbeat timer/cron (e.g. 5-minute
interval via `schedule(CronExpression="*/5 * * * *")` or `/schedule`) to periodically wake up, inspect daemon
logs, sync the git bus, and process peer tasks without requiring manual human prompts.

**Tidying the group — `/cleanup` (admin only).** A round leaves one-off topics and machine
notices behind, and the bots are group admins, so the transport can clear them itself.
`/cleanup@<bot>` is answered by the transport, never by a model turn, and only for the admin:
anyone else gets a refusal. The coordinator is the bot that runs it; the peers act on its
`TOPIC_GONE` note instead of running one of their own.

- `/cleanup@<bot>` — a dry run. Every topic this transport knows, with its id, name, idle
  time, linked issue/PR and that item's state, plus the General messages it can still delete;
  and how many it can no longer reach. Nothing changes.
- `/cleanup@<bot> topics done` — the topics whose linked issue or PR is closed or merged and
  that have been idle at least `limits.cleanup_idle_hours` (default 24).
- `/cleanup@<bot> topic <id>` — that one topic. `/cleanup@<bot> topics all` — every topic
  except General and the ids in `limits.cleanup_keep`.
- `/cleanup@<bot> general` — the General messages this bot can still delete, oldest first,
  in batches of 100.
- Every deleting form first replies with what it will remove and a one-use code; nothing is
  deleted until `/cleanup@<bot> confirm <code>` lands within 5 minutes, because a deletion
  cannot be undone.

**After a delete** the bot forgets that topic — its topic memory, the topic's CLI session,
the issue/PR → topic map, its per-topic `/model` and `/reasoning` choices, pending hand-offs
and the topic-name cache — and posts one machine `NOTE intent=TOPIC_GONE ref=<topic>` so the
peers forget it too, with no model turn on either side. When a round's tracking issue closes,
the coordinator posts the dry run once in General and asks the admin whether to delete it.


**The status snapshot on every heartbeat (issue #124).** Each beat carries one structured
record of what the bot is doing, taken in code — no adapter call, no model turn, no tokens:
state (`idle`, `busy`, `paused`, `frozen` or `stuck`, where `stuck` is a turn that has run past
`adapter.timeout_seconds` or whose CLI has written nothing for `limits.turn_silence_minutes`), **when that state began** — recorded at the transition, so it
holds across heartbeats instead of resetting each beat — the discussion it is working in
(as its `tg-…` token, never a chat id), the turn's elapsed time against its timeout, the queue
of messages waiting for it, the armed `WAKE` timers with their next fire time, the model and
effort, `rev:`, uptime, the restart count, and its lease term and expiry when it holds the
coordinator lease. It rides the pulse — on the bot's own git ref, or the `STATUS intent=PULSE`
line when there is no bus — so a bot that missed a beat can still be rendered, and a snapshot
that stopped being renewed shows as **stale** (more than two beats old) rather than as current.
Nothing in the record is a token, key, chat id or local path.

**A turn that is running but silent (issue #161).** No CLI process lives between turns, so the
transport knows exactly when a bot is busy; what it could not see was whether the running turn
was making progress, and a hung turn was only found at the hard timeout. Now the adapter behind
each topic reads its CLI's stdout and stderr chunk by chunk and records the moment the last byte
arrived, and the snapshot carries that as `silent`. Past `limits.turn_silence_minutes` (default
5) the state reads `stuck (silent 7m)` — the snapshot's line, the pulse and `/status` — and it
goes back to `busy` as soon as output resumes. An adapter that reads its CLI's pipes only at the
end (nothing to stream) reports "silent since the turn started" and nothing more. With
`limits.turn_silence_kill_minutes` (default 0, off), a turn silent past that limit is stopped
through the `/stop` path, its CLI tree with it, and the topic is told once. This is liveness
only: it never starts a turn and never touches a `WAKE`.

A run the bot's own command starts with `--output-format stream-json` (Claude, Reasonix) is
progress the transport can see as it happens. Such a run prints one JSON object per line and
ends with a `result` event; the adapters take **that** event as the reply, and a stream that
ended before it is reported as a broken turn (`streamed no result`) rather than posting the
progress lines into the topic as prose.


**What a bot has spent — `/usage` (no model turn).** Every model turn appends one row to a
per-bot ledger (`usage-<alias>.json`, beside the transport's other state): when it started and
ended, the discussion as its token, the outcome, and the tokens in and out and the cost **when
the runtime reports them** — Claude reports `total_cost_usd` and its token usage, Codex reports
input tokens. `/usage@<bot>` answers from the ledger alone, so it costs nothing: turns and
tokens for today, the same since this process started, quota left and its reset time when the
runtime's own records expose them, and the last few turns. A number the runtime did not report
is shown as **unknown**, never as zero, and a total says how many of its rows reported one. The
ledger survives a restart and is capped (2000 rows, then the oldest are dropped — and the
output says where its history now starts).

`/usage@<bot> all` lists every bot's usage today: this bot's from its ledger, each peer's from
the `usage.today` its latest heartbeat carried. The coordinator gets the same lines added to each
turn's prompt (code, no tokens), so a cost report never needs anyone to look for ledger files.

**The command centre — a pinned dashboard (issue #117).** The coordinator keeps one message
pinned in a control topic (or the admin's DM) and edits it in place on every heartbeat, so the
swarm's state is readable at a glance without asking anyone anything and without a model turn.
Set where it lives in `config.yaml`; with no `control` section, nothing is pinned:

```yaml
control:
  chat_id: -1001234567890     # optional: the admin's DM, or the group; defaults to the home channel
  topic_id: 307               # optional: the forum topic; defaults to General
```

The message is built in code from the records the transport already holds — the status
snapshots (#124), the turn ledger (#125) and the round state (#86) — and it reads in four
parts. **Alerts first**: a bot DOWN or stuck over its timeout, one frozen or out of quota, a
hand-off nobody answered, the round's blockers. Then **a line per bot**: state and since when,
the topic as its `tg-…` token, queue and armed WAKEs, model and effort, rev, uptime and
restarts, today's tokens and cost, quota and its reset. Then the **round board**: each item's
owner, PR, head, CI, the review and QA verdicts — a verdict counts only on the head it names —
and whether it can merge. A number no runtime reported stays **unknown**, never zero.

Each bot's spend rides its own heartbeat: the pulse carries today's totals from that bot's
ledger (`turns`, tokens in and out, cost, quota and its reset time, each with the count of
rows that actually reported one), so the dashboard shows every bot's tokens and cost without
asking any of them for a turn. A peer that has not said anything is `unknown` there — never a
zero it did not report.

Telegram allows 4,096 characters in one message, so the layout drops whole lines in that
order when it runs out of room — the round board first, the alerts last — and says how many
it hid. The pin is the state, and a failover retires it rather than adopting it: a new
coordinator (a failover, #83) reads the chat's pinned message and recognises the previous
dashboard by its `SWARM` marker, but **Telegram lets a bot edit only its own messages**, so it
cannot take that message over. It unpins the predecessor's message (retiring its keyboard with
it) and pins its own, so there is still exactly one live dashboard — a different message, not
a second one. A restart of the same bot adopts its own pin and posts nothing. If the unpin
fails (a bot without pin rights), the press path is the second line of defence: a control
pressed on a bot the lease no longer names as coordinator is refused, so a stale pin answers
"<that bot> coordinates now" instead of "done". A refresh that cannot be delivered is a log line and a status,
never a failed heartbeat, and an unchanged dashboard sends nothing at all.

The buttons under it come from `CONTROLS` in `swarmly/telegram/control.py` — pause, resume,
stop turn, restart, model, effort, WAKE, freeze and read-only `/cli` per bot; pause-all, the
lease, roles, cleanup, sync and admit for the swarm; and a nudge that costs the owner a turn.
Only a control the running transport can actually carry out is shown, so a button that exists
is a button that works (today: pause, resume, freeze, a safe restart and a stop of one bot;
model, effort, WAKE and read-only `/cli`, which only *report*; pause *everything*; hand the
lease over and fail it back (#154, below); reassign a role; a resync of the swarm; the
cleanup dry run; and a nudge to an item's owner, which costs that bot a turn).
A press on a reporting control answers with what the setting is now and the command that
changes it — a one-tap button cannot take the value a change would need — it changes nothing
and leaves no `CONTROL` note, because the durable line is for who stopped what, not for who
read a setting. `/sync` is one of the controls that is not confirmed, like pause and resume:
it changes no bot's state, it asks the peers to say who they are again. The cleanup button is
the *dry run* — the same code `/cleanup` runs with no argument — so a press can never delete
anything; the words it answers with name the command that would, with its own code. **Stop**
cancels the turns in flight, drops the messages queued behind them and kills any `/cli` run —
the `/stop all` form, because the dashboard speaks for the whole bot rather than one topic. Three rules bound every press:

- **The admin only.** The presser's Telegram user id is checked before the action is even
  looked up, so a stranger is refused and learns nothing about what the buttons do — and that
  answer comes before the lease check too. A stranger pressing a keyboard left over from an
  earlier coordinator is told it is the admin's, never "<bot> coordinates now": who leads is
  not a stranger's to learn (#157).
- **Anything that stops or changes something asks twice.** The first tap mints a one-use code
  and answers with a **Confirm button that carries it** — `ctl:confirm:<code>` — which is the
  only thing that runs the action; the code dies when it is used or after 2 minutes. The same
  answer offers the code as text, and typing `ctl:confirm:<code>` in the chat means exactly
  what the button means: it goes through the press handler, never to the model as prose.
- **Every action leaves a record.** A run posts one `NOTE intent=CONTROL` line —
  `CONTROL bot.restart target=self by=<user id> at=<UTC> state=done` — so the durable plane
  says who stopped what and when. A press that picked a value names it in the same line
  (`CONTROL bot.model target=self value=claude-opus-4 by=…`): a record that says a model
  changed and not which one is not a record.

A swarm-wide control — pause everything — holds the bot that owns the message and broadcasts
`target=*`, so every peer applies it: one press stops the swarm, and each bot keeps its own
`CONTROL` record of the press that stopped it.

That note is also how a control reaches **another** bot: the transport that holds the message
carries out a press for itself, and for any other bot the same `CONTROL` line *is* the
dispatch. Three checks decide it, and none of them trusts the note's own words: **who sent
it** (only the bot holding the dashboard — the coordinator — commands the others, or any known
peer while no lease is readable; `by=` is free text, so without this any bot could write an
admin's id and stop the swarm), **who it names** (the target, or `*` for a swarm-wide
control), and **which admin pressed** (the id must be *this* bot's admin, so a peer repeats the
authorisation against its own config). A note that fails any of them is refused and only
logged — and a note this bot cannot actually carry out answers `failed:`, never `applied:`.
That refusal is never posted in the chat: one press can reach four bots, and a line per peer
that could not follow would put four messages in front of the admin for one action, so the
record is the log and the answer is the presser's own (#157).
One press is one line on the bus: a swarm-wide control posts its own `target=*` note, and the
pressing bot does not add a second, local one.

**A control that needs a value carries it on the button (#153).** Model, effort and WAKE are
pickers: pressing one sends a short list — the models `/model` already knows, the effort
levels, the timers this bot holds — as a message of its own (the dashboard's keyboard is
rewritten on every beat, so a picker cannot live there), and the chosen value rides that
button's own callback data, `ctl:<action>:<target>:<value>`. Nothing is kept on disk, so a
press that arrives after a restart still means what the button said, and the value is capped
so the data always fits Telegram's 64 bytes. A picked value *always* asks for the second tap,
whatever the bare control's rule is, and it is applied by the same function the typed command
runs — `/model <name>`, `/reasoning <level>`, `/wakes cancel <id>` — so a picked value has the
same validation and the same words back as a typed one. A control joins the pickers only when
its choices can be read in code: listing them costs no model turn. Reassigning a role needs
**two** values, so it picks twice: the bot goes in the target slot (`ctl:swarm.role:Codex`) and
the role fills the value next to it (`ctl:swarm.role:Codex:qa-tester`), which keeps one callback
carrying the whole choice. Offering a list is not confirmed — it changes nothing; the second tap
arrives with the finished pick. The picker is posted where the dashboard lives
(`control.chat_id`/`control.topic_id`, else the home channel) and it names the bot the press will
*change*, so the role pick's first step asks which bot rather than claiming one is already chosen.
The role list includes the project's own role files (`<bus repo>/.agents/roles`), and the WAKE
picker says what it does — it cancels a timer, so its buttons read `cancel <id>`.

**Hand the lease over, and fail it back (#154).** Holding the coordinator role is automatic
(#83, #126) — it is renewed on the bus, taken over when it lapses, and reclaimed by the
configured coordinator when it comes back. What was missing is an *explicit* move, and it is
two writes by two bots, because a lease claim is signed by whoever writes it: no bot can claim
the role **for** another.

- **Hand lease** picks a bot, asks twice, then the holder writes a *yield* (an expiry in the
  past at its own term, so nobody waits for the lease to lapse) and one `CONTROL` line —
  `CONTROL swarm.lease target=<new holder> value=take by=<user id>` — naming its successor.
  The successor reads that line, claims **term N+1** as a promoted claim (the admin's press is
  the authority that makes a claim over a live lease legitimate, the same rule `/role` uses),
  announces the term, and pins its own dashboard: the old one is retired, never edited, exactly
  as after a failover.
- **Fail back** is the same operation aimed at the coordinator the swarm *names*
  (`membership.state.coordinator`, else a peer whose role is `coordinator`, else this bot's own
  role). A holder hands the role back; the named coordinator that does not hold it claims the
  next term itself. A swarm that names nobody is told so rather than guessing a successor.
- **Every move bumps the term**, so there is never a moment with two live coordinators: the new
  claim is term N+1 and the old one — yielded, or lapsed — cannot be revived by claiming its
  own term again. Only the **live holder** may give the role away (`lease_fence`), so a stale
  dashboard, one that slept through a failover, cannot appoint anybody.
- One exception to "only the coordinator's notes are obeyed", and only for this action: the
  giver writes the note *and then* yields, so by the time a peer reads it the giver is no
  longer the live leader. A `swarm.lease` note is therefore also obeyed from the bot the lease
  still *records* as the holder at that term — the one bot allowed to give the role away.
  Every other control keeps the strict rule, so a lapsed holder still acts on nothing.

Deploy, upgrade, merging a PR and editing configs or secrets are not on the keyboard at all.

**Native CLI commands — `/cli` (admin only, no model turn).** Swarmly does not reimplement
what each CLI already does. `/cli@<bot> <command>` runs the command on that bot's CLI and
streams the output back into one message that is edited as it goes. It never sends a prompt and
never starts a turn, so it costs **no tokens** — and it works while the bot is paused or out of
quota, which is exactly when someone wants to ask it a question.

- `/cli@<bot> --version`, `mcp list`, `login status`, `models` — one-shot subcommands, run as a
  plain subprocess (`limits.native_timeout_seconds`, default 20s).
- `/cli@<bot> /cost`, `/context`, `/status`, `/model`, `/mcp` — interactive slash commands. The
  CLI resumes **the discussion's own session** in a short-lived terminal, is given the command,
  prints its answer, and exits. Nothing is typed into it but that command.
- `/cli!@<bot> /clear` — the `!` is required for a command that *changes* something. The bot
  answers with what it would do and how to allow it; it does not run anything on the first ask.
- Refused outright: `/compact`, `/review`, `/init` and every other command that would call the
  model (paying tokens from a path built to spend none), `/login`, `/logout` and anything
  touching credentials, and any command not on that runtime's allowlist. A refusal says which,
  and why.
- `/stop@<bot>` also kills a passthrough that is still running.

**What it will not do.** A picker (Claude's `/model` list) prints its options but cannot take a
selection through Telegram: choosing stays with swarmly's `/model` and `/reasoning`. A
passthrough is refused while a turn is running in that topic, because both would drive the same
CLI session. The output is stripped of terminal escapes, capped at
`adapter.max_output_chars` (default 3500) and redacted like any other outbound text.

**Two Telegram limits shape this.** `deleteForumTopic` reaches a topic of any age, but the
Bot API cannot *list* a group's topics, so the topics offered are the ones the transports
know (`topics-<alias>.json`). `deleteMessage` works only on messages younger than 48 hours
and history cannot be listed either, so General cleanup covers only the ids a transport
recorded while running: a per-chat ledger keeps them with their timestamps and prunes those
past 48 h, and the dry run says how many it can no longer reach. Older General messages stay
the admin's to delete by hand.

**Self-wake timers (issue #81).** A reply can arm a timer the transport fires without a model
turn: `WAKE in=10m every=5m until=2h check="gh pr checks 75" then="merge it"`. The line is
stripped like the other directives and answered with one line in the topic (`⏰ wake <id>: … at
<first fire>, every 5m, until <expiry>`), so what will run and when is auditable. The check is
parsed with `shlex`, run without a shell in the checkout the topic uses, and may only start with
a read-only prefix (`swarmly.telegram.wake.ALLOWED_ARGV`: `gh pr checks|view|list|status|diff`,
`gh run view|list`, `gh api` GET only, `gh issue view`, `gh search`, and
`git status|log|diff|show|rev-parse` — never `git fetch` or `git ls-remote`, whose `ext::`
remotes run a command); `-X`/`--method`/`-f`/`--input`/`--output`, shell
metacharacters and anything not on `PATH` are refused, and the refusal is posted in place of the
line. Times are absolute, so a restart resumes the schedule instead of re-arming it.
**One standing check per PR or issue, per topic:** arming another check on the same PR or issue
(however it is worded: `gh pr view 173`, `gh pr checks 173`, `gh api repos/o/r/pulls/173`) in
the same topic replaces the armed timer (the ack says `replaces <id>`) instead of adding one. A
check that exits 0 with the same output the bot last acted on re-arms silently instead of
starting a turn; durations and timestamps in the output (`2m3s`, `about 1 hour ago`) do not
count as a change. Before each firing the transport reads the PR or issue itself: a merged or
closed one ends the timer with no turn, and the turn for an open PR names its head as it is
now, because a head written into `then=` goes stale. An expiry with nothing new since the last
firing retires quietly. So a standing `gh issue view` costs a turn only when the issue changed
(live, round 5 and 22 Oct: copies filled the cap and each firing posted "nothing changed"). **A due timer waits out a turn.** When a timer
comes due for a topic that already has a turn running, the tick neither claims nor runs it — a
running turn often settles the same item, and a second turn queued behind it is a paid "nothing
changed". It stays armed and unclaimed, and is evaluated — expiry included — on the first tick
after that topic's turn ends; the tick reports `held: turn running`. Limits:

| `limits.` | default | what it bounds |
|---|---|---|
| `wake_max` | 10 | live timers one bot may hold |
| `wake_max_per_reply` | 3 | timers one reply may arm (the overflow is refused, not trimmed) |
| `wake_tick_seconds` | 15 | how often the transport looks for due timers; `0` disables the loop |
| `wake_check_seconds` | 120 | a check's own budget; past it the process group is killed |
| `wake_max_attempts` | 50 | firings of one timer before it gives up with a turn saying so |
| `wake_claim_settle_seconds` | 30 | how long a claim must be visible before its writer may fire it, so two overlapping pushes cannot both fire |
| `wake_check_allow` | `["gh", "git"]` | the outer gate on which binary a check may start with |

The check is parsed token by token and every flag is normalized first, so an attached value
(`-XPOST`, `--field=body=x`, `--output=/path`) cannot smuggle a write past the allowlist; `gh api`
carries only read flags (`--jq`, `--paginate`, `--slurp`, `-i`, `-H`). `check=` and `then=` must be
quoted — an unquoted `then` would silently keep only its first word.

A timer whose state the bus refuses is still armed (the local schedule stands) but the ack says
`⚠️ WAKE armed locally only: …`, because the swarm cannot adopt a promise the bus does not have.

`/wakes@<bot>` lists the live timers with their next time, check and attempt count;
`/wakes@<bot> cancel <id>` drops one (admin only); `/status@<bot>` carries `wakes=N`. A frozen
bot runs no check, and neither does a paused one. The fire claims live on the bus under
`.agents/wakes/<alias>/`, one per timer per firing, so an acting
coordinator (#83) can see a timer a dead bot left armed, and two bots cannot fire it twice.

**The expectations table — `/owed` (issue #162).** Facts open and close rows; a model's word
never does. Each row names who owes what (a bot, `CI`, or `admin`), on which item and head,
who is waiting, the fact that opened it, the last value observed and a deadline, and its
id `<kind>:<item>[@<head>]…:<debtor>` *is* the settlement rule: re-observing a fact updates
one row, a different head is a different debt. The line says each thing once — `Reasonix owes a
review verdict on 22e68c5 for #170`, one `on` and the item introduced by `for` — and a sweep
refreshes a row's observation only where it is the thing that observes (the gap on a progress
row), never a value another source wrote.

What this transport infers and records **today**: the hand-off, and the facts the round
state already carries. A reply that addresses a peer opens one row per peer addressed (the
multi-recipient case that missed DeepSeek on 22 Oct), in the discussion it was made in; that
peer's reply **in that discussion** closes it, and a reply anywhere else does not. A sweep on
the heartbeat tick reads the round's items and opens the rest, each bound to the fact that
opened it: a new head (CI owes a result, the reviewer and QA owe verdicts on **that** head), a
fix-first or QA fail (the developer owes a new head), CI green with a merge verdict and a QA
pass on one head (the coordinator owes the merge), an unticked item with an owner (that owner
owes progress), and an item waiting on the admin (the admin owes an answer — a reminder, never
an action). A fact the state already carries closes its own row in the same pass — a verdict,
a CI result (a failed run counts: it is an answer), a new head answering the old head's
verdicts, a merge answering the item — and a row for a head becomes owed again only when the
head moves.

Rows live in this bot's state (`.agents/state/owed/`, one file per row) and are mirrored as bus
documents under `.agents/owed/<alias>/`, so a successor coordinator inherits what is still
open; only a row that actually changed is written, so a beat without news costs no bus commit.
`/owed@<bot>` lists the open rows, most overdue first, and says `cannot read the table` rather
than "nothing is owed" when a row file is unreadable. Nothing here reads a bot's prose: a
question to the admin is observed as a round item waiting on the admin, not as a sentence.

**Waking on news (#163).** A row wakes the bot that owes it only when the value observed for it
differs from the value that last woke anyone (`Row.woken`), and the wake carries the delta —
"a result on c166987 on #146: pending to green", not "check again", with the head said once
rather than twice — so a standing check that finds nothing unchanged starts no turn at all.
**A wake needs a bot behind it**: `CI` owes a result and `admin` owes an answer, and neither
has a turn to start, so their rows are observed and never woken; the transport passes the
roster (`owed_bots()`) and a debtor off it is treated the same way. **A bot wakes only what it
owes**: every table folds every writer's documents, so a peer's row is on it too, and waking it
here would run *my* turn about *their* debt and write `woken` on the shared row before the
debtor ever saw the news. An escalation is posted by the **coordinator** — the swarm's one
voice to the admin — and held by everyone else. The transport looks at the
table once per heartbeat, right after the sweep: it resolves the row's `tg-` token back to a
chat and thread through its own topics (a debt no topic owns wakes in General), **holds** the
wake while that topic already has a turn running (the rule from #160 — a turn queued behind a
turn usually settles the same debt), and starts at most two turns a tick until the per-round
budget lands (#164). What woke whom, and when (`Row.woken_at`), is written onto the row before
the next tick, so a restart cannot wake the same bot about the same observation twice. **An
ignored wake is nudged, then escalated**: a wake is given `WAKE_GRACE` (ten minutes), after
which the same observation earns one more wake that says nothing has changed, and after
`MAX_WAKES` the row goes to the admin instead — **in General**, the place the admin is
addressed, not the discussion the debt lives in — and only once. Silence is not the same as an
ignored wake: a row seeded with a value and never woken (`wakes == 0`) still wakes nobody. A
claim that contradicts the facts compares heads through `same_head`, so a seven-character SHA
the forty-character head agrees with is not a contradiction.

**Invariants, deadlines, budget (#164).** Three rules keep the table honest while nobody is
looking. **An open round item with no live row is an orphan** — nothing owes the next step on
it — so the sweep opens `the next actor owes a next step on #N`, carrying the concrete gap
(`PR #146: CI green, merge verdict, QA pass, not merged`): that is the 09:13 case where no WAKE
was armed and nothing in the table said the round was stuck. `next_actor()` derives who must act
next from the item's own facts (a result, then the reviewer, then QA, then the coordinator), and
the orphan's debtor is that bot — a reviewer with a merge in hand is the one reminded, while an
item waiting on nothing but CI goes to the coordinator, and a role nobody holds stays a flag
rather than a guess: that row is still the coordinator's to assign, but it carries
`⚑ no next actor: …`, so `/owed` shows a PR nobody is on instead of one more coordinator debt.
The flag is computed on the table **as the facts leave it** — a CI row the same pass opens and
closes is not a live row — and it **clears**: when the roster gains the missing role, or the
item finds its actor, the sweep retires that row with `closed_by: the item's next actor is
decided`. The next-actor row carries the sweep's own `key_extra` (`@next`), which is what keeps
it apart from an owner's progress row and lets the same debt's gap move with the item as QA and
review land instead of freezing at the day it opened. **Every row has a
deadline**: one that no fact has moved for `limits.stall_minutes` (default 30) climbs three
rungs, each happening once and each logged — a reminder to the debtor in code, then one
coordinator turn to reassign or escalate, then the admin. **Who climbs**: only the row's own
debtor posts the reminder, and only the coordinator the two rungs after it, in code — the table
folds every bot's documents, so without that check four bots would each post the reminder and
push the ladder four rungs for one stall (the rule #177 set for wakes, applied to the ladder). **A round's coordinator wakes are
budgeted** by `limits.round_wake_budget` (default 20 a day, counted in
`.agents/state/owed-wake-budget.json`, beside the table and never inside it, so a restart does
not refill it): reaching it pauses those wakes, leaves the table readable in `/owed`, and tells
the admin **once per round per day** — the notice returns at midnight, because a one-shot flag
left it silent for good. The count belongs to the round written **last**, not to the last name:
round keys are names (`repo#86`, `repo#5`, `repo#10`), so the newest is chosen by the state's
own `term`, `seq` and `updated_at`, and the lexically last key is only the fallback. Both
settings are validated with the rest of `limits` (`stall_minutes`, `round_wake_budget`, where 0
means the ladder off and no cap). The wake budget replaces the per-tick cap for coordinators;
the tick's own two-turn bound stands.

**The head under review is frozen (#183).** Three rules, on the facts the round state already
carries. **A claim is checked**: when a message names a SHA as a PR's head ("#173 is at 840f8b0",
"pushed 840f8b0 to #173") and the PR's `headRefOid` is another commit, the transport posts one
notice in that discussion and nothing closes — the PR's head decides, not the claim (a bare SHA,
or a SHA in another sentence, is not a claim). **A rewrite under review is flagged**: a verdict
row is pinned to a head, so an open verdict whose item has moved on is a branch rebuilt under
review; the transport says so once per (item, old head, new head), in the row's own discussion,
and the row stays open — the verdict on that head is still owed, and nits go to follow-up issues
instead of a new head. **"Ready" means mergeable**: `head_freeze.ready_blockers()` answers a
ready claim with GitHub's `mergeable` fact (a `CONFLICTING` PR is not ready) and with the head
the verdicts were given on. Both checks are said once and remembered in
`.agents/state/head-freeze-seen.json`, so a restart and a quiet beat repeat nothing. The head
every check compares against is the **round state's** (`items[*].head`, which is the PR's
`headRefOid` once the transport reads it), not a live GitHub call — and the rewrite check runs
**inside the sweep, before the closing facts**, because `settle` closes exactly the stale
verdict rows it reads.

**Short-lived notices.** The same group fills with its own machinery: an ONLINE line every
restart, SEEN answers, "busy", "paused", "moved this to …", hand-off reminders, receipt
failures. Each transport deletes **its own** notices from General once they are older than
`limits.ephemeral_notices_minutes` (default 60; `0` disables the sweep). Only messages that
transport itself posted *and* marked as machine notices are recorded — never a person's
message, never a model reply, never a peer's post — and only while they are inside
Telegram's 48-hour delete window. The sweep runs on its own one-minute timer, so a notice
disappears at most a minute after its limit. What the transport cannot delete stays for
`/cleanup` or the admin.

## 6. Durable bus poller (`bus-poll --once`) — scheduling per host, gates

When the Telegram transport is unreachable (daemon down, host offline, network partition), agents
fall back to the durable git bus: every addressed message is still delivered to the adapter, but
the Telegram echo is skipped until the transport comes back. To make that delivery run *without*
the daemon being up, the per-host bus poller is a one-shot CLI subcommand:

```bash
python -m swarmly.telegram.cli --config agents/hermes/config.yaml bus-poll --once
```

This subcommand runs with **no Telegram token and no Telegram network** — it only touches the
git bus. It is the entry point for per-host schedulers (Task Scheduler on Windows, cron + systemd
on Linux) and exits 0 on success. It shares the same load → inject → ledger core as the in-
transport `check_gitbus()` so the two paths cannot drift.

### Schedule shapes

| host | scheduler | entry |
|---|---|---|
| Windows | Task Scheduler | `schtasks /create /tn "a2a-bus-poll" /tr "C:\path\to\.venv\Scripts\python.exe -m swarmly.telegram.cli --config C:\agents\hermes\config.yaml bus-poll --once" /sc minute /mo 10` |
| Linux | systemd timer + cron fallback | `[Unit] Description=Swarmly bus poll After=network-online.target [Service] Type=oneshot ExecStart=/srv/a2a/.venv/bin/python -m swarmly.telegram.cli --config /srv/a2a/config/hermes.yaml bus-poll --once` plus a `*.timer` that fires every 10 min |
| Linux | plain cron | `*/10 * * * * /srv/a2a/.venv/bin/python -m swarmly.telegram.cli --config /srv/a2a/config/hermes.yaml bus-poll --once` |

### Config

The first-class `bus:` section supersedes the legacy `fallback.gitbus_*` keys. The legacy keys
remain accepted as aliases; the `bus:` keys always win when both are present.

```yaml
bus:
  repo: /srv/projects/your-project          # required: unset, the bus is not polled
  ref: origin/main                          # default
  poll_minutes: 10                          # 0 disables; range 1–180
  enabled: true                             # false == poll_minutes: 0
  verify_settle_seconds: 3                  # settle before the verify probe (0 disables; capped at 30)
  senders: [codex, claude]                  # optional: only these bus senders are delivered
  allow_unsigned: false                     # default; true turns off signature AND revocation checks (demos only)
fallback:                                   # legacy aliases still accepted
  gitbus_poll_minutes: 10
  gitbus_root: /srv/projects/your-project
  gitbus_ref: origin/main
```

**Interval default.** When neither `bus.poll_minutes` nor `fallback.gitbus_poll_minutes`
is set, the poller runs every **30 minutes** (the legacy
fallback default was 10). Set `bus.poll_minutes: 10` to keep the old cadence — the
shipped template does.

**Broadcast FREEZE/RESUME over the bus is currently refused**: the admin gate
needs an author identity the bus path cannot supply yet, so a fleet-wide freeze must arrive
via the Telegram path, and an outage lift uses *directed* envelopes (`to=<alias>`), which
apply even while frozen. A bus message counts as a broadcast when either its header recipient
or its signed `to=` is `*` (or empty). A refused broadcast is recorded in `machine_events`:
it freezes nothing. A FREEZE/RESUME whose signed `to=` names another agent is recorded and
ignored, whatever its header says.

### Gates and contracts

* **No default bus, and no history.** Only the repository in `bus.repo` (or the legacy
  `fallback.gitbus_root`) is polled; without one the cycle is skipped (`no_bus_repo`) and
  nothing is read. A cycle without a usable seen-ledger (the first cycle, a deleted ledger,
  or a corrupt one, which is quarantined as `.corrupt-<time>`) records every message already
  on the bus as seen, stamps the ledger with `since`, and delivers none of them (`baseline`);
  entries in the deferred store are left out and still drain. A new baseline therefore drops
  whatever arrived since the last completed poll, and so does pointing `bus.repo` at a
  different checkout from the one holding the ledger. Later cycles deliver only unseen
  messages stamped after `since` (allowing 15 minutes of clock skew), so history stays
  undelivered even after its ids rotate out of the 5,000-id ledger. The cap counts ids, not
  age: a message newer than `since` whose id has rotated out would be delivered again, so a
  bus with more than 5,000 messages inside the 7-day signature window is not supported. The
  ledger is written to
  a temporary file and renamed, so an interrupted write never truncates it. A ledger from
  an older version (ids but no `since`) is upgraded rather than re-baselined: its file's
  last write time becomes `since`, so mail that arrived after that poll is still delivered.
* **Signed messages only.** A bus message reaches the agent only when its signature verifies
  against the sender's key in `config/a2a_config.json` under `bus.repo` (an `A2A_KEY_<SENDER>`
  variable for HMAC, the public key for Ed25519), and its sender is not revoked. With
  `bus.senders` set, the sender must also be listed. Anyone who can write to the bus can
  otherwise put a prompt in front of the agent, so a message that is unsigned, whose payload
  was changed, or that is signed with a key the receiver does not hold, is never delivered.
  Unseen rejected mail addressed to this agent is counted in one warning per cycle and listed
  in `rejected` (the outcome, `bus-poll --once` output and the heartbeat). Rejected messages
  are not marked seen, so once the receiving host holds the sender's key they are delivered on
  a later cycle: the scheduled `bus-poll --once` reads `.env` each run, while a long-running
  transport needs a restart, and the registry is read from the `bus.repo` checkout, so a
  registration merged upstream counts once that checkout is updated. A sender
  verifies only when it has a registry entry (an `A2A_KEY_<SENDER>` in the environment alone
  is not enough), and two registered ids that map to the same variable (`a-b`, `a_b`) verify
  neither. With HMAC every receiver holds every sender's secret; prefer Ed25519 for bus
  senders. FREEZE/RESUME envelopes pass the same check. Messages deferred while frozen by a
  version that did not check signatures are dropped with a warning.
  `bus.allow_unsigned: true` turns off the signature and revocation checks, for a local demo
  bus without keys.
* **What a signature covers.** A bus signature covers the whole envelope: payload, sender,
  recipient, `message_id`, intent, priority and timestamps (`docs/ENVELOPES.md`). A copied
  message cannot be re-addressed or given a new id, and its own signed timestamp must be less
  than 7 days old, so an old RESUME cannot be replayed to lift a freeze. Messages signed the
  old way (payload only, no `v2:` prefix) are refused unless the registry sets
  `"accept_v1_signatures": true`, which is meant only for the hours of a rollout.
* **Single-writer lock.** A per-host `.agents/state/<alias>.poll.lock` file prevents two pollers
  (e.g. a Task Scheduler tick racing the in-transport tick) from double-delivering. The loser
  skips the cycle.
* **Freeze stays bus-visible.** While frozen, the poller still reads/ledgers and applies
  `STATUS intent=FREEZE`/`intent=RESUME` envelopes. Actionable bodies are deferred (not dropped,
  not marked seen) and deliver after unfreeze; admin/STATUS pass through.
* **Delivery verification.** Every injection carries a unique planted token
  (`[BUS_DELIVERY_TOKEN=<token>]` in `response_instructions`). The `codex_thread` adapter probes
  the reply-store SQLite for the token; the `antigravity_thread` adapter probes the conversation
  SQLite at `~/.gemini/antigravity/conversations/<conv_id>.db` read-only with the
  `step_type = 101 AND step_payload LIKE '%<token>%'` filter (the only inbound record;
  tool-argument echoes at `step_type = 15` are NOT counted). A probe **error** logs and continues
  (never escalates); only a clean probe with no row after N attempts (default 3) marks
  `delivery_unknown` + escalates a `BLOCKED` envelope. The first probe waits out the settle
  window (`bus.verify_settle_seconds`, default 3 s, capped at 30 s) so a just-delivered message
  is not probed before the session can show it. An escalation whose bus write fails
  is counted in `bus_write_failures` (cycle outcome, CLI summary, and the heartbeat) — a fresh
  beat with failed writes does not imply the escalation was persisted.
* **Offline echo skip.** When Telegram is down the poller delivers adapter-first and SKIPS the
  echo — no retry queue. Outbound fallback envelopes carry `message_id=<original>` plus
  `dedupe=envelope:<id>` / `dedupe=ticket:<id>` so reconnect-side receipts can dedupe.
* **Broadcasts.** `to=*` traffic is delivered through the same `MachineEventClassifier`
  (`HELLO`/`RECEIPT`/`ALERT` recorded as state, no model wake) under the existing
  alert discipline.

## 7. Adding an agent

1. Create the bot in @BotFather, enable **Bot-to-Bot Communication Mode**, and add it to the
   group as an **admin** (or disable its group privacy).
2. Copy the example config onto that machine; set `name`, `alias`, `bot_token`, the shared
   `home_channel_id` and `admin_user_id`.
3. Run `check`, then `run`. On start it posts `HELLO intent=ONLINE ref=<alias>`, with a line naming its adapter, role, the model and reasoning effort General really runs at, and its revision. Each comes from `/model` and `/reasoning`, else `adapter.model` and `adapter.effort`, else the CLI's own settings (`swarmly/telegram/cli_defaults.py`: Codex `~/.codex/config.toml`, Reasonix `reasonix.toml` / its global `config.toml`, Claude Code `ANTHROPIC_MODEL` / `CLAUDE_CODE_EFFORT_LEVEL` / `settings.json`), else the model the CLI reported on its last turn (Claude's `modelUsage`). What none of these names reads `not known until its first turn` or `the model default`; effort is left out for an adapter without one.
4. In the group, the admin sends `/admit @newbot`. Every running bot that saw the newcomer's
   HELLO adds it to its peers and answers with a silent `HELLO intent=WELCOME` mentioning it;
   the newcomer, having seen itself admitted, adds each bot that welcomes it in the next 10
   minutes. Nothing else is needed on the other machines.

Only the admin can `/admit` or `/remove @bot` (a bot added to the group by anyone else stays
out of the bots that admit). The newcomer itself has nothing to check a welcome against: it
trusts the bot accounts that welcome it by alias and @username in its window. So after an
`/admit`, check the @username of each bot that welcomed it and `/remove` any that is not one
of the swarm's bots (`/status` lists aliases only, so it cannot show a bot that took a member's
alias). A removed bot comes back only with a new `/admit`. Peers learned this way are kept
next to the config in `<config>.roster.json` and merged into `peers:` on load; a peer written
in `peers:` always wins and `/remove` never drops it. `/status` lists the current peers
(`peers=`).

A bot that was offline during `/admit` catches up with `/sync` (admin only): every bot posts a
silent `HELLO intent=SYNC` listing its first-hand peers (its config, the bots it admitted
itself, and the bots Telegram confirmed as group admins when they joined) as `Alias=@username`, and for the next 10 minutes each bot adds the bots that one of
its known peers lists. A peer learned second hand is not passed on. A newcomer that missed its
welcomes catches up with a second `/admit @newbot`: the bots that already have it welcome it
again. Writing the newcomer into every other agent's `peers:` map still works too.

A few things to know:

* A newcomer's HELLO stays admissible for 24 hours, on the bots that saw it and have not
  restarted since (`/sync` catches up one that has). After that, restart the newcomer (it posts
  its HELLO on start) before the `/admit`.
* Give each bot a distinctive one-word alias (letters, digits, `-` or `_`). A bot whose alias
  is not one word can't be admitted or listed by `/sync`, only written into `peers:`. The
  welcome shows the alias the newcomer chose; an ordinary word such as `Note` would make a
  person's "Note: @HermesBot ..." count as naming that bot first. `/remove` it and pick
  another alias.
* `/admit`, `/remove` and `/sync` post nothing back except the welcomes and peer lists; a
  handle nobody knows, or an alias already in use, only shows in the bots' logs. Check
  `/status` afterwards.
* Admit new bots once every bot in the group runs a version with `/admit`. An older bot
  treats `/admit @it` as an ordinary request and answers it, and can't be admitted itself.

Adding a new *runtime* (rather than a new bot) is one class in `swarmly/telegram/adapters/` plus a
line in `build_adapter` — the channel itself never changes.

## 8. Tests

```bash
python -m unittest discover -s tests -t .        # no network, no aiogram needed for routing
```

The routing, loop-safety, pacing, escaping and adapter command construction are all exercised
with an injected sender and a fake subprocess runner, so the suite makes no API calls and
spawns no agent processes. aiogram-specific paths are covered behind `skipUnless`.

