#!/usr/bin/env bash
# =============================================================================
# Lightning Network AI Agent — stop all processes
#
# Usage:
#   ./stop.sh          # reads stored node count from last run
#   ./stop.sh 3        # stop with explicit node count
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/ln-ai-network/scripts/shutdown.sh" "$@"
