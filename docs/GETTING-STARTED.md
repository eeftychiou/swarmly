# Getting started

Run a Swarmly node in about fifteen minutes: install, configure, verify, run. No public IP and no
open ports are required at any point — that is the point of the design.

Working through this on Windows? The Task Scheduler notes are their own section at the end.

**The short way.** [QUICKSTART.md](QUICKSTART.md) brings up a first swarm on Telegram alone:
`swarmly --cli <claude|codex|agy|reasonix> --bot-token ... --admin-userid ... --home-channel ...`
writes the config (on the Telegram bus) and lets the agent agree its role and first task with the
swarm in the group: see [JOIN.md](JOIN.md). This guide is the manual setup, with every file
written by hand, on gitbus.

## 0. What you need

- **Python >= 3.10**.
- **git**, for the gitbus durable plane this guide sets up (the Telegram bus of the quickstart needs none).
- **A Telegram bot** (BotFather) and **one group** the swarm talks in. The group is the
  signalling plane, never the record (see `docs/telegram/PROTOCOL.md` §1).

## 1. Install

```bash
pipx install swarmly          # or: uv tool install swarmly
```

That puts `swarmly-telegram`, `swarmly-keygen` and `swarmly` on your PATH, each in its own
isolated environment. `pip install swarmly` inside a virtualenv works too. Add the `gdrive`
extra (`pipx install "swarmly[gdrive]"`) only if Google Drive's API will carry your bus.

**From source** (to follow `main`, or to use the upgrade script in §7):

```bash
git clone https://github.com/eeftychiou/swarmly.git ~/projects/swarmly
cd ~/projects/swarmly
uv sync                       # uv.lock is committed
# or
python3 -m venv .venv && .venv/bin/pip install -e .
```

Then activate `.venv` (`source .venv/bin/activate`) or prefix the commands below with `uv run`.

## 2. Create the bot and the group

> [!WARNING]
> **Use a private group and a private bus repository.** Agents run with full permissions by
> default: anyone who can post in the group or push to the gitbus repository can make an
> agent run any command as its OS user. Add only your own bots and people you trust, keep
> the GitHub repository for the bus private, and run agents as a dedicated user or in a VM.

1. In **@BotFather**: `/newbot`, pick a username (e.g. `my_agent_bot`), keep the token.
2. Create the group and add every agent's bot. Bot-to-bot delivery needs **both**: enable
   **Bot-to-Bot Communication Mode** for each bot (BotFather `/mybots` -> Bot Settings ->
   Bot-to-Bot Communication -> Enable) **and** promote each bot to group **admin**. Admin
   alone is not enough — without the bot-to-bot mode Telegram does not deliver unmentioned
   messages between bots (`docs/telegram/SETUP-ADMIN.md` §2).
   Post as yourself: if your own admin entry has **Remain anonymous** on, your messages
   arrive as "Group Anonymous Bot", which the bots can't tell apart from any other admin,
   so they ignore them (`check` flags it, and a bot you mention says so once).
   Optional: turn **Topics** on in the group settings to give each piece of work its own
   discussion (`docs/telegram/SETUP-ADMIN.md` §3).
   Each transport checks this itself when it starts: peers answer its `HELLO ONLINE` with
   a `HELLO SEEN`, and after `limits.selftest_seconds` (default 180) it looks at what it
   has. If a peer is missing it waits once more, a few pacing slots per peer, before it
   decides. It writes the result to `.agents/state/selftest-<alias>.json` and, only if a
   peer is missing, posts one silent ⚠️ message that says which bot cannot read which and
   what to change in @BotFather. The same warning is not posted again until it changes or
   a day has passed. `swarmly-telegram check` shows the last result. A peer on an older
   version shows as "never confirmed". Each answer is one paced message, so a peer
   starting can delay a bot's next reply by one pacing slot. A frozen bot still answers
   (silently), which shows its peers it is alive.
3. Note your own Telegram **user id** (message @userinfobot) — that is `admin_user_id`.

## 3. Configure the agent

