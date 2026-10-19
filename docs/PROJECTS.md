# Multi-Project Coordination & Topic Routing

Swarmly supports coordinating agent swarms across multiple projects and repositories.
In this model:
- **GitHub is the default durable coordination plane**: issues, PRs, review comments, and discussions record decisions, tasks, and code changes durably.
- **Telegram is the realtime coordination plane**: a Telegram supergroup with forum topics hosts low-latency agent-to-agent exchanges and human oversight.
- **Topic-to-project routing** binds each Telegram forum topic by its numeric thread ID to a specific project repository and local working directory.

---

## 1. Concepts

### Durable plane (GitHub) vs realtime plane (Telegram)

In multi-agent engineering workflows, high-bandwidth collaboration requires two distinct communication planes:
1. **Realtime plane (Telegram)**: Fast notifications, status updates, ping handoffs, and interactive human steering happen inside Telegram forum topics. Each topic represents a specific stream of work (e.g., a feature, an integration, or a maintenance track).
2. **Durable plane (GitHub)**: The authoritative record of tasks, technical proposals, code reviews, and merged changes lives in GitHub issues and pull requests. Agents sign their durable actions (`— <alias>`), link issues, and create pull requests.
3. **Missed-ping backstop**: If an agent misses a Telegram notification, a background watcher checks GitHub notifications at regular intervals (`github.poll_minutes`). Any missed review requests, assignments, or mentions queue turns in the bound Telegram topic, skipping items that were already addressed recently.

### Topic-to-project binding

Instead of binding an entire agent process to a single checkout, the `projects:` section in `config.yaml` maps Telegram forum topics (by numeric topic ID) to specific GitHub repositories and local directories.

When a message arrives in a topic:
- The router checks if the topic ID is registered to a project.
- If bound, an isolated adapter is lazily instantiated with its working directory set to the project's workspace.
- The turn prompt is framed with project context: `Project: <key> (github.com/<owner>/<repo>)`.
- Unbound topics and the General topic (`thread_id is None`) continue to use the default `adapter.cwd`.

### Git worktrees vs shared working copy

For projects mapped to multiple active topics, concurrent agent turns on the same checkout risk stepping on each other's unstaged edits or checking out conflicting branches:

- **Shared working copy (`worktrees: false`, default)**:
  All topics bound to the project share the base `cwd`. Suitable for sequential tasks or single-topic projects.
