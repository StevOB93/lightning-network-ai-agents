#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# create_network.sh
#
# Deterministic Lightning regtest network preparation.
#
# Responsibilities:
# - Ensure Bitcoin wallet exists and is loaded
# - Mine regtest blocks (cached or fresh)
# - Wait for Lightning nodes to be chain-ready
# - Fund Lightning wallets
# - Connect nodes
# - Open channels
# - Confirm channels reach CHANNELD_NORMAL
#
# NEVER starts daemons.
# ALWAYS fails loudly.
#
###############################################################################

### --- Arguments --------------------------------------------------------------

if [[ $# -ne 1 ]]; then
  echo "[FATAL] Usage: $0 <num_nodes>"
  exit 1
fi

NUM_NODES="$1"

if ! [[ "$NUM_NODES" =~ ^[1-9][0-9]*$ ]]; then
  echo "[FATAL] num_nodes must be a positive integer"
  exit 1
fi

### --- Environment ------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

BITCOIN_DIR="$LN_RUNTIME/bitcoin/shared"
LIGHTNING_BASE="$LN_RUNTIME/lightning"

BITCOIN_WALLET="regtest-wallet"

BITCOIN_CLI_BASE="bitcoin-cli -regtest -datadir=$BITCOIN_DIR"
BITCOIN_CLI_WALLET="$BITCOIN_CLI_BASE -rpcwallet=$BITCOIN_WALLET"

### --- Logging ---------------------------------------------------------------

log()   { echo "[INFO] $*"; }
fatal() { echo "[FATAL] $*" >&2; exit 1; }

### --- Bitcoin RPC ------------------------------------------------------------

log "Creating Lightning network with $NUM_NODES nodes"
log "Bitcoin backend: shared"
log "Mode: cached"

BTC_HEIGHT="$($BITCOIN_CLI_BASE getblockcount 2>/dev/null || true)"
[[ -z "$BTC_HEIGHT" ]] && fatal "Bitcoin RPC not available — run start.sh first"

IBD="$($BITCOIN_CLI_BASE getblockchaininfo | jq -r '.initialblockdownload')"

### --- Bitcoin wallet ---------------------------------------------------------

if $BITCOIN_CLI_BASE listwallets | jq -e ".[] | select(. == \"$BITCOIN_WALLET\")" >/dev/null; then
  log "Bitcoin wallet already loaded"
elif $BITCOIN_CLI_BASE listwalletdir | jq -e ".wallets[].name | select(. == \"$BITCOIN_WALLET\")" >/dev/null; then
  log "Loading existing Bitcoin wallet"
  $BITCOIN_CLI_BASE loadwallet "$BITCOIN_WALLET" >/dev/null
else
  log "Creating Bitcoin wallet"
  $BITCOIN_CLI_BASE createwallet "$BITCOIN_WALLET" >/dev/null
fi

### --- Regtest blocks ---------------------------------------------------------

if [[ "$BTC_HEIGHT" -lt 150 ]]; then
  log "Mining blocks to bootstrap regtest chain"
  ADDR="$($BITCOIN_CLI_WALLET getnewaddress)"
  $BITCOIN_CLI_BASE generatetoaddress 150 "$ADDR" >/dev/null
  BTC_HEIGHT="$($BITCOIN_CLI_BASE getblockcount)"
else
  log "Using cached regtest chain ($BTC_HEIGHT blocks)"
fi

### --- Lightning sync gate ----------------------------------------------------

log "Waiting for Lightning nodes to be chain-ready..."

SYNC_TIMEOUT=60

for i in $(seq 1 "$NUM_NODES"); do
  LN_DIR="$LIGHTNING_BASE/node-$i"
  LN_CLI="lightning-cli --network=regtest --lightning-dir=$LN_DIR"

  elapsed=0
  while true; do
    INFO="$($LN_CLI getinfo 2>/dev/null || true)"
    [[ -z "$INFO" ]] && fatal "Lightning RPC unreachable for node-$i"

    LN_HEIGHT="$(echo "$INFO" | jq -r '.blockheight')"
    SYNCED="$(echo "$INFO" | jq -r '.synced_to_chain')"
    WARN="$(echo "$INFO" | jq -r '.warning_bitcoind_sync // empty')"

    if [[ "$SYNCED" == "true" && -z "$WARN" ]]; then
      break
    fi

    if [[ "$LN_HEIGHT" == "$BTC_HEIGHT" && "$IBD" == "false" ]]; then
      log "node-$i using cached-chain sync path"
      break
    fi

    [[ "$elapsed" -ge "$SYNC_TIMEOUT" ]] && fatal "Lightning node-$i did not become chain-ready"

    sleep 1
    elapsed=$((elapsed + 1))
  done

  log "Lightning node-$i chain-ready"
done

### --- Fund Lightning wallets -------------------------------------------------

log "Funding Lightning wallets"

for i in $(seq 1 "$NUM_NODES"); do
  LN_DIR="$LIGHTNING_BASE/node-$i"
  LN_CLI="lightning-cli --network=regtest --lightning-dir=$LN_DIR"

  BAL="$($LN_CLI listfunds | jq '[.outputs[].amount_msat] | add // 0')"

  if [[ "$BAL" -gt 0 ]]; then
    log "node-$i already funded"
    continue
  fi

  ADDR="$($LN_CLI newaddr | jq -r '.bech32')"
  $BITCOIN_CLI_WALLET sendtoaddress "$ADDR" 1 >/dev/null
done

log "Mining blocks to confirm Lightning funds"
ADDR="$($BITCOIN_CLI_WALLET getnewaddress)"
$BITCOIN_CLI_BASE generatetoaddress 6 "$ADDR" >/dev/null

### --- Connect nodes ----------------------------------------------------------

log "Connecting Lightning nodes"

for i in $(seq 2 "$NUM_NODES"); do
  SRC="$LIGHTNING_BASE/node-1"
  DST="$LIGHTNING_BASE/node-$i"

  SRC_CLI="lightning-cli --network=regtest --lightning-dir=$SRC"
  DST_ID="$(lightning-cli --network=regtest --lightning-dir=$DST getinfo | jq -r '.id')"
  PORT=$((9735 + i - 1))

  if ! $SRC_CLI listpeers | jq -e ".peers[] | select(.id == \"$DST_ID\")" >/dev/null; then
    $SRC_CLI connect "$DST_ID@127.0.0.1:$PORT" >/dev/null
  fi
done

### --- Open channels ----------------------------------------------------------

log "Opening Lightning channels"

CHANNEL_SATS=100000

for i in $(seq 2 "$NUM_NODES"); do
  SRC="$LIGHTNING_BASE/node-1"
  DST="$LIGHTNING_BASE/node-$i"

  SRC_CLI="lightning-cli --network=regtest --lightning-dir=$SRC"
  DST_ID="$(lightning-cli --network=regtest --lightning-dir=$DST getinfo | jq -r '.id')"

  if ! $SRC_CLI listchannels | jq -e ".channels[] | select(.destination == \"$DST_ID\")" >/dev/null; then
    $SRC_CLI fundchannel "$DST_ID" "$CHANNEL_SATS" >/dev/null
  fi
done

log "Mining blocks to confirm channels"
ADDR="$($BITCOIN_CLI_WALLET getnewaddress)"
$BITCOIN_CLI_BASE generatetoaddress 6 "$ADDR" >/dev/null

### --- Channel state verification ---------------------------------------------

log "Waiting for channels to reach CHANNELD_NORMAL"

SRC="$LIGHTNING_BASE/node-1"
SRC_CLI="lightning-cli --network=regtest --lightning-dir=$SRC"

elapsed=0
while true; do
  BAD="$($SRC_CLI listchannels | jq '[.channels[] | select(.state != "CHANNELD_NORMAL")] | length')"
  [[ "$BAD" -eq 0 ]] && break
  [[ "$elapsed" -ge 60 ]] && fatal "Channels did not reach CHANNELD_NORMAL"
  sleep 1
  elapsed=$((elapsed + 1))
done

log "Lightning network successfully created"
