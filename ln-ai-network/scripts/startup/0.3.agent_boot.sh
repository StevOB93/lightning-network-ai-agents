#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# INTERNAL: called by scripts/1.start.sh — do not run directly
###############################################################################
if [[ "${LN_AI_INTERNAL_CALL:-0}" != "1" ]]; then
  echo "[FATAL] This script is internal. Run: ./scripts/1.start.sh"
  exit 2
fi

###############################################################################
# PATH RESOLUTION
###############################################################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -f "$PROJECT_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env.sh"
else
  echo "[FATAL] env.sh not found at $PROJECT_ROOT/env.sh"
  exit 1
fi

echo "=================================================="
echo "AGENT BOOT"
echo "=================================================="

: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"

# Optional arg: explicit python path (passed by 1.start.sh)
VENV_PY="${1:-$PROJECT_ROOT/.venv/bin/python}"

AGENT_LOG="$RUNTIME_DIR/agent.log"
AGENT_PID_FILE="$RUNTIME_DIR/agent.pid"

mkdir -p "$RUNTIME_DIR"

###############################################################################
# PREVENT DUPLICATE AGENT
###############################################################################
if [[ -f "$AGENT_PID_FILE" ]]; then
  EXISTING_PID="$(cat "$AGENT_PID_FILE" || true)"
  if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" >/dev/null 2>&1; then
    echo "[AGENT] Agent already running (PID $EXISTING_PID)."
    exit 0
  fi
fi

###############################################################################
# VERIFY PYTHON
###############################################################################
if [[ ! -x "$VENV_PY" ]]; then
  echo "[FATAL] Python executable not found/executable: $VENV_PY"
  echo "[HINT] Run: ./scripts/0.install.sh (creates venv) or ./scripts/1.start.sh (creates venv)"
  exit 127
fi

###############################################################################
# START PERSISTENT AGENT
###############################################################################
echo "[AGENT] Starting persistent AI agent..."
echo "[AGENT] Using python: $VENV_PY"
echo "[AGENT] Log file: $AGENT_LOG"

(
  cd "$PROJECT_ROOT"
  exec "$VENV_PY" -u -m ai.agent
) >"$AGENT_LOG" 2>&1 &

AGENT_PID=$!
echo "$AGENT_PID" > "$AGENT_PID_FILE"

sleep 2

if ! kill -0 "$AGENT_PID" >/dev/null 2>&1; then
  echo "[FATAL] Agent failed to start."
  tail -n 50 "$AGENT_LOG" || true
  exit 1
fi

echo "[AGENT] Agent running (PID $AGENT_PID)."
echo "[AGENT] Agent layer ready."