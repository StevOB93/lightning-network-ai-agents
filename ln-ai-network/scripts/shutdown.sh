#!/usr/bin/env bash

set -euo pipefail

###############################################################################
# PATH RESOLUTION
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_COUNT="${1:-2}"

echo "=================================================="
echo "LN-AI SYSTEM SHUTDOWN"
echo "=================================================="

"$PROJECT_ROOT/scripts/shutdown/1.agent_shutdown.sh"
"$PROJECT_ROOT/scripts/shutdown/2.control_plane_shutdown.sh"
"$PROJECT_ROOT/scripts/shutdown/3.infra_shutdown.sh" "$NODE_COUNT"

echo "=================================================="
echo "SYSTEM STOPPED"
echo "=================================================="
