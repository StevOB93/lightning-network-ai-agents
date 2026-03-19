#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# LN-AI FULL SYSTEM START (deterministic + logged)
#
# Usage:
#   ./scripts/1.start.sh [NODE_COUNT]
#
# Examples:
#   ./scripts/1.start.sh 2
#   REINSTALL_PY_DEPS=1 ./scripts/1.start.sh 3
#
# Logs:
#   logs/system/start.log
#   logs/system/0.1.infra_boot.log
#   logs/system/0.2.control_plane_boot.log
#   logs/system/0.3.agent_boot.log
#   logs/system/0.4.ui_server.log
###############################################################################

usage() {
  cat <<'EOF'
LN-AI FULL SYSTEM START

Usage:
  ./scripts/1.start.sh [NODE_COUNT]

Environment:
  REINSTALL_PY_DEPS=1   Reinstall Python deps (pip install -r requirements.txt)

Notes:
  - Run from ln-ai-network/ (this script resolves PROJECT_ROOT automatically).
  - Per-step logs land in: logs/system/
EOF
}

# This file lives in: <repo>/scripts/1.start.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

NODE_COUNT="${1:-2}"

# Validate NODE_COUNT is a positive integer
if ! [[ "$NODE_COUNT" =~ ^[0-9]+$ ]] || [[ "$NODE_COUNT" -lt 1 ]]; then
  echo "[FATAL] NODE_COUNT must be a positive integer. Got: '$NODE_COUNT'"
  echo
  usage
  exit 2
fi

LOG_DIR="$PROJECT_ROOT/logs/system"
mkdir -p "$LOG_DIR"

START_LOG="$LOG_DIR/start.log"
exec > >(tee -a "$START_LOG") 2>&1

echo "=================================================="
echo "LN-AI FULL SYSTEM START"
echo "Project: $PROJECT_ROOT"
echo "Nodes:   $NODE_COUNT"
echo "Log:     $START_LOG"
echo "=================================================="

###############################################################################
# Load deterministic env (and optional local .env)
###############################################################################
if [[ -f "$PROJECT_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env.sh"
else
  echo "[FATAL] env.sh not found at $PROJECT_ROOT/env.sh"
  exit 1
fi

# Safe provider banner (no secrets)
echo "[INFO] LLM_PROVIDER=${LLM_PROVIDER:-openai}"
if [[ "${LLM_PROVIDER:-openai}" == "ollama" ]]; then
  echo "[INFO] OLLAMA_MODEL=${OLLAMA_MODEL:-}"
  echo "[INFO] OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-}"
fi

###############################################################################
# Python environment bootstrap (use venv python explicitly)
###############################################################################
if ! command -v python3 >/dev/null 2>&1; then
  echo "[FATAL] python3 not found. Install: sudo apt-get update && sudo apt-get install -y python3 python3-venv"
  exit 127
fi

VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[SETUP] Creating virtual environment at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

echo "[SETUP] Using python: $VENV_PY"
if [[ ! -x "$VENV_PY" ]]; then
  echo "[FATAL] venv python not found/executable at $VENV_PY"
  exit 127
fi

###############################################################################
# Python deps policy
# - By default: do NOT reinstall every run
# - If LLM_PROVIDER=ollama: ensure 'requests' exists (required by ollama adapter)
###############################################################################
install_requirements_if_present() {
  if [[ -f "$PROJECT_ROOT/requirements.txt" ]]; then
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet -r "$PROJECT_ROOT/requirements.txt"
    return 0
  fi
  return 1
}

if [[ "${REINSTALL_PY_DEPS:-0}" == "1" ]]; then
  echo "[SETUP] Reinstalling Python dependencies (REINSTALL_PY_DEPS=1)..."
  if ! install_requirements_if_present; then
    echo "[WARN] requirements.txt not found at $PROJECT_ROOT/requirements.txt; skipping."
  fi
else
  echo "[SETUP] Skipping pip install (set REINSTALL_PY_DEPS=1 to force)."

  if [[ "${LLM_PROVIDER:-openai}" == "ollama" ]]; then
    if ! "$VENV_PY" -c "import requests" >/dev/null 2>&1; then
      echo "[SETUP] Missing Python module 'requests' (required for Ollama). Installing..."
      if ! install_requirements_if_present; then
        "$VENV_PY" -m pip install --quiet --upgrade pip
        "$VENV_PY" -m pip install --quiet requests
      fi
    fi
  fi
fi

###############################################################################
# Helpers: run steps with per-step log files
###############################################################################
run_step() {
  local step_name="$1"
  local step_path="$2"
  shift 2

  local step_log="$LOG_DIR/$step_name.log"

  echo "--------------------------------------------------"
  echo "[STEP] $step_name"
  echo "[STEP] Script: $step_path"
  echo "[STEP] Log:    $step_log"
  echo "--------------------------------------------------"

  if [[ ! -f "$step_path" ]]; then
    echo "[FATAL] Missing script: $step_path"
    echo "[INFO] Available startup scripts under $PROJECT_ROOT/scripts/startup:"
    find "$PROJECT_ROOT/scripts/startup" -maxdepth 2 -type f -name "*.sh" 2>/dev/null | sed 's|^|  - |' || true
    exit 127
  fi

  # Truncate log for a clean run
  : > "$step_log"

  # Run the step with stdout/stderr redirected to a FILE (not a pipe)
  # Mark as internal to prevent accidental direct execution of sub-scripts.
  LN_AI_INTERNAL_CALL=1 bash "$step_path" "$@" >"$step_log" 2>&1 &
  local step_pid=$!

  # Stream the file live while the step runs (no pipe inheritance issues)
  if tail --help 2>/dev/null | grep -q -- "--pid"; then
    tail -n +1 -f --pid="$step_pid" "$step_log"
  else
    # Fallback if --pid isn't available
    tail -n +1 -f "$step_log" &
    local tail_pid=$!
    wait "$step_pid" || true
    kill "$tail_pid" 2>/dev/null || true
  fi

  # Propagate step exit code
  wait "$step_pid"
}

###############################################################################
# Startup sequence
###############################################################################
run_step "0.1.infra_boot"         "$PROJECT_ROOT/scripts/startup/0.1.infra_boot.sh"         "$NODE_COUNT"
run_step "0.2.control_plane_boot" "$PROJECT_ROOT/scripts/startup/0.2.control_plane_boot.sh"
run_step "0.3.agent_boot"         "$PROJECT_ROOT/scripts/startup/0.3.agent_boot.sh"         "$VENV_PY"
run_step "0.4.ui_server"          "$PROJECT_ROOT/scripts/startup/0.4.ui_server.sh"           "$VENV_PY"

UI_HOST="${UI_HOST:-127.0.0.1}"
UI_PORT="${UI_PORT:-8008}"

echo "=================================================="
echo "SYSTEM READY"
echo "Web UI:  http://$UI_HOST:$UI_PORT"
echo "Logs:    $LOG_DIR"
echo "=================================================="