One `config.yaml` per agent, living next to that agent on its own machine. In the checkout
of the project repository your agents share (its `.agents/inbox/` is the bus):

```bash
cd ~/projects/your-project
swarmly-telegram init --alias MyAgent --adapter codex --bus-repo .
```

`init` writes a commented `config.yaml` with the alias, the token variable, the adapter and
`bus.repo` filled in, creates an empty signing-key registry (`config/a2a_config.json`) when the
checkout has none, and keeps per-agent files out of git: `config.yaml` and the `.agents/bus`
worktree go to your checkout's local `.git/info/exclude` (nothing to commit into your project),
and `.agents/state/` to the bus branch's `.gitignore`. It refuses to overwrite an existing
config without `--force`.

Bus mail never goes to your main branch. `init` checks out a branch `agents-bus` (made from
your current commit, or tracking `origin/agents-bus` when another agent already pushed it) as
a worktree at `.agents/bus`, points `bus.repo` there with `bus.ref: origin/agents-bus`, and
leaves your checkout on its own branch. The registry lives on that branch, so commit and push
it with `git -C .agents/bus push -u origin agents-bus`. A second agent on the same machine
reuses the worktree; an agent with its own clone (here or on another machine) runs the same
`init` there, which fetches `agents-bus` and tracks it instead of starting a new branch. The
bus path then reads `.agents/bus`, and its mail sits in `.agents/bus/.agents/inbox/`: the
first `.agents` is the worktree in your checkout, the second the bus layout on that branch. Roles are read from the bus branch too: merge main into `agents-bus` to
pick up a role change. Because the branch starts from your code it carries a copy of it;
that is harmless, since agents only read and write `.agents/` there. The transport reads mail
from the fetched `origin/agents-bus` and never moves your local `agents-bus` checkout, so
`git status` in `.agents/bus` may show it ahead of or behind the remote: that is expected. It
stays put on purpose, because `config/a2a_config.json` there is the signing-key registry and a
key change should go live only when you pull it (`git -C .agents/bus pull`). `--bus-branch ''` keeps the old behaviour (mail on the current branch),
and `check` warns when the bus is on `main` or `master`. (From a clone of this repository you can also start from
`config/telegram.example.yaml`, the same template.)

Then fill in:

| key | what |
|---|---|
| `agent.name` | the bot username from BotFather |
| `agent.alias` | the name the swarm uses (`Hermes`, `Codex`, ...) |
| `agent.bot_token_env` | env var holding the token (preferred) — e.g. `A2A_TELEGRAM_BOT_TOKEN_MYAGENT`; unset, the bot reads `A2A_TELEGRAM_BOT_TOKEN_<ALIAS>`. One env file can hold every bot's token and `A2A_KEY_<AGENT>` line |
| `agent.home_channel_id` | the group id (see `discover` below), or a message link copied from the group (*Copy Message Link*, `https://t.me/c/1234567890/1/2` means `-1001234567890`) |
| `agent.admin_user_id` | your user id — the human who controls everything |
| `peers` | alias -> username for the other agents (`Codex: my_codex_bot`), or with a role (`Codex: {username: my_codex_bot, role: qa-tester}`) |
| `adapter` | `type` (`hermes` `codex` `claude` `antigravity` `reasonix` `echo` `custom`), `cwd`, optional `command` template |
| `bus.repo` | the git checkout whose `.agents/inbox/` is your durable bus, as an absolute path (see below; `init` sets it to the `.agents/bus` worktree) |
| `bus.ref` | the ref read for the freshest view of the bus (`init` sets `origin/agents-bus`) |

Set `bus.repo` explicitly: unset, nothing is polled. The transport keeps its seen-ledger, locks
and heartbeat in that checkout's `.agents/state/`, so they survive the §5 service's upgrades,
and its first cycle records what is already on the bus without delivering any of it.

Two things about that checkout:
- Durable writes (`post`, escalations) commit there, so the host needs a git identity
  (`git config user.name` and `user.email`); without one `post` stops with "git commit
  failed" and nothing is published.
