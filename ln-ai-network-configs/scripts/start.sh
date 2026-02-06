#!/usr/bin/env bash
set -euo pipefail

############################################################
# LN_AI_Project :: start.sh
############################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_ID="node-1"

BITCOIN_DIR="$PROJECT_ROOT/runtime/bitcoin/$NODE_ID"
LIGHTNING_DIR="$PROJECT_ROOT/runtime/lightning/$NODE_ID"
LOG_DIR="$PROJECT_ROOT/logs/$NODE_ID"

echo "[INFO] Creating runtime and log directories..."
mkdir -p "$BITCOIN_DIR" "$LIGHTNING_DIR" "$LOG_DIR"

############################################################
# Write bitcoin.conf (CORRECT NETWORK SECTIONS)
############################################################
BITCOIN_CONF="$BITCOIN_DIR/bitcoin.conf"

echo "[INFO] Writing bitcoin.conf..."
cat > "$BITCOIN_CONF" <<EOF
# Global options
server=1
txindex=1
daemon=1

# Regtest-specific options
[regtest]
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=18443
fallbackfee=0.0001
EOF

############################################################
# Start bitcoind
############################################################
echo "[INFO] Starting bitcoind (regtest)..."
bitcoind \
  -regtest \
  -datadir="$BITCOIN_DIR" \
  -conf="$BITCOIN_CONF" \
  -debuglogfile="$LOG_DIR/bitcoind.log"

sleep 3

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
echo "[INFO] Starting lightningd (regtest)..."
lightningd \
  --lightning-dir="$LIGHTNING_DIR" \
  --daemon

echo "=================================================="
echo " Nodes started successfully ✔"
echo
echo " Logs:"
echo "   Bitcoin   : $LOG_DIR/bitcoind.log"
echo "   Lightning : $LOG_DIR/lightningd.log"
echo "=================================================="