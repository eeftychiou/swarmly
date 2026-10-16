# Swarmly

[![tests](https://github.com/eeftychiou/swarmly/actions/workflows/tests.yml/badge.svg)](https://github.com/eeftychiou/swarmly/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)

> **Not the Agent2Agent protocol.** Swarmly is an independent project. It does not
> implement, and is not affiliated with, the Agent2Agent (A2A) Protocol that started at Google and
> is now hosted by the Linux Foundation. Its message formats are its own; their identifiers
> (`a2a/v1`, `a2a/2`) and the `A2A_*` variables only abbreviate "agent-to-agent". It coordinates
> coding-agent command-line tools through a git repository, a synced folder and Telegram, rather
> than through A2A's HTTP/JSON-RPC interface and Agent Cards. To interoperate with A2A-compliant
> agents, use the official specification and SDKs at [a2a-protocol.org](https://a2a-protocol.org).

**Coordinate coding-agent CLIs from different vendors, on different machines, on one project, with
a human in charge.**

Swarmly connects agent runtimes such as the **Antigravity CLI**, **OpenAI Codex CLI**,
**Claude Code** and **DeepSeek (via the Reasonix CLI)** through two planes that need no public IP and no open ports:

- **The durable plane** holds the record. Signed message envelopes are stored in one of three
  places: a git repository (**gitbus**, `.agents/inbox/`), a **synced folder** (Google Drive for
  desktop, Dropbox, OneDrive, Syncthing, a network share), or **Google Drive** through its API.
  The record survives restarts, works offline and catches up on its own.
- **Telegram** is the instant plane. Every agent has a bot in one group, and the human can watch and
  override everything there. Messages are short and point at the durable record.

An optional TCP gateway (the "fast plane") is included for agents on the same network.

> **Swarm kernel (a2a/2, preview):** signed per-agent lanes off `main`, one validated write
> path (`swarmly` CLI and MCP server) and a safety guard. See [docs/SWARM.md](https://github.com/eeftychiou/swarmly/blob/main/docs/SWARM.md).

> **Topic-to-project routing (multi-project):** Telegram forum topics bound to project repositories, with GitHub issues and pull requests as the default durable plane, git worktree isolation, and a missed-ping notification backstop. See [docs/PROJECTS.md](https://github.com/eeftychiou/swarmly/blob/main/docs/PROJECTS.md).

> **Roles:** give each agent a role (coordinator, developer, reviewer, QA tester, researcher, or
> your own) and its turns follow that role's workflow. See [docs/ROLES.md](https://github.com/eeftychiou/swarmly/blob/main/docs/ROLES.md).

> **New here?** [Getting started](https://github.com/eeftychiou/swarmly/blob/main/docs/GETTING-STARTED.md) brings a node up in about fifteen
> minutes. Then see the [envelope cheat-sheet](https://github.com/eeftychiou/swarmly/blob/main/docs/ENVELOPES.md),
> [adopting Swarmly in your project](https://github.com/eeftychiou/swarmly/blob/main/docs/ADOPT.md) and the [runnable demo](https://github.com/eeftychiou/swarmly/blob/main/examples/README.md).

---

## Supported agents

| Runtime | `adapter.type` | How it is driven | Setup |
|---|---|---|---|
| Antigravity CLI | `antigravity`, `antigravity_thread` | `agy -p`, or queued into an open Antigravity conversation | [SETUP-ANTIGRAVITY](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/SETUP-ANTIGRAVITY.md) |
| OpenAI Codex CLI | `codex`, `codex_thread` | `codex exec`, or queued into an open Codex task | [SETUP-CODEX](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/SETUP-CODEX.md) |
| Claude Code | `claude`, `claude_thread` | `claude -p`, or one persistent session per topic (recommended) | [SETUP-CLAUDE](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/SETUP-CLAUDE.md) |
| Reasonix (DeepSeek) | `reasonix` | `reasonix run -p` | [SETUP-REASONIX](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/SETUP-REASONIX.md) |
| Anything with a CLI | `custom` | any command line with a `{prompt}` placeholder | [telegram-channel.md §4](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram-channel.md) |
| Hermes Agent (untested) | `hermes` | `hermes -z` | [SETUP-HERMES](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/SETUP-HERMES.md) |

The first four runtimes are the ones Swarmly has been run with on real projects. The `hermes`
adapter is written against Hermes Agent's documented one-shot mode but has never been run
against a real Hermes install; treat it as a starting point and report what you find.

## Coordination planes

| Plane | Transport | Role | Status |
|---|---|---|---|
| Durable | GitHub issues & PRs (`github:` backstop) | default durable coordination plane across projects | stable |
| Durable | gitbus (`.agents/inbox/` of the repository named in `bus.repo`) | the record: tasks, handoffs, reviews, blockers | stable |
| Durable | synced folder (`bus.transport: folder`) | the same record without git | new |
| Durable | Google Drive API (`bus.transport: gdrive`) | the same record for headless nodes | new |
| Instant | Telegram group, one bot per agent | low-latency pings, ACKs, human override | stable |
| Fast | TCP gateway with challenge-response auth (`swarmly/server.py`) | same-network broadcast, archived to gitbus | experimental |

Each project picks a durable coordination plane — GitHub issues and PRs (see [`docs/PROJECTS.md`](https://github.com/eeftychiou/swarmly/blob/main/docs/PROJECTS.md)) or an envelope bus transport. Telegram runs beside it:
`swarmly-telegram post` writes the envelope durably and then pings the peer on Telegram with a
pointer to it. [`docs/TRANSPORTS.md`](https://github.com/eeftychiou/swarmly/blob/main/docs/TRANSPORTS.md) covers the setup for each transport
and how to choose between them. The channel rules (delivery semantics, the escalation ladder)
are in [`docs/telegram/PROTOCOL.md`](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/PROTOCOL.md); the bus poller's gates and
dedupe keys are in [`docs/telegram-channel.md` §6](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram-channel.md#6-durable-bus-poller-bus-poll---once--scheduling-per-host-gates).

## Install

```bash
pipx install swarmly          # or: uv tool install swarmly, or pip install swarmly in a venv
swarmly-telegram --help
```

Python 3.10 or newer and git are the only prerequisites. One install gives you the three
commands: `swarmly-telegram` (the Telegram transport and bus poller), `swarmly-keygen` (signing
keys) and `swarmly` (the swarm kernel). Google Drive as a bus needs `pipx install
"swarmly[gdrive]"`. To work on Swarmly itself, install from a clone instead:
`pip install -e ".[test]"`.

> [!WARNING]
> **Keep the Telegram group and the gitbus repository private.** Every agent runs with full
> permissions by default (`adapter.permissions: full`): no approvals and no sandbox. Anyone
> who can post in the group or push to the bus repository can make an agent run any command
> as the user it runs as, with that user's files, GitHub token and API keys. Use a private
> Telegram group with only your own bots and people you trust, and a private GitHub
> repository for the gitbus. Run the agents as a dedicated OS user, or in a VM or container,
> and set `adapter.permissions: standard` or `strict` on any Claude, Codex or Reasonix bot
> that does not need more (the default Antigravity command and the untested Hermes adapter have no such levels).

## Quickstart

**1. Try the bus with no tokens at all.** From a clone, two agents exchange a signed message
through a throwaway git repository, or through a plain shared folder:

```bash
git clone https://github.com/eeftychiou/swarmly.git && cd swarmly
examples/minimal-bus-demo.sh        # gitbus
examples/folder-bus-demo.sh         # folder bus (what Drive or Dropbox would sync)
```

**2. Set up an agent.** Each agent gets its own Telegram bot (from @BotFather) and its own
`config.yaml`. Run this in the project repository your agents will share; its
`.agents/inbox/` becomes the durable bus:

```bash
cd ~/projects/your-project
swarmly-telegram init --alias DeepSeek --adapter reasonix --bus-repo .
echo 'A2A_TELEGRAM_BOT_TOKEN_DEEPSEEK=123456:ABC...' >> ~/.a2a-telegram.env
swarmly-keygen --agent deepseek --env-file ~/.a2a-telegram.env --registry .agents/bus/config/a2a_config.json
git -C .agents/bus add .gitignore config/a2a_config.json
git -C .agents/bus commit -m "Register deepseek" && git -C .agents/bus push -u origin agents-bus
swarmly-telegram whoami     # proves the bot token
swarmly-telegram discover   # lists the chats the bot sees: your group's id is home_channel_id
# edit config.yaml: agent.name (the bot's username), home_channel_id, admin_user_id, peers
```

`init` writes `config.yaml`, an empty signing-key registry and the `.gitignore` lines that keep
the config and the transport's state out of git. The agent commits its bus messages in this
checkout, so the host needs a git identity (`git config user.name` and `user.email`). `--adapter` is `reasonix`, `codex`, `claude`,
`antigravity` (or a `*_thread` variant), `hermes` (untested), `echo` to test the channel without an agent CLI, or
`custom`. `swarmly-keygen` writes the private key to the env file (owner-only) and only the
public key to the registry; every peer verifies against that committed registry.

**3. Check it, then run it.**

```bash
swarmly-telegram check      # validates the config and says what is still missing
swarmly-telegram run        # serve the Telegram channel and poll the bus
swarmly-telegram post --to Codex "PR #7 is ready"   # durable write + Telegram ping
```

Every command reads `./config.yaml` unless you pass `--config path/to/config.yaml`. [Getting started](https://github.com/eeftychiou/swarmly/blob/main/docs/GETTING-STARTED.md) walks through the Telegram side (the group,
bot-to-bot mode, admin rights) and running the transport as a service.

A minimal agent config:

```yaml
agent:
  name: DeepSeekBot
  alias: DeepSeek
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_DEEPSEEK
  home_channel_id: -1001234567890
  admin_user_id: 1234567890        # your numeric Telegram user id (from @userinfobot)
adapter:
  type: reasonix
  cwd: /home/user/your-project

# Optional: route Telegram forum topics to separate project repos & checkouts
# projects:
#   webapp:
#     repo: example-org/webapp
#     cwd: /home/user/webapp
#     topics: [12, 15]
#     worktrees: true
# github:
#   login: deepseek-bot
#   poll_minutes: 5
```

Telegram needs **Bot-to-Bot Communication Mode** enabled for each bot in @BotFather, and each bot
promoted to group admin. [`docs/telegram/SETUP-ADMIN.md`](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/SETUP-ADMIN.md) walks
through it.

## Messages

Every message is an envelope that conforms to
[`schemas/a2a_envelope.schema.json`](https://github.com/eeftychiou/swarmly/blob/main/schemas/a2a_envelope.schema.json), stored as YAML
front matter followed by a readable body:

```yaml
---
protocol: a2a/v1.0
message_id: msg-20260913T124500Z-codex
correlation_id: pr-47
timestamp_utc: '2026-09-13T12:45:00.123456+00:00'
sender:
  agent_id: codex
  model_family: openai
  framework: codex
recipient:
  agent_id: antigravity
intent: PR_REVIEW_VERDICT
priority: HIGH
signature: 'hmac-sha256:7f8a9b2c3d4e5f...'
payload_json: >-
  {"pr_number": 47, "verdict": "CHANGES_REQUESTED"}
---

# Review: PR #47
Two findings, both in the retry path.
```

Intents include `TASK_PROPOSE`, `TASK_CLAIM`, `TASK_HANDOFF`, `PR_REVIEW_REQUEST`,
`PR_REVIEW_VERDICT`, `STATUS_UPDATE`, `QUERY_REQUEST` and `ESCALATE_BLOCKER`. The full list is in
[`swarmly/models.py`](https://github.com/eeftychiou/swarmly/blob/main/swarmly/models.py) and [`docs/ENVELOPES.md`](https://github.com/eeftychiou/swarmly/blob/main/docs/ENVELOPES.md).

Send and read from the command line:

```bash
# codex's key, from `swarmly-keygen --agent deepseek --env-file .env --registry config/a2a_config.json`
set -a; . ./.env; set +a          # sending refuses without A2A_KEY_CODEX
python scripts/a2a_bus.py send --sender codex --recipient antigravity \
  --intent PR_REVIEW_REQUEST --priority HIGH --payload '{"pr_number": 47}'
python scripts/a2a_bus.py inbox      # lists messages and verifies signatures
```

## Security model

**Read this before you run an agent.** Every agent this project drives can run code on its host,
and messages from the bus and from the Telegram group become that agent's prompts. Treat anyone
who can write to your bus or post in your group as someone who can run commands as the agent's
OS user. These limits remain:

- **The default permission level is full: no checks and no sandbox.** Every runtime starts
  at `adapter.permissions: full`, so an agent can pull, commit, push and run `gh` out of the
  box. Keep the group and the bus repository private (the warning above). `standard` still
  lets the agent run interpreters, package managers and git, which amounts to arbitrary code
  execution; `strict` only edits files. The default Antigravity command passes
  `--dangerously-skip-permissions` (an explicit `adapter.command` without it keeps approvals).
- **Agents run as the transport's OS user.** Runtimes do not inherit variables whose names look
  like secrets (tokens, `A2A_KEY_*`, passwords), but that is a denylist: the CLI's own API key
  passes, and so does a secret with an unusual name. As the same user an agent can also read
  the transport's `.env` files and, on Linux, `/proc/<pid>/environ`. The real boundary is the
  OS: run each agent as a separate low-privilege user, or in a container or VM, with only that
  agent's secrets.
- **With HMAC, every receiver can sign as every sender.** Each node holds each HMAC sender's
  secret to verify it. Prefer Ed25519 for bus senders: the registry then holds only public
  keys.
- **Write access to the bus repository is the trust anchor.** Each node trusts the keys and
  revocations in its bus checkout's `config/a2a_config.json`, so anyone who can push there can
  add a trusted sender, replace a key or drop a revocation. Restrict who can write to the bus
  repository and protect that file (branch protection, CODEOWNERS or required review).
- **Revocation needs every node's copy of the registry.** `revocation_list` is read from
  `config/a2a_config.json` in each node's bus checkout, so a revocation takes effect on a node
  only once that checkout carries it.
- **The fast-plane gateway (`swarmly/server.py`) is experimental:** raw TCP with newline-delimited
  JSON, plaintext, and the client does not authenticate the server. It binds `127.0.0.1` by
  default; reach it from another machine only through an SSH tunnel or a TLS proxy.
- **The swarm safety guard is advisory** for every CLI except Claude Code, which has an
  enforcing hook.

What does hold today:

- **No bus by default.** Without `bus.repo` nothing is polled, and the first cycle on a bus
  records what is already there without delivering any of it.
- **Bus messages are verified on delivery.** A message is delivered only when its signature
  verifies against a registered, non-revoked sender. The signature covers the whole envelope
  (payload, sender, recipient, message id, intent, timestamps), so a copied message cannot be
  re-addressed or given a new id, and it is accepted only within 7 days of its signed
  timestamp. Old payload-only signatures are refused unless the registry sets
  `"accept_v1_signatures": true` for a rollout; `bus.allow_unsigned: true` turns the checks off
  for a local demo bus. See [docs/ENVELOPES.md](https://github.com/eeftychiou/swarmly/blob/main/docs/ENVELOPES.md).
- **No keys ship, and signing fails closed.** The registry (`config/a2a_config.json`) starts
  with no agents. `swarmly-keygen --agent <id> --env-file <file> --registry <registry>` creates an
  Ed25519 key per agent: the private key stays in that agent's env file, and only the public key
  is registered, with `key_type: ed25519`, so only `ed25519:` signatures verify against it.
  Sending refuses without `A2A_KEY_<SENDER>`. (An HMAC agent is registered by the *name* of an
  environment variable, and has no key while that variable is unset.)
- **Telegram authorization.** People are authorised by numeric user id only (`admin_user_id`,
  `allowed_users`); strangers are ignored by default; forwards and anonymous-admin posts are not
  treated as bots.
- **No shell.** Agent CLIs are started from argument lists, never through a shell. On Windows an
  npm `.cmd` shim is replaced by the script it wraps and any other `.cmd`/`.bat` is refused, so
  prompt text never reaches `cmd.exe`. Timeouts kill the whole process tree.
- **The gateway checks what it archives.** Message ids and header fields are validated, so a
  client cannot write outside the inbox; idle connections are closed and their number capped.

[SECURITY.md](https://github.com/eeftychiou/swarmly/blob/main/SECURITY.md) lists the same limits and says how to report a vulnerability.


## Python SDK

```python
import asyncio
from swarmly import A2AClient, MessageIntent, MessagePriority

async def main():
    client = A2AClient(agent_id="codex", private_key="...", host="127.0.0.1", port=8765)
    await client.connect()                       # answers the challenge automatically
    client.on_message(lambda msg: print(msg.sender.agent_id, msg.payload))
    await client.send_message(
        intent=MessageIntent.PR_REVIEW_VERDICT,
        payload={"pr_number": 47, "verdict": "APPROVED"},
        recipient_id="antigravity",
        priority=MessagePriority.HIGH,
    )

asyncio.run(main())
```

## Documentation

| Topic | Where |
|---|---|
| First node in fifteen minutes | [docs/GETTING-STARTED.md](https://github.com/eeftychiou/swarmly/blob/main/docs/GETTING-STARTED.md) |
| Durable transports (git, folder, Google Drive) and Telegram | [docs/TRANSPORTS.md](https://github.com/eeftychiou/swarmly/blob/main/docs/TRANSPORTS.md) |
| Using Swarmly in another project | [docs/ADOPT.md](https://github.com/eeftychiou/swarmly/blob/main/docs/ADOPT.md) |
| Telegram channel, adapters, rules | [docs/telegram-channel.md](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram-channel.md) |
| Channel protocol v1.1 | [docs/telegram/PROTOCOL.md](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/PROTOCOL.md) |
| Safe upgrade and rollback | [docs/telegram/UPGRADE.md](https://github.com/eeftychiou/swarmly/blob/main/docs/telegram/UPGRADE.md) |
| Every document, by audience | [docs/README.md](https://github.com/eeftychiou/swarmly/blob/main/docs/README.md) |
| How this repository is maintained | [AGENTS.md](https://github.com/eeftychiou/swarmly/blob/main/AGENTS.md) |

## Status and roadmap

This repository is developed by the agents it connects. Claude Code, Antigravity and Codex
coordinate here over their own gitbus and Telegram, under a human maintainer.

Planned:

- more cloud transports beside git, folders and Google Drive (S3-compatible storage, OneDrive's API);
- carrying the GitHub event watcher used in production gitbus deployments back into this library.

## Running the tests

```bash
pip install -e ".[test]"
python -m unittest discover tests
```

## How this was built

I designed Swarmly and wrote its protocol and the process rules in
[AGENTS.md](https://github.com/eeftychiou/swarmly/blob/main/AGENTS.md): every pull request gets an independent review with a merge or fix-first
verdict before whoever acts as maintainer, usually an agent, merges it; I make the calls the
agents escalate to me.
Most of the implementation was written by the coding agents it connects. This public repository
starts from a single commit; the development history stays in a private repository, because it
carries the identifiers of the deployment it was built on. Of its roughly 320 non-merge commits
at the time of writing, about 280 were made by Claude Code, OpenAI Codex CLI and Antigravity
agents under per-agent git identities (for example `Codex <codex@a2a.local>`). They
claim tasks, review each other's pull requests and report status over this project's own bus and
Telegram channel. In the first week, before per-agent identities were introduced, some agent
work was committed under my name.

— Eftychios Eftychiou

## Acknowledgements

- [ductor](https://github.com/PleasePrompto/ductor) (MIT) by PleasePrompto: the Telegram
  transport's forum topics, typing indicator, busy queue, `/stop`, incoming files and long-reply
  splitting were reimplemented from its Telegram transport. See [THIRD_PARTY_NOTICES.md](https://github.com/eeftychiou/swarmly/blob/main/THIRD_PARTY_NOTICES.md).
- [superpowers](https://github.com/obra/superpowers) (MIT) by Jesse Vincent: the format of the
  repository's agent skill (`.agents/skills/vacuous-green/`) and its "verify red" step follow
  superpowers' skills.
- [aiogram](https://github.com/aiogram/aiogram) (MIT), the Telegram Bot API framework the
  transport is built on.

## Contributing and license

Contributions are welcome. See [CONTRIBUTING.md](https://github.com/eeftychiou/swarmly/blob/main/CONTRIBUTING.md). Released under the
[MIT License](https://github.com/eeftychiou/swarmly/blob/main/LICENSE).
