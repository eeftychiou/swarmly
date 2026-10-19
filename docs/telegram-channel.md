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
when there is one, else in General. Short answers stay in General. A
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
  continues the same context. A `custom` adapter is allowed too: only set N above 1 if each
  run is independent.
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

**Two Telegram limits shape this.** `deleteForumTopic` reaches a topic of any age, but the
Bot API cannot *list* a group's topics, so the topics offered are the ones the transports
know (`topics-<alias>.json`). `deleteMessage` works only on messages younger than 48 hours
and history cannot be listed either, so General cleanup covers only the ids a transport
recorded while running: a per-chat ledger keeps them with their timestamps and prunes those
past 48 h, and the dry run says how many it can no longer reach. Older General messages stay
the admin's to delete by hand.

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
3. Run `check`, then `run`. On start it posts `HELLO intent=ONLINE ref=<alias>`.
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
