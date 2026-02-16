#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$PROJECT_ROOT/env.sh"

echo "=================================================="
echo "CONTROL PLANE SHUTDOWN"
echo "=================================================="

MCP_PID_FILE="$RUNTIME_DIR/mcp.pid"

if [ -f "$MCP_PID_FILE" ]; then
    MCP_PID=$(cat "$MCP_PID_FILE" || true)

    if [ -n "$MCP_PID" ] && kill -0 "$MCP_PID" >/dev/null 2>&1; then
        echo "[CONTROL] Stopping MCP (PID $MCP_PID)..."
        kill "$MCP_PID"
        wait "$MCP_PID" 2>/dev/null || true
    fi

    rm -f "$MCP_PID_FILE"
else
    echo "[CONTROL] No MCP pid file found."
fi

echo "[CONTROL] Control plane shutdown complete."
