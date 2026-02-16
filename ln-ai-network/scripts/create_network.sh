#!/usr/bin/env bash

# ==============================================================
# create_network.sh
#
# Deterministic Lightning Network topology builder
#
# - Uses shared Bitcoin backend (regtest only)
# - Ensures wallet exists and funded
# - Connects nodes in linear topology
# - Opens deterministic channels
# - Waits for CHANNELD_NORMAL
# - Fully state-verified
# ==============================================================

set -euo pipefail

source "$(dirname "$0")/../env.sh"

NODE_COUNT="$1"

if [ -z "$NODE_COUNT" ]; then
    echo "[ERROR] Usage: ./scripts/create_network.sh <node_count>"
    exit 1
fi

if [ "$NODE_COUNT" -lt 2 ]; then
    echo "[ERROR] At least 2 nodes required."
    exit 1
fi

echo "[INFO] Creating deterministic Lightning network with $NODE_COUNT nodes"

WALLET_NAME="shared-wallet"

# --------------------------------------------------------------
# ENSURE BITCOIN WALLET EXISTS AND LOADED
# --------------------------------------------------------------

echo "[INFO] Verifying Bitcoin wallet..."

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" listwalletdir \
    | jq -e ".wallets[] | select(.name==\"$WALLET_NAME\")" >/dev/null 2>&1; then

    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" createwallet "$WALLET_NAME"
fi

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" listwallets \
    | grep -q "\"$WALLET_NAME\""; then

    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" loadwallet "$WALLET_NAME" >/dev/null
fi

echo "[INFO] Wallet ready."

# --------------------------------------------------------------
# ENSURE SUFFICIENT BALANCE
# --------------------------------------------------------------

BALANCE=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcwallet="$WALLET_NAME" getbalance)

if (( $(echo "$BALANCE < 10" | bc -l) )); then
    echo "[INFO] Mining additional blocks for funding..."
    ADDR=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcwallet="$WALLET_NAME" getnewaddress)
    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" generatetoaddress 100 "$ADDR"
fi

# --------------------------------------------------------------
# VERIFY LIGHTNING RPC READINESS
# --------------------------------------------------------------

echo "[INFO] Verifying Lightning RPC readiness..."

for i in $(seq 1 $NODE_COUNT); do
    NODE_DIR="$LIGHTNING_BASE/node-$i"

    if ! lightning-cli --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; then
        echo "[FATAL] node-$i lightning RPC not ready."
        exit 1
    fi
done

echo "[INFO] All Lightning nodes ready."

# --------------------------------------------------------------
# FETCH NODE IDS
# --------------------------------------------------------------

declare -A NODE_IDS

for i in $(seq 1 $NODE_COUNT); do
    NODE_DIR="$LIGHTNING_BASE/node-$i"

    ID=$(lightning-cli --lightning-dir="$NODE_DIR" getinfo | jq -r '.id')

    if [ -z "$ID" ] || [ "$ID" == "null" ]; then
        echo "[FATAL] node-$i has invalid ID."
        exit 1
    fi

    NODE_IDS[$i]="$ID"
done

echo "[INFO] Node identities verified."

# --------------------------------------------------------------
# FUND LIGHTNING WALLETS (IF NEEDED)
# --------------------------------------------------------------

echo "[INFO] Funding Lightning wallets if required..."

for i in $(seq 1 $NODE_COUNT); do
    NODE_DIR="$LIGHTNING_BASE/node-$i"

    FUNDS=$(lightning-cli --lightning-dir="$NODE_DIR" listfunds | jq '.outputs | length')

    if [ "$FUNDS" -eq 0 ]; then
        ADDR=$(lightning-cli --lightning-dir="$NODE_DIR" newaddr | jq -r '.bech32')

        bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
            -rpcwallet="$WALLET_NAME" sendtoaddress "$ADDR" 1
    fi
done

# Confirm funding
MINER_ADDR=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcwallet="$WALLET_NAME" getnewaddress)
bitcoin-cli -regtest -datadir="$BITCOIN_DIR" generatetoaddress 6 "$MINER_ADDR"

# --------------------------------------------------------------
# CONNECT NODES (LINEAR TOPOLOGY)
# node-1 <-> node-2 <-> node-3 ...
# --------------------------------------------------------------

echo "[INFO] Establishing deterministic peer connections..."

for i in $(seq 1 $((NODE_COUNT - 1))); do

    TARGET=$((i + 1))
    PORT=$((LIGHTNING_BASE_PORT + TARGET - 1))
    NODE_DIR="$LIGHTNING_BASE/node-$i"

    PEER_COUNT=$(lightning-cli --lightning-dir="$NODE_DIR" listpeers | jq '.peers | length')

    if [ "$PEER_COUNT" -eq 0 ]; then
        lightning-cli --lightning-dir="$NODE_DIR" connect \
            "${NODE_IDS[$TARGET]}" 127.0.0.1 "$PORT"
    fi

    # Wait until peer connected=true
    echo "[INFO] Waiting for node-$i peer connection..."

    while true; do
        CONNECTED=$(lightning-cli --lightning-dir="$NODE_DIR" listpeers \
            | jq -r '.peers[0].connected')

        if [ "$CONNECTED" == "true" ]; then
            break
        fi
        sleep 1
    done
done

# --------------------------------------------------------------
# OPEN CHANNELS (IF NOT ALREADY OPEN)
# --------------------------------------------------------------

echo "[INFO] Opening channels where necessary..."

for i in $(seq 1 $((NODE_COUNT - 1))); do

    TARGET=$((i + 1))
    NODE_DIR="$LIGHTNING_BASE/node-$i"

    CHANNEL_COUNT=$(lightning-cli --lightning-dir="$NODE_DIR" listpeers \
        | jq '.peers[0].channels | length')

    if [ "$CHANNEL_COUNT" -eq 0 ]; then
        lightning-cli --lightning-dir="$NODE_DIR" fundchannel \
            "${NODE_IDS[$TARGET]}" 1000000
    fi
done

# Mine confirmation blocks
bitcoin-cli -regtest -datadir="$BITCOIN_DIR" generatetoaddress 6 "$MINER_ADDR"

# --------------------------------------------------------------
# WAIT FOR CHANNELD_NORMAL
# --------------------------------------------------------------

echo "[INFO] Waiting for channels to reach CHANNELD_NORMAL..."

for i in $(seq 1 $((NODE_COUNT - 1))); do
    NODE_DIR="$LIGHTNING_BASE/node-$i"

    while true; do
        STATE=$(lightning-cli --lightning-dir="$NODE_DIR" listpeers \
            | jq -r '.peers[0].channels[0].state')

        if [ "$STATE" == "CHANNELD_NORMAL" ]; then
            break
        fi
        sleep 1
    done
done

# --------------------------------------------------------------
# FINAL VERIFICATION
# --------------------------------------------------------------

echo "[INFO] Verifying expected channel topology..."

EXPECTED=$((NODE_COUNT - 1))
ACTUAL=0

for i in $(seq 1 $((NODE_COUNT - 1))); do
    NODE_DIR="$LIGHTNING_BASE/node-$i"
    COUNT=$(lightning-cli --lightning-dir="$NODE_DIR" listchannels | jq '.channels | length')
    ACTUAL=$((ACTUAL + COUNT))
done

if [ "$ACTUAL" -lt "$EXPECTED" ]; then
    echo "[FATAL] Channel topology incomplete."
    exit 1
fi

echo "[SUCCESS] Deterministic Lightning network established."
