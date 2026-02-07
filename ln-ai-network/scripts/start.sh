#!/usr/bin/env bash
#
# start.sh
#
# Start N isolated Bitcoin Core + Core Lightning nodes on REGTEST.
#
# HARD GUARANTEES:
# - One bitcoind per lightningd
# - No use of ~/.bitcoin or ~/.lightning
# - Explicit configs only
# - Deterministic ports
# - Scales to N nodes
# - Runnable from any directory
#

set -u

#######################################
# Resolve project environment
#######################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

#######################################
# Node count (default = 1)
#######################################
NODE_COUNT="${1:-1}"

#######################################
# Deterministic port bases
#######################################
BITCOIN_RPC_BASE=18443
BITCOIN_P2P_BASE=18444
LIGHTNING_P2P_BASE=9735

#######################################
# Track failures explicitly
#######################################
FAILED_NODES=()

echo "[INFO] Starting $NODE_COUNT Bitcoin + Lightning regtest nodes"

###############################################################################
# BITCOIN CORE STARTUP
###############################################################################
echo "[INFO] Starting Bitcoin Core nodes..."

for i in $(seq 1 "$NODE_COUNT"); do
  NODE="node-$i"

  BTC_DIR="$LN_RUNTIME/bitcoin/$NODE"
  BTC_LOG="$LN_LOGS/bitcoin/$NODE"

  RPC_PORT=$((BITCOIN_RPC_BASE + i - 1))
  P2P_PORT=$((BITCOIN_P2P_BASE + i - 1))

  mkdir -p "$BTC_DIR" "$BTC_LOG"

  #
  # IMPORTANT:
  # - regtest=1 MUST be global
  # - [regtest] only overrides values
  # - rpccookiefile MUST be unique per node
  #
  cat >"$BTC_DIR/bitcoin.conf" <<EOF
# -------------------------------
# GLOBAL SETTINGS
# -------------------------------
regtest=1
daemon=1
server=1
txindex=1
fallbackfee=0.0002
rpccookiefile=$BTC_DIR/rpccookie

# -------------------------------
# REGTEST-SPECIFIC OVERRIDES
# -------------------------------
[regtest]
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=$RPC_PORT
port=$P2P_PORT
EOF

  echo "[INFO] Starting bitcoind for $NODE (RPC $RPC_PORT, P2P $P2P_PORT)"

  bitcoind \
    -conf="$BTC_DIR/bitcoin.conf" \
    -datadir="$BTC_DIR" \
    >>"$BTC_LOG/bitcoind.log" 2>&1 &
done

###############################################################################
# WAIT FOR BITCOIN RPC AVAILABILITY
###############################################################################
echo "[INFO] Waiting for Bitcoin RPCs..."

for i in $(seq 1 "$NODE_COUNT"); do
  BTC_DIR="$LN_RUNTIME/bitcoin/node-$i"
  READY=0

  for _ in {1..40}; do
    if bitcoin-cli -regtest -datadir="$BTC_DIR" getblockchaininfo >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 0.5
  done

  if [ "$READY" -ne 1 ]; then
    FAILED_NODES+=("node-$i")
  fi
done

if [ "${#FAILED_NODES[@]}" -ne 0 ]; then
  echo
  echo "[FATAL] The following Bitcoin nodes failed to start:"
  for n in "${FAILED_NODES[@]}"; do
    echo "  - $n (check logs/bitcoin/$n/bitcoind.log)"
  done
  exit 1
fi

###############################################################################
# CORE LIGHTNING STARTUP
###############################################################################
echo "[INFO] Starting Core Lightning nodes..."

for i in $(seq 1 "$NODE_COUNT"); do
  NODE="node-$i"

  BTC_DIR="$LN_RUNTIME/bitcoin/$NODE"
  LN_DIR="$LN_RUNTIME/lightning/$NODE"
  LN_LOG="$LN_LOGS/lightning/$NODE"

  BTC_RPC_PORT=$((BITCOIN_RPC_BASE + i - 1))
  LN_P2P_PORT=$((LIGHTNING_P2P_BASE + i - 1))

  mkdir -p "$LN_DIR" "$LN_LOG"

  #
  # CRITICAL:
  # - bitcoin-datadir tells lightning where the cookie + chain live
  # - without this, bcli will try ~/.bitcoin and fail
  #
  cat >"$LN_DIR/config" <<EOF
network=regtest
lightning-dir=$LN_DIR

# Lightning P2P
addr=127.0.0.1:$LN_P2P_PORT
log-level=debug

# Explicit Bitcoin backend wiring
bitcoin-datadir=$BTC_DIR
bitcoin-rpcconnect=127.0.0.1
bitcoin-rpcport=$BTC_RPC_PORT

# Safety / determinism
disable-plugin=cln-grpc
EOF

  echo "[INFO] Starting lightningd for $NODE (P2P $LN_P2P_PORT)"

  lightningd \
    --conf="$LN_DIR/config" \
    >>"$LN_LOG/lightningd.log" 2>&1 &
done

###############################################################################
# WAIT FOR LIGHTNING RPC AVAILABILITY
###############################################################################
echo "[INFO] Waiting for Lightning RPCs..."

for i in $(seq 1 "$NODE_COUNT"); do
  LN_DIR="$LN_RUNTIME/lightning/node-$i"
  until lightning-cli --lightning-dir="$LN_DIR" getinfo >/dev/null 2>&1; do
    sleep 0.5
  done
done

###############################################################################
# SUCCESS SUMMARY
###############################################################################
echo
echo "[SUCCESS] Network started cleanly:"
for i in $(seq 1 "$NODE_COUNT"); do
  echo "  - node-$i (bitcoin RPC $((BITCOIN_RPC_BASE + i - 1)), lightning $((LIGHTNING_P2P_BASE + i - 1)))"
done
echo
