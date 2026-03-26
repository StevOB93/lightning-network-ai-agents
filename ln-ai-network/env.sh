#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load local .env if present (DO NOT COMMIT .env)
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
  # Restrict .env to owner-only (contains API keys and password hashes)
  chmod 600 "$PROJECT_ROOT/.env" 2>/dev/null || true
fi

# Deterministic paths
export RUNTIME_DIR="$PROJECT_ROOT/runtime"
export BITCOIN_DIR="$RUNTIME_DIR/bitcoin/shared"
export LIGHTNING_BASE="$RUNTIME_DIR/lightning"

# Ports (override in .env if desired)
export BITCOIN_RPC_PORT="${BITCOIN_RPC_PORT:-18443}"
export BITCOIN_P2P_PORT="${BITCOIN_P2P_PORT:-18444}"
export LIGHTNING_BASE_PORT="${LIGHTNING_BASE_PORT:-9735}"

# Regtest only
export NETWORK="${NETWORK:-regtest}"
export LN_RUNTIME="$RUNTIME_DIR"
export BITCOIN_RPC_USER="${BITCOIN_RPC_USER:-lnrpc}"
export BITCOIN_RPC_PASSWORD="${BITCOIN_RPC_PASSWORD:-lnrpcpass}"
export BITCOIN_RPC_HOST="${BITCOIN_RPC_HOST:-127.0.0.1}"

# Regtest funding defaults (override in .env if desired)
export CONF_BLOCKS="${CONF_BLOCKS:-6}"                     # blocks to mine for confirmations
export CHANNEL_FUNDING_SAT="${CHANNEL_FUNDING_SAT:-1000000}" # satoshis per channel open
export NODE_FUNDING_BTC="${NODE_FUNDING_BTC:-10}"           # BTC to fund node-1 at boot

# Lightning node bind and announce addresses — controls cross-machine peer connectivity.
#
# LN_BIND_HOST: the IP address lightningd binds its peer-to-peer port on.
#   "127.0.0.1" (default) means only processes on this machine can connect as peers.
#   "0.0.0.0"             accepts connections on all interfaces (LAN/WAN).
#   A specific LAN IP     (e.g. "192.168.1.10") restricts binding to one NIC.
#
# LN_ANNOUNCE_HOST: the IP or hostname advertised to the Lightning gossip network
#   so remote peers know where to reach this node's listening port.
#   Defaults to LN_BIND_HOST, which is correct for single-machine setups.
#   Override when behind NAT: set LN_ANNOUNCE_HOST to your public IP or DDNS hostname
#   while keeping LN_BIND_HOST=0.0.0.0 so the OS binds all interfaces but advertises
#   the externally-reachable address to peers.
#
# To connect nodes across two machines:
#   On each machine, add to .env:
#     LN_BIND_HOST=0.0.0.0
#     LN_ANNOUNCE_HOST=<this-machine's-public-or-LAN-IP>
#   Then open the Lightning port (LIGHTNING_BASE_PORT + node number) in your firewall.
export LN_BIND_HOST="${LN_BIND_HOST:-127.0.0.1}"
export LN_ANNOUNCE_HOST="${LN_ANNOUNCE_HOST:-$LN_BIND_HOST}"

# Safety check: prevent running with placeholder key
# Support both LLM_BACKEND (current) and LLM_PROVIDER (legacy)
_LLM_BACKEND="${LLM_BACKEND:-${LLM_PROVIDER:-}}"
if [[ "${_LLM_BACKEND}" == "gemini" ]]; then
  if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "[env.sh] ERROR: GEMINI_API_KEY not set. Create .env from .env.example and set it." >&2
  elif [[ "${GEMINI_API_KEY}" == "__REPLACE_WITH_REAL_KEY__" ]]; then
    echo "[env.sh] ERROR: GEMINI_API_KEY still placeholder. Set a real key in your local .env." >&2
  fi
elif [[ "${_LLM_BACKEND}" == "openai" ]]; then
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "[env.sh] ERROR: OPENAI_API_KEY not set. Create .env from .env.example and set it." >&2
  elif [[ "${OPENAI_API_KEY}" == "__REPLACE_WITH_REAL_KEY__" ]]; then
    echo "[env.sh] ERROR: OPENAI_API_KEY still placeholder. Set a real key in your local .env." >&2
  fi
elif [[ "${_LLM_BACKEND}" == "claude" || "${_LLM_BACKEND}" == "anthropic" ]]; then
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "[env.sh] ERROR: ANTHROPIC_API_KEY not set. Add it to your local .env file." >&2
  elif [[ "${ANTHROPIC_API_KEY}" == "__REPLACE_WITH_REAL_KEY__" || "${ANTHROPIC_API_KEY}" == "__PASTE_YOUR_ANTHROPIC_KEY_HERE__" ]]; then
    echo "[env.sh] ERROR: ANTHROPIC_API_KEY still placeholder. Paste your real key in .env." >&2
  fi
fi
