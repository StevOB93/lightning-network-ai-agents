#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# LN-AI SYSTEM SHUTDOWN (logged)
#
# Usage:
#   ./scripts/shutdown.sh [NODE_COUNT]
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_COUNT="${1:-2}"

# Validate NODE_COUNT is a positive integer
if ! [[ "$NODE_COUNT" =~ ^[0-9]+$ ]] || [[ "$NODE_COUNT" -lt 1 ]]; then
  echo "[FATAL] NODE_COUNT must be a positive integer. Got: '$NODE_COUNT'"
  exit 2
fi

LOG_DIR="$PROJECT_ROOT/logs/system"
mkdir -p "$LOG_DIR"

SHUTDOWN_LOG="$LOG_DIR/shutdown.log"
exec > >(tee -a "$SHUTDOWN_LOG") 2>&1

echo "=================================================="
echo "LN-AI SYSTEM SHUTDOWN"
echo "Project: $PROJECT_ROOT"
echo "Nodes:   $NODE_COUNT"
echo "Log:     $SHUTDOWN_LOG"
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

# Mark internal for sub-scripts
export LN_AI_INTERNAL_CALL=1

"$PROJECT_ROOT/scripts/shutdown/1.agent_shutdown.sh"
"$PROJECT_ROOT/scripts/shutdown/2.control_plane_shutdown.sh"
"$PROJECT_ROOT/scripts/shutdown/3.infra_shutdown.sh" "$NODE_COUNT"

echo "=================================================="
echo "SYSTEM STOPPED"
echo "=================================================="