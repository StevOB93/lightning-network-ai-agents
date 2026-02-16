#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$PROJECT_ROOT/env.sh"

echo "=================================================="
echo "AGENT SHUTDOWN"
echo "=================================================="

AGENT_PID_FILE="$RUNTIME_DIR/agent.pid"

if [ -f "$AGENT_PID_FILE" ]; then
    AGENT_PID=$(cat "$AGENT_PID_FILE" || true)

    if [ -n "$AGENT_PID" ] && kill -0 "$AGENT_PID" >/dev/null 2>&1; then
        echo "[AGENT] Stopping agent (PID $AGENT_PID)..."
        kill "$AGENT_PID"
        wait "$AGENT_PID" 2>/dev/null || true
    fi

    rm -f "$AGENT_PID_FILE"
else
    echo "[AGENT] No agent pid file found."
fi

echo "[AGENT] Agent shutdown complete."
