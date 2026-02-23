#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load local .env if present (DO NOT COMMIT .env)
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
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

# Safety check: prevent running with placeholder key
if [[ "${LLM_PROVIDER:-}" == "openai" ]]; then
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "[env.sh] ERROR: OPENAI_API_KEY not set. Create .env from .env.example and set it." >&2
  elif [[ "${OPENAI_API_KEY}" == "__REPLACE_WITH_REAL_KEY__" ]]; then
    echo "[env.sh] ERROR: OPENAI_API_KEY still placeholder. Set a real key in your local .env." >&2
  fi
fi
