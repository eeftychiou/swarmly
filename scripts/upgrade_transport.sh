#!/usr/bin/env bash
# upgrade_transport.sh — verify-before-switch upgrades for an A2A transport.
#
# Template: set paths/service names per host via environment (or edit the defaults).
# See docs/telegram/UPGRADE.md. Subcommands:
#   verify <ref>                    run the suite at <ref> in a throwaway worktree
#   switch <ref> [--service NAME]   verify at <ref> (default), repoint 'current', restart.
#                                   Any failed gate auto-reverts: tests fail  -> nothing
#                                   switched; restart fails or the transport stays
#                                   unconfirmed -> automatic rollback to 'previous'.
#                                   Escape hatches: --skip-verify, --allow-unconfirmed.
#   rollback [--service NAME]       repoint to the previous deploy dir and restart
#   status                          show current/previous targets and service state
#
# Env: UPGRADE_REPO (checkout), UPGRADE_DEPLOY_ROOT (deploy dirs + current/previous links),
#      UPGRADE_PYTHON (interpreter with the deps installed), UPGRADE_SERVICE (systemd unit),
#      UPGRADE_NOTICE_TIMEOUT (seconds to wait for the transport's online-news
#      receipt, `A2A_ONLINE_NOTICE rev=<sha>`, default 30),
#      UPGRADE_SETTLE_SECONDS / UPGRADE_ROLLBACK_SETTLE_SECONDS (post-restart settle waits),
#      UPGRADE_LOCK_WAIT (seconds to wait for the per-root lock, default 30).
#
# Service lifecycle (issue #84). The restart/health/notice-reading trio is a set of
# overridable shell snippets, so a host without systemd (Windows Task Scheduler,
# launchd, a plain wrapper) can run `switch`/`rollback` — and its automatic revert —
# unchanged, instead of leaving the recovery path asserted but unexercised:
#   UPGRADE_SERVICE_START        snippet; default `systemctl restart "$svc"`
#   UPGRADE_SERVICE_IS_ACTIVE    snippet; default `systemctl is-active --quiet "$svc"`
#   UPGRADE_SERVICE_RECEIPT      snippet; default `journalctl -u "$svc" --since "@$since"`
# Each is eval'd with the unit name in $svc and (for the receipt) the restart's epoch
# seconds in $since. The receipt MUST print only lines from $since onward: that anchor
# is what stops a pre-restart notice passing the gate, so a receipt which ignores
# $since silently weakens it. `--service`/UPGRADE_SERVICE may be omitted only when all
# three hooks are supplied — that host has taken over the lifecycle.
set -euo pipefail

REPO="${UPGRADE_REPO:-$HOME/projects/swarmly}"
ROOT="${UPGRADE_DEPLOY_ROOT:-$HOME/transport}"
PYTHON="${UPGRADE_PYTHON:-python3}"
SERVICE="${UPGRADE_SERVICE:-}"
SERVICE_START="${UPGRADE_SERVICE_START:-systemctl restart \"\$svc\"}"
SERVICE_IS_ACTIVE="${UPGRADE_SERVICE_IS_ACTIVE:-systemctl is-active --quiet \"\$svc\"}"
SERVICE_RECEIPT="${UPGRADE_SERVICE_RECEIPT:-journalctl -u \"\$svc\" --since \"@\$since\" --no-pager}"
NOTICE_TIMEOUT="${UPGRADE_NOTICE_TIMEOUT:-30}"
CURRENT="$ROOT/current"
PREVIOUS="$ROOT/previous"
# Settle waits (seconds), overridable for slow hosts and scripted tests.
SETTLE_SECONDS="${UPGRADE_SETTLE_SECONDS:-5}"
ROLLBACK_SETTLE_SECONDS="${UPGRADE_ROLLBACK_SETTLE_SECONDS:-3}"
# Seconds to wait for a peer upgrade run before refusing the lock (review NIT 4).
LOCK_WAIT="${UPGRADE_LOCK_WAIT:-30}"

die() { echo "upgrade_transport: $*" >&2; exit 1; }

# Numeric env knobs feed `sleep`, `flock -w` and deadline arithmetic; a
# malformed value must fail loudly BEFORE anything mutates (review MINOR 3):
# `sleep abc` used to die after `current` was repointed (no rollback) and
# `flock -w abc` mis-reported a peer holding the lock.
require_uint() {
    case "$2" in
        ''|*[!0-9]*) die "$1 must be a non-negative integer (got: '$2')" ;;
    esac
}
require_uint UPGRADE_SETTLE_SECONDS "$SETTLE_SECONDS"
require_uint UPGRADE_ROLLBACK_SETTLE_SECONDS "$ROLLBACK_SETTLE_SECONDS"
require_uint UPGRADE_NOTICE_TIMEOUT "$NOTICE_TIMEOUT"
require_uint UPGRADE_LOCK_WAIT "$LOCK_WAIT"

# The host has taken over the whole service lifecycle (issue #84) only when all
# three snippets are supplied; a partial set would silently fall back to systemd
# for the missing half. In that case no unit name is needed.
LIFECYCLE_OVERRIDDEN=0
if [ -n "${UPGRADE_SERVICE_START:-}" ] && [ -n "${UPGRADE_SERVICE_IS_ACTIVE:-}" ] \
    && [ -n "${UPGRADE_SERVICE_RECEIPT:-}" ]; then
    LIFECYCLE_OVERRIDDEN=1
fi

# service_start / service_active / service_receipt — the single seam to the
# supervisor. The snippet runs in this function's scope, so it sees $svc (the
# unit/task name, possibly empty) and, for the receipt, $since (epoch seconds of
# the restart). Operators own these strings exactly as they own UPGRADE_REPO.
# Each hook runs in a SUBSHELL: a hook that terminates (exit/return) must read as
# a non-zero status so the auto-revert contract still holds, and a hook cannot
# corrupt this script's own state ($sha/$ts/$CURRENT) on its way out.
service_start() {
    local svc="$1"
    ( eval "$SERVICE_START" )
}

service_active() {
    local svc="$1"
    ( eval "$SERVICE_IS_ACTIVE" )
}

service_receipt() {
    local svc="$1" since="$2"
    ( eval "$SERVICE_RECEIPT" )
}

# A unit name is only required when this host has NOT taken over the lifecycle:
# with all three hooks supplied the snippets are the supervisor, whatever the
# operator names it (and UPGRADE_SERVICE may legitimately be empty there).
require_service() {
    [ -n "$1" ] || [ "$LIFECYCLE_OVERRIDDEN" -eq 1 ] || \
        die "no service name (use --service or UPGRADE_SERVICE, or supply UPGRADE_SERVICE_START/_IS_ACTIVE/_RECEIPT for a non-systemd host)"
}

# Where to look when a switch reverts: the same source the notice gate read.
# Naming journalctl on a Task-Scheduler host sends the operator to a command
# that does not exist there.
receipt_hint() {
    if [ "$LIFECYCLE_OVERRIDDEN" -eq 1 ]; then
        echo "run UPGRADE_SERVICE_RECEIPT (see docs/telegram/UPGRADE.md)"
    else
        echo "journalctl -u ${1:-<service>} -n 100 --no-pager"
    fi
}

resolve() { git -C "$REPO" rev-parse --verify "$1^{commit}"; }

target_of() {
    # Human-readable target of a current/previous link ('(none)' when unset).
    if [ -L "$1" ]; then readlink -f "$1" 2>/dev/null || echo '(broken)'; else echo '(none)'; fi
}

fetch_refs() {
    git -C "$REPO" fetch --quiet origin --tags 2>/dev/null || \
        echo "upgrade_transport: fetch failed (offline?); using local refs" >&2
}

