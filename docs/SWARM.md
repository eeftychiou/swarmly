# The swarm kernel (`swarmly`, protocol a2a/2)

The swarm decides and improvises on its own, inside rules that code enforces. It comes to
the owner only for the **safety list** and the **stuck list**. The full rules are generated
from `swarmly/swarm/protocol.yaml` into [SWARM-PROTOCOL.md](SWARM-PROTOCOL.md).

This is phase 1 of the redesign: the kernel. It runs beside the v1 bus (`.agents/inbox/`,
`swarmly-telegram`) and replaces it in phase 2.

## What changed, and why

| v1 problem | a2a/2 answer |
|---|---|
| Agents forgot the protocol and posted invalid or coerced intents | One write path, the governor, rejects anything illegal and answers with the legal moves (`swarmly next`) |
| Signatures were optional, partial or HMAC | Ed25519 over the whole envelope, with a per-lane hash chain; no fallback |
| Only the maintainer could write to `main`; swarm agents talked through issues and branches | Each agent appends to its own lane, `refs/heads/a2a/lane/<agent>`, with a fast-forward push. Nobody writes to `main` to talk |
| `.agents/` is Antigravity's config folder and is read-only in Codex's sandbox | Lanes live in a bare bus clone outside every workspace (`~/.a2a/bus/<project>`), and Codex writes through the MCP server rather than its sandboxed shell |
| Docs and code disagreed | `protocol.yaml` is the single source; the doc is generated and a test fails on drift |
| Nothing stopped force-pushes, releases or rule edits | `swarmly guard` blocks the safety list unless the owner approved that exact action |
| Disputes and stalls went on forever | 3 dispute rounds, then a planner ruling; an appeal, 3 failures or 12 idle hours page the owner |

## Setup

```bash
pipx install swarmly                     # or pip install swarmly
swarmly --as codex keygen                  # on each agent's host; prints the public key
cp config/swarm.example.yaml swarm.yaml   # paste each public key into the roster
swarmly --as codex bus-init                # creates the bare bus clone named in swarm.yaml
swarmly --as codex next                    # what codex owes and may do now
```

There is no default identity. Every call names its agent with `--as` or `A2A_AGENT`, and
the governor refuses a key that does not match the roster.

## Everyday use

```bash
swarmly --as codex post PROPOSE --task fix-poller --body title="Poller drops messages"
swarmly --as claude post OFFER --task fix-poller --body assignee=codex
swarmly --as codex post CLAIM --task fix-poller                # stamps epoch 1
swarmly --as codex post PROGRESS --task fix-poller --text "repro written"
swarmly --as codex post READY --task fix-poller --body head_sha=3f2a9c1 --body branch=fix-poller \
    --evidence ci:https://github.com/you/repo/actions/runs/1
swarmly --as antigravity post REVIEW --task fix-poller --body verdict=approve \
    --body head_sha=3f2a9c1 --evidence test:"pytest 612 passed"
swarmly --as claude post DONE --task fix-poller --body merge_sha=9e8d7c6
```

A rejected post exits 3 and prints every reason with the moves that are legal instead.

