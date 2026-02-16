#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$PROJECT_ROOT/env.sh"

echo "=================================================="
echo "CONTROL PLANE BOOT"
echo "=================================================="

echo "[CONTROL] Verifying Lightning infrastructure..."

if ! lightning-cli --network=regtest \
    --lightning-dir="$LIGHTNING_BASE/node-1" \
    getinfo >/dev/null 2>&1; then

    echo "[FATAL] Infrastructure not ready."
    exit 1
fi

echo "[CONTROL] Infrastructure verified."
echo "[CONTROL] MCP will be launched by agent."
echo "[CONTROL] Control plane ready."
