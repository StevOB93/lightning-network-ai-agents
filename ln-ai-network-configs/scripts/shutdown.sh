#!/usr/bin/env bash
set -euo pipefail

############################################################
# LN_AI_Project :: shutdown.sh
# ----------------------------------------------------------
# Purpose:
#   - Gracefully shut down Core Lightning
#   - Gracefully shut down Bitcoin Core
#
# Why order matters:
#   - lightningd depends on bitcoind
#   - lightningd must stop FIRST
#
# Assumes:
#   - start.sh was used to start the nodes
#   - runtime directories still exist
############################################################

# Resolve paths robustly (works from anywhere)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_ID="node-1"

BITCOIN_DIR="$PROJECT_ROOT/runtime/bitcoin/$NODE_ID"
LIGHTNING_DIR="$PROJECT_ROOT/runtime/lightning/$NODE_ID"

############################################################
# Shut down Core Lightning (FIRST)
############################################################
echo "[INFO] Shutting down Core Lightning..."

if [ -S "$LIGHTNING_DIR/regtest/lightning-rpc" ]; then
  lightning-cli \
    --lightning-dir="$LIGHTNING_DIR" \
    stop
  echo "[INFO] lightningd stopped cleanly."
else
  echo "[INFO] lightningd is not running (no RPC socket)."
fi

############################################################
# Shut down Bitcoin Core (SECOND)
############################################################
echo "[INFO] Shutting down Bitcoin Core..."

if bitcoin-cli -regtest -datadir="$BITCOIN_DIR" getnetworkinfo >/dev/null 2>&1; then
  bitcoin-cli \
    -regtest \
    -datadir="$BITCOIN_DIR" \
    stop
  echo "[INFO] bitcoind stopped cleanly."
else
  echo "[INFO] bitcoind is not running or RPC unavailable."
fi

############################################################
# Final confirmation
############################################################
echo "=================================================="
echo " Shutdown complete ✔"
echo
echo " Node: $NODE_ID"
echo " Runtime data preserved."
echo "=================================================="