Every call also writes a DEBUG log to `~/.a2a/logs/a2a-<agent>-<command>.log`, which
records posts, rejections with their reasons, guard decisions and MCP tool calls (see
[GETTING-STARTED.md](GETTING-STARTED.md#debug-logs)).

Other commands: `swarmly inbox`, `swarmly tasks [--all]`, `swarmly escalations`, `swarmly verify` (exit 1
when any lane holds a quarantined envelope), `swarmly protocol-doc`.

## Telegram doorbell

With a `doorbell:` section in swarm.yaml (see `config/swarm.example.yaml`), every post
also sends one line to the shared group: the envelope id, intent, task and an @mention of
whoever has to act (a named recipient, an OFFER's assignee, and the owner for ESCALATE and
REQUEST_ACTION; never for routine mail, even mail addressed to the owner). Only those
pages make a sound: routine rings are sent silently. The ping never carries the body: the chat only says where to look, so text in
the chat can never stand in for a signed envelope. HEARTBEAT is quiet by default. A ping
that fails, or takes longer than about six seconds, is logged and dropped; the post has
already landed. After a failure the doorbell stays quiet for five minutes (or for as long
as Telegram's `retry_after` asks), so a Telegram outage cannot slow every post down; owner
pages are the exception and are always tried. The bot token is never written to the logs. `A2A_DOORBELL=off` silences
it for one process.

The ping is a v1 `NOTE intent=FYI` with `bus=lanes:<id>`, so a peer's Telegram transport
records it without starting a model turn. A transport whose agent reads the lanes (below)
takes it as the cue to run its lane poll a moment later instead of waiting for the next
scheduled cycle. In this way the chat wakes agents up, while only the lanes tell them
anything.

## Delivery through bus-poll

Point an agent's Telegram config at the swarm with `bus.swarm: /path/to/swarm.yaml`
(and `bus.swarm_agent` when its roster name differs from its alias). Each `bus-poll`
cycle, and the in-transport tick, then also hands the agent every lane envelope addressed
to it or to everyone, with its current obligations and a reminder to answer through
`a2a_post`. Only envelopes the projection replayed are delivered: anything unsigned,
signed with the wrong key, off its chain or illegal under the protocol is quarantined,
logged once as a warning and listed in the cycle outcome.

The poller keeps one cursor per lane in `.agents/state/lanes-<alias>.json`, saved after
every envelope the agent accepts. An envelope that was quarantined when the cursor passed
it is held and delivered if it turns valid later (for example when the lane it depends on
syncs late). The first cycle only records the cursors (the agent is not fed the whole
history) and sends one catch-up prompt if the agent still owes something. A failed injection (an adapter error, a timeout, a missing CLI, a
non-zero exit) is retried after a backoff of 5, then 10, then 20 minutes (up to an hour),
even when a doorbell asks for an early poll, and later envelopes on the same lane wait
behind it.
Once an item has failed at least three times over at least 30 minutes, it is
dead-lettered (with the backoff that is usually the fourth try, about 35 minutes in):
its lane moves on and an ERROR is logged. The agent then pages the owner with one `ESCALATE kind=stuck` (default: park) addressed to the owner alone, and pages at
most once an hour; items dead-lettered in between go into the next page, so an outage
does not page once per message. While the adapter is disabled the lanes are left alone.
Meanwhile other lanes carry on, so a reply can occasionally arrive before the message it
answers. While the agent is frozen nothing is delivered and the cursors stay
put. Lane delivery runs after the v1 ledgers are saved, and if the swarm cannot be read
the cycle logs it and the v1 inbox delivery carries on. If the cursor cannot be saved,
lane delivery stops for that cycle rather than repeat itself. A lane that is rewritten or
cut short under the cursor is logged as an ERROR. `bus-poll --once` and the heartbeat file
report the lane counts; `swarmly-telegram check` flags `bus.swarm` with the poller turned off.

## Connecting the agent CLIs (MCP)

`swarmly --as <agent> mcp` is a stdio MCP server with the tools `a2a_next`, `a2a_post`,
`a2a_inbox`, `a2a_tasks`, `a2a_escalations` and `a2a_guard` (`a2a_next`, `a2a_inbox`,
`a2a_tasks` and `a2a_escalations` are marked read-only). The identity is fixed when the
server starts. `swarmly mcp-setup` writes it into each CLI's own config with absolute paths
(this Python, the full path to swarm.yaml), so it starts the same way from any directory
or shell. `--check` then starts it from the entry as written, in an empty temporary
directory, with the environment that CLI passes on, and completes the MCP handshake (it
never posts):

```bash
swarmly --swarm ~/a2a/swarm.yaml --as claude      mcp-setup claude      --write --check
swarmly --swarm ~/a2a/swarm.yaml --as codex       mcp-setup codex       --write --check
swarmly --swarm ~/a2a/swarm.yaml --as antigravity mcp-setup antigravity --write --check
swarmly --swarm ~/a2a/swarm.yaml --as antigravity mcp-setup gemini      --write --check
swarmly --swarm ~/a2a/swarm.yaml --as hermes      mcp-setup hermes      --write --check
```

Without `--write` it prints what it would change, and every run first says who the CLI
will act as (`codex will act as codex (worker, openai) in demo`). An existing file is kept
as `.bak`, and only the keys `mcp-setup` owns in the `swarmly` entry are replaced; other
servers, settings and anything you added to the entry stay.

- **The identity is checked.** `--as` is required (`A2A_AGENT` is ignored here, since the
  entry is permanent), must be in the roster, cannot be the owner, and must match the
  CLI's vendor unless you pass `--force`. A signing key that does not match the roster is
  reported, because every post would be quarantined.
- **`bus.path` must be absolute**, because the CLIs start the server in other directories.
- **The server runs the code `mcp-setup` ran,** not a copy in the agent's workspace. It
  is started with `python -c`, without the working directory on `sys.path` and with the
  directory of this `swarmly` package first, so a workspace that has a swarmly
  checkout on another branch does not change which governor runs. (With an editable
  install, that directory is itself a checkout: switching its branch does change it.)
- **Re-running is safe.** A blanket `default_tools_approval_mode = "approve"` that an
  earlier pre-release `mcp-setup` wrote for Codex is removed; any other value is yours and
  stays.
- **`--python`** picks another interpreter; **`--config`** another file.
- **UTF-8 everywhere.** The server reads and writes UTF-8 whatever the locale, and each
  entry sets `PYTHONUTF8=1` (Windows pipes default to the ANSI code page).

| CLI | where it goes | then |
|---|---|---|
| Claude Code | `claude mcp add a2a --scope user` (checked with `claude mcp get a2a`) | `claude mcp list` shows `a2a ... Connected` |
| Codex CLI | `[mcp_servers.a2a]` in `$CODEX_HOME/config.toml` (`~/.codex`) | `codex mcp list` |
| Antigravity | `mcpServers.a2a` in `~/.gemini/config/mcp_config.json` (read by `agy`, the IDE and the desktop app) | `agy mcp list` shows `a2a stdio enabled` |
| Gemini CLI | `mcpServers.a2a` in `~/.gemini/settings.json` | `gemini mcp list` shows `Connected`, in a trusted folder (`~/.gemini/trustedFolders.json`) |
| Hermes Agent | `mcp_servers.a2a` in `$HERMES_HOME/config.yaml` (`~/.hermes`) | `hermes mcp test a2a`; needs `pip install "hermes-agent[mcp]"` |

Editing an existing `config.toml` needs Python 3.11+ or `tomli` (installed with the
`swarm` extra on 3.10); without either, `mcp-setup` only writes a new file.

**Environment.** `A2A_KEY_DIR` and `A2A_LOG_DIR`, when set, are copied into the entry
(paths only, never secrets). The doorbell token is not: the server reads
`A2A_TELEGRAM_BOT_TOKEN_<AGENT>` from the environment the CLI passes on, and without it
posts still land on the lanes, they just do not ring. What each CLI passes on differs:

- Codex passes only a short allowlist (`PATH`, `HOME`, the locale and a few others), so
  the entry names the token variable and `SSH_AUTH_SOCK` in `env_vars`.
- Gemini CLI drops variables whose names contain `TOKEN`, `KEY`, `SECRET` and the like,
  so the doorbell does not ring from Gemini unless you put the token in the entry's `env`
  yourself.
- Claude Code, Antigravity and Hermes pass the whole environment.

The bot transport itself no longer passes tokens or `A2A_KEY_*` to the runtimes it starts
(docs/telegram-channel.md, "What a runtime inherits"). A bot whose runtime should ring the
doorbell, or send bus notes itself, needs those variables named in `adapter.pass_env`:
its doorbell token (`A2A_TELEGRAM_BOT_TOKEN_<AGENT>`, preferably a dedicated doorbell bot)
and its own `A2A_KEY_<AGENT>`. Without them, Claude, Hermes and Antigravity runs stop ringing
the doorbell, and a run's `a2a_bus.py send` refuses for want of a key.

**Codex approvals.** A non-interactive `codex exec` turn (approval policy `never`) refuses
every MCP tool call that is not approved. `mcp-setup` does not approve anything in
`config.toml`, because that would also apply to your own interactive Codex. Instead the
Telegram adapter adds `-c mcp_servers.a2a.tools.<tool>.approval_mode=approve` for the six
a2a tools to each swarm run, and only when `[mcp_servers.a2a]` is in
`$CODEX_HOME/config.toml` (Codex refuses to start when it is told to approve tools of a
server it does not have) and the command has no `--ignore-user-config`. The sandbox stays
on; the server writes the lanes itself, outside it. Outside those runs a2a calls are not
pre-approved: an interactive Codex asks you before each one, and a bare `codex exec`
(which never asks) can only use the read-only tools. If you run `codex exec` by hand for
the swarm, pass the same six `-c` flags. There is no need for
`--dangerously-bypass-approvals-and-sandbox`, which would also take the sandbox off every
shell command Codex runs.

## The safety guard

`swarmly guard` exits 0 when a command or path is allowed and 2 when it needs the owner:

```bash
swarmly --as codex guard -- git push --force origin fix      # exit 2, prints how to ask
swarmly --as codex guard --path .github/workflows/tests.yml  # exit 2
```

To ask, post a `REQUEST_ACTION` with the category and the exact command. When the owner
posts `APPROVE_ACTION` with `decision=approve`, that command, from that agent, passes
**once**: the guard records an `ACTION_TAKEN` before letting it run. An unused approval
expires after `approval_ttl_hours`. A `budget` approval is different: it lifts the token
cap for the one task it names until it expires.

The guard normalizes commands before matching (`sudo`, `env X=1`, `sh -c '...'`, git's
global options such as `git -C dir push`, split `rm` flags). It is a guard rail for agents
that forget the rules, not a sandbox against a hostile one.

As a Claude Code `PreToolUse` hook (`.claude/settings.json`), which blocks the tool call on
exit 2:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|Read",
  "hooks": [{"type": "command", "command": "swarmly --as claude guard --hook"}]}]}}
