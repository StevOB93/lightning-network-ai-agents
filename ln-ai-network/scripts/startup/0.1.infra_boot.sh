#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$PROJECT_ROOT/env.sh"

NODE_COUNT="${1:?Must specify node count}"

TARGET_HEIGHT=1200
WALLET_NAME="shared-wallet"

RPC_USER="lnrpc"
RPC_PASS="lnrpcpass"

echo "=================================================="
echo "INFRA BOOT"
echo "Nodes: $NODE_COUNT"
echo "=================================================="

###############################################################################
# DIRECTORY STRUCTURE
###############################################################################

mkdir -p "$RUNTIME_DIR"
mkdir -p "$BITCOIN_DIR"
mkdir -p "$LIGHTNING_BASE"

###############################################################################
# START BITCOIN
###############################################################################

echo "[INFRA] Ensuring bitcoind running..."

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    getblockchaininfo >/dev/null 2>&1; then

    bitcoind \
        -regtest \
        -datadir="$BITCOIN_DIR" \
        -rpcport="$BITCOIN_RPC_PORT" \
        -rpcbind=127.0.0.1 \
        -rpcallowip=127.0.0.1 \
        -rpcuser="$RPC_USER" \
        -rpcpassword="$RPC_PASS" \
        -port="$BITCOIN_P2P_PORT" \
        -fallbackfee=0.0002 \
        -server=1 \
        -daemon

    until bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
        -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
        getblockchaininfo >/dev/null 2>&1; do
        sleep 1
    done
fi

echo "[INFRA] Bitcoin RPC ready."

###############################################################################
# WALLET
###############################################################################

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    listwalletdir | jq -e ".wallets[] | select(.name==\"$WALLET_NAME\")" >/dev/null 2>&1; then

    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
        -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
        createwallet "$WALLET_NAME"
fi

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    listwallets | grep -q "\"$WALLET_NAME\""; then

    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
        -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
        loadwallet "$WALLET_NAME"
fi

###############################################################################
# ENSURE BLOCK HEIGHT
###############################################################################

CURRENT_HEIGHT=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    getblockcount)

if [ "$CURRENT_HEIGHT" -lt "$TARGET_HEIGHT" ]; then
    MINE=$((TARGET_HEIGHT - CURRENT_HEIGHT))
    ADDR=$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
        -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
        -rpcwallet="$WALLET_NAME" getnewaddress)

    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
        -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
        generatetoaddress "$MINE" "$ADDR"
fi

echo "[INFRA] Block height sufficient."

###############################################################################
# START LIGHTNING NODES
###############################################################################

for i in $(seq 1 "$NODE_COUNT"); do

    NODE_DIR="$LIGHTNING_BASE/node-$i"
    PORT=$((LIGHTNING_BASE_PORT + i - 1))
    LOG_FILE="$NODE_DIR/lightningd.log"

    mkdir -p "$NODE_DIR"

    # Clean port collision
    if ss -lnt | grep -q ":$PORT "; then
        echo "[INFRA] Cleaning port $PORT..."
        fuser -k "$PORT"/tcp || true
        sleep 1
    fi

    if ! lightning-cli --network=regtest \
        --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; then

        echo "[INFRA] Starting lightningd node-$i..."

        lightningd \
            --network=regtest \
            --lightning-dir="$NODE_DIR" \
            --addr=127.0.0.1:$PORT \
            --bitcoin-rpcconnect=127.0.0.1 \
            --bitcoin-rpcport="$BITCOIN_RPC_PORT" \
            --bitcoin-rpcuser="$RPC_USER" \
            --bitcoin-rpcpassword="$RPC_PASS" \
            --bitcoin-datadir="$BITCOIN_DIR" \
            --log-file="$LOG_FILE" &

        until lightning-cli --network=regtest \
            --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; do
            sleep 1
        done
    fi

    echo "[INFRA] node-$i ready."
done

echo "[INFRA] Infrastructure ready."
