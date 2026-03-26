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

RPC_USER="${BITCOIN_RPC_USER:-lnrpc}"
RPC_PASS="${BITCOIN_RPC_PASSWORD:-lnrpcpass}"

echo "=================================================="
echo "INFRA SHUTDOWN"
echo "Nodes: $NODE_COUNT"
echo "Reset: ${RESET_MODE:-<none>}"
echo "=================================================="

: "${BITCOIN_DIR:?env.sh must set BITCOIN_DIR}"
: "${LIGHTNING_BASE:?env.sh must set LIGHTNING_BASE}"
: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"

###############################################################################
# CLOSE ALL CHANNELS (prevents stale channels from accumulating across restarts)
###############################################################################
BTC() {
  bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcport="${BITCOIN_RPC_PORT:-18443}" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" "$@"
}

BITCOIN_UP=false
if BTC getblockchaininfo >/dev/null 2>&1; then
  BITCOIN_UP=true
fi

CLOSED_ANY=false
for i in $(seq 1 "$NODE_COUNT"); do
  NODE_DIR="$LIGHTNING_BASE/node-$i"

  if lightning-cli --network=regtest \
    --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; then

    # Collect all active channel IDs for this node
    CHANNEL_IDS="$(
      lightning-cli --network=regtest --lightning-dir="$NODE_DIR" listpeers \
        | jq -r '.peers[].channels[] | select(.state == "CHANNELD_NORMAL") | .channel_id' \
        2>/dev/null || echo ""
    )"

    for CHAN_ID in $CHANNEL_IDS; do
      echo "[INFRA] Closing channel $CHAN_ID on node-$i..."
      lightning-cli --network=regtest --lightning-dir="$NODE_DIR" \
        close "$CHAN_ID" 0 >/dev/null 2>&1 || true
      CLOSED_ANY=true
    done
  fi
done

# Mine blocks to confirm closures (bitcoind must still be running)
if [[ "$CLOSED_ANY" == "true" ]] && [[ "$BITCOIN_UP" == "true" ]]; then
  echo "[INFRA] Mining blocks to confirm channel closures..."
  WALLET_NAME="shared-wallet"
  BTC loadwallet "$WALLET_NAME" >/dev/null 2>&1 || true
  MINER_ADDR="$(BTC -rpcwallet="$WALLET_NAME" getnewaddress 2>/dev/null || echo "")"
  if [[ -n "$MINER_ADDR" ]]; then
    BTC generatetoaddress "${CONF_BLOCKS:-6}" "$MINER_ADDR" >/dev/null 2>&1 || true
  fi
fi

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