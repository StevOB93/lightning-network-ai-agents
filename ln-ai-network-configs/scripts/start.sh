#!/usr/bin/env bash
set -euo pipefail

############################################################
# LN_AI_Project :: start.sh
# ----------------------------------------------------------
# Purpose:
#   - Start a single regtest Bitcoin node
#   - Start a single regtest Core Lightning node
#   - Write explicit config files so CLIs work predictably
#
# Contract:
#   - install.sh has already been run successfully
#   - No software installation happens here
############################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_ID="node-1"

BITCOIN_DIR="$PROJECT_ROOT/runtime/bitcoin/$NODE_ID"
LIGHTNING_DIR="$PROJECT_ROOT/runtime/lightning/$NODE_ID"
LOG_DIR="$PROJECT_ROOT/logs/$NODE_ID"

RPC_PORT=18443
P2P_PORT=18444

echo "[INFO] Creating runtime and log directories..."
mkdir -p "$BITCOIN_DIR" "$LIGHTNING_DIR" "$LOG_DIR"

############################################################
# Write bitcoin.conf (NO QUOTING TRICKS)
############################################################
BITCOIN_CONF="$BITCOIN_DIR/bitcoin.conf"

echo "[INFO] Writing bitcoin.conf..."
cat > "$BITCOIN_CONF" <<EOF
regtest=1
server=1
txindex=1
daemon=1

rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=$RPC_PORT

fallbackfee=0.0001
EOF

############################################################
# Start bitcoind
############################################################
if pgrep -x bitcoind >/dev/null; then
  echo "[INFO] bitcoind already running."
else
  echo "[INFO] Starting bitcoind (regtest)..."
  bitcoind \
    -datadir="$BITCOIN_DIR" \
    -conf="$BITCOIN_CONF" \
    -debuglogfile="$LOG_DIR/bitcoind.log"
fi

sleep 2

############################################################
# Write lightning config
############################################################
LIGHTNING_CONF="$LIGHTNING_DIR/config"

echo "[INFO] Writing lightning config..."
cat > "$LIGHTNING_CONF" <<EOF
network=regtest
bitcoin-datadir=$BITCOIN_DIR
log-file=$LOG_DIR/lightningd.log
EOF

############################################################
# Start lightningd
############################################################
if pgrep -x lightningd >/dev/null; then
  echo "[INFO] lightningd already running."
else
  echo "[INFO] Starting lightningd (regtest)..."
  lightningd \
    --lightning-dir="$LIGHTNING_DIR" \
    --daemon
fi

echo "=================================================="
echo " Nodes started successfully ✔"
echo
echo " CLI commands:"
echo "   bitcoin-cli -regtest -datadir=$BITCOIN_DIR getblockchaininfo"
echo "   lightning-cli --lightning-dir=$LIGHTNING_DIR getinfo"
echo
echo " Logs:"
echo "   Bitcoin   : $LOG_DIR/bitcoind.log"
echo "   Lightning : $LOG_DIR/lightningd.log"
echo "=================================================="