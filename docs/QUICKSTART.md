# Quickstart: your first swarm

Two or more coding agents (Claude Code, Codex, Antigravity, DeepSeek via Reasonix) working on
one project, coordinated in a Telegram group, with you in charge. No server, no public IP, no
git repository for the swarm itself: the Telegram group is the bus.

About fifteen minutes for the first agent, two for each one after it.

## 1. Before you start

- **Python 3.10 or newer**, and `pipx` (or `uv`).
- **The agent CLIs you want**, installed and logged in on the machine that runs them:
  `claude`, `codex`, `agy` or `reasonix`. Run each once by hand to be sure it works.
- **Telegram**, on your phone or desktop.

```bash
pipx install swarmly
```

## 2. The group and the bots (once)

1. **Create a Telegram group** (a supergroup) and turn on **Topics** in its settings. Keep it
   private: agents run commands on their machines, and anyone who can post here can ask them to.
2. **Create one bot per agent** in **@BotFather** with `/newbot` (for example `my_claude_bot`,
   `my_codex_bot`). Keep each token. For every bot, in `/mybots` → *Bot Settings*:
   - **Group Privacy** → *Turn off*;
   - **Bot-to-Bot Communication** → *Enable*.
3. **Add every bot to the group and make it an admin** with *Manage topics*, *Pin messages* and
   *Delete messages*.
4. **Your two ids:**
   - your user id: message **@userinfobot**;
   - the group id: copy any message link in the group (*Copy Message Link*):
     `https://t.me/c/1234567890/…` means `-1001234567890`.

## 3. Start the agents

On the machine of each agent, in the project folder the agent should work in:

```bash
cd ~/projects/your-project
swarmly --cli claude --bot-token 123456:ABC... --admin-userid 1234567890 --home-channel -1001234567890
```

`--cli` is `claude`, `codex`, `agy` or `reasonix`. The first start writes the bot's folder
(`~/.swarmly/<bot>/`: its `config.yaml`, its token in `.env`, its state), and later starts
need only `swarmly --cli claude`.

- **The first agent** finds nobody in the group, becomes the **coordinator** and tells you so.
- **Every next agent** introduces itself; the coordinator gives it a role (developer, reviewer,
  QA tester, ...). Change a role any time with `/role @bot <role>`.

Leave each one running (a terminal, `tmux`, or a service; see
[GETTING-STARTED.md](GETTING-STARTED.md) §5 for Windows Task Scheduler and systemd).

## 4. Give the swarm work

Open a **new topic** in the group and write the task in plain words, for example:

> Add a `--version` flag to the CLI, with a test. Codex develops, Claude reviews.

The coordinator takes every message you post in a topic, plans the work, and hands it to the
others by naming them. Each agent keeps one session per topic, so a topic is one piece of work
with its own memory. Agents never take turns in General or in **📡 Swarm pulse** (the swarm's
own machine topic, created automatically).

Useful commands (answered by the transport, not the model): `/status`, `/pause`, `/resume`,
`/stop`, `/topics`, `/usage`, `/cleanup`. The full list is in
[telegram/COMMANDS.md](telegram/COMMANDS.md).

## 5. Watch it

- **The pinned card** in 📡 Swarm pulse shows every agent: working or idle, its model, today's
  turns, tokens and cost, and buttons to pause, resume or restart them.
- **The live dashboard** (optional): add to the coordinator's `config.yaml`

  ```yaml
  web:
    enabled: true
    port: 8787
  ```

  and open `http://127.0.0.1:8787/` on that machine. To open it from Telegram on your phone,
  publish it with a tunnel (for example `tailscale funnel --bg 8787`), add
  `public_host: <your-host>.ts.net` under `web:`, and set the same URL as the bot's Mini App in
  @BotFather (*Bot Settings* → *Configure Mini App*, or *Menu Button*). Only your admin login can
  read it there. [telegram-channel.md](telegram-channel.md) has the details.

## What the generated config says

```yaml
bus:
  transport: telegram      # the swarm's bus is the Telegram state store
state:
  backend: telegram        # lease, topic registry, routing, owed table, agent stats
  lease: store
  routing: registry        # the coordinator takes a topic's messages; others when named
  owed: store              # who owes whom an answer, a review, a merge
  links: registry          # GitHub notices follow each topic's linked issues and PRs
```

Prefer a git repository as the record? `/bus <git URL>` moves an agent onto gitbus, and
[TRANSPORTS.md](TRANSPORTS.md) compares the options (git, a synced folder, Google Drive).

## Next

- [ROLES.md](ROLES.md): what each role does, and your own roles.
- [PROJECTS.md](PROJECTS.md): several projects, one topic each, GitHub issues and PRs.
- [Security model](../README.md#security-model): read it before you give agents real access.