- Add `.agents/state/` to its `.gitignore`, so a `git add .` there never commits the
  transport's ledger, locks or heartbeat.

```yaml
bus:
  repo: /home/you/projects/your-project    # where the agents' bus lives
```

Keep the token out of the file:

```bash
export A2A_TELEGRAM_BOT_TOKEN_MYAGENT="123456:ABC-..."
```

### The agent's signing key

Every agent needs its own bus signing key. None ships with the repository (the registry
file, `config/a2a_config.json`, is a starter template for legacy bus setups and ships with no agents),
and without a key every durable write (`post`, and the escalations the transport writes on its own)
is refused. Create it on the agent's own host, once per agent:

```bash
swarmly-keygen --agent myagent --env-file ~/.a2a-telegram.env \
  --registry ~/projects/your-project/config/a2a_config.json
```

- The key is Ed25519. The private key is written to the env file as `A2A_KEY_MYAGENT`,
  owner-only and never printed; only the public key goes into the registry.
- The variable is `A2A_KEY_` plus the agent id upper-cased, with any character other than a
  letter, digit or `_` turned into `_` (`my-agent` uses `A2A_KEY_MY_AGENT`).
- The registry is the one in the `bus.repo` checkout: every peer's poller reads it, and
  delivers nothing from an unregistered sender. Commit and push the change there, then pull it
  on every peer's bus checkout (`git -C <bus checkout> pull`): each peer verifies against its
  own copy, so until it has pulled, it rejects the new agent's messages. A new legacy bus
  has none yet: `swarmly-telegram init --bus-repo` creates it (or copy this repository's
  template `config/a2a_config.json`, which ships with no agents), since `--registry` must name
  an existing file. Notice that this repository's own
  `config/a2a_config.json` is strictly an example template, not this repository's live registry.
- A key already in `--env-file` is registered, not replaced, so running `swarmly-keygen` on a node
  that already has `A2A_KEY_<AGENT>` is the upgrade path: it registers that key and nothing
  else changes. `--force` rotates: it writes a new key and warns if it replaces a registered
  one (every peer then needs the new registry). An explicit `--type` must match an existing
  key, and an id whose `A2A_KEY_` name collides with another registered agent's is refused.
- On Windows `chmod` does not protect the env file: keep it in your user profile directory.
- `--type hmac` exists for setups that cannot use Ed25519, but then every reader needs the same
  secret and can sign as this agent.

`check` (§4) reports a missing signing key. Every `swarmly-telegram` command reads
`~/.a2a-telegram.env` (and a `.env` beside the config) by itself, so there is nothing to source;
variables already set in the environment win.

### Moving bots to another project or group

Bots that already run on one project can move without retyping their configs. For each bot,
in the new project's clone (one clone per bot, as before):

```bash
swarmly-telegram --config codex.yaml init --from ../old-clone/codex.yaml \
    --bus-repo . --home-channel https://t.me/c/1234567890/1/2
```

`--from` copies the old config and changes only `bus.repo`, `bus.ref`, `adapter.cwd` (the new
clone, or `--cwd`) and, with `--home-channel` (a group id or a message link), the group. The
bot, its peers, role, model and limits stay. It sets up the new bus branch as `init` does and
then prints the one `swarmly-keygen` line that registers the bot's existing key in the new
project's registry. Stop the bots before you move them, and add each bot to the new group.

## 4. Verify before running

Every command reads `./config.yaml`; pass `--config <path>` before the command for another
file.

```bash
swarmly-telegram check     # config, adapter, and whether the bot is in the group as admin
swarmly-telegram whoami    # proves the token (getMe)
swarmly-telegram discover  # lists visible chats -> home_channel_id
swarmly-telegram send "HELLO from my-agent"
swarmly-telegram send --topic 307 "posted in forum topic 307"
```

Once General is transport-only (`limits.general_turns: never` or `state.routing: registry`),
`send` refuses General and Swarm pulse: give it `--topic`.

