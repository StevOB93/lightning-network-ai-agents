#!/usr/bin/env bash
set -euo pipefail

############################################################
# LN_AI_Project :: mine_blocks.sh
# ----------------------------------------------------------
# Purpose:
#   - Mine regtest blocks
#   - Ensure spendable BTC exists
#
# Assumes:
#   - install.sh has been run
#   - start.sh has been run
#   - bitcoind is running
############################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_ID="node-1"
BITCOIN_DIR="$PROJECT_ROOT/runtime/bitcoin/$NODE_ID"
WALLET_NAME="regtest-wallet"

echo "[INFO] Ensuring wallet exists and is loaded..."

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" listwallets | jq -e ".[] | select(. == \"$WALLET_NAME\")" >/dev/null; then
  if bitcoin-cli -regtest -datadir="$BITCOIN_DIR" listwalletdir | jq -e ".wallets[].name | select(. == \"$WALLET_NAME\")" >/dev/null; then
    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" loadwallet "$WALLET_NAME"
  else
    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" createwallet "$WALLET_NAME"
  fi
fi

echo "[INFO] Creating mining address..."
MINER_ADDR="$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcwallet="$WALLET_NAME" getnewaddress)"

echo "[INFO] Mining 101 blocks (coinbase maturity)..."
bitcoin-cli \
  -regtest \
  -datadir="$BITCOIN_DIR" \
  generatetoaddress 101 "$MINER_ADDR"

echo "[INFO] Done. Current balance:"
bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcwallet="$WALLET_NAME" getbalance

echo "=================================================="
echo " Regtest BTC is now spendable ✔"