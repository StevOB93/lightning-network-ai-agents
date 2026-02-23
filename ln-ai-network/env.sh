#!/usr/bin/env bash

# ==============================================================
# env.sh
#
# Deterministic environment configuration
# Absolute paths derived from this file's location
# No reliance on pwd
# ==============================================================

set -e

# Get absolute path of project root (parent of this file)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------
# Runtime directories
# --------------------------------------------------------------

export RUNTIME_DIR="$PROJECT_ROOT/runtime"

export BITCOIN_DIR="$RUNTIME_DIR/bitcoin/shared"

export LIGHTNING_BASE="$RUNTIME_DIR/lightning"

# --------------------------------------------------------------
# Deterministic ports
# --------------------------------------------------------------

export BITCOIN_RPC_PORT=18443
export BITCOIN_P2P_PORT=18444

export LIGHTNING_BASE_PORT=9735

# --------------------------------------------------------------
# Regtest only enforcement
# --------------------------------------------------------------

export NETWORK="regtest"

export LN_RUNTIME="$RUNTIME_DIR"


