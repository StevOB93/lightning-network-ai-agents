#!/usr/bin/env bash
# =============================================================================
# Lightning Network AI Agent — top-level launcher
#
# Usage:
#   ./run.sh           # start with 2 nodes (default)
#   ./run.sh 3         # start with 3 nodes
#
# First time? Run setup first:
#   ./setup.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/ln-ai-network/scripts/1.start.sh" "$@"
