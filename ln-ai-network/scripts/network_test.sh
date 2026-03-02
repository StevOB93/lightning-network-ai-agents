#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# network_test.sh
#
# Deterministic Lightning Network bring-up + verification
#
# - Uses shared Bitcoin regtest backend (same RPC creds as infra boot)
# - Waits for N CLN nodes RPC readiness
# - Ensures shared wallet exists/loaded
# - Ensures chain height >= 1000
# - Funds nodes
# - Connects N nodes in linear topology:
#     node-1 <-> node-2 <-> node-3 ...
# - Opens deterministic linear channels
# - Waits for CHANNELD_NORMAL
#
# Usage:
#   ./scripts/network_test.sh [node_count]
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load deterministic env (and optional local .env)
if [[ -f "$PROJECT_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env.sh"
else
  echo "[FATAL] env.sh not found at $PROJECT_ROOT/env.sh"
  exit 1
fi

NODE_COUNT="${1:-2}"

# Validate NODE_COUNT
if ! [[ "$NODE_COUNT" =~ ^[0-9]+$ ]] || [[ "$NODE_COUNT" -lt 1 ]]; then
  echo "[ERROR] Usage: ./scripts/network_test.sh [node_count]"
  echo "[ERROR] node_count must be a positive integer. Got: '$NODE_COUNT'"
  exit 2
fi

echo "=================================================="
echo "[INFO] Deterministic Lightning network test"
echo "[INFO] Project: $PROJECT_ROOT"
echo "[INFO] Nodes:   $NODE_COUNT"
echo "=================================================="

# Requirements checks (lightweight)
command -v bitcoin-cli >/dev/null 2>&1 || { echo "[FATAL] bitcoin-cli not found"; exit 127; }
command -v lightning-cli >/dev/null 2>&1 || { echo "[FATAL] lightning-cli not found"; exit 127; }
command -v jq >/dev/null 2>&1 || { echo "[FATAL] jq not found"; exit 127; }

# ------------------------------------------------------------------------------
# Bitcoin RPC settings (MUST match scripts/startup/0.1.infra_boot.sh)
# ------------------------------------------------------------------------------
RPC_USER="lnrpc"
RPC_PASS="lnrpcpass"

# env.sh should provide BITCOIN_DIR and BITCOIN_RPC_PORT; enforce it
: "${BITCOIN_DIR:?env.sh must set BITCOIN_DIR}"
: "${BITCOIN_RPC_PORT:?env.sh must set BITCOIN_RPC_PORT}"
: "${LIGHTNING_BASE:?env.sh must set LIGHTNING_BASE}"

BTC() {
  bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcport="$BITCOIN_RPC_PORT" -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" "$@"
}

WALLET_NAME="shared-wallet"

# ------------------------------------------------------------------------------
# BITCOIN WALLET SETUP
# ------------------------------------------------------------------------------
echo "[INFO] Ensuring Bitcoin wallet exists..."

# Ensure bitcoind is up (otherwise errors will be confusing)
if ! BTC getblockchaininfo >/dev/null 2>&1; then
  echo "[FATAL] Bitcoin RPC not ready. Start system first: ./scripts/1.start.sh $NODE_COUNT"
  exit 1
fi

# Create wallet if missing
if ! BTC listwalletdir | jq -e ".wallets[] | select(.name==\"$WALLET_NAME\")" >/dev/null 2>&1; then
  BTC createwallet "$WALLET_NAME" >/dev/null
fi

# Load wallet (idempotent)
BTC loadwallet "$WALLET_NAME" >/dev/null 2>&1 || true

# ------------------------------------------------------------------------------
# ENSURE BLOCKCHAIN HEIGHT
# ------------------------------------------------------------------------------
BLOCKS="$(BTC getblockcount)"

if [[ "$BLOCKS" -lt 1000 ]]; then
  echo "[INFO] Mining initial 1000 blocks (current height: $BLOCKS)..."
  ADDR="$(BTC -rpcwallet="$WALLET_NAME" getnewaddress)"
  BTC generatetoaddress 1000 "$ADDR" >/dev/null
fi

# ------------------------------------------------------------------------------
# WAIT FOR ALL LIGHTNING RPCS
# ------------------------------------------------------------------------------
echo "[INFO] Waiting for Lightning RPC readiness..."

for i in $(seq 1 "$NODE_COUNT"); do
  NODE_DIR="$LIGHTNING_BASE/node-$i"
  echo "[INFO]  - waiting: node-$i ($NODE_DIR)"
  until lightning-cli --network=regtest --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; do
    sleep 1
  done
done

echo "[INFO] All Lightning RPCs ready"

# ------------------------------------------------------------------------------
# FETCH NODE IDS
# ------------------------------------------------------------------------------
declare -A NODE_IDS
for i in $(seq 1 "$NODE_COUNT"); do
  NODE_DIR="$LIGHTNING_BASE/node-$i"
  NODE_IDS["$i"]="$(lightning-cli --network=regtest --lightning-dir="$NODE_DIR" getinfo | jq -r '.id')"
done

# ------------------------------------------------------------------------------
# FUND LIGHTNING NODES
# ------------------------------------------------------------------------------
echo "[INFO] Funding Lightning nodes..."

for i in $(seq 1 "$NODE_COUNT"); do
  NODE_DIR="$LIGHTNING_BASE/node-$i"
  ADDR="$(lightning-cli --network=regtest --lightning-dir="$NODE_DIR" newaddr | jq -r '.bech32')"
  BTC -rpcwallet="$WALLET_NAME" sendtoaddress "$ADDR" 1 >/dev/null
done

# Confirm funding
MINER_ADDR="$(BTC -rpcwallet="$WALLET_NAME" getnewaddress)"
BTC generatetoaddress 6 "$MINER_ADDR" >/dev/null

# ------------------------------------------------------------------------------
# CONNECT NODES (LINEAR TOPOLOGY)
# node-1 <-> node-2 <-> node-3 ...
# ------------------------------------------------------------------------------
echo "[INFO] Connecting peers..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
  TARGET=$((i + 1))
  PORT=$((9735 + TARGET - 1))

  lightning-cli --network=regtest --lightning-dir="$LIGHTNING_BASE/node-$i" connect \
    "${NODE_IDS[$TARGET]}" 127.0.0.1 "$PORT" >/dev/null
done

# ------------------------------------------------------------------------------
# VERIFY PEER CONNECTIVITY
# ------------------------------------------------------------------------------
echo "[INFO] Verifying peer connectivity..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
  NODE_DIR="$LIGHTNING_BASE/node-$i"

  while true; do
    PEER_COUNT="$(
      lightning-cli --network=regtest --lightning-dir="$NODE_DIR" listpeers \
        | jq ".peers | length"
    )"

    if [[ "$PEER_COUNT" -ge 1 ]]; then
      break
    fi

    sleep 1
  done
done

# ------------------------------------------------------------------------------
# OPEN CHANNELS
# ------------------------------------------------------------------------------
echo "[INFO] Opening channels..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
  TARGET=$((i + 1))
  NODE_DIR="$LIGHTNING_BASE/node-$i"

  lightning-cli --network=regtest --lightning-dir="$NODE_DIR" fundchannel \
    "${NODE_IDS[$TARGET]}" 1000000 >/dev/null
done

# Mine confirmation blocks
BTC generatetoaddress 6 "$MINER_ADDR" >/dev/null

# ------------------------------------------------------------------------------
# WAIT FOR CHANNELD_NORMAL
# ------------------------------------------------------------------------------
echo "[INFO] Waiting for CHANNELD_NORMAL..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
  NODE_DIR="$LIGHTNING_BASE/node-$i"

  while true; do
    STATE="$(
      lightning-cli --network=regtest --lightning-dir="$NODE_DIR" listpeers \
        | jq -r ".peers[0].channels[0].state"
    )"

    if [[ "$STATE" == "CHANNELD_NORMAL" ]]; then
      break
    fi

    sleep 1
  done
done

echo "[SUCCESS] Deterministic Lightning network established"