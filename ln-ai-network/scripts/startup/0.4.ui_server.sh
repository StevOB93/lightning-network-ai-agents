#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# INTERNAL: called by scripts/1.start.sh — do not run directly
###############################################################################
if [[ "${LN_AI_INTERNAL_CALL:-0}" != "1" ]]; then
  echo "[FATAL] This script is internal. Run: ./scripts/1.start.sh"
  exit 2
fi

###############################################################################
# PATH RESOLUTION
###############################################################################
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
echo "UI SERVER BOOT"
echo "=================================================="

: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"

# Optional arg: explicit python path (passed by 1.start.sh)
VENV_PY="${1:-$PROJECT_ROOT/.venv/bin/python}"

UI_LOG="$RUNTIME_DIR/ui_server.log"
UI_PID_FILE="$RUNTIME_DIR/ui_server.pid"

mkdir -p "$RUNTIME_DIR"

###############################################################################
# PREVENT DUPLICATE UI SERVER
###############################################################################
if [[ -f "$UI_PID_FILE" ]]; then
  EXISTING_PID="$(cat "$UI_PID_FILE" || true)"
  if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" >/dev/null 2>&1; then
    echo "[UI] UI server already running (PID $EXISTING_PID)."
    exit 0
  fi
fi

###############################################################################
# VERIFY PYTHON
###############################################################################
if [[ ! -x "$VENV_PY" ]]; then
  echo "[FATAL] Python executable not found/executable: $VENV_PY"
  echo "[HINT] Run: ./scripts/0.install.sh (creates venv) or ./scripts/1.start.sh"
  exit 127
fi

###############################################################################
# RESOLVE HOST / PORT
###############################################################################
UI_HOST="${UI_HOST:-127.0.0.1}"
UI_PORT="${UI_PORT:-8008}"

###############################################################################
# START UI SERVER
###############################################################################
echo "[UI] Starting web UI server..."
echo "[UI] Using python: $VENV_PY"
echo "[UI] Log file:     $UI_LOG"
echo "[UI] URL:          http://$UI_HOST:$UI_PORT"

(
  cd "$PROJECT_ROOT"
  exec "$VENV_PY" -u -m scripts.ui_server
) >"$UI_LOG" 2>&1 &

UI_PID=$!
echo "$UI_PID" > "$UI_PID_FILE"

sleep 1

if ! kill -0 "$UI_PID" >/dev/null 2>&1; then
  echo "[FATAL] UI server failed to start."
  tail -n 30 "$UI_LOG" || true
  exit 1
fi

echo "[UI] UI server running (PID $UI_PID)."
echo "[UI] Web UI: http://$UI_HOST:$UI_PORT"

# Attempt to open the browser (WSL + Linux compatible)
_open_browser() {
  local url="$1"
  # wslu (WSL utility) — most reliable on WSL
  if command -v wslview >/dev/null 2>&1; then
    wslview "$url" &>/dev/null & return 0
  fi
  # Windows explorer.exe — always available in WSL
  if command -v explorer.exe >/dev/null 2>&1; then
    explorer.exe "$url" &>/dev/null & return 0
  fi
  # Native Linux fallback
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" &>/dev/null & return 0
  fi
  return 1
}

if _open_browser "http://$UI_HOST:$UI_PORT"; then
  echo "[UI] Browser opened."
else
  echo "[UI] Could not auto-open browser — navigate to http://$UI_HOST:$UI_PORT"
fi

echo "[UI] UI server layer ready."
