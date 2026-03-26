#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# INTERNAL: called by scripts/1.start.sh — do not run directly
###############################################################################
if [[ "${LN_AI_INTERNAL_CALL:-0}" != "1" ]]; then
  echo "[FATAL] This script is internal. Run: ./scripts/1.start.sh"
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -f "$PROJECT_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env.sh"
else
  echo "[FATAL] env.sh not found at $PROJECT_ROOT/env.sh"
  exit 1
fi

NODE_COUNT="${1:?Must specify node count}"

# Validate NODE_COUNT is a positive integer
if ! [[ "$NODE_COUNT" =~ ^[0-9]+$ ]] || [[ "$NODE_COUNT" -lt 1 ]]; then
  echo "[FATAL] NODE_COUNT must be a positive integer. Got: '$NODE_COUNT'"
  exit 2
fi

# Minimum block height to reach before the network is considered ready.
# 1200 is the standard regtest baseline (sufficient coinbase maturity for all
# test operations). Override with REGTEST_TARGET_HEIGHT in .env to speed up
# quick local dev runs or use a higher value for specific test scenarios.
TARGET_HEIGHT="${REGTEST_TARGET_HEIGHT:-1200}"
WALLET_NAME="shared-wallet"

# MUST match MCP server defaults and other scripts unless overridden via env.sh/.env
RPC_USER="${BITCOIN_RPC_USER:-lnrpc}"
RPC_PASS="${BITCOIN_RPC_PASSWORD:-lnrpcpass}"

have_cmd() { command -v "$1" >/dev/null 2>&1; }

echo "=================================================="
echo "INFRA BOOT"
echo "Nodes (dirs): $NODE_COUNT"
echo "Node autostart: node-1 only (AI controls the rest)"
echo "=================================================="

###############################################################################
# DIRECTORY STRUCTURE (from env.sh)
###############################################################################
: "${RUNTIME_DIR:?env.sh must set RUNTIME_DIR}"
: "${BITCOIN_DIR:?env.sh must set BITCOIN_DIR}"
: "${LIGHTNING_BASE:?env.sh must set LIGHTNING_BASE}"
: "${BITCOIN_RPC_PORT:?env.sh must set BITCOIN_RPC_PORT}"
: "${BITCOIN_P2P_PORT:?env.sh must set BITCOIN_P2P_PORT}"
: "${LIGHTNING_BASE_PORT:?env.sh must set LIGHTNING_BASE_PORT}"

mkdir -p "$RUNTIME_DIR"
mkdir -p "$BITCOIN_DIR"
mkdir -p "$LIGHTNING_BASE"

###############################################################################
# START BITCOIN
###############################################################################
echo "[INFRA] Ensuring bitcoind running..."

if ! have_cmd bitcoin-cli || ! have_cmd bitcoind; then
  echo "[FATAL] bitcoin-cli/bitcoind not found. Run ./scripts/0.install.sh"
  exit 127
fi

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
  -rpcport="$BITCOIN_RPC_PORT" \
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

  WAIT_SECS=0
  MAX_WAIT=60
  until bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcport="$BITCOIN_RPC_PORT" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    getblockchaininfo >/dev/null 2>&1; do
    sleep 1
    WAIT_SECS=$((WAIT_SECS + 1))
    if [[ $WAIT_SECS -ge $MAX_WAIT ]]; then
      echo "[FATAL] bitcoind RPC not ready after ${MAX_WAIT}s — aborting"
      exit 1
    fi
  done
fi

echo "[INFRA] Bitcoin RPC ready."

###############################################################################
# WALLET
###############################################################################
if ! have_cmd jq; then
  echo "[FATAL] jq not found. Install it (apt install -y jq) or run ./scripts/0.install.sh"
  exit 127
fi

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
  -rpcport="$BITCOIN_RPC_PORT" \
  -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
  listwalletdir | jq -e ".wallets[] | select(.name==\"$WALLET_NAME\")" >/dev/null 2>&1; then

  bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcport="$BITCOIN_RPC_PORT" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    createwallet "$WALLET_NAME" >/dev/null
fi

if ! bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
  -rpcport="$BITCOIN_RPC_PORT" \
  -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
  listwallets | grep -q "\"$WALLET_NAME\""; then

  bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcport="$BITCOIN_RPC_PORT" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    loadwallet "$WALLET_NAME" >/dev/null 2>&1 || true
fi

###############################################################################
# ENSURE BLOCK HEIGHT
###############################################################################
CURRENT_HEIGHT="$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
  -rpcport="$BITCOIN_RPC_PORT" \
  -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
  getblockcount)"

if [[ "$CURRENT_HEIGHT" -lt "$TARGET_HEIGHT" ]]; then
  MINE=$((TARGET_HEIGHT - CURRENT_HEIGHT))
  ADDR="$(bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcport="$BITCOIN_RPC_PORT" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    -rpcwallet="$WALLET_NAME" getnewaddress)"

  bitcoin-cli -regtest -datadir="$BITCOIN_DIR" \
    -rpcport="$BITCOIN_RPC_PORT" \
    -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
    generatetoaddress "$MINE" "$ADDR" >/dev/null
