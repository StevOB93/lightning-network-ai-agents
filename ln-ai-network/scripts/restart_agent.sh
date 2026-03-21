#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME="${ROOT}/runtime/agent"
LOG="${RUNTIME}/agent.log"

# Source env.sh to pick up ALLOW_LLM, LLM_BACKEND, etc. from .env
if [[ -f "$ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/env.sh"
fi

ALLOW_LLM="${ALLOW_LLM:-1}"
LLM_BACKEND="${LLM_BACKEND:-ollama}"
PY="${PYTHON:-${ROOT}/.venv/bin/python}"

MODE="${1:-}"  # optional: "fresh" to archive+clear inbox/outbox/cursor

usage() {
  cat <<'USAGE'
Usage:
  scripts/restart_agent.sh            # restart agent (keep inbox/outbox)
  scripts/restart_agent.sh fresh      # archive + clear inbox/outbox + reset cursor, then restart

Environment overrides:
  ALLOW_LLM=1|0
  LLM_BACKEND=ollama|openai|gemini
  PYTHON=/path/to/python

Examples:
  scripts/restart_agent.sh
  scripts/restart_agent.sh fresh
  LLM_BACKEND=openai scripts/restart_agent.sh fresh
USAGE
}

if [[ "${MODE}" == "-h" || "${MODE}" == "--help" ]]; then
  usage
  exit 0
fi

cd "${ROOT}"

mkdir -p "${RUNTIME}"
mkdir -p "${RUNTIME}/archive"

echo "[restart_agent] repo: ${ROOT}"
echo "[restart_agent] python: ${PY}"
echo "[restart_agent] ALLOW_LLM=${ALLOW_LLM} LLM_BACKEND=${LLM_BACKEND}"
echo "[restart_agent] mode: ${MODE:-restart}"

# ---------------------------------------------------------------------------
# STOP EXISTING AGENT
# ---------------------------------------------------------------------------

echo "[restart_agent] stopping existing agent (if any)..."

# Preferred: kill by PID from the pid file (same approach as shutdown.sh)
AGENT_PID_FILE="${RUNTIME}/agent.pid"
KILLED_BY_PID=0

if [[ -f "${AGENT_PID_FILE}" ]]; then
  EXISTING_PID="$(cat "${AGENT_PID_FILE}" || true)"
  if [[ -n "${EXISTING_PID}" ]] && kill -0 "${EXISTING_PID}" >/dev/null 2>&1; then
    echo "[restart_agent] sending SIGTERM to PID ${EXISTING_PID}..."
    kill "${EXISTING_PID}" || true
    # Wait up to 5s for clean shutdown
    for _i in 1 2 3 4 5; do
      if ! kill -0 "${EXISTING_PID}" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    # If still alive after 5s, force-kill
    if kill -0 "${EXISTING_PID}" >/dev/null 2>&1; then
      echo "[restart_agent] process did not exit in 5s, sending SIGKILL..."
      kill -9 "${EXISTING_PID}" || true
      sleep 0.5
    fi
    KILLED_BY_PID=1
  else
    echo "[restart_agent] PID ${EXISTING_PID} not running."
  fi
  rm -f "${AGENT_PID_FILE}"
fi

# Fallback: pkill on the process pattern in case the pid file was stale/missing
if [[ "${KILLED_BY_PID}" == "0" ]]; then
  pkill -f "python.*-m.*ai\.pipeline" || true
  sleep 0.5
fi

echo "[restart_agent] removing stale lock (if any)..."
rm -f "${RUNTIME}/pipeline.lock" || true

# ---------------------------------------------------------------------------
# OPTIONAL: FRESH MODE — archive + clear queue state
# ---------------------------------------------------------------------------

if [[ "${MODE}" == "fresh" ]]; then
  ts="$(date +%Y%m%d_%H%M%S)"
  echo "[restart_agent] archiving + clearing inbox/outbox/cursor (ts=${ts})"

  [[ -f "${RUNTIME}/inbox.jsonl"  ]] && cp -f "${RUNTIME}/inbox.jsonl"  "${RUNTIME}/archive/inbox.${ts}.jsonl"  || true
  [[ -f "${RUNTIME}/outbox.jsonl" ]] && cp -f "${RUNTIME}/outbox.jsonl" "${RUNTIME}/archive/outbox.${ts}.jsonl" || true

  : > "${RUNTIME}/inbox.jsonl"
  : > "${RUNTIME}/outbox.jsonl"

  # reset cursor so read_new() starts from the beginning of the cleared file
  rm -f "${RUNTIME}/inbox.offset" || true
fi

# ---------------------------------------------------------------------------
# START AGENT
# ---------------------------------------------------------------------------

echo "[restart_agent] starting agent (nohup)..."
nohup env ALLOW_LLM="${ALLOW_LLM}" LLM_BACKEND="${LLM_BACKEND}" \
  "${PY}" -u -m ai.pipeline > "${LOG}" 2>&1 &
AGENT_PID="$!"
echo "${AGENT_PID}" > "${AGENT_PID_FILE}"

echo "[restart_agent] started pid=${AGENT_PID}"

# Give it a moment to crash if it's going to crash
sleep 1

echo "[restart_agent] verify process:"
if ps -p "${AGENT_PID}" -o pid,cmd,etime >/dev/null 2>&1; then
  ps -p "${AGENT_PID}" -o pid,cmd,etime
  echo "[restart_agent] OK: agent is running"
else
  echo "[restart_agent] ERROR: agent exited immediately."
  echo "[restart_agent] last 200 log lines:"
  tail -n 200 "${LOG}" || true
  exit 1
fi

echo "[restart_agent] last log lines:"
tail -n 30 "${LOG}" || true

echo "[restart_agent] done."
