#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# network_test.sh
#
# Read-only Lightning regtest health check.
#
# Responsibilities:
# - Verify Bitcoin RPC
# - Verify Lightning RPC for all nodes
# - Verify chain readiness
# - Verify peers and channels
# - Perform a minimal test payment (1 sat)
#
# This script:
# - Is FAST
# - Is NON-DESTRUCTIVE
# - NEVER creates wallets
# - NEVER mines blocks
# - NEVER modifies topology
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

BITCOIN_CLI="bitcoin-cli -regtest -datadir=$BITCOIN_DIR"

### --- Logging ---------------------------------------------------------------

log()   { echo "[INFO] $*"; }
fatal() { echo "[FATAL] $*" >&2; }

### --- Bitcoin RPC ------------------------------------------------------------

log "Checking Bitcoin RPC"

if ! BTC_HEIGHT="$($BITCOIN_CLI getblockcount 2>/dev/null)"; then
  fatal "Bitcoin RPC unreachable"
  exit 10
fi

IBD="$($BITCOIN_CLI getblockchaininfo | jq -r '.initialblockdownload')"

log "Bitcoin height: $BTC_HEIGHT"

### --- Lightning RPC + chain readiness ---------------------------------------

log "Checking Lightning nodes"

for i in $(seq 1 "$NUM_NODES"); do
  LN_DIR="$LIGHTNING_BASE/node-$i"
  LN_CLI="lightning-cli --network=regtest --lightning-dir=$LN_DIR"

  if ! INFO="$($LN_CLI getinfo 2>/dev/null)"; then
    fatal "Lightning RPC unreachable for node-$i"
    exit 20
  fi

  LN_HEIGHT="$(echo "$INFO" | jq -r '.blockheight')"
  SYNCED="$(echo "$INFO" | jq -r '.synced_to_chain')"
  WARN="$(echo "$INFO" | jq -r '.warning_bitcoind_sync // empty')"

  # Chain readiness (same logic as create_network.sh)
  if [[ "$SYNCED" == "true" && -z "$WARN" ]]; then
    log "node-$i chain-ready (normal path)"
  elif [[ "$LN_HEIGHT" == "$BTC_HEIGHT" && "$IBD" == "false" ]]; then
    log "node-$i chain-ready (cached-chain path)"
  else
    fatal "node-$i not chain-ready"
    exit 30
  fi
done

### --- Peer topology ----------------------------------------------------------

log "Checking peer connectivity"

SRC_DIR="$LIGHTNING_BASE/node-1"
SRC_CLI="lightning-cli --network=regtest --lightning-dir=$SRC_DIR"

for i in $(seq 2 "$NUM_NODES"); do
  DST_DIR="$LIGHTNING_BASE/node-$i"
  DST_ID="$(lightning-cli --network=regtest --lightning-dir=$DST_DIR getinfo | jq -r '.id')"

  if ! $SRC_CLI listpeers | jq -e ".peers[] | select(.id == \"$DST_ID\" and .connected == true)" >/dev/null; then
    fatal "node-1 not connected to node-$i"
    exit 40
  fi
done

### --- Channel state ----------------------------------------------------------

log "Checking channel states"

BAD="$($SRC_CLI listchannels | jq '[.channels[] | select(.state != "CHANNELD_NORMAL")] | length')"

if [[ "$BAD" -ne 0 ]]; then
  fatal "One or more channels not in CHANNELD_NORMAL"
  exit 50
fi

### --- Test payment -----------------------------------------------------------

log "Performing test payment (1 sat)"

DST_DIR="$LIGHTNING_BASE/node-$NUM_NODES"
DST_CLI="lightning-cli --network=regtest --lightning-dir=$DST_DIR"

INVOICE="$($DST_CLI invoice 1 test-sat test-sat | jq -r '.bolt11')"

if ! $SRC_CLI pay "$INVOICE" >/dev/null; then
  fatal "Test payment failed"
  exit 60
fi

log "Test payment succeeded"

### --- Success ---------------------------------------------------------------

log "Lightning network health: OK"
exit 0
