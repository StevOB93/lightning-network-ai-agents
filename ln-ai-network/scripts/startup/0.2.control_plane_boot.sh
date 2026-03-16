#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# INTERNAL: called by scripts/1.start.sh — do not run directly
###############################################################################
if [[ "${LN_AI_INTERNAL_CALL:-0}" != "1" ]]; then
  echo "[FATAL] This script is internal. Run: ./scripts/1.start.sh"
  exit 2
fi

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
echo "CONTROL PLANE BOOT"
echo "=================================================="

echo "[CONTROL] Verifying Lightning infrastructure..."

: "${LIGHTNING_BASE:?env.sh must set LIGHTNING_BASE}"

if ! command -v lightning-cli >/dev/null 2>&1; then
  echo "[FATAL] lightning-cli not found. Run ./scripts/0.install.sh"
  exit 127
fi

if ! lightning-cli --network=regtest \
  --lightning-dir="$LIGHTNING_BASE/node-1" \
  getinfo >/dev/null 2>&1; then

  echo "[FATAL] Infrastructure not ready."
  exit 1
fi

echo "[CONTROL] Infrastructure verified."
echo "[CONTROL] MCP will be launched by agent."
echo "[CONTROL] Control plane ready."