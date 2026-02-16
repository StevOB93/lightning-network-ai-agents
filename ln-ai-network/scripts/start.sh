#!/usr/bin/env bash

set -euo pipefail

###############################################################################
# PROJECT ROOT RESOLUTION
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_COUNT="${1:-2}"

echo "=================================================="
echo "LN-AI FULL SYSTEM START"
echo "Nodes: $NODE_COUNT"
echo "=================================================="

###############################################################################
# PYTHON VIRTUAL ENVIRONMENT (DETERMINISTIC)
###############################################################################

if [ ! -d "$PROJECT_ROOT/.venv" ]; then
    echo "[SETUP] Creating virtual environment..."
    python3 -m venv "$PROJECT_ROOT/.venv"
fi

source "$PROJECT_ROOT/.venv/bin/activate"

if [ -f "$PROJECT_ROOT/requirements.txt" ]; then
    echo "[SETUP] Installing Python dependencies..."
    pip install --quiet --upgrade pip
    pip install --quiet -r "$PROJECT_ROOT/requirements.txt"
else
    echo "[WARN] No requirements.txt found."
fi

###############################################################################
# INFRASTRUCTURE
###############################################################################

"$PROJECT_ROOT/scripts/startup/infra_boot.sh" "$NODE_COUNT"

###############################################################################
# CONTROL PLANE
###############################################################################

"$PROJECT_ROOT/scripts/startup/control_plane_boot.sh"

###############################################################################
# AGENT
###############################################################################

"$PROJECT_ROOT/scripts/startup/agent_boot.sh"

echo "=================================================="
echo "SYSTEM READY"
echo "Bitcoin + Lightning + MCP + AI Agent Online"
echo "=================================================="