fi

echo "[INFRA] Block height sufficient."

###############################################################################
# CREATE NODE DIRECTORIES FOR 1..N (AI will start/manage most nodes)
###############################################################################
echo "[INFRA] Ensuring node directories exist under $LIGHTNING_BASE ..."
for i in $(seq 1 "$NODE_COUNT"); do
  mkdir -p "$LIGHTNING_BASE/node-$i"
done

###############################################################################
# START ONLY node-1 (required for control-plane readiness check)
###############################################################################
if ! have_cmd lightningd || ! have_cmd lightning-cli; then
  echo "[FATAL] lightningd/lightning-cli not found. Run ./scripts/0.install.sh"
  exit 127
fi

NODE1_DIR="$LIGHTNING_BASE/node-1"
NODE1_PORT="$LIGHTNING_BASE_PORT"
NODE1_LOG="$NODE1_DIR/lightningd.log"

if ! lightning-cli --network=regtest --lightning-dir="$NODE1_DIR" getinfo >/dev/null 2>&1; then
  echo "[INFRA] Starting lightningd node-1 (required baseline)..."

  # LN_BIND_HOST / LN_ANNOUNCE_HOST come from env.sh (sourced above).
  # Default "127.0.0.1" keeps single-machine behaviour — no inbound connections from
  # other machines.  Set both in .env to allow cross-machine Lightning peer connections:
  #   LN_BIND_HOST=0.0.0.0          (bind all interfaces)
  #   LN_ANNOUNCE_HOST=<public-ip>  (advertise the externally-reachable address)
  #
  # Address flag logic:
  #   --addr=HOST:PORT       → binds AND announces (use when bind == announce)
  #   --bind-addr=HOST:PORT  → binds only, no announce (use to separate the two)
  # Passing both --bind-addr and --addr for the SAME address causes lightningd to
  # report "Duplicate announce address", so we only use --bind-addr when the hosts
  # actually differ.
  if [[ "$LN_BIND_HOST" == "$LN_ANNOUNCE_HOST" ]]; then
    LN_ADDR_ARGS=("--addr=${LN_BIND_HOST}:${NODE1_PORT}")
  else
    LN_ADDR_ARGS=(
      "--bind-addr=${LN_BIND_HOST}:${NODE1_PORT}"
      "--addr=${LN_ANNOUNCE_HOST}:${NODE1_PORT}"
    )
  fi

  lightningd \
    --network=regtest \
    --lightning-dir="$NODE1_DIR" \
    "${LN_ADDR_ARGS[@]}" \
    --bitcoin-rpcconnect=127.0.0.1 \
    --bitcoin-rpcport="$BITCOIN_RPC_PORT" \
    --bitcoin-rpcuser="$RPC_USER" \
    --bitcoin-rpcpassword="$RPC_PASS" \
    --bitcoin-datadir="$BITCOIN_DIR" \
    --log-file="$NODE1_LOG" &

  until lightning-cli --network=regtest --lightning-dir="$NODE1_DIR" getinfo >/dev/null 2>&1; do
    sleep 1
  done
fi

echo "[INFRA] node-1 ready."

# ── FUND NODE-1 LIGHTNING WALLET ──────────────────────────────────────────────
# infra_boot mines blocks to the Bitcoin Core wallet but leaves the Lightning
# wallet empty. Fund it now so the agent can open channels immediately.
echo "[INFRA] Funding node-1 Lightning wallet (${NODE_FUNDING_BTC} BTC)..."
NODE1_ADDR=$(lightning-cli --network=regtest --lightning-dir="$LIGHTNING_BASE/node-1" newaddr | jq -r '.bech32')
MINER_ADDR=$(bitcoin-cli -regtest \
  -rpcconnect="${BITCOIN_RPC_HOST:-127.0.0.1}" -rpcport="$BITCOIN_RPC_PORT" \
  -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
  -rpcwallet="$WALLET_NAME" getnewaddress)
bitcoin-cli -regtest \
  -rpcconnect="${BITCOIN_RPC_HOST:-127.0.0.1}" -rpcport="$BITCOIN_RPC_PORT" \
  -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
  -rpcwallet="$WALLET_NAME" sendtoaddress "$NODE1_ADDR" "$NODE_FUNDING_BTC" >/dev/null
bitcoin-cli -regtest \
  -rpcconnect="${BITCOIN_RPC_HOST:-127.0.0.1}" -rpcport="$BITCOIN_RPC_PORT" \
  -rpcuser="$RPC_USER" -rpcpassword="$RPC_PASS" \
  generatetoaddress "$CONF_BLOCKS" "$MINER_ADDR" >/dev/null
echo "[INFRA] Node-1 Lightning wallet funded (${NODE_FUNDING_BTC} BTC confirmed)."

echo "[INFRA] Infrastructure ready."
echo "[INFRA] Note: nodes 2..$NODE_COUNT are NOT started here."
echo "[INFRA] The AI agent should start/stop nodes via MCP tools (ln_node_start/ln_node_stop)."