# acquire_lock — one upgrade run at a time per deploy root (review round 2, F3).
# Concurrent switch/rollback/verify invocations share the verify-<sha>/deploy-<sha>
# worktrees and destroy each other's runs: both `worktree add`s race, one removes
# the tree the other is testing, both fail mid-suite and the repo is left with a
# stale .git/worktrees entry. Waits up to 30s for a peer to finish, then refuses
# loudly. Degrades to a warning (not serialised) where flock is unavailable.
acquire_lock() {
    LOCK_FILE="$ROOT/.upgrade.lock"
    mkdir -p "$ROOT"
    if ! command -v flock >/dev/null 2>&1; then
        echo "upgrade_transport: flock not found — concurrent runs are NOT serialised (see docs/telegram/UPGRADE.md)" >&2
        return 0
    fi
    exec 9>"$LOCK_FILE" || die "cannot open lock file $LOCK_FILE"
    if ! flock -w "$LOCK_WAIT" 9; then
        die "another upgrade_transport.sh invocation is active (lock: $LOCK_FILE) — retry after it finishes"
    fi
}

# verify_ref <sha> — suite at <sha> in a throwaway worktree on THIS host.
# Returns 0 when the tree imports correctly AND the full suite passes; else 1.
# The worktree is removed on every exit path (success or failure).
verify_ref() {
    local sha="$1" wt ok=0
    wt="$ROOT/verify-$sha"
    mkdir -p "$ROOT"
    git -C "$REPO" worktree prune
    rm -rf "$wt"
    git -C "$REPO" worktree add --quiet --detach "$wt" "$sha"
    # Prove the target tree is what the tests would import (editable installs can
    # shadow it). Compare RESOLVED paths: a Windows build writes __file__ with
    # backslashes, so a "/verify-" substring test always failed there (review F2).
    if ! (
        cd "$wt"
        WT="$wt" PYTHONPATH="$wt" "$PYTHON" - <<'PY'
import os
import pathlib
import swarmly.telegram.channel as channel
wt = pathlib.Path(os.environ["WT"]).resolve()
got = pathlib.Path(channel.__file__).resolve()
assert wt in got.parents, "wrong tree would be under test: " + str(got)
print("verify: importing", got)
PY
    ); then
        ok=1
    fi
    # The suite is one worker per core now (#320): serially it is most of the job's wall time,
    # and `-n auto` is what CI and the maintainers run. pytest and xdist are the `test` extra,
    # which a transport's own venv may not have -- so ask, and fall back to the stdlib runner
    # rather than failing a verify on a host that only installed the transport (#322).
    if [ "$ok" -eq 0 ] && (
        cd "$wt"
        "$PYTHON" -c "import pytest, xdist" >/dev/null 2>&1
    ); then
        (
            cd "$wt"
            PYTHONPATH="$wt" "$PYTHON" -m pytest -n auto -q tests
        ) || ok=1
    elif [ "$ok" -eq 0 ]; then
        echo "verify: pytest/xdist not in $PYTHON; running the suite serially" >&2
        (
            cd "$wt"
            PYTHONPATH="$wt" "$PYTHON" -m unittest discover tests
        ) || ok=1
    fi
    git -C "$REPO" worktree remove --force "$wt" 2>/dev/null || true
    return "$ok"
}

cmd_verify() {
    local ref="${1:?usage: verify <ref>}" sha
    fetch_refs
    sha="$(resolve "$ref")"
    echo "verify: running the full suite at $sha on this host"
    if verify_ref "$sha"; then
        echo "verify OK: $sha"
    else
        die "verify FAILED at $sha — nothing was switched; fix the revision before shipping it"
    fi
}

