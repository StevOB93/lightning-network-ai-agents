#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# LN-AI FULL SYSTEM START (deterministic + logged)
#
# Usage:
#   ./scripts/1.start.sh 2
#
# Logs:
#   logs/system/start.log
#   logs/system/0.1.infra_boot.log
#   logs/system/0.2.control_plane_boot.log
#   logs/system/0.3.agent_boot.log
###############################################################################

# This file lives in: <repo>/scripts/1.start.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_COUNT="${1:-2}"

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

if [[ -f "$PROJECT_ROOT/requirements.txt" ]]; then
  echo "[SETUP] Installing dependencies..."
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -r "$PROJECT_ROOT/requirements.txt"
else
  echo "[WARN] requirements.txt not found at repo root; skipping pip install."
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
  bash "$step_path" "$@" >"$step_log" 2>&1 &
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

echo "=================================================="
echo "SYSTEM READY"
echo "Logs: $LOG_DIR"
echo "=================================================="