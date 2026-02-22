#!/usr/bin/env bash

set -euo pipefail

###############################################################################
# PATH RESOLUTION
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_COUNT="${1:-2}"

echo "=================================================="
echo "LN-AI FULL SYSTEM START"
echo "Nodes: $NODE_COUNT"
echo "=================================================="

###############################################################################
# PYTHON ENVIRONMENT BOOTSTRAP
###############################################################################

VENV_DIR="$PROJECT_ROOT/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[SETUP] Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

echo "[SETUP] Activating virtual environment..."
source "$VENV_DIR/bin/activate"

if [ -f "$PROJECT_ROOT/requirements.txt" ]; then
    echo "[SETUP] Installing dependencies..."
    pip install --quiet --upgrade pip
    pip install --quiet -r "$PROJECT_ROOT/requirements.txt"
fi

###############################################################################
# STARTUP SEQUENCE (ORDERED)
###############################################################################

"$PROJECT_ROOT/scripts/startup/0.1.infra_boot.sh" "$NODE_COUNT"
"$PROJECT_ROOT/scripts/startup/0.2.control_plane_boot.sh"
"$PROJECT_ROOT/scripts/startup/0.3.agent_boot.sh"

echo "=================================================="
echo "SYSTEM READY"
echo "=================================================="
