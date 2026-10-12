# Upgrading a transport safely

The channel is both the tool and the artifact under change: upgrades happen while the swarm is
using the transport, and a bad upgrade drops a member off the channel. The 2026-09-19 incidents
(the `agy`/IDE collision; a second transport dark for an hour) were exactly this class. This
runbook makes the upgrade moment safe.

## Upgrading across the rename to Swarmly

The import package `a2a` is now `swarmly`, and the commands are `swarmly-telegram`,
`swarmly-keygen` and `swarmly`. The wire formats (`a2a/v1`, `a2a/2`, `[A2A v1.1]`), the
`A2A_*` variables, the MCP server name `a2a` and the paths under `.agents/` and `~/.a2a*` keep
their names, so signatures, keys and configs stay valid.

On each host, as part of the upgrade and **before restarting or switching** the transport:

1. Remove the old distribution, then reinstall the checkout so the new package and commands
   exist: `pip uninstall` the distribution installed before the rename, then
   `pip install -e .` (or `uv sync`). Until the reinstall the old console scripts
   fail; without the uninstall the stale distribution still claims the alias scripts.
2. Re-point launchers, services and scheduled tasks from `-m a2a.telegram.cli` to
   `-m swarmly.telegram.cli`, and re-run `swarmly mcp-setup <cli> --write` for any MCP entry.

Until then the old command names keep working for one release: `a2a-telegram`, `a2a-keygen`
and `a2a` run the new ones and print a one-line notice to stderr naming the replacement.

`scripts/upgrade_transport.sh verify` and `switch` check the target by importing
`swarmly`, so they refuse a revision from before the rename. `rollback` to such a revision
still works, since it only repoints and restarts.

## The rules

1. **Upgrade to a verified revision, never to bare `HEAD`.** A revision is "verified" when the
   CI gate (`.github/workflows/tests.yml`) is green on it — point tags at those.
2. **Verify *at the target* on the host before switching.** `scripts/upgrade_transport.sh
   verify <ref>` creates a throwaway worktree at that revision, proves the suite would import
   the target tree, and runs the full suite *on that machine*. If verification fails, nothing
   has been touched. `switch` runs this same verification **automatically, before it touches
   anything** — a switch to a revision whose suite fails is refused with `current` unchanged.
3. **Keep a rollback target.** The service runs from a stable path (`…/current`) that points at
   a deploy directory. Switching = repoint + restart; rollback = repoint back + restart.
   Seconds, not archaeology.
4. **Health-gate the restart — and revert automatically when the gate fails.** After a switch:
   the restart exits cleanly, the process is active, and the transport emits its machine receipt
   line

   ```
   A2A_ONLINE_NOTICE rev=<full-sha> alias=<alias> chat=<chat-id>
   ```

   whose `rev=` must be **a prefix of the target sha** — the gate checks the receipt is
   *after this restart* and fails on a missing, rev-less, or mismatched one. The receipt is
   emitted by `AgentChannel.start()` before the human-facing HELLO notice is sent, so it does
   not depend on the network cooperating. The script auto-rolls-back when the
   restart fails **including when `systemctl restart` itself returns non-zero**, when the
   process is not active, **and when no matching receipt appears within the window**
   (`UPGRADE_NOTICE_TIMEOUT`, default 30s); the watchdog covers slower failures.
   Escape hatches, both loud on stderr: `--skip-verify` (emergency only, recorded in the
   success line) and `--allow-unconfirmed` (accept a switch whose receipt is silent, rev-less,
   or mismatched — recorded loudly).
5. **Drain or isolate before restarting.** A restart must never collide with an in-flight run
   that owns local state (the `agy`/IDE collision class). Either let queued work
   finish first, or isolate the upgrade from the live session (worker session / queue).
6. **Canary the swarm.** Upgrade one peer first; watch one watchdog window (or ~30–60 min of
   normal traffic); only then the rest. A shared bad revision should strand at most the canary.
7. **Keep wire changes additive.** During a rolling upgrade, new and old transports coexist in
   one channel. Each release must interoperate with its predecessor: read what you wrote,
   ignore unknown envelope fields, and never remove a field in the same release that stops
   using it — deprecate for one cycle first.

