#!/usr/bin/env bash
###############################################################################
# startup.sh — FINAL, VERIFIED, BACKEND-SAFE
# Starts bitcoind, two Core Lightning nodes, then agents (in correct order)
###############################################################################

set -euo pipefail

###############################################################################
# CONFIG
###############################################################################

BASE="$HOME/LN_AI_Project"

BITCOIND_DIR="$BASE/data/bitcoind"
CLN1_DIR="$BASE/data/cln1"
CLN2_DIR="$BASE/data/cln2"

AGENT_DIR="$BASE/agents"
LOG_DIR="$BASE/logs"

NETWORK="regtest"

BITCOIND_BIN="bitcoind"
LIGHTNINGD_BIN="lightningd"
PYTHON_BIN="python3"

###############################################################################
# HELPERS
###############################################################################

die() {
  echo "❌ ERROR: $1" >&2
  exit 1
}

info() {
  echo "▶ $1"
}

wait_for_path() {
  local path="$1"
  local name="$2"

  info "Waiting for $name..."
  for _ in {1..60}; do
    if [ -e "$path" ]; then
      info "$name is ready"
      return
    fi
    sleep 1
  done

  die "Timed out waiting for $name ($path)"
}

###############################################################################
# PRE-FLIGHT CHECKS
###############################################################################

[ -d "$BASE" ] || die "Project directory not found: $BASE"
command -v "$BITCOIND_BIN" >/dev/null || die "bitcoind not in PATH"
command -v "$LIGHTNINGD_BIN" >/dev/null || die "lightningd not in PATH"
command -v "$PYTHON_BIN" >/dev/null || die "python3 not found"
[ -d "$AGENT_DIR" ] || die "agents directory missing: $AGENT_DIR"

###############################################################################
# DIRECTORY SETUP (MUST BE FIRST)
###############################################################################

info "Ensuring directory structure exists"

mkdir -p \
  "$BITCOIND_DIR" \
  "$CLN1_DIR" \
  "$CLN2_DIR" \
  "$LOG_DIR"

###############################################################################
# BITCOIN CORE (REGTEST)
###############################################################################

info "Starting Bitcoin Core ($NETWORK)"

if pgrep -x bitcoind >/dev/null; then
  info "bitcoind already running"
else
  "$BITCOIND_BIN" \
    -regtest \
    -daemon \
    -datadir="$BITCOIND_DIR" \
    > "$LOG_DIR/bitcoind.log" 2>&1
fi

# Bitcoin RPC readiness = cookie file exists
wait_for_path "$BITCOIND_DIR/regtest/.cookie" "Bitcoin Core RPC"

###############################################################################
# CORE LIGHTNING NODES
###############################################################################

start_cln() {
  local dir="$1"
  local port="$2"
  local name="$3"
  local log="$LOG_DIR/$name.log"

  local rpc="$dir/$NETWORK/lightning-rpc"

  if [ -S "$rpc" ]; then
    info "$name already running"
    return
  fi

  info "Starting $name"

  "$LIGHTNINGD_BIN" \
    --network="$NETWORK" \
    --lightning-dir="$dir" \
    --bitcoin-datadir="$BITCOIND_DIR" \
    --bind-addr="127.0.0.1:$port" \
    --disable-dns \
    --log-file="$log" \
    --daemon
}

start_cln "$CLN1_DIR" 9735 "cln1"
start_cln "$CLN2_DIR" 9737 "cln2"

# Wait for CLN RPC sockets (CRITICAL)
wait_for_path "$CLN1_DIR/$NETWORK/lightning-rpc" "CLN1 RPC socket"
wait_for_path "$CLN2_DIR/$NETWORK/lightning-rpc" "CLN2 RPC socket"

###############################################################################
# AGENTS (START LAST)
###############################################################################

start_agent() {
  local dir="$1"
  local name="$2"
  local log="$LOG_DIR/$name-agent.log"

  if pgrep -f "alternating_agent.py $dir" >/dev/null; then
    info "$name agent already running"
    return
  fi

  info "Starting $name agent"

  "$PYTHON_BIN" "$AGENT_DIR/alternating_agent.py" "$dir" \
    > "$log" 2>&1 &
}

start_agent "$CLN1_DIR" "cln1"
start_agent "$CLN2_DIR" "cln2"

###############################################################################
# DONE
###############################################################################

info "✅ Startup complete"
info "Logs available in: $LOG_DIR"