# rollback_internal <svc> — repoint to PREVIOUS and restart. Returns 0/1 (never dies):
# callers decide how to report a stack of failures.
rollback_internal() {
    local svc="$1" prev
    [ -L "$PREVIOUS" ] || { echo "rollback: no previous target recorded in $PREVIOUS" >&2; return 1; }
    prev="$(readlink -f "$PREVIOUS" 2>/dev/null || true)"
    [ -d "$prev" ] || { echo "rollback: previous target is missing: $prev" >&2; return 1; }
    # Review round 3 (minor): record the revert too — the switch line alone left
    # the durable record claiming the broken revision was still deployed.
    printf '%s rollback %s (from %s)\n' "$(date -u +%FT%TZ)" "$prev" "$(target_of "$CURRENT")" \
        >> "$ROOT/switch.log" 2>/dev/null || true
    ln -sfn "$prev" "$CURRENT"
    if ! service_start "$svc"; then
        echo "rollback: restart of ${svc:-<service>} failed; manual intervention needed" >&2
        return 1
    fi
    sleep "$ROLLBACK_SETTLE_SECONDS"
    if service_active "$svc"; then
        echo "rollback: ${svc:-<service>} active at $prev"
        return 0
    fi
    echo "rollback: restart at $prev failed; manual intervention needed" >&2
    return 1
}

cmd_rollback() {
    local svc="$SERVICE"
    while [ $# -gt 0 ]; do
        case "$1" in
            --service) svc="${2:?--service needs a name}"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    require_service "$svc"
    rollback_internal "$svc" || exit 1
}

auto_revert() {
    # $1 = svc, $2 = failure summary
    local svc="$1" why="$2" rolled
    echo "switch: $why — rolling back automatically" >&2
    if rollback_internal "$svc"; then
        rolled="$(target_of "$CURRENT")"
        die "switch failed ($why); ROLLED BACK to $rolled — investigate: $(receipt_hint "$svc")"
    fi
    die "switch failed ($why) AND rollback failed — manual recovery: point $CURRENT at a known-good deploy dir, then restart ${svc:-<service>}"
}

