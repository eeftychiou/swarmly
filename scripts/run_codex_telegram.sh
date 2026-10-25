#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 CONFIG [ENV_FILE]" >&2
  exit 2
fi

CONFIG="$1"
ENV_FILE="${2:-}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_EXE="${A2A_PYTHON:-$REPO_DIR/.venv/bin/python}"

if [[ ! -f "$CONFIG" ]]; then
  echo "Codex Telegram config not found: $CONFIG" >&2
  exit 2
fi
if [[ ! -x "$PYTHON_EXE" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_EXE="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_EXE="$(command -v python)"
  else
    echo "Python executable not found; create .venv or set A2A_PYTHON" >&2
    exit 2
  fi
fi

if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Same PATH pin as the PowerShell twin: the daemon spawns the Codex CLI by name.
if ! command -v codex >/dev/null 2>&1; then
  home_dir="${HOME:-}"
  for candidate in "${CODEX_HOME:-$home_dir/.codex}/bin/codex" "$home_dir/.local/bin/codex" \
                   "/usr/local/bin/codex" "/opt/homebrew/bin/codex"; do
    if [[ -x "$candidate" ]]; then
      PATH="$(dirname "$candidate"):$PATH"
      export PATH
      break
    fi
  done
fi
if command -v codex >/dev/null 2>&1; then
  echo "codex CLI       : $(command -v codex)"
else
  echo "warning: codex CLI not found on PATH - codex/codex_thread adapters cannot run" >&2
fi

"$PYTHON_EXE" -m swarmly.telegram.cli --config "$CONFIG" check
exec "$PYTHON_EXE" -m swarmly.telegram.cli --config "$CONFIG" run