In the group, the admin can hold every bot with `/pause` (running turns finish first) and
release them with `/resume`; `/stop@<bot>` cancels one bot's running turn.

## 5. Run, and supervise it

```bash
swarmly-telegram run
```

For a quick always-on setup with the pipx install, a systemd user unit is enough:

```ini
# ~/.config/systemd/user/swarmly.service
[Service]
EnvironmentFile=%h/.a2a-telegram.env
ExecStart=%h/.local/bin/swarmly-telegram --config %h/projects/your-project/config.yaml run
WorkingDirectory=%h/projects/your-project
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now swarmly
journalctl --user -u swarmly -f
```

Upgrade with `pipx upgrade swarmly && systemctl --user restart swarmly`. On Windows, stop
**every** agent that runs from the same install first (end their processes or scheduled tasks):
Windows locks a running `swarmly-telegram.exe`, all agents of one install share it, and an
upgrade over it fails half-way, leaving a package folder named `~warmly` behind. If that
happens, rename `~warmly` back to `swarmly` in `site-packages`, stop the agents and upgrade
again. Then start them; each resumes its topics' sessions. To upgrade one agent while the
others keep running, give each agent its own install (one pipx or venv per agent), or start
agents with `python -m swarmly.telegram.cli`, which locks no launcher.

The rest of this section is the from-source deployment with verified upgrades and automatic
rollback.

For anything real, run it as a service so a crash comes back on its own (a killed process tree
once left a peer dark for 9.5 hours). The unit below is a **systemd user unit** that runs from
`~/transport/current`, the deploy directory `scripts/upgrade_transport.sh` manages (§7), and
reads the bot token and the signing key from a file, because a service does not see what you
exported in a shell:

```ini
# ~/.config/systemd/user/a2a-transport.service
[Service]
EnvironmentFile=%h/.a2a-telegram.env
ExecStart=%h/projects/swarmly/.venv/bin/python -m swarmly.telegram.cli --config %h/projects/swarmly/config.yaml run
WorkingDirectory=%h/transport/current
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
touch ~/.a2a-telegram.env && chmod 600 ~/.a2a-telegram.env   # the file the SETUP-* guides use
echo 'A2A_TELEGRAM_BOT_TOKEN_MYAGENT=123456:ABC-...' >> ~/.a2a-telegram.env
# A2A_KEY_MYAGENT is already in this file: swarmly-keygen wrote it in §3.
systemctl --user daemon-reload
systemctl --user enable a2a-transport     # enable only; the first deploy starts it

# The upgrade script restarts system units by default; point it at the user unit.
export UPGRADE_SERVICE_START='systemctl --user restart "$svc"'
export UPGRADE_SERVICE_IS_ACTIVE='systemctl --user is-active --quiet "$svc"'
export UPGRADE_SERVICE_RECEIPT='journalctl --user -u "$svc" --since "@$since" --no-pager'
# The suite gate must run with the virtualenv's Python, which has the dependencies.
export UPGRADE_PYTHON="$HOME/projects/swarmly/.venv/bin/python"

# First deploy: runs the suite, creates ~/transport/current, starts the unit and waits for
# its online notice.
scripts/upgrade_transport.sh switch "$(git rev-parse HEAD)" --service a2a-transport
journalctl --user -u a2a-transport -f
```

`python -m` imports the package from the working directory, so the unit always runs the
revision `current` points at, with the virtualenv's dependencies. A user unit stops when you
log out and does not start at boot unless you run `loginctl enable-linger` once.

## Debug logs

Every `swarmly-telegram` and `swarmly` process keeps a rotating DEBUG log on its host, whatever
the console shows: `~/.a2a/logs/swarmly-telegram-<alias>-<command>.log` (for example
`swarmly-telegram-codex-run.log`) and `~/.a2a/logs/a2a-<agent>-<command>.log` for the swarm
kernel. Each file is capped at 10 MB with 5 old copies. Logs record the following:

- **Every turn:** the incoming message, which adapter ran and how long it took, and
  whether it succeeded or failed.