cmd_switch() {
    local ref="${1:?usage: switch <ref> [--service NAME] [--skip-verify] [--allow-unconfirmed]}"
    shift || true
    local svc="$SERVICE" skip_verify=0 allow_unconfirmed=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --service) svc="${2:?--service needs a name}"; shift 2 ;;
            --skip-verify) skip_verify=1; shift ;;
            --allow-unconfirmed) allow_unconfirmed=1; shift ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    require_service "$svc"
    fetch_refs
    local sha dest
    sha="$(resolve "$ref")"

    # Gate 1: the suite at the target must pass BEFORE anything is touched.
    if [ "$skip_verify" -eq 1 ]; then
        echo "switch: WARNING — --skip-verify given; the suite was NOT run at $sha" >&2
    else
        echo "switch: verifying $sha before any change (tests must pass to proceed)"
        if ! verify_ref "$sha"; then
            echo "switch: tests FAILED at $sha" >&2
            echo "switch: NOT switching — 'current' is unchanged ($(target_of "$CURRENT"))" >&2
            exit 1
        fi
        echo "switch: verify OK at $sha"
    fi

    dest="$ROOT/deploy-$sha"
    mkdir -p "$ROOT"
    if [ ! -d "$dest" ]; then
        git -C "$REPO" worktree prune
        git -C "$REPO" worktree add --quiet --detach "$dest" "$sha"
    fi
    if [ -L "$CURRENT" ]; then
        # Record the outgoing deploy for rollback. Never let this bookkeeping
        # break the switch (round-2 F1): a DANGLING current canonicalises to
        # empty and the old `ln -sfn ""` aborted under set -e.
        prev_target="$(readlink -f "$CURRENT" 2>/dev/null || true)"
        if [ -n "$prev_target" ] && [ "$prev_target" = "$dest" ]; then
            # Re-switching to the revision already deployed: aliasing previous
            # to current would make `rollback` a no-op with a healthy-looking
            # receipt (review F6) — keep the existing previous.
            echo "switch: re-switching to the deployed revision; previous unchanged ($(target_of "$PREVIOUS"))" >&2
        elif [ -n "$prev_target" ] && [ -d "$prev_target" ]; then
            ln -sfn "$prev_target" "$PREVIOUS"
        elif prev_recorded="$(readlink -f "$PREVIOUS" 2>/dev/null || true)"; \
                [ -n "$prev_recorded" ] && [ -d "$prev_recorded" ]; then
            # Current is dangling but a usable previous already exists (review
            # N1, round 3): "not recorded" was wrong for this state — rollback
            # still works. Leave previous alone.
            echo "switch: current is dangling; previous left unchanged ($(target_of "$PREVIOUS"))" >&2
        else
            echo "switch: current is dangling (${prev_target:-<unresolvable>}); previous not recorded" >&2
        fi
    fi
    # Durable pre-switch record (review F8): the audit/recovery line is written
    # before anything mutates and never depends on archaeology.
    printf '%s switch %s (from %s)\n' "$(date -u +%FT%TZ)" "$sha" "$(target_of "$CURRENT")" \
        >> "$ROOT/switch.log" 2>/dev/null || true
    ln -sfn "$dest" "$CURRENT"
    echo "switch: current -> $dest ($sha)"
    echo "switch: previous -> $(target_of "$PREVIOUS")"

    # Gate 2: the restart itself. A bare `systemctl restart` used to die under
    # `set -e` BEFORE the check below could run — `current` repointed, no
    # rollback, the exact outcome this script exists to prevent (review F1).
    ts=$(date +%s)
    if ! service_start "$svc"; then
        auto_revert "$svc" "service failed to restart"
    fi
    sleep "$SETTLE_SECONDS"
    if ! service_active "$svc"; then
        auto_revert "$svc" "service failed to restart"
    fi

    # Gate 3: the transport must come up on the channel AFTER this restart, on the
    # TARGET revision. The window is anchored to the restart's timestamp and the
    # receipt is actually verified — a receipt that is missing, stale, or carries
    # a mismatched revision does not pass (round-3 findings 1+2).
    #
    # Issue #70: the receipt is now the dedicated MACHINE line
    #
    #     A2A_ONLINE_NOTICE rev=<full-sha> alias=<alias> chat=<chat-id>
    #
    # emitted by AgentChannel.start() (swarmly/telegram/channel.py) directly, alongside
    # the unchanged human-facing HELLO notice.
    #
    # Why the payload-substring selector below was abandoned rather than patched a
    # fifth time. It used to match this transport's OWN notice as
    #   swarmly.telegram: outbound to chat ...: ['"][A2A v1.1] HELLO to=* intent=ONLINE rev=<sha>
    # and that class of selector failed four review rounds in a row (PR #61 r4-r7),
    # each time because channel.py logs peer message TEXT verbatim, so ANY line
    # whose text quotes the notice satisfies a substring match. r4 selected the
    # rev-LESS connect line (every healthy switch auto-reverted); r5 matched an
    # inbound peer message merely citing a rev; r6/r7 were outflanked by messages
    # quoting the whole anchored line. The lesson is not that the anchors were too
    # loose — it is that a substring over a *payload a peer can echo* is spoofable
    # by construction. A fixed literal emitted by exactly one call site removes the
    # guesswork about a log format, though a peer can still quote it in message
    # text; that residue is what the 'inbound from' exclusion below covers.
    #
    # Two consequences, both deliberate:
    #   * the 'swarmly.telegram: ' logger-name anchor is GONE. It was coupled to this
    #     process's own logging format, so a host whose supervisor REFORMATS its log
    #     would lose the anchor even though the receipt was present (reachable on
    #     the non-systemd host shapes #84 opened up).
    #   * the 'inbound from' exclusion STAYS, and is still load-bearing: it is not
    #     decoration. With the logger anchor gone, a peer message whose text quotes
    #     the new literal would otherwise match — and the swarm does quote log lines
    #     to each other, this receipt included.
    #
    # The selector matches the literal ALONE — no hex requirement. That is load
    # bearing, not laxity: `_git_head()` in channel.py degrades to the literal
    # `rev=unknown` when git cannot be read, and a receipt line is a line the
    # transport DID emit. Requiring `[0-9a-f]{7,40}` would filter that line out
    # and report a failed attestation as no receipt at all, and it would make the
    # "no rev field" arm below unreachable. The `sed` that follows decides, so the
    # three shapes stay distinct: receipt+rev (verify), receipt with `rev=unknown`
    # or no rev (unverifiable), and no receipt line at all (nothing emitted).
    #
    # A rev-less receipt is deliberately NOT a fallback. Gate 3 used to fall back to
    # the rev-less `connected as @` connect line, which only ever produced the
    # "no rev field" failure below; accepting the receipt's own literal without its
    # revision would have made `--allow-unconfirmed` the only arm ever reached and
    # quietly weakened the gate. The receipt, or nothing.
    #
    # Issue #84: the window is read through UPGRADE_SERVICE_RECEIPT (default
    # `journalctl -u "$svc" --since "@$ts"`), so a host whose supervisor keeps no
    # journal wires the same gate to its own log. #84 changed only WHERE the gate
    # reads, never WHAT it accepts: the selector still validates the TEXT of the
    # receipt, so a receipt hook cannot buy a passing revision it did not report.
    #
    # Issue #101: the selector is anchored to the emitter's own suffix, CR-safely.
    # The masking class it closes: `grep -F 'A2A_ONLINE_NOTICE' | tail -1` lets
    # ANY later line quoting the token outrank the genuine receipt — our OWN
    # outbound lines ('outbound to chat ...', logged verbatim by channel.py)
    # included. A reply quoting the TARGET sha in the receipt's shape then rode
    # `tail -1` through the prefix-compare while the transport was still
    # reporting the OLD rev: a failed switch masked as a clean one. The
    # `-v 'outbound to chat'` exclusion removes that class; `-v 'inbound from'`
    # above removes the peer-echo class.
    #
    # The end-anchor `chat=-?[0-9]+$` is what makes "the receipt shape" mean the
    # emitter's own line: channel.py logs `A2A_ONLINE_NOTICE rev=%s alias=%s
    # chat=%s` with the chat id LAST, so a mid-payload quote no longer matches.
    #
    # CRLF trap (measured, #101): hosts whose log consumer terminates records
    # `\r\n` exist, and in GNU ERE `\r` is a literal `r` — `chat=-?[0-9]+$`
    # does NOT match `chat=-100\r`. Anchoring without `tr -d '\r'` therefore
    # auto-reverted every healthy switch whose receipt arrived CRLF-terminated.
    # The strip must run BEFORE the match (the rev extraction below then sees
    # clean bytes too).
    #
    # #84 trade, stated honestly: the 'swarmly.telegram: ' logger-name anchor stays
    # gone (it was coupled to our own logging format), so the selector formally
    # rests on the emitter's suffix instead. A supervisor that WRAPS or
    # reformats the line (splitting the receipt, or appending fields after the
    # chat id) still breaks matching — and the gate then fails closed, the
    # direction #101 chose over accepting a quoted receipt.
    notice_line=""; notice_rev=""
    deadline=$(( $(date +%s) + NOTICE_TIMEOUT ))
    while :; do
        window="$(service_receipt "$svc" "$ts" 2>/dev/null || true)"
        notice_line="$(printf '%s\n' "$window" \
            | tr -d '\r' \
            | grep -v 'inbound from' \
            | grep -v 'outbound to chat' \
            | grep -E 'A2A_ONLINE_NOTICE rev=[^ ]+ alias=.* chat=-?[0-9]+$' \
            | tail -1 || true)"
        if [ -n "$notice_line" ]; then
            notice_rev="$(printf '%s\n' "$notice_line" \
                | sed -n 's/.*rev[:=][[:space:]]*\([0-9a-fA-F]\{7,40\}\).*/\1/p' | head -1 | tr 'A-F' 'a-f')"
            [ -n "$notice_rev" ] && break
        fi
        [ "$(date +%s)" -ge "$deadline" ] && break
        sleep 1
    done
    if [ -z "$notice_line" ]; then
        if [ "$allow_unconfirmed" -eq 1 ]; then
            echo "switch: WARNING — no 'A2A_ONLINE_NOTICE rev=<sha>' receipt within ${NOTICE_TIMEOUT}s; accepted (--allow-unconfirmed)" >&2
        else
            auto_revert "$svc" "no 'A2A_ONLINE_NOTICE rev=<sha>' receipt within ${NOTICE_TIMEOUT}s"
        fi
    else
        if [ -z "$notice_rev" ]; then
            # Review round 3 (finding 1): the docs name `rev:` as the receipt, so
            # a notice WITHOUT one fails the gate — silence is not evidence.
            if [ "$allow_unconfirmed" -eq 1 ]; then
                echo "switch: WARNING — notice carries no rev field; accepted (--allow-unconfirmed)" >&2
            else
                auto_revert "$svc" "notice carries no rev field (revision unverifiable)"
            fi
        else
            # Review round 3 (finding 2): the comparison is a PREFIX relation
            # against the FULL target sha — truncating both sides to 7 chars
            # accepted any revision sharing the prefix.
            case "$sha" in
                "$notice_rev"*) : ;;
                *)
                    if [ "$allow_unconfirmed" -eq 1 ]; then
                        echo "switch: WARNING — notice reports rev=${notice_rev}, target is ${sha}; accepted (--allow-unconfirmed)" >&2
                    else
                        auto_revert "$svc" "notice reports rev=${notice_rev}, target is ${sha}"
                    fi ;;
            esac
        fi
    fi
    if [ "$skip_verify" -eq 1 ]; then
        # Review F7: the audit line must distinguish an unverified switch.
        echo "switch: $svc active at $sha (UNVERIFIED — --skip-verify)"
    else
        echo "switch: $svc active at $sha"
    fi
}

