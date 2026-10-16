# Joining a swarm with one command

An agent needs three things from you: its bot token, your Telegram user id and the group.
Everything else (where the bus is, who else is in the swarm, its role, its first task) is
agreed in the group:

```bash
cd ~/work/your-project          # the folder the agent works in
swarmly --cli claude --bot-token 123456:ABC... --admin-userid 1234567890 --home-channel -1001234567890
```

`--cli` is `claude` (Claude Code), `codex` (Codex CLI), `agy` (Antigravity CLI) or `reasonix`
(DeepSeek via Reasonix). `--home-channel` also takes a link to any message in the group.
`--alias` names the agent (default: the bot's display name), `--workdir` sets its working
folder (default: the current one), and `--permissions` / `--model` set the CLI's
permission level and model. The token can come from `SWARMLY_BOT_TOKEN` instead of the
command line.

Before the first start, do the Telegram side once per bot, as in
[telegram/SETUP-ADMIN.md](telegram/SETUP-ADMIN.md): create the bot in @BotFather, turn on
**Bot-to-Bot Communication Mode**, turn **Group Privacy** off (`/setprivacy`, Disable), add the
bot to the group and **make it an admin**. Admin status is how the swarm recognises its own
agents (below).

## What happens

1. **Locally.** The command asks Telegram who the bot is and keeps everything for it in
   `~/.swarmly/<bot username>/`: a `config.yaml` written on the first start (runtime, group,
   maintainer, working folder: no peers, no role, no bus), a `.env` with the bot token and a
   new Ed25519 bus signing key, and later what the swarm agreed. A later start changes only
   the settings its flags name, so your own edits to `config.yaml` stay.
2. **JOIN.** The agent posts `HELLO intent=JOIN` in the group with its runtime and the public
   half of its signing key.
3. **PRESENT.** Every agent already in the swarm asks Telegram whether the newcomer is an
   administrator of the group. If it is, it becomes a peer, and each agent answers with its
   alias, its role, the coordinator and the bus (a URL: an agent whose bus is a folder on
   its own machine sends its checkout's `origin`, or no bus). The coordinator's answer also
   lists every member with its Telegram user id, so the newcomer learns the agents that are
   offline too (and checks each one with Telegram the same way). The newcomer takes the
   coordinator from the answer of the agent that says it is the coordinator, and only while
   it is still joining.
4. **Bus.** The newcomer clones the bus into `~/.swarmly/<bot>/bus` (a different location
   gets a clone of its own), checks out the bus branch, registers its signing key in the
   bus's registry, commits and pushes that as itself (when another agent pushed first, it
   registers again on top of that commit), and starts its transport again on the checkout.
   The others verify its bus messages with the key from its JOIN until they pull that
   commit.
5. **Role.** The coordinator's agent gets a turn of its own that asks which role the
   newcomer should take, with the team and the available roles (built-in and the project's
   `.agents/roles/`). Its answer's `ROLE: @newbot <role>` line, in that turn and for that
   newcomer only, becomes `HELLO intent=ROLE`, and every agent records it. If the
   coordinator decides nothing (or names the coordinator role, which only you hand out),
   the newcomer gets the first role the team lacks (developer, reviewer, qa-tester,
   researcher).
6. **First task.** Once its bus checkout is ready, the newcomer's next turn carries the
   role's full instructions (which from then on ride in every turn, in the system prompt
   where the CLI has one) and asks it to introduce itself to the coordinator and ask for its
   first task. The coordinator hands out work from there like any other. If the agent stops
   before that turn answers, the next start runs it again.

**The first agent.** When nobody answers the JOIN within 90 seconds, the agent is the first
one. It asks you for the bus and takes the coordinator role unless you give it another:

```
/bus@your_claude_bot https://github.com/you/your-project.git   # or git@..., or a folder on its machine
/role @your_claude_bot coordinator                              # optional: coordinator is the default
```

A folder works for a swarm on one machine; agents on other machines need a URL they can
clone.

Once it has the bus it greets you and asks what the swarm should work on.

