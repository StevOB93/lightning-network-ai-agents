#!/usr/bin/env bash
set -euo pipefail

############################################################
# LN_AI_Project :: mine_blocks.sh
# ----------------------------------------------------------
# Purpose:
#   - Mine regtest blocks
#   - Ensure spendable BTC exists
#
# Usage:
#   ./scripts/tools/mine_blocks.sh [COUNT]   # default: 101
#
# Assumes:
#   - install.sh has been run
#   - start.sh has been run
#   - bitcoind is running
############################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load deterministic env
if [[ -f "$PROJECT_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env.sh"
else
  echo "[FATAL] env.sh not found at $PROJECT_ROOT/env.sh"
  exit 1
fi

: "${BITCOIN_DIR:?env.sh must set BITCOIN_DIR}"
: "${BITCOIN_RPC_PORT:?env.sh must set BITCOIN_RPC_PORT}"

BLOCK_COUNT="${1:-101}"
WALLET_NAME="shared-wallet"
RPC_USER="${BITCOIN_RPC_USER:-lnrpc}"
RPC_PASS="${BITCOIN_RPC_PASSWORD:-lnrpcpass}"

BTC() {
  bitcoin-cli -regtest -datadir="$BITCOIN_DIR" -rpcport="$BITCOIN_RPC_PORT" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" "$@"
}

echo "[INFO] Ensuring wallet exists and is loaded..."

if ! BTC listwallets | jq -e ".[] | select(. == \"$WALLET_NAME\")" >/dev/null 2>&1; then
  if BTC listwalletdir | jq -e ".wallets[].name | select(. == \"$WALLET_NAME\")" >/dev/null 2>&1; then
    BTC loadwallet "$WALLET_NAME"
  else
    BTC createwallet "$WALLET_NAME" >/dev/null
  fi
fi

echo "[INFO] Creating mining address..."
MINER_ADDR="$(BTC -rpcwallet="$WALLET_NAME" getnewaddress)"

echo "[INFO] Mining $BLOCK_COUNT blocks..."
BTC generatetoaddress "$BLOCK_COUNT" "$MINER_ADDR" >/dev/null

echo "[INFO] Done. Current balance:"
BTC -rpcwallet="$WALLET_NAME" getbalance

echo "=================================================="
echo " Regtest BTC is now spendable"
echo "=================================================="
