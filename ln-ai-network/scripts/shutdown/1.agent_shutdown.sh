#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# INTERNAL: called by scripts/shutdown.sh — do not run directly
###############################################################################
if [[ "${LN_AI_INTERNAL_CALL:-0}" != "1" ]]; then
  echo "[FATAL] This script is internal. Run: ./scripts/shutdown.sh"
  exit 2
fi

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
echo "AGENT SHUTDOWN"
echo "=================================================="

: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"

AGENT_PID_FILE="$RUNTIME_DIR/agent.pid"

if [[ -f "$AGENT_PID_FILE" ]]; then
  AGENT_PID="$(cat "$AGENT_PID_FILE" || true)"

  if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" >/dev/null 2>&1; then
    echo "[AGENT] Stopping agent (PID $AGENT_PID)..."
    kill "$AGENT_PID" || true
    wait "$AGENT_PID" 2>/dev/null || true
  fi

  rm -f "$AGENT_PID_FILE"
else
  echo "[AGENT] No agent pid file found."
fi

echo "[AGENT] Agent shutdown complete."