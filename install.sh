#!/usr/bin/env bash
# =============================================================================
# Lightning Network AI Agent — one-time install
#
# Run this once before your first ./start.sh
# Installs Bitcoin Core, Core Lightning, Python venv, and dependencies.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/ln-ai-network/scripts/0.install.sh" "$@"
