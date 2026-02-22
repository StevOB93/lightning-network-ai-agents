#!/usr/bin/env bash

set -euo pipefail

###############################################################################
# PATH RESOLUTION
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$PROJECT_ROOT/env.sh"

echo "=================================================="
echo "AGENT BOOT"
echo "=================================================="

AGENT_LOG="$RUNTIME_DIR/agent.log"
AGENT_PID_FILE="$RUNTIME_DIR/agent.pid"

###############################################################################
# PREVENT DUPLICATE AGENT
###############################################################################

if [ -f "$AGENT_PID_FILE" ]; then
    EXISTING_PID=$(cat "$AGENT_PID_FILE" || true)
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" >/dev/null 2>&1; then
        echo "[AGENT] Agent already running (PID $EXISTING_PID)."
        exit 0
    fi
fi

###############################################################################
# START PERSISTENT AGENT
###############################################################################

echo "[AGENT] Starting persistent AI agent..."

(
    cd "$PROJECT_ROOT"
    exec "$PROJECT_ROOT/.venv/bin/python" -u -m ai.agent
) > "$AGENT_LOG" 2>&1 &

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
