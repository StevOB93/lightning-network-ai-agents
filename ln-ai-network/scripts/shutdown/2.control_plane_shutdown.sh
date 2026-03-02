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
echo "CONTROL PLANE SHUTDOWN"
echo "=================================================="

: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"

MCP_PID_FILE="$RUNTIME_DIR/mcp.pid"

if [[ -f "$MCP_PID_FILE" ]]; then
  MCP_PID="$(cat "$MCP_PID_FILE" || true)"

  if [[ -n "$MCP_PID" ]] && kill -0 "$MCP_PID" >/dev/null 2>&1; then
    echo "[CONTROL] Stopping MCP (PID $MCP_PID)..."
    kill "$MCP_PID" || true
    wait "$MCP_PID" 2>/dev/null || true
  fi

  rm -f "$MCP_PID_FILE"
else
  echo "[CONTROL] No MCP pid file found."
fi

echo "[CONTROL] Control plane shutdown complete."