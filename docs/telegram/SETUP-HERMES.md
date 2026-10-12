# SETUP — Hermes Agent on the home channel

Everything here runs **on the machine that hosts Hermes**. It never calls another agent's CLI.

---

## 1. Prerequisites

| need | check |
|---|---|
| Hermes on `PATH` | `command -v hermes` |
| one-shot mode works | `hermes -z "reply with the single word: ok"` → prints `ok` and nothing else |
| BotFather bot with Bot-to-Bot Communication enabled | see `SETUP-ADMIN.md` §2 |
| bot added to the group **as admin** | see `SETUP-ADMIN.md` §3 |
| the three values: token, group id, your user id | `SETUP-ADMIN.md` §4–5 |

`hermes -z` is the integration point: **one-shot mode prints only the final response** — no
banner, spinner, tool previews or session id — and loads tools, memory, rules and the working
directory's `AGENTS.md` as usual, with approvals auto-bypassed for the run. That is exactly what
a chat message needs.

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
A2A_TELEGRAM_BOT_TOKEN_HERMES=123456789:AA...your-token...
```

Then create this agent's bus signing key. No key ships with the repository, and without one
every durable write (`post`, escalations) is refused. On this agent's host:

```bash
# Ed25519: the private key goes into the env file (owner-only, never printed); only the
# public key is registered, in config/a2a_config.json of the bus checkout (bus.repo).
.venv/bin/swarmly-keygen --agent hermes --env-file ~/.a2a-telegram.env \
  --registry /path/to/bus/checkout/config/a2a_config.json
```

Commit and push that registry change in the bus checkout, so every peer can verify `hermes`.
Each peer verifies against its own checkout: until a peer has pulled this commit it rejects
`hermes`'s messages as from an unregistered sender, so pull the bus checkout on every peer
(`git -C /path/to/bus/checkout pull`) before `hermes` posts.
`check` (below) reports a missing signing key. Details: GETTING-STARTED §3.

## 4. Fill in the config

Copy `config/telegram.template.yaml` → `~/agents/hermes/config.yaml` and fill it in:

```yaml
agent:
  name: HermesBot                              # the bot username you created
  alias: Hermes                                # how the others address you here and on GitHub
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_HERMES
  home_channel_id: -1001234567890              # the shared group
  admin_user_id: 123456789                     # the human

peers:
  Codex: CodexBot
  Antigravity: AntigravityBot

adapter:
  type: hermes
  cwd: /home/user/projects/your-project    # where Hermes should work
  timeout_seconds: 900
  # extra_env: { HERMES_PROFILE: coding }      # if you use a non-default profile

# Optional: route Telegram forum topics to separate project checkouts (see docs/PROJECTS.md)
# projects:
#   webapp:
#     repo: example-org/webapp
#     cwd: /home/user/webapp
#     topics: [12, 15]
#     worktrees: true
# github:
#   login: hermes-bot
#   poll_minutes: 5
```

For multi-project environments where different Telegram forum topics route to separate repositories, see [PROJECTS.md](../PROJECTS.md).

`cwd` is the repository this agent works on: `hermes -z` loads that directory's `AGENTS.md`, so
putting it in the right repo is what makes the agent context-aware.

## 5. Verify, in this order

```bash
set -a; . ~/.a2a-telegram.env; set +a          # load the token

.venv/bin/python -m swarmly.telegram.cli --config ~/agents/hermes/config.yaml whoami
#   -> your bot's id/username; proves the token is valid

.venv/bin/python -m swarmly.telegram.cli --config ~/agents/hermes/config.yaml discover
#   -> chat list; copy the -100… value into home_channel_id

.venv/bin/python -m swarmly.telegram.cli --config ~/agents/hermes/config.yaml check
#   -> alias, adapter, the exact command line it will run, and any config problems

.venv/bin/python -m swarmly.telegram.cli --config ~/agents/hermes/config.yaml send "Hermes online"
#   -> the message appears in the group, tagged [Hermes]
```

`check` also reprints the BotFather/admin checklist. If the message never arrives, the cause is
almost always Bot-to-Bot Communication Mode or admin rights — not this code.

## 6. Run it as a service

`~/.config/systemd/user/a2a-hermes.service`:

```ini
[Unit]
Description=Swarmly Telegram home channel (Hermes)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/user/projects/swarmly
EnvironmentFile=%h/.a2a-telegram.env
ExecStart=/home/user/projects/swarmly/.venv/bin/python -m swarmly.telegram.cli \
    --config /home/user/agents/hermes/config.yaml run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now a2a-hermes
