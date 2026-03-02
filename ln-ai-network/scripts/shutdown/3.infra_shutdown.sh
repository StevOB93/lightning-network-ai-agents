#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# INTERNAL: called by scripts/shutdown.sh — do not run directly
###############################################################################
if [[ "${LN_AI_INTERNAL_CALL:-0}" != "1" ]]; then
  echo "[FATAL] This script is internal. Run: ./scripts/shutdown.sh"
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

NODE_COUNT="${1:-2}"
RESET_MODE="${2:-}"

# Validate NODE_COUNT is a positive integer
if ! [[ "$NODE_COUNT" =~ ^[0-9]+$ ]] || [[ "$NODE_COUNT" -lt 1 ]]; then
  echo "[FATAL] NODE_COUNT must be a positive integer. Got: '$NODE_COUNT'"
  exit 2
fi

RPC_USER="lnrpc"
RPC_PASS="lnrpcpass"

echo "=================================================="
echo "INFRA SHUTDOWN"
echo "Nodes: $NODE_COUNT"
echo "Reset: ${RESET_MODE:-<none>}"
echo "=================================================="

: "${BITCOIN_DIR:?env.sh must set BITCOIN_DIR}"
: "${LIGHTNING_BASE:?env.sh must set LIGHTNING_BASE}"
: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"

###############################################################################
# STOP LIGHTNING
###############################################################################
for i in $(seq 1 "$NODE_COUNT"); do
  NODE_DIR="$LIGHTNING_BASE/node-$i"

  if lightning-cli --network=regtest \
    --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; then

    echo "[INFRA] Stopping lightningd node-$i..."
    lightning-cli --network=regtest \
      --lightning-dir="$NODE_DIR" stop || true

    # Wait until RPC stops responding
    until ! lightning-cli --network=regtest \
      --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; do
      sleep 1
    done
  fi
done

echo "[INFRA] Lightning stopped."

###############################################################################
# STOP BITCOIN
###############################################################################
if bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
  -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
  getblockchaininfo >/dev/null 2>&1; then

  echo "[INFRA] Stopping bitcoind..."
  bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" stop || true

  until ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    getblockchaininfo >/dev/null 2>&1; do
    sleep 1
  done
fi

echo "[INFRA] Bitcoin stopped."

###############################################################################
# OPTIONAL RESET
###############################################################################
if [[ "$RESET_MODE" == "reset" ]]; then
  echo "[INFRA] Removing runtime directory..."
  rm -rf "$RUNTIME_DIR"
  echo "[INFRA] Runtime removed."
fi

echo "[INFRA] Infrastructure shutdown complete."