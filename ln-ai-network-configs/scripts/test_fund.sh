#!/usr/bin/env bash
set -euo pipefail

############################################################
# LN_AI_Project :: test_fund.sh
# ----------------------------------------------------------
# Purpose:
#   - Ensure a Bitcoin Core wallet exists
#   - Fund the Lightning wallet on regtest
#
# Assumes:
#   - install.sh has been run
#   - start.sh has been run
#   - bitcoind + lightningd are running
############################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_ID="node-1"

BITCOIN_DIR="$PROJECT_ROOT/runtime/bitcoin/$NODE_ID"
LIGHTNING_DIR="$PROJECT_ROOT/runtime/lightning/$NODE_ID"

WALLET_NAME="regtest-wallet"

############################################################
# Sanity checks
############################################################
if [ ! -d "$BITCOIN_DIR" ]; then
  echo "[ERROR] Bitcoin datadir not found: $BITCOIN_DIR"
  exit 1
fi

if [ ! -d "$LIGHTNING_DIR" ]; then
  echo "[ERROR] Lightning datadir not found: $LIGHTNING_DIR"
  exit 1
fi

############################################################
# Ensure Bitcoin wallet exists and is loaded
############################################################
echo "[INFO] Ensuring Bitcoin wallet exists..."

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" listwallets | jq -e ".[] | select(. == \"$WALLET_NAME\")" >/dev/null; then
  echo "[INFO] Wallet not loaded. Checking if it exists on disk..."

  if bitcoin-cli -regtest -datadir="$BITCOIN_DIR" listwalletdir | jq -e ".wallets[].name | select(. == \"$WALLET_NAME\")" >/dev/null; then
    echo "[INFO] Wallet exists. Loading wallet..."
    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" loadwallet "$WALLET_NAME"
  else
    echo "[INFO] Wallet does not exist. Creating wallet..."
    bitcoin-cli -regtest -datadir="$BITCOIN_DIR" createwallet "$WALLET_NAME"
  fi
else
  echo "[INFO] Wallet already loaded."
fi

############################################################
# Generate Lightning address
############################################################
echo "[INFO] Generating Lightning address..."

ADDR="$(lightning-cli \
  --lightning-dir="$LIGHTNING_DIR" \
  newaddr \
  | jq -r '.bech32')"

echo "[INFO] Lightning address: $ADDR"

############################################################
# Fund Lightning wallet
############################################################
echo "[INFO] Sending 1 BTC to Lightning wallet..."

bitcoin-cli \
  -regtest \
  -datadir="$BITCOIN_DIR" \
  -rpcwallet="$WALLET_NAME" \
  sendtoaddress "$ADDR" 1

############################################################
# Mine blocks (confirm + mature)
############################################################
echo "[INFO] Mining blocks..."

MINER_ADDR="$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcwallet="$WALLET_NAME" getnewaddress)"

bitcoin-cli \
  -regtest \
  -datadir="$BITCOIN_DIR" \
  generatetoaddress 101 "$MINER_ADDR"

############################################################
# Show Lightning funds
############################################################
echo "[INFO] Lightning wallet funds:"

lightning-cli \
  --lightning-dir="$LIGHTNING_DIR" \
  listfunds

echo "=================================================="
echo " Lightning wallet funded successfully ✔"
echo "=================================================="