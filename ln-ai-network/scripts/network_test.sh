#!/usr/bin/env bash

# ==============================================================
# create_network.sh
#
# Deterministic Lightning Network creator
#
# - Uses shared Bitcoin regtest backend
# - Connects N CLN nodes
# - Opens deterministic linear channels
# - Verifies CHANNELD_NORMAL
# - No race conditions
# - No assumptions
# ==============================================================

set -e

source "$(dirname "$0")/../env.sh"

NODE_COUNT="$1"

if [ -z "$NODE_COUNT" ]; then
    echo "[ERROR] Usage: ./scripts/create_network.sh <node_count>"
    exit 1
fi

echo "[INFO] Creating deterministic Lightning network with $NODE_COUNT nodes"

# --------------------------------------------------------------
# BITCOIN WALLET SETUP
# --------------------------------------------------------------

echo "[INFO] Ensuring Bitcoin wallet exists..."

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" listwallets | grep -q "\"shared-wallet\""; then
    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" createwallet "shared-wallet"
fi

bitcoin-cli -regtest -datadir="$BITCOIN_DIR" loadwallet "shared-wallet" >/dev/null 2>&1 || true

# --------------------------------------------------------------
# ENSURE BLOCKCHAIN HEIGHT
# --------------------------------------------------------------

BLOCKS=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" getblockcount)

if [ "$BLOCKS" -lt 1000 ]; then
    echo "[INFO] Mining initial 1000 blocks..."
    ADDR=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" getnewaddress)
    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" generatetoaddress 1000 "$ADDR"
fi

# --------------------------------------------------------------
# WAIT FOR ALL LIGHTNING RPCS
# --------------------------------------------------------------

echo "[INFO] Waiting for Lightning RPC readiness..."

for i in $(seq 1 $NODE_COUNT); do
    while ! lightning-cli --lightning-dir="$LIGHTNING_BASE/node-$i" getinfo >/dev/null 2>&1; do
        sleep 1
    done
done

echo "[INFO] All Lightning RPCs ready"

# --------------------------------------------------------------
# FETCH NODE IDS
# --------------------------------------------------------------

declare -A NODE_IDS

for i in $(seq 1 $NODE_COUNT); do
    NODE_IDS[$i]=$(
        lightning-cli --lightning-dir="$LIGHTNING_BASE/node-$i" getinfo | jq -r '.id'
    )
done

# --------------------------------------------------------------
# FUND LIGHTNING NODES
# --------------------------------------------------------------

echo "[INFO] Funding Lightning nodes..."

for i in $(seq 1 $NODE_COUNT); do
    ADDR=$(
        lightning-cli --lightning-dir="$LIGHTNING_BASE/node-$i" newaddr | jq -r '.bech32'
    )

    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" sendtoaddress "$ADDR" 1
done

# Confirm funding
MINER_ADDR=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" getnewaddress)
bitcoin-cli -regtest -datadir="$BITCOIN_DIR" generatetoaddress 6 "$MINER_ADDR"

# --------------------------------------------------------------
# CONNECT NODES (LINEAR TOPOLOGY)
# node-1 <-> node-2 <-> node-3 ...
# --------------------------------------------------------------

echo "[INFO] Connecting peers..."

for i in $(seq 1 $((NODE_COUNT - 1))); do

    TARGET=$((i + 1))
    PORT=$((9735 + TARGET - 1))

    lightning-cli --lightning-dir="$LIGHTNING_BASE/node-$i" connect \
        "${NODE_IDS[$TARGET]}" 127.0.0.1 "$PORT"

done

# --------------------------------------------------------------
# VERIFY PEER CONNECTIVITY
# --------------------------------------------------------------

echo "[INFO] Verifying peer connectivity..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
    TARGET=$((i + 1))

    while true; do
        PEER_COUNT=$(
            lightning-cli --lightning-dir="$LIGHTNING_BASE/node-$i" listpeers \
            | jq ".peers | length"
        )

        if [ "$PEER_COUNT" -ge 1 ]; then
            break
        fi

        sleep 1
    done
done

# --------------------------------------------------------------
# OPEN CHANNELS
# --------------------------------------------------------------

echo "[INFO] Opening channels..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
    TARGET=$((i + 1))

    lightning-cli --lightning-dir="$LIGHTNING_BASE/node-$i" fundchannel \
        "${NODE_IDS[$TARGET]}" 1000000
done

# Mine confirmation blocks
bitcoin-cli -regtest -datadir="$BITCOIN_DIR" generatetoaddress 6 "$MINER_ADDR"

# --------------------------------------------------------------
# WAIT FOR CHANNELD_NORMAL
# --------------------------------------------------------------

echo "[INFO] Waiting for CHANNELD_NORMAL..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
    TARGET=$((i + 1))

    while true; do
        STATE=$(
            lightning-cli --lightning-dir="$LIGHTNING_BASE/node-$i" listpeers \
            | jq -r ".peers[0].channels[0].state"
        )

        if [ "$STATE" == "CHANNELD_NORMAL" ]; then
            break
        fi

        sleep 1
    done
done

echo "[SUCCESS] Deterministic Lightning network established"
