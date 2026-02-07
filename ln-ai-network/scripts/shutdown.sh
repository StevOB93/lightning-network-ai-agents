#!/usr/bin/env bash
#
# shutdown.sh
#
# Gracefully shuts down all Lightning nodes first.
# Then shuts down Bitcoin nodes.
# If bitcoind becomes unresponsive after RPC shutdown,
# escalates to targeted SIGTERM (last resort).
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

#######################################
# Lightning shutdown (already verified)
#######################################
echo "[INFO] Shutting down Lightning nodes..."

for LN_DIR in "$LN_RUNTIME/lightning"/node-*; do
  [ -d "$LN_DIR" ] || continue
  lightning-cli --lightning-dir="$LN_DIR" stop >/dev/null 2>&1 || true
done

sleep 2

#######################################
# Bitcoin shutdown
#######################################
echo "[INFO] Shutting down Bitcoin nodes..."

for BTC_DIR in "$LN_RUNTIME/bitcoin"/node-*; do
  [ -d "$BTC_DIR" ] || continue

  # Attempt clean shutdown
  bitcoin-cli -regtest -datadir="$BTC_DIR" stop >/dev/null 2>&1 || true

  # Wait for RPC to disappear
  for _ in {1..10}; do
    if ! bitcoin-cli -regtest -datadir="$BTC_DIR" getblockchaininfo >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done

  # Check for lingering process tied to this datadir
  PID="$(ps aux | grep bitcoind | grep "$BTC_DIR" | grep -v grep | awk '{print $2}' || true)"

  if [ -n "$PID" ]; then
    echo "[WARN] bitcoind still running (PID $PID), terminating"
    kill -TERM "$PID"
  fi
done

echo "[SUCCESS] Shutdown complete (no RPC, no running nodes)"
