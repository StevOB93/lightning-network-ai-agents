#!/usr/bin/env bash
set -euo pipefail

############################################
# start.sh
#
# Responsibilities:
#   - Start Bitcoin Core (regtest)
#   - Create N managed Lightning nodes
#   - Ensure no hard-coded identities or ports
#   - Leave system ready for manual funding
#
# This script MUST be safe on a fresh clone.
############################################

echo "▶ Starting lightning-network-ai-agents"

# ------------------------------------------------------------
# Resolve important paths
# ------------------------------------------------------------

# Absolute path to scripts/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Absolute path to repo root
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load helper libraries
source "$SCRIPT_DIR/lib/port_allocator.sh"
source "$SCRIPT_DIR/lib/node_manager.sh"

# ------------------------------------------------------------
# Load network configuration
# ------------------------------------------------------------

CONFIG_FILE="$ROOT_DIR/config/network.defaults.yml"

# Extract values using simple parsing (YAML-lite)
NODE_COUNT=$(grep managed_nodes "$CONFIG_FILE" | awk '{print $2}')
BITCOIN_NETWORK=$(grep bitcoin_network "$CONFIG_FILE" | awk '{print $2}')

# ------------------------------------------------------------
# Define runtime paths (all ephemeral)
# ------------------------------------------------------------

RUNTIME_DIR="$ROOT_DIR/runtime"
NODES_DIR="$RUNTIME_DIR/nodes"
LOGS_DIR="$RUNTIME_DIR/logs"
SOCKETS_DIR="$RUNTIME_DIR/sockets"
PORTS_DIR="$RUNTIME_DIR/ports"

mkdir -p "$NODES_DIR" "$LOGS_DIR" "$SOCKETS_DIR" "$PORTS_DIR"

# ------------------------------------------------------------
# Start Bitcoin Core (regtest)
# ------------------------------------------------------------

# These credentials are LOCAL ONLY and exist only in runtime/
BITCOIN_RPC_PORT=18443
BITCOIN_RPC_USER=rpcuser
BITCOIN_RPC_PASSWORD=rpcpass

BITCOIND_DIR="$RUNTIME_DIR/bitcoind"
mkdir -p "$BITCOIND_DIR"

# Only start bitcoind if it isn't already running
if ! pgrep -f "bitcoind.*regtest" >/dev/null; then
  echo "▶ Starting bitcoind (regtest)"

  bitcoind \
    -regtest \
    -daemon \
    -datadir="$BITCOIND_DIR" \
    -rpcuser="$BITCOIN_RPC_USER" \
    -rpcpassword="$BITCOIN_RPC_PASSWORD" \
    -rpcport="$BITCOIN_RPC_PORT"
else
  echo "✔ bitcoind already running"
fi

# Give bitcoind time to initialize RPC
sleep 2

# ------------------------------------------------------------
# Start Lightning nodes
# ------------------------------------------------------------

for i in $(seq 1 "$NODE_COUNT"); do
  # Logical node identifier
  NODE_ID="node$i"

  echo "▶ Initializing $NODE_ID"

  # Create filesystem layout
  create_node_dirs "$NODE_ID"

  # Dynamically allocate ports and paths
  LIGHTNING_PORT=$(allocate_port)
  RPC_SOCKET="$SOCKETS_DIR/$NODE_ID.sock"
  LOG_FILE="$LOGS_DIR/$NODE_ID.log"
  NODE_DIR="$NODES_DIR/$NODE_ID"

  # Render node-specific config from template
  sed \
    -e "s|{{BITCOIN_NETWORK}}|$BITCOIN_NETWORK|g" \
    -e "s|{{BITCOIN_RPC_PORT}}|$BITCOIN_RPC_PORT|g" \
    -e "s|{{BITCOIN_RPC_USER}}|$BITCOIN_RPC_USER|g" \
    -e "s|{{BITCOIN_RPC_PASSWORD}}|$BITCOIN_RPC_PASSWORD|g" \
    -e "s|{{LIGHTNING_PORT}}|$LIGHTNING_PORT|g" \
    -e "s|{{RPC_SOCKET}}|$RPC_SOCKET|g" \
    -e "s|{{LOG_FILE}}|$LOG_FILE|g" \
    "$ROOT_DIR/config/cln/lightning.conf.tpl" \
    > "$NODE_DIR/lightning.conf"

  # Start Lightning node in daemon mode
  lightningd \
    --lightning-dir="$NODE_DIR" \
    --conf="$NODE_DIR/lightning.conf" \
    --daemon
done

# ------------------------------------------------------------
# Final status
# ------------------------------------------------------------

echo
echo "✅ All managed Lightning nodes are running"
echo
echo "Next steps (manual validation):"
echo "  1. Generate blocks with bitcoin-cli"
echo "  2. Get a new address from each node"
echo "  3. Fund nodes manually to confirm operation"
echo
