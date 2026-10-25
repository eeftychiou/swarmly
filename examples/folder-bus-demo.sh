#!/usr/bin/env bash
# Folder-bus demo - the durable plane without git.
#
# A plain directory stands in for a folder that Google Drive for desktop, Dropbox,
# OneDrive or Syncthing keeps in sync between machines. "alice" posts a typed message
# for "bob" into it, and "bob" reads the inbox from the same folder. Nothing outside
# the temp dir is touched; no tokens, no network.
#
# Usage:  examples/folder-bus-demo.sh [shared-folder]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SHARED="${1:-$(mktemp -d /tmp/a2a-shared.XXXXXX)}"
PY="${PYTHON:-python3}"
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

echo "shared folder: $SHARED   (point a sync client at a folder like this one)"

# 1. alice writes an envelope into the shared folder.
"$PY" "$REPO/scripts/a2a_bus.py" --transport folder --folder "$SHARED" send \
  --sender alice --recipient bob --intent PR_REVIEW_REQUEST \
  --text "PR #7 ready for review; see the diff."

# 2. The envelope is an ordinary file; a sync client uploads it as-is.
echo
ls -1 "$SHARED/inbox"

# 3. bob, on another machine, reads the same folder.
echo
"$PY" "$REPO/scripts/a2a_bus.py" --transport folder --folder "$SHARED" inbox | grep -A2 "@alice"

echo
echo "Agents poll it with bus.transport: folder in their config.yaml; see docs/TRANSPORTS.md."
