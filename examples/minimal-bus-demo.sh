#!/usr/bin/env bash
# Minimal A2A durable-bus demo - no Telegram, no tokens, no network.
#
# A throwaway clone of this repo acts as the bus. "alice" posts a typed
# message for "bob" through the real CLI, and the demo shows the resulting
# envelope file plus the inbox view. Everything runs in a temp dir; nothing
# outside it is touched.
#
# Usage:  examples/minimal-bus-demo.sh [demo-dir]
#         A2A_REPO=/path/to/checkout examples/minimal-bus-demo.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${A2A_REPO:-$(cd "$HERE/.." && pwd)}"
DEMO_DIR="${1:-$(mktemp -d /tmp/a2a-demo.XXXXXX)}"
BUS="$DEMO_DIR/bus"
PY="${PYTHON:-python3}"

# Windows consoles/pipes default to the ANSI code page (cp1252); the CLI prints check
# marks, so force UTF-8 I/O for every child process (found in review, PR #74).
export PYTHONIOENCODING=utf-8

# A throwaway signing key for "alice", for this demo only. Real agents keep theirs in a
# private .env (swarmly-keygen --agent alice --env-file <.env> --registry <registry>); nothing
# is ever signed without one.
export A2A_KEY_ALICE="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"

# A throwaway registry that knows alice, so her message verifies. Real deployments register
# agents in config/a2a_config.json of the bus checkout.
export A2A_CONFIG="$(mktemp /tmp/a2a-registry.XXXXXX)"
printf '{"authorized_agents": {"alice": {"public_key_or_secret_name": "A2A_KEY_ALICE"}}}\n' \
  > "$A2A_CONFIG"

echo "repo: $REPO"
echo "demo: $DEMO_DIR"

# 1. A bus is just a git repo with .agents/inbox/ - here, a throwaway clone.
git clone -q "$REPO" "$BUS"
cd "$BUS"

# 2. alice posts a typed message for bob (the CLI commits it to the bus).
"$PY" scripts/a2a_bus.py send --sender alice --recipient bob \
  --intent PR_REVIEW_REQUEST \
  --text "PR #7 ready for review; see the diff."

# 3. The envelope is now a committed file in the bus inbox.
NEWEST="$(ls -t .agents/inbox/*.md | head -1)"
echo
echo "--- newest envelope: $NEWEST ---"
head -16 "$NEWEST"

# 4. The inbox view is what a poller sees on its next tick.
echo
echo "--- inbox (alice verifies with the demo key in A2A_KEY_ALICE) ---"
OUT="$(mktemp)"
if ! "$PY" scripts/a2a_bus.py inbox > "$OUT" 2>&1; then
  echo "FAIL: inbox command failed:"; cat "$OUT"; rm -f "$OUT"; exit 1
fi
grep -A2 "@alice" "$OUT" || { echo "FAIL: alice's message not found in the inbox"; rm -f "$OUT"; exit 1; }
rm -f "$OUT"

echo
echo "Done. In a real deployment each host runs, on a schedule:"
echo "  $PY -m swarmly.telegram.cli --config config.yaml bus-poll --once"
echo "See docs/GETTING-STARTED.md and docs/ADOPT.md."