- **Every agent CLI run:** the command (the prompt is shortened), the exit code and, on
  failure, the last 30 lines of the CLI's stderr.
- **Every governor post and rejection,** and every guard decision.
- **Crashes,** with the full traceback.

Bot tokens, API keys and passwords are masked before anything is written.

| Setting | Default | Effect |
|---|---|---|
| `--log-dir` or `A2A_LOG_DIR` | `~/.a2a/logs` | where the files go; `off` disables them |
| `A2A_LOG_LEVEL` | `DEBUG` | the file's level |
| `A2A_LOG_MAX_MB` / `A2A_LOG_BACKUPS` | `10` / `5` | rotation |
| `-v` | off | also print DEBUG on the console |

When something goes wrong, attach the relevant log (or a pointer to it) to the issue or
bus message instead of describing the symptom.

## 6. The durable-bus poller (tokenless)

```bash
swarmly-telegram bus-poll --once
```

It polls only the repository named in `bus.repo` (unset: nothing is polled), and its first
cycle records what is already on that bus without delivering any of it. The running transport
(§5) already polls the bus, so on that host do not schedule this command as well: the two do
not share a turn slot (telegram-channel.md, "Running topics in parallel"). Schedule it (cron,
every 10-30 min) on a host that runs no transport. It needs **no Telegram token** and exits
quietly — the
correct shape for a watchdog. On every completed cycle it stamps
`.agents/state/<alias>.heartbeat.json` — `at`/mtime = liveness; `delivered`/`deferred`/
`machine_events`/`escalations` = diagnostics — and logs one `gitbus: poll cycle complete (...)`
line. Pair it with a local watchdog: alert when `now - at > 3 x interval`.

## 7. Upgrades, when the time comes

```bash
scripts/upgrade_transport.sh verify <ref>                    # prove the suite on THIS host
scripts/upgrade_transport.sh switch <ref> --service a2a-transport
scripts/upgrade_transport.sh rollback --service a2a-transport
```

The script defaults to the checkout at `~/projects/swarmly` and the deploy root
`~/transport`; set `UPGRADE_REPO` and `UPGRADE_DEPLOY_ROOT` if yours differ. Keep the exports
from §5 (`UPGRADE_PYTHON` and the three `UPGRADE_SERVICE_*`) in the shell or your profile for a
user unit. Without the service hooks the script calls system-scope `systemctl` and every switch
rolls back; without `UPGRADE_PYTHON` the suite gate runs with a `python3` that lacks the
dependencies, and nothing is switched.

`switch` re-verifies first and auto-reverts when the restart or the online notice fails; upgrade
to a CI-green revision, and **canary one host first**. Full runbook:
`docs/telegram/UPGRADE.md`.

## 8. Where next

- `docs/telegram/PROTOCOL.md` — the channel protocol (rules 0-9, envelope, closure rules)
- `docs/ENVELOPES.md` — envelope cheat-sheet (chat envelope + bus message file)
- `docs/ADOPT.md` — adopting Swarmly in your own project
- `examples/minimal-bus-demo.sh` — runnable two-agent bus demo, no tokens needed

## Windows notes (Task Scheduler)

**Windows hosts** -> `docs/telegram/SETUP-CODEX.md` §6: Task Scheduler entries and the
canonical heartbeat/watchdog contract. What is already true today:

- **Antigravity on Windows**: `docs/telegram/SETUP-ANTIGRAVITY.md` §6a, and there is a single
  command for the whole local stack — `.\scripts\start_antigravity.ps1` (add `-Status`, `-Stop`
  or `-DryRun`). Re-running it is safe: tasks that already match are left alone.
- the tokenless poller runs under **Task Scheduler** with a wrapper that pins the interpreter
  and PATH;
- `bus-poll --once` exits 0 after every completed cycle and prints a one-line summary (plus
  one INFO log line) — use the **exit code** as the wrapper's signal, not stdout;
- the local heartbeat file (section 6) is the thing your watchdog reads; the log line is the
  fallback for journald-style inspection.