cmd_status() {
    echo "repo:     $REPO"
    # Bound-check the defaults: `status` used to print a plausible-looking path
    # and exit 0 on a host where that path does not exist (issue #84).
    if [ ! -d "$REPO/.git" ] && [ ! -f "$REPO/.git" ]; then
        echo "          WARNING: not a git checkout — set UPGRADE_REPO" >&2
    fi
    if [ ! -d "$ROOT" ]; then
        echo "          WARNING: deploy root does not exist — set UPGRADE_DEPLOY_ROOT" >&2
    fi
    echo "current:  $(target_of "$CURRENT")"
    echo "previous: $(target_of "$PREVIOUS")"
    if [ "$LIFECYCLE_OVERRIDDEN" -eq 1 ]; then
        echo "service:  ${SERVICE:-<host-managed>} (host lifecycle hooks)"
        if service_active "$SERVICE"; then echo "active"; else echo "inactive"; fi
    elif [ -n "$SERVICE" ]; then
        service_active "$SERVICE" && echo "active" || echo "inactive"
    else
        echo "service:  (none — set UPGRADE_SERVICE or the lifecycle hooks)"
    fi
}

case "${1:-}" in
    verify|switch|rollback) acquire_lock ;;
esac
case "${1:-}" in
    verify)   shift; cmd_verify "$@" ;;
    switch)   shift; cmd_switch "$@" ;;
    rollback) shift; cmd_rollback "$@" ;;
    status)   cmd_status ;;
    *) die "usage: upgrade_transport.sh {verify <ref> | switch <ref> [--service NAME] [--skip-verify] [--allow-unconfirmed] | rollback [--service NAME] | status}

  --service / UPGRADE_SERVICE is optional when UPGRADE_SERVICE_START,
  UPGRADE_SERVICE_IS_ACTIVE and UPGRADE_SERVICE_RECEIPT are all set
  (non-systemd hosts: see issue #84 and docs/telegram/UPGRADE.md)." ;;
esac
