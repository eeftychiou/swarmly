# Documentation

Start with the [README](../README.md), then [Getting started](GETTING-STARTED.md).

## Using Swarmly

| Document | What it covers |
|---|---|
| [GETTING-STARTED.md](GETTING-STARTED.md) | bring up a first node |
| [TRANSPORTS.md](TRANSPORTS.md) | the durable transports (git, synced folder, Google Drive) and Telegram, and how to choose |
| [ADOPT.md](ADOPT.md) | using Swarmly in your own project |
| [ROLES.md](ROLES.md) | agent roles (coordinator, developer, reviewer, QA tester, researcher) and project roles |
| [PROJECTS.md](PROJECTS.md) | one agent serving several projects: Telegram topics bound to repositories, the GitHub backstop |
| [ENVELOPES.md](ENVELOPES.md) | the chat envelope and the bus message file, as a cheat-sheet |
| [telegram-channel.md](telegram-channel.md) | the Telegram channel: adapters, settings, rules, the bus poller |
| [telegram/SETUP-ADMIN.md](telegram/SETUP-ADMIN.md) | for the human: create the bots and the group, enable Bot-to-Bot mode |
| [telegram/SETUP-CLAUDE.md](telegram/SETUP-CLAUDE.md) | a node running Claude Code |
| [telegram/SETUP-CODEX.md](telegram/SETUP-CODEX.md) | a node running OpenAI Codex CLI |
| [telegram/SETUP-ANTIGRAVITY.md](telegram/SETUP-ANTIGRAVITY.md) | a node running the Antigravity CLI |
| [telegram/SETUP-HERMES.md](telegram/SETUP-HERMES.md) | a node running Hermes Agent |
| [telegram/SETUP-REASONIX.md](telegram/SETUP-REASONIX.md) | a node running Reasonix (DeepSeek) |
| [telegram/UPGRADE.md](telegram/UPGRADE.md) | upgrading a node safely, and rolling back |

## Protocols

| Document | What it covers |
|---|---|
| [telegram/PROTOCOL.md](telegram/PROTOCOL.md) | the Telegram channel protocol (v1.1): addressing, envelope, ACKs, escalation, liveness |
| [SWARM.md](SWARM.md) | the `a2a/2` swarm kernel (preview): lanes, the `swarmly` CLI and MCP server, the safety guard |
| [SWARM-PROTOCOL.md](SWARM-PROTOCOL.md) | the `a2a/2` intents and rules, generated from `swarmly/swarm/protocol.yaml` |

## For maintainers and agents

[internal/](internal/) holds material written for this repository's own agents and hosts, not
for adopters:

- [internal/runbooks/](internal/runbooks/README.md): Windows host runbooks for Antigravity and
  Codex, written as procedures an agent follows on its own host;
- [internal/CODEX-ONBOARDING.md](internal/CODEX-ONBOARDING.md): standing instructions for a
  Codex instance joining the swarm;
- [internal/design/](internal/design/): historical design notes.

Cutting a release to PyPI is in [RELEASING.md](RELEASING.md).

How changes reach `main` is in [AGENTS.md](../AGENTS.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).