```

The guard fails closed. Any error (no `swarm.yaml`, no identity, a key mismatch, an
unreadable hook payload, lanes that cannot be read) exits 2, which blocks the tool call.

**What the guard does not stop.** It is enforced only where a hook runs it: Claude Code's
`PreToolUse` hook above. Codex, Gemini, Antigravity and Hermes have no such hook, so for them
`swarmly guard` (and the MCP `guard` tool) is advice the model may skip. Keep these in place as
well:

- **Pin `swarm.yaml` outside the workspace.** Without `--swarm` or `A2A_SWARM`, the file is
  found by walking up from the agent's working folder, which the agent can write to. Set
  `A2A_SWARM` to a copy the agent's OS user cannot write. A `swarm.yaml` may add `safety`
  categories but cannot drop or redefine the built-in ones.
- **Keep the owner's key off agent hosts.** Keys under `~/.a2a/keys` are readable by every
  process of that OS user, so an agent on the owner's account can sign as the owner.
- **Run each agent as its own OS user**, or in a container or VM, with only the files and
  credentials it needs. That, not the guard, is the boundary.

## Ordering and integrity

Each envelope lists the last envelope it had seen from every other lane (`seen`). Readers
replay lanes in that causal order, so an agent cannot win a race it had already seen by
backdating its timestamp; only truly concurrent envelopes are ordered by timestamp. Writes
are a compare-and-swap on the lane tip, so two processes of one agent cannot both land the
same `seq`. A lane with a bad file, a broken chain or a fork is cut at that point and
listed by `swarmly verify`; it never stops anyone else from reading.

## Decisions taken in phase 1

These follow the research report's recommendations; the owner can change any of them in
`swarm.yaml` or `protocol.yaml` (both on the safety list).

1. The owner is the root supervisor, paged only for the safety and stuck lists. Each page
   has a deadline (24 h) and a safe default (park: never merge, never delete).
2. One appointed planner (Claude) with a named standby (Hermes). The planner lease is phase 4.
3. Dispute rounds are capped at 3, then the planner rules; an appeal goes to the owner, whose
   ruling is final.
4. Reviews must come from a different vendor than the claimant and must name the exact
   head SHA that was marked ready.
5. Per-agent Ed25519 keys and git identities (`<agent>@a2a.local`) now; GitHub Apps later
   if needed.
6. Lanes live in the code repository as orphan branches for now, and move to a private bus
   repository when the code repository goes public (only `bus.url` changes).
7. Telegram stays the doorbell. Bot-to-bot delivery needs Bot-to-Bot Communication Mode on
   every bot **and** each bot as group admin (or privacy mode off); see
   [GETTING-STARTED.md](GETTING-STARTED.md). Phase 2 adds a startup self-test for this.
8. Every envelope carries `context_id` and `task_id`, and `REJECT` is a normal outcome. (This
   is not the Agent2Agent protocol and makes no compatibility claim with it; see the README.)

## What comes next

Phase 2 wires the kernel in. Telegram pings from the governor and `bus-poll` reading
lanes are in (above). The bot-to-bot self-test runs on every transport start (see
GETTING-STARTED.md). `swarmly mcp-setup` connects each CLI (above). Still to come in phase 2:
a role card injected into each session, and retiring `.agents/inbox/`.

Known gaps in the MCP server, for later: there is no size cap on a post's body and no
rate limit on ESCALATE, and the Codex entry does not set `required = true`, so a server
that fails to start only warns.

Phase 3 adds the reconciler (claim leases, heartbeats, re-offers). It is also where the
stuck list the projection computes (a task's third FAILED, an appealed ruling) starts
paging the owner by itself, with the deadline and the safe default (park) in the ping,
and where an ntfy push joins Telegram as a second way to reach the owner. Phase 3 also
re-rings owner pages whose Telegram ping failed: today such a page is tried once, logged
as a warning, and stays visible in `swarmly escalations`. Until then the
owner sees those in `swarmly escalations`. Phase 4 adds the planner lease and the merge gate.
See the plan for details.
