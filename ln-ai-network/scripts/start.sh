#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <num_nodes>"
  exit 1
fi

NUM_NODES="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

BITCOIN_DATADIR="$LN_RUNTIME/bitcoin/shared"
BITCOIN_LOGDIR="$LN_LOGS/bitcoin/shared"
BITCOIN_CONF="$BITCOIN_DATADIR/bitcoin.conf"
COOKIE_FILE="$BITCOIN_DATADIR/regtest/.cookie"

mkdir -p "$BITCOIN_DATADIR" "$BITCOIN_LOGDIR"

cat > "$BITCOIN_CONF" <<EOF
regtest=1
daemon=1
server=1
txindex=1
fallbackfee=0.0002

[regtest]
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=18443
port=18444
EOF

if ! pgrep -f "bitcoind.*$BITCOIN_DATADIR" >/dev/null; then
  echo "[INFO] Starting shared Bitcoin backend..."
  "$BITCOIND" -conf="$BITCOIN_CONF" -datadir="$BITCOIN_DATADIR" \
    >"$BITCOIN_LOGDIR/bitcoind.log" 2>&1
fi

echo "[INFO] Waiting for Bitcoin RPC..."
for _ in {1..30}; do
  if [[ -f "$COOKIE_FILE" ]] && \
     "$BITCOIN_CLI" -conf="$BITCOIN_CONF" -datadir="$BITCOIN_DATADIR" \
       getblockchaininfo >/dev/null 2>&1; then
    echo "[INFO] Bitcoin RPC ready"
    break
  fi
  sleep 1
done

echo "[INFO] Starting Lightning nodes..."

for n in $(seq 1 "$NUM_NODES"); do
  LN_NODE_DIR="$LN_RUNTIME/lightning/node-$n"
  LN_LOG_DIR="$LN_LOGS/lightning/node-$n"
  mkdir -p "$LN_NODE_DIR" "$LN_LOG_DIR"

  if pgrep -f "lightningd.*$LN_NODE_DIR" >/dev/null; then
    continue
  fi

  "$LIGHTNINGD" \
    --network=regtest \
    --lightning-dir="$LN_NODE_DIR" \
    --log-file="$LN_LOG_DIR/lightningd.log" \
    --addr="127.0.0.1:$((9735 + n - 1))" \
    --bitcoin-rpcconnect=127.0.0.1 \
    --bitcoin-rpcport=18443 \
    --bitcoin-datadir="$BITCOIN_DATADIR" \
    --daemon
done

echo "[INFO] Waiting for Lightning RPCs..."

for n in $(seq 1 "$NUM_NODES"); do
  for _ in {1..30}; do
    if "$LIGHTNING_CLI" \
         --network=regtest \
         --lightning-dir="$LN_RUNTIME/lightning/node-$n" \
         getinfo >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
done

echo "[SUCCESS] Bitcoin + Lightning fully started and synchronized"
