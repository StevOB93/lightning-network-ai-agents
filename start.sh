#!/usr/bin/env bash
# =============================================================================
# Lightning Network AI Agent — start the full system
#
# Starts Bitcoin (regtest), Lightning nodes, the AI pipeline,
# and the web UI. Opens the browser automatically.
#
# Usage:
#   ./start.sh         # start with 2 nodes (default)
#   ./start.sh 3       # start with 3 nodes
#
# First time? Run install first:
#   ./install.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/ln-ai-network/scripts/1.start.sh" "$@"
