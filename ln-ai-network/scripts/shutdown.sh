#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# shutdown.sh
#
# Gracefully shuts down:
# - All Core Lightning nodes
# - Shared Bitcoin Core regtest backend
#
# This script:
# - NEVER deletes runtime data
# - NEVER assumes working directory
# - FAILS loudly if something refuses to stop
#
###############################################################################

### --- Environment ------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

BITCOIN_DIR="$LN_RUNTIME/bitcoin/shared"
LIGHTNING_BASE="$LN_RUNTIME/lightning"

BITCOIN_CLI="bitcoin-cli -regtest -datadir=$BITCOIN_DIR"

### --- Logging ---------------------------------------------------------------

log()   { echo "[INFO] $*"; }
warn()  { echo "[WARN] $*" >&2; }
fatal() { echo "[FATAL] $*" >&2; exit 1; }

### --- Shutdown Lightning ----------------------------------------------------

log "Shutting down Lightning nodes"

if [[ -d "$LIGHTNING_BASE" ]]; then
  for LN_DIR in "$LIGHTNING_BASE"/node-*; do
    [[ ! -d "$LN_DIR" ]] && continue

    LN_CLI="lightning-cli --network=regtest --lightning-dir=$LN_DIR"

    if $LN_CLI getinfo >/dev/null 2>&1; then
      log "Stopping Lightning node: $(basename "$LN_DIR")"
      $LN_CLI stop >/dev/null || warn "Failed to stop $(basename "$LN_DIR") gracefully"
    fi
  done
fi

### --- Wait for lightningd to exit -------------------------------------------

log "Waiting for lightningd processes to exit"

for _ in $(seq 1 10); do
  if ! pgrep -f lightningd >/dev/null; then
    break
  fi
  sleep 1
done

if pgrep -f lightningd >/dev/null; then
  warn "Forcing remaining lightningd processes"
  pkill -TERM lightningd || true
fi

### --- Shutdown Bitcoin ------------------------------------------------------

log "Shutting down Bitcoin Core"

if $BITCOIN_CLI getblockcount >/dev/null 2>&1; then
  $BITCOIN_CLI stop >/dev/null || warn "Bitcoin Core did not shut down cleanly"
fi

### --- Wait for bitcoind to exit ---------------------------------------------

log "Waiting for bitcoind to exit"

for _ in $(seq 1 10); do
  if ! pgrep -f bitcoind >/dev/null; then
    break
  fi
  sleep 1
done

if pgrep -f bitcoind >/dev/null; then
  warn "Forcing remaining bitcoind process"
  pkill -TERM bitcoind || true
fi

### --- Final verification ----------------------------------------------------

if pgrep -f 'bitcoind|lightningd' >/dev/null; then
  fatal "One or more processes failed to shut down"
fi

log "All Lightning and Bitcoin processes shut down cleanly"