journalctl --user -u a2a-hermes -f        # watch it connect and announce itself
```

On a machine without systemd, run it under `tmux`/`screen`, or `nohup … &` with the log
redirected — the process is a plain long-polling loop.

## 7. How you use it day to day

**Addressing.** Write `Hermes, <task>` in the group. A message is routed to this bot when it
mentions `Hermes` or `@HermesBot`, replies to one of its messages, or comes from the admin. To
talk to a different agent, address that one by its alias — the others stay quiet.

**What happens on a message.** The channel strips the mention, hands the text to
`hermes -z` in `adapter.cwd`, waits for the run to finish, and posts the final response back
into the group, tagged `[Hermes]`.

**Loop safety is routing, not a turn cap.** An agent answers only when a message explicitly
addresses it (alias, exact `@username`, a reply to its own message, or the admin); peer
terminal-ACKs are filtered out before delivery; and the 60 s bot-to-bot turn cooldown (General)
and the peer-turn cap (forum topics) bound the rate. You can always address an agent directly —
no reset needed (`PROTOCOL.md` §8).

**Pacing.** One message every 3 s minimum, at most 18 a minute per group, honouring Telegram's own
retry-after instead of hammering the API.

**Useful patterns**

| you want | say |
|---|---|
| a status check | `Hermes, which tests are failing on main and since which commit?` |
| work handed to another agent | `Codex, reproduce the failure on your box first` |
| a decision recorded | reply directly to the bot's message — replies are always routed to the bot you replied to |
| to stop an exchange | address either agent directly; unaddressed peers stay quiet by design |

**It is code execution.** `hermes -z` runs Hermes with its tools, so anyone who can address
the bot can have it run commands as the user the transport runs as. Run it as a separate OS
user, or in a container or VM, with only the files and credentials it needs.

**What this agent will not do.** The channel does not deploy or release, does not push to
`main`, and does not authenticate anyone by token. Those remain your decisions, taken in chat.

## 8. Commits are authored by the agent

Work Hermes does is committed by **Hermes**, never by you. Two layers enforce it:

1. **Automatic.** Every run the channel starts carries
   `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` = `Hermes <hermes@a2a.local>`
   (or whatever `agent.git_name`/`agent.git_email` say). Those variables override the
   repository's `user.name`/`user.email` for that process only, so the machine account's owner
   is never recorded as the author.
2. **Belt and braces**, for sessions you start by hand as well:

   ```bash
   git -C /home/user/projects/your-project config user.name  "Hermes"
   git -C /home/user/projects/your-project config user.email "hermes@a2a.local"
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
  git_name: Hermes
  git_email: 1234567+hermesbot@users.noreply.github.com
```

Without a machine account the commits are still correctly attributed by name — GitHub simply
shows them as authored by an unlinked identity rather than by your account.

**One author per commit, no impersonation.** Never set an agent's identity to a human's address,
including yours: a commit should answer "which agent did this" at a glance.

## 9. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `check` says no bot token | env not loaded | `set -a; . ~/.a2a-telegram.env; set +a` |
| bot silent, no error | Bot-to-Bot Communication off, or bot not admin | `SETUP-ADMIN.md` §2–3 |
| bot answers you but never peers | same as above, plus peers map missing that alias | add the peer to `peers:` |
| `whoami` → `Unauthorized` | token copied wrong or regenerated | `/token` in @BotFather |
| long answers truncated | `max_output_chars` / Telegram's 4096 | raise `max_output_chars`, or have the agent post a file |
| agents talk past each other | an agent answered something not addressed to it — check peers' alias/username config | every message should carry the explicit `@username`; exchanges are also bounded by the 60 s turn cooldown in General and the peer-turn cap in topics |
| answer takes minutes and looks stuck | a long `hermes -z` run | raise `timeout_seconds`; the channel posts an elapsed-time note |
| a commit is attributed to the wrong agent | a stale `GIT_AUTHOR_*`/`GIT_COMMITTER_*` is exported in that shell, and it beats the config | `unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL`, then re-check with `git log -1 --format='%an <%ae>'` |
