#!/usr/bin/env bash
# =============================================================================
# start_multi_agent.sh — Boot N pipeline instances for multi-agent mode
#
# Usage:
#   ./scripts/start_multi_agent.sh [NODE_COUNT]
#
# Each pipeline instance gets:
#   - Its own runtime directory: runtime/agent-{N}/
#   - NODE_NUMBER={N} environment variable
#   - MULTI_AGENT=1 flag
#   - Registration in the shared runtime/registry.jsonl
#
# Prerequisites:
#   - Bitcoin + Lightning nodes must already be running (scripts/1.start.sh)
#   - MCP server must be running
#   - Python venv must exist
#
# To stop: ./scripts/shutdown.sh (kills all agent PIDs)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source environment
if [[ -f "$PROJECT_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env.sh"
else
  echo "[FATAL] env.sh not found at $PROJECT_ROOT/env.sh"
  exit 1
fi

: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"

NODE_COUNT="${1:-${NODE_COUNT:-2}}"
VENV_PY="${PROJECT_ROOT}/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "[FATAL] Python executable not found: $VENV_PY"
  echo "[HINT]  Run ./scripts/0.install.sh first."
  exit 127
fi

echo "=============================================="
echo "  MULTI-AGENT BOOT  (${NODE_COUNT} agents)"
echo "=============================================="

# Clear stale registry entries
REGISTRY="$RUNTIME_DIR/registry.jsonl"
if [[ -f "$REGISTRY" ]]; then
  rm -f "$REGISTRY"
  echo "[MULTI] Cleared stale registry."
fi

PIDS=()
for N in $(seq 1 "$NODE_COUNT"); do
  AGENT_DIR="$RUNTIME_DIR/agent-${N}"
  AGENT_LOG="$AGENT_DIR/agent.log"
  AGENT_PID_FILE="$AGENT_DIR/agent.pid"

  mkdir -p "$AGENT_DIR"

  # Skip if already running
  if [[ -f "$AGENT_PID_FILE" ]]; then
    EXISTING_PID="$(cat "$AGENT_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
      echo "[AGENT-${N}] Already running (PID $EXISTING_PID). Skipping."
      PIDS+=("$EXISTING_PID")
      continue
    fi
  fi

  echo "[AGENT-${N}] Starting pipeline (agent-${N})..."

  (
    cd "$PROJECT_ROOT"
    export NODE_NUMBER="$N"
    export MULTI_AGENT=1
    export NODE_COUNT="$NODE_COUNT"
    exec "$VENV_PY" -u -m ai.pipeline
  ) >"$AGENT_LOG" 2>&1 &

  PID=$!
  echo "$PID" > "$AGENT_PID_FILE"
  PIDS+=("$PID")
  echo "[AGENT-${N}] Started (PID $PID). Log: $AGENT_LOG"
done

# Give them a moment to register
sleep 2

# Verify all agents are alive
FAILED=0
for N in $(seq 1 "$NODE_COUNT"); do
  AGENT_PID_FILE="$RUNTIME_DIR/agent-${N}/agent.pid"
  if [[ -f "$AGENT_PID_FILE" ]]; then
    PID="$(cat "$AGENT_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      echo "[AGENT-${N}] Running (PID $PID)."
    else
      echo "[AGENT-${N}] FAILED to start. Check: $RUNTIME_DIR/agent-${N}/agent.log"
      FAILED=1
    fi
  fi
done

if [[ "$FAILED" -eq 1 ]]; then
  echo ""
  echo "[WARN] Some agents failed to start. Check logs above."
  exit 1
fi

echo ""
echo "=============================================="
echo "  All ${NODE_COUNT} agents running."
echo "  Registry: $REGISTRY"
echo "=============================================="
