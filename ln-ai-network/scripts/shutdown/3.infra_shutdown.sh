#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$PROJECT_ROOT/env.sh"

NODE_COUNT="${1:-2}"
RESET_MODE="${2:-}"

RPC_USER="lnrpc"
RPC_PASS="lnrpcpass"

echo "=================================================="
echo "INFRA SHUTDOWN"
echo "=================================================="

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

if [ "$RESET_MODE" = "reset" ]; then
    echo "[INFRA] Removing runtime directory..."
    rm -rf "$RUNTIME_DIR"
    echo "[INFRA] Runtime removed."
fi

echo "[INFRA] Infrastructure shutdown complete."