## Procedure (per host)

```bash
# 0. pick a verified ref (tag, or a main commit with a green CI run)
git -C ~/projects/swarmly fetch origin --tags

# 1. verify at the target — runs the suite on THIS host, touches nothing
scripts/upgrade_transport.sh verify <ref>

# 2. switch + restart. switch re-verifies first (tests must pass to proceed);
#    on any failed gate it auto-reverts: suite failure = nothing switched,
#    restart/no-notice failure = automatic rollback to 'previous'.
#    CANARY HOST FIRST.
scripts/upgrade_transport.sh switch <ref> --service a2a-transport

# 3. confirm the channel shows the new revision in the online notice;
#    on trouble:
scripts/upgrade_transport.sh rollback --service a2a-transport
```

`scripts/upgrade_transport.sh` is a **template**: set `UPGRADE_REPO`, `UPGRADE_DEPLOY_ROOT`,
`UPGRADE_PYTHON`, `UPGRADE_SERVICE`, optionally `UPGRADE_NOTICE_TIMEOUT`, per host (or edit
the defaults at the top). Slow hosts can raise the settle waits after `systemctl restart`
(`UPGRADE_SETTLE_SECONDS`, default 5; `UPGRADE_ROLLBACK_SETTLE_SECONDS`, default 3) and the
lock wait (`UPGRADE_LOCK_WAIT`, default 30s); the scripted tests set them to 0/1. The script
itself must be checked out with **LF** line endings (`.gitattributes` enforces `*.sh text eol=lf`)
— CRLF breaks it under `set -euo pipefail`.

## Hosts without systemd

`switch`, `rollback` and their automatic revert are **not systemd-specific**: the service
lifecycle is three overridable snippets, each `eval`'d with the unit name in `$svc` and (for
the receipt) the restart's epoch seconds in `$since`.

| variable | default | what it must do |
|---|---|---|
| `UPGRADE_SERVICE_START` | `systemctl restart "$svc"` | start/restart the supervisor; non-zero exit ⇒ auto-revert |
| `UPGRADE_SERVICE_IS_ACTIVE` | `systemctl is-active --quiet "$svc"` | exit 0 iff the transport is running |
| `UPGRADE_SERVICE_RECEIPT` | `journalctl -u "$svc" --since "@$since" --no-pager` | print the log lines **from `$since` onward** |

**User units** (`systemctl --user`, as in GETTING-STARTED §5): the defaults above talk to the
system manager and will not find a user unit, so every switch fails its health gate and rolls
back. Set:

```bash
export UPGRADE_SERVICE_START='systemctl --user restart "$svc"'
export UPGRADE_SERVICE_IS_ACTIVE='systemctl --user is-active --quiet "$svc"'
export UPGRADE_SERVICE_RECEIPT='journalctl --user -u "$svc" --since "@$since" --no-pager'
```

The unit must run from `$UPGRADE_DEPLOY_ROOT/current` (default `~/transport/current`), or a
switch changes nothing that is running. Two more settings go with it:

- `UPGRADE_PYTHON` must name a Python that has the dependencies, for example the checkout's
  `.venv/bin/python` after `uv sync`; the default `python3` usually does not, and the suite
  gate then refuses every switch.
- The agent's `config.yaml` must set `bus.repo` to the bus checkout, outside the deploy root.
  Unset, nothing is polled; the ledger, locks and heartbeat live in that checkout, so they
  survive every switch.

When all three are set, `--service`/`UPGRADE_SERVICE` is optional: the host has taken over
the lifecycle and the unit name is only a label. Set a *partial* set and the remainder still
falls back to systemd — `require_service` refuses a missing name unless all three are present,
so the mixture is never silent.

Two things the receipt must not do:

- **It must not ignore `$since`.** That anchor is what stops a *pre-restart* notice satisfying
  the gate; a receipt that returns the whole log reopens the round-4/5 false-pass class
  (stale line, or an inbound peer message citing a revision).
- **It must not invent a revision.** The selector and the prefix check run on the receipt's
  text exactly as before, so a wide receipt cannot buy a passing revision it did not report.