- **Isolated git worktrees (`worktrees: true`)**:
  Swarmly automatically provisions an isolated git worktree for each topic under `<cwd>/.swarmly-worktrees/topic-<thread_id>`, detached from `origin/HEAD`.
  The `.swarmly-worktrees/` directory is automatically ignored in `.git/info/exclude` (so the repository's tracked `.gitignore` is never modified).
  If worktree creation fails (for instance in an uninitialized git repo), Swarmly logs a warning and falls back to `cwd` without dropping or failing the turn.

---

## 2. Configuration (`config.yaml`)

Add `projects:` and `github:` sections to your agent's `config.yaml`:

```yaml
agent:
  name: HermesBot
  alias: Hermes
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_HERMES
  home_channel_id: -1001234567890
  admin_user_id: 1234567890

peers:
  Codex: CodexBot
  Antigravity: AntigravityBot

adapter:
  type: hermes
  cwd: ~/projects/default-workspace    # fallback for General and unbound topics
  timeout_seconds: 900

# Project bindings
projects:
  webapp:
    repo: example-org/webapp          # owner/name on GitHub (required)
    cwd: ~/projects/webapp           # local checkout (must exist on disk)
    topics: [12, 15]                   # topic IDs in the home group (integers)
    # External group topics can be specified as "<chat_id>/<thread_id>"
    worktrees: true                    # one isolated git worktree per topic

  shop:
    repo: example-org/shop
    cwd: ~/projects/shop
    topics: [21]
    worktrees: false                   # operates directly in cwd

# GitHub durable plane and notification backstop
github:
  login: swarmly-hermes-bot            # this agent's own GitHub username (required unless poll_minutes: 0)
  token_env: GH_TOKEN_HERMES           # env var holding that account's token (default GH_TOKEN);
                                       # the agent's runtime gets it as GH_TOKEN, so its `gh`
                                       # acts as the agent. Never point it at the owner's token.
  poll_minutes: 5                      # backstop poll interval in minutes (0 disables backstop)
  mark_read: true                      # mark handled notifications as read on GitHub
```

---

## 3. Step-by-Step Migration Guide

### Keeping existing single-project behaviour

If your agent only works on a single codebase:
- Simply omit the `projects:` and `github:` sections from `config.yaml`.
- All turns, including forum topics and General messages, continue to run in `adapter.cwd` as before.
- Full backward compatibility is preserved.

### Migrating an agent to multiple projects

Follow these steps to configure multi-project routing:

#### Step 1: Prepare local checkouts
Ensure the local repositories exist on the agent's host:
```bash
git clone https://github.com/example-org/webapp ~/projects/webapp
git clone https://github.com/example-org/shop ~/projects/shop
```

#### Step 2: Create Telegram forum topics
In your Telegram supergroup (which must have Topics enabled in group settings):
- Create a new topic for each stream of work, e.g., "Webapp Development", "Shop".

#### Step 3: Discover numeric topic IDs with `/where`
Topic names are display labels and can change. Swarmly binds topics strictly by their **numeric thread ID**.
To find the topic ID:
1. In the Telegram topic, send:
   ```text
   /where@HermesBot
   ```
2. The bot will respond immediately:
   ```text
   WHERE Hermes chat=-1001234567890 topic=12 not bound: Telegram only
   ```
3. Take note of the `topic=12` number.

#### Step 4: Update `config.yaml`
Add the `projects:` mapping with the discovered numeric topic IDs, and configure `github:` with the agent's bot identity.

#### Step 5: Verify configuration with `check`
Run the CLI check command to validate the configuration:
```bash
swarmly-telegram --config config.yaml check
```
The check command verifies:
- All project `cwd` paths exist on disk, and each `repo` is `owner/name`.
- Topic IDs are positive integers and not bound to multiple projects.
- `github.login` is present when the backstop polls (`github.poll_minutes` above 0).
- The adapter's CLI resolves in every project's working directory.

`check` does not call GitHub. Prove the token with one backstop cycle, which exits 1 when
GitHub cannot be reached:
```bash
swarmly-telegram --config config.yaml gh-poll --once
```

---

## 4. Diagnostic Commands

### `/where`
Sends an immediate diagnostic reply indicating the active routing:
- **In an unbound topic**:
  `WHERE Hermes chat=-1001234567890 topic=99 not bound: Telegram only`
- **In a bound topic**:
  `WHERE Hermes chat=-1001234567890 topic=12 project=webapp repo=example-org/webapp workdir=/home/user/projects/webapp/.swarmly-worktrees/topic-12`

### `/status`
Reports agent health, message limits, and routing state:
- Includes `project=<key>` when asked within a bound topic.
- Reports GitHub backstop status: `github: @<login> (backstop: 5m, last: 2026-10-10T16:30:00Z)` or `github: disabled`.

### `gh-poll` CLI command
Run a one-time check of GitHub notifications outside the background loop (also spelled
`github-poll`). It needs no Telegram token, and it does not post to Telegram: the agent's
turn runs, and the agent answers on GitHub itself. It exits 1 when GitHub cannot be reached
(missing `gh`, a bad token, a rate limit). The running transport polls on its own and posts
a silent warning in the group, at most hourly, when its polls fail.
```bash
# Human-readable summary
swarmly-telegram --config config.yaml gh-poll --once

# JSON output for scripts
swarmly-telegram --config config.yaml gh-poll --once --json
```

---

## 5. Troubleshooting

### Worktree creation failure fallback
- If `worktrees: true` is configured but git worktree creation fails (e.g., bare clone, detached HEAD without origin, or disk permissions), Swarmly logs a warning:
  `Failed to create worktree at ...; falling back to cwd`
- The turn executes safely inside the project's base `cwd`. Turns are never aborted due to worktree provisioning issues.

### Missing repo or cwd at startup
- `swarmly-telegram check` catches missing directories or invalid `owner/repo` formats immediately.
- If a project `cwd` does not exist, validation fails with:
  `projects.<key>.cwd does not exist: <path>`

### Notifications backstop deduplication & lag
- The notifications backstop polls every `github.poll_minutes` (default 5 minutes).
- **Deduplication**: When an issue or PR is mentioned in the topic (e.g. `PR #42`, `issue #10`, `#42`), Swarmly records the reference in `_topic_recent_mentions`. If a background GitHub notification for `#42` arrives afterwards, it is recognized as already handled and skipped silently without generating duplicate turns.
- Dedupe covers every topic bound to the project, for two hours, and lives in the running
  transport's memory (a standalone `gh-poll` has none).
- Handled notifications are persisted to `.agents/state/gh_notifications-<alias>.json` in the
  `bus.repo` checkout (with no bus: the Swarmly checkout, or `~/.a2a` for a pip install). GitHub keeps one
  notification thread per issue or pull request; new activity on a handled thread fires
  again when it is addressed to the agent (mention, review request, assignment) or is the
  agent's own issue or pull request. Threads it only commented on or follows fire once.
- The backstop reads the notifications of the account that owns `github.token_env`'s token,
  so that token must belong to the agent's own account (`github.login`), not the owner's.
- Thread adapters (`codex_thread`, `antigravity_thread`) keep one conversation by design, so
  every project shares it; use a per-run adapter (`codex`, `claude_thread`, ...) for
  separate sessions per project.

### CI wake: a PR's checks wake the waiting agent
- When the agent names a pull request in a reply it posts -- a
  `https://github.com/<owner>/<repo>/pull/<n>` link, or a bare `#<n>` in a topic bound to a
  project -- the same backstop poll (`github.poll_minutes`) also asks `gh` for that PR's
  `statusCheckRollup`.
- Exactly one turn runs in the topic when every check reaches a terminal state, pass or fail,
  saying what finished, naming the failing checks on a failure, and linking the PR.
- A wake is recorded per head commit and check state (`<sha>:<success|failure>`) in
  `.agents/state/gh_ci-<alias>.json`, next to the notification state: a restart neither
  forgets a watch nor repeats a wake, and a new push is reported again.
- A watch is dropped when it fires, when `gh` has no checks for it, or after 12 hours, so a PR
  with no CI configured does not cost polls forever. `github.poll_minutes: 0` disables it with
  the rest of the backstop.
- An empty `statusCheckRollup` counts as pending, not success: a pull request pushed a moment
  ago has no checks until GitHub materialises them, and a wake then would announce a run that
  did not exist yet. A `StatusContext` in the `EXPECTED` state is still running, not passed.
- The pull request's own state is read in the same `gh` call: a PR that is already merged or
  closed wakes nobody (a merge notice or an upgrade call names landed work), and its watch is
  dropped on the first poll instead of waiting out the 12-hour TTL.
- The wake turn is admitted like a notification turn: a bound topic outside the home group with
  no context id (`SWARMLY_REDACTION_SECRET` unset) is refused and reported in the poll outcome,
  rather than run into the shared General session.

### GitHub token permissions
When generating a Personal Access Token (PAT) for the agent:
- **Classic token** (needed for the backstop: GitHub's notifications API does not accept
  fine-grained tokens): `repo`, `notifications` and `read:org` scopes.
- **Fine-grained token** (routing and `gh` use without the backstop, `github.poll_minutes: 0`):
  Read and Write for **Issues** and **Pull requests**, Read-only for **Metadata**.
- Store the token in an environment variable (e.g. `GH_TOKEN_HERMES`) referenced by `github.token_env` in `config.yaml`.
