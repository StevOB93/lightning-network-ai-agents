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
LOCK_FILE="$RUNTIME_DIR/agent/pipeline.lock"

_kill_and_wait() {
  local pid="$1"

  echo "[AGENT] Sending SIGTERM to PID ${pid}..."
  kill "${pid}" || true

  # Poll until the process is actually gone (bash's `wait` only works for
  # child processes of the current shell; polling is reliable for any PID).
  local i
  for i in 1 2 3 4 5; do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      echo "[AGENT] Process ${pid} exited."
      return 0
    fi
    sleep 1
  done

  # Force-kill if still alive after 5 s
  if kill -0 "${pid}" >/dev/null 2>&1; then
    echo "[AGENT] Process ${pid} still alive after 5 s — sending SIGKILL..."
    kill -9 "${pid}" || true
    sleep 0.5
  fi
}

if [[ -f "$AGENT_PID_FILE" ]]; then
  AGENT_PID="$(cat "$AGENT_PID_FILE" || true)"

  if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" >/dev/null 2>&1; then
    _kill_and_wait "$AGENT_PID"
  else
    echo "[AGENT] PID ${AGENT_PID:-unknown} not running."
  fi

  rm -f "$AGENT_PID_FILE"
else
  echo "[AGENT] No agent pid file found — checking for process by name..."
  # Fallback: kill any stray ai.pipeline process (e.g. pid file was lost)
  pkill -f "python.*-m.*ai\.pipeline" || true
  sleep 1
fi

# Remove the lock file so the next boot doesn't see a stale lock.
# The Python StartupLock also handles stale locks, but removing it here
# is the cleanest guarantee that the next startup sees a fresh state.
if [[ -f "$LOCK_FILE" ]]; then
  echo "[AGENT] Removing lock file: $LOCK_FILE"
  rm -f "$LOCK_FILE"
fi

echo "[AGENT] Agent shutdown complete."