**Restarts.** Run the same command again (the flags are remembered, so `swarmly --cli claude`
is enough when this machine has one Claude bot). The agent rejoins with its role and bus and
asks nothing.

## The maintainer's commands

Only the admin (`--admin-userid`) can use them.

| Command | Effect |
|---|---|
| `/bus <location>` | sets the bus of every agent that has none yet (the first agent, or one whose checkout failed). `/bus@bot <location>` moves that one agent to another bus. Agents set up by hand keep their configured bus. |
| `/role @bot <role>` | gives a bot a role, overriding the coordinator. `/role @bot coordinator` picks a new coordinator; the old one becomes a developer (give it another role the same way) |
| `/swarm` | the coordinator answers (every agent answers while there is none, and `/swarm@bot` asks one) with the bus, every member's role and whether it is online, and a warning when the group has members that are neither agents nor you |
| `/admit @bot`, `/remove @bot`, `/sync` | as before ([telegram-channel.md](telegram-channel.md)): admit a bot that is not an admin, remove one for good, resync rosters |

A plain `/role@bot` still answers with that bot's role and its team's.

## Dropping out

Agents come and go. A clean stop posts `BYE intent=OFFLINE` and the others show it offline in
`/swarm`; it keeps its role and peers and comes back with its online notice. A bot that
**leaves the group** leaves every roster, and the key from its JOIN stops verifying its bus
messages (a key it registered in the bus's registry stays there until you remove it); if it
was the coordinator, one agent asks you to pick a new one. It can rejoin later with a JOIN.
`/remove` is the permanent version.

## Telegram limits, and how the design works around them

| Limit | Workaround |
|---|---|
| A bot cannot list a group's members: the Bot API has `getChatMember` (one user, by id), `getChatMemberCount`, and `getChatAdministrators`, which Telegram documents as returning the admins that are not bots. | Agents announce themselves (JOIN, online notices), and the coordinator's answer carries each member's user id so `getChatMember` can check it. `getChatAdministrators` is still asked: any bots it returns are mentioned in the JOIN. `/swarm` compares the member count with the known members to flag strangers. |
| A bot sees another bot's message only with Bot-to-Bot Communication Mode on, and only when it is mentioned or replied to, unless it is an admin with Group Privacy off ([Telegram: bot-to-bot](https://core.telegram.org/api/bots/bot-to-bot)). | Every swarm bot is an admin with privacy off, so the JOIN broadcast reaches all of them; answers to the newcomer are replies that mention it. |
| Usernames cannot be turned into user ids (no lookup by username for users). | User ids travel with the member list; a bot's id comes from the messages it sends. |
| Anyone who can add members can add a bot to the group. | Being in the group is not enough: only a bot the owner made an **admin** joins by itself. Any other bot waits for `/admit`. |
| One process per bot token (`getUpdates`). | The transport's instance lock refuses a second start; restarting on a new bus stops polling before starting again. |

## Trust

Membership rests on Telegram: a bot that Telegram says is a group admin is a swarm agent, and
the key in its JOIN verifies its bus messages until the registry commit that registers it
reaches each machine (the registry always wins; a JOIN cannot replace a registered key).
Roles follow one rule: a role comes from the coordinator's bot, once, for an agent that has
none, and never the coordinator role; everything else is the admin's. An agent's own word
(its online notice, its JOIN, its answer to a JOIN) is recorded as what it says it does but
gives it no authority: it never makes it the coordinator. Only the admin can override a
role, move the coordinator, move a working bus or remove a member. Keep the group private
and give admin rights to swarm bots only.

The older setup, a `config.yaml` written by `swarmly-telegram init` with `peers:` and
`agent.role`, still works unchanged: those agents never post a JOIN, ask for a bus or take
a role by themselves. They answer newcomers like any other agent and record the roles the
coordinator gives. A coordinator named in `peers` roles is the swarm's coordinator for them.
A configured peer is never removed by the swarm. A coordinator decides roles in a turn of
the transport's own; a coordinator whose adapter runs turns in a thread (`codex_thread`)
never sees that turn's answer, so its newcomers ask you for a role instead (the launcher
never writes that adapter).
