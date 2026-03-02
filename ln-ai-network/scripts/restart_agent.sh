#!/usr/bin/env bash
set -euo pipefail

ROOT="${HOME}/lightning-network-ai-agents/ln-ai-network"
RUNTIME="${ROOT}/runtime/agent"
LOG="${RUNTIME}/agent.log"

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
  LLM_BACKEND=ollama|openai|...
  PYTHON=/path/to/python

Examples:
  scripts/restart_agent.sh
  scripts/restart_agent.sh fresh
  LLM_BACKEND=ollama scripts/restart_agent.sh fresh
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

if [[ "${MODE}" == "fresh" ]]; then
  ts="$(date +%Y%m%d_%H%M%S)"
  echo "[restart_agent] archiving + clearing inbox/outbox/cursor (ts=${ts})"

  [[ -f "${RUNTIME}/inbox.jsonl"  ]] && cp -f "${RUNTIME}/inbox.jsonl"  "${RUNTIME}/archive/inbox.${ts}.jsonl"  || true
  [[ -f "${RUNTIME}/outbox.jsonl" ]] && cp -f "${RUNTIME}/outbox.jsonl" "${RUNTIME}/archive/outbox.${ts}.jsonl" || true

  : > "${RUNTIME}/inbox.jsonl"
  : > "${RUNTIME}/outbox.jsonl"

  # reset cursor/state so truncation never wedges read_new()
  rm -f "${RUNTIME}/inbox.offset" "${RUNTIME}/agent.lock" || true
fi

echo "[restart_agent] stopping existing agent (if any)..."
pkill -f "python -m ai\.agent" || true
pkill -f "ai/agent\.py" || true
sleep 0.3

echo "[restart_agent] removing stale lock..."
rm -f "${RUNTIME}/agent.lock" || true

echo "[restart_agent] starting agent (nohup)..."
nohup env ALLOW_LLM="${ALLOW_LLM}" LLM_BACKEND="${LLM_BACKEND}" "${PY}" -m ai.agent > "${LOG}" 2>&1 &
AGENT_PID="$!"

echo "[restart_agent] started pid=${AGENT_PID}"

# Give it a moment to crash if it’s going to crash
sleep 0.8

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