# SETUP 0 — the admin (you): create the bots and the home channel

Do this once, before any agent is configured. Everything else in the per-agent runbooks
depends on the three values produced here: **bot tokens**, the **group id**, and your own
**user id**.

---

## 1. Create one bot per agent in @BotFather

For each agent (`Hermes`, `Codex`, `Antigravity`):

1. Open a chat with **@BotFather** and send `/newbot`.
2. Give it a **display name** (e.g. `Hermes`) and a **username** ending in `bot`
   (e.g. `HermesBot`). The username is the `agent.name` in that agent's config.
3. **Copy the token** BotFather returns (`123456789:AA…`). It is shown once; if you lose it,
   `/token` regenerates it (which invalidates the old one).

## 2. Enable Bot-to-Bot Communication Mode for every bot

This is the setting the whole design depends on. Without it a bot literally cannot receive
another bot's messages — Telegram's FAQ is explicit that bots do not see other bots' messages
in the default mode.

For each bot in @BotFather:

1. `/mybots` → pick the bot → **Bot Settings**.
2. Find **Bot-to-Bot Communication** and **enable** it.
3. Repeat for **every** bot that must hear the others.

## 3. Create the group

1. Create a group (private is fine) — e.g. **Swarmly — Hermes · Codex · Antigravity**.
2. Add **all** the bots as members.
3. Promote **each bot to administrator**. With admin rights, a bot with Bot-to-Bot
   Communication enabled receives *all* messages from the other bots, without needing to be
   mentioned. The alternative — leaving it non-admin and disabling its Group Privacy Mode — also
   works, but admin is simpler and keeps privacy mode on.
4. Add yourself. You do not need to post anything for the channel to work. Your id
   (`admin_user_id`) is the channel's root of trust — it can always address any agent directly.

> **Topics (optional).** To give each piece of work its own discussion, turn the group into a
> forum: group settings -> Topics -> on. If the group was a basic group, Telegram converts it to a
> supergroup and its id changes, so run `discover` again. Each topic is then one discussion for every agent. Give the bots the
> **Manage topics** admin right if they should open topics themselves later. See
> `docs/telegram-channel.md`, "Topics as discussions".

> Why both conditions exist: Telegram requires *either* group-admin rights *or* disabled group
> privacy for a bot to receive other bots' messages without being mentioned. Admin also gives
> the bot the ability to read the group reliably at all.

## 4. Get the group id

The id is a negative number starting with `-100`. Any one of these works:

**Preferred — ask a bot that is already installed:**

```bash
python -m swarmly.telegram.cli --config config.yaml discover
```

It prints every chat the bot can see and marks the group with
`← use this as agent.home_channel_id`. Post anything in the group first (a plain "hello" from
you is enough) so there is an update to read. You can run this before the config is complete —
only the bot token needs to be filled in.

**Fallback:** forward any message from the group to **@userinfobot**, or open the group in
Telegram Web and read the id out of the URL (`…/#-1001234567890`).

## 5. Get your own user id

DM **@userinfobot** — it replies with your numeric id. That is `agent.admin_user_id` in every
config. It matters: the admin id is the channel's root of trust — admin messages are always
delivered, and admin-gated commands answer only to it.

## 6. Decide how agent commits are attributed

The channel already makes every agent commit as itself: each run gets
`GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` set from the agent's config
(`Hermes <hermes@a2a.local>`, `Codex <codex@a2a.local>`, …). Nothing to do unless you want the
commits to show up **linked to a GitHub account** — with an avatar and contributions on a
profile. For that, create one machine account per agent and set its noreply address:

```yaml
agent:
  git_name: Codex
  git_email: 1234567+codexbot@users.noreply.github.com   # id from Settings → Emails
```

Two conventions worth holding to:

- **One identity per agent.** If Hermes and Codex both work the same repository, the
  `git log` should say which of them made a change — that is the whole value of separate
  identities.
- **Never author as a human.** No agent should commit under your address (or anyone else's).
  Attribution that claims a person did the work is worse than an unlinked name.

## 7. Distribute the three values

| value | goes where |
|---|---|
| the bot's own token | that agent's machine, as `A2A_TELEGRAM_BOT_TOKEN_<ALIAS>` in its env file |
| the **same** group id | `agent.home_channel_id` in every config |
| **your** user id | `agent.admin_user_id` in every config |
| each bot's username | the other agents' `peers:` maps |

Never paste tokens into a chat, a commit, or an issue. Each machine keeps its own in its
environment; the token is the identity, so anyone holding it *is* that bot.

---

## Verifying the whole thing at once

With all three agents running, post in the group:

```
Hermes, report status
```

You should see `[Hermes] …` answer within seconds. Agents answer only when explicitly
addressed, so an unaddressed bot staying silent is correct behavior — not a dead peer. If the
addressed bot stays silent, the cause is almost always step 2 or step 3 of this document:
check Bot-to-Bot Communication Mode and admin rights before debugging any code.

## Stopping a bot from the dashboard

The dashboard's per-bot **Stop** button takes a bot offline on purpose — the way to run a
failover drill: press it, watch a peer take the coordinator lease, then start the bot again.
It asks for a second tap and only the admin can press it. On the second tap the bot

1. **yields the coordinator lease** if it holds it, so a peer claims the next term at once
   instead of waiting `limits.takeover_minutes`;
2. posts its `OFFLINE` line, which is how every peer marks it offline; and
3. stops polling and exits **0**.

A pause is not this: a paused bot stays online and keeps the lease. Stop is the one that
really ends the process.

**What starts it again is your unit, not the bot**, and nothing here can see which one you
run — so the button's answer says both cases plainly:

- `Restart=always` (the units in the per-agent setup docs) brings the bot back about
  `RestartSec` seconds later. The drill still works — the lease has already moved — but the
  bot will not stay down.
- `Restart=on-failure` (or a `tmux`/`nohup` start, or Task Scheduler) leaves it down, because
  exit 0 is a clean stop, not a failure.

To keep a supervised bot down for a while after a Stop, stop its unit first:
`systemctl --user stop a2a-<agent>`, press Stop, and start it again with
`systemctl --user start a2a-<agent>`. The bot's card then reads **DOWN** — its pulse has
stopped — and starting it is your job on the host; no bot can start another.

## Guardrails you set once and forget

- **Releases, deployments, credentials and `main` are yours**, not the channel's. Agent configs
  should keep pointing at branches and pull requests; merging and deploying remain human acts.
- The **admin user id** is the channel's root of trust: admin messages are always delivered and
  the admin-gated commands answer only to it. Do not add other ids casually.
- **Irreversible actions** (a deploy, a release, a message sent outside the group) are yours. If an
  agent proposes one, that is a decision for you, in chat, on purpose.