Example — a Windows host whose transport is supervised by Task Scheduler and logs to a file:

```bash
export UPGRADE_SERVICE_START='schtasks /end /tn a2a-transport >/dev/null 2>&1; schtasks /run /tn a2a-transport'
export UPGRADE_SERVICE_IS_ACTIVE='schtasks /query /tn a2a-transport /fo csv /nh | grep -q Running'
export UPGRADE_SERVICE_RECEIPT='tail -n 400 "$UPGRADE_DEPLOY_ROOT/transport.host.log"'
export UPGRADE_SERVICE=a2a-transport   # label only
scripts/upgrade_transport.sh switch <ref>
```

The same drill then runs unchanged: fault-inject a switch, watch the notice gate miss, and
see the automatic revert repoint `current` and restart the task — the B2/B3 acceptance the
issue asks for. Without this, that half of the resilience loop is asserted, not proven.

`status` now warns when `UPGRADE_REPO`/`UPGRADE_DEPLOY_ROOT` point at a path that does not
exist; previously it printed the default path and exited 0 on any host, which is a
meaningless success.

## Concurrency and edge cases

- **One run at a time per deploy root.** `switch`/`rollback`/`verify` take a lock
  (`$UPGRADE_DEPLOY_ROOT/.upgrade.lock`, via `flock` — waits up to `UPGRADE_LOCK_WAIT`
  (default 30s), then refuses with a clear message; the refusal is proven by a test that holds
  the lock externally). Without it, two concurrent runs share the `verify-<sha>`/`deploy-<sha>`
  worktrees, race each other's `worktree add/remove`, and **both** fail mid-suite leaving a
  stale worktree entry (review round 2, F3). Hosts without `flock` degrade to a loud warning
  (not serialised).
- **A dangling `current` never aborts a switch.** If `current` points at a vanished deploy (its
  path is unresolvable), the switch proceeds; it reports `previous left unchanged (<target>)`
  when a usable `previous` is already recorded — `rollback` still works — and
  `previous not recorded` only when there is no usable previous at all. A later `rollback` in
  that state fails fast with `no previous target recorded` instead of half-applying
  (review round 2 F1; round 3 N1).
- **Re-switching to the deployed revision keeps the old `previous`.** Aliasing `previous` to
  `current` would make `rollback` a healthy-looking no-op; the script skips the bookkeeping and
  says so (review F6).
- **`--skip-verify` is visible in the receipt.** The success line ends
  `(UNVERIFIED — --skip-verify)` so the audit trail distinguishes an emergency switch
  (review F7).
- **Every switch appends to `$UPGRADE_DEPLOY_ROOT/switch.log`** (`ts switch <sha> (from <dir>)`)
  *before* mutating any link, and every revert appends `ts rollback <dir> (from <dir>)` — hand
  recovery never depends on archaeology, and the log no longer claims a reverted revision is
  still deployed (review F8; round-3 minor).
- **The failure paths are tested.** `tests/test_upgrade_transport.py` drives the real script
  end-to-end (fixture repo + fake `systemctl`/`journalctl`): restart failures (both shapes),
  silent/rev-less/wrong-rev/same-prefix notices, `--allow-unconfirmed`, dangling `current`,
  re-switch, `--skip-verify`, `switch.log`, lock refusal, and concurrent switches (review F5).
  On Windows run the suite **inside WSL** — natively the e2e class skips (POSIX required), and
  a skip is not a pass.

## Why this removes the "dropped agent" deficiency

| failure mode | the rule that kills it |
|---|---|
| pulled a revision that fails in place | verify-at-target before switching — and `switch` refuses to touch anything when the suite fails |
| stuck on a broken revision until someone notices | `current`/`previous` pointers → one-command rollback; failed switches roll back automatically |
| transport restarts into an error and goes silent | health gate + auto-rollback (non-zero `systemctl restart`, inactive unit, **or** no `A2A_ONLINE_NOTICE rev=<sha>` receipt with the *target* revision in the window); the watchdog catches the rest |
| restart collides with live runtime state | drain/isolate rule |
| everyone upgrades the same bad revision at once | canary sequencing |
| nobody knows which revision a peer runs | `rev:` in the online notice |
