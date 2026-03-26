#!/usr/bin/env bash
# Interactive security setup for the Lightning Agent web UI.
#
# Prompts for an admin password, generates a session secret and master key,
# hashes the password with PBKDF2-SHA256, and writes the values to .env.
# Optionally generates a self-signed TLS certificate.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"

echo "============================================"
echo "  Lightning Agent — Security Setup"
echo "============================================"
echo ""

# Ensure .env exists
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$PROJECT_ROOT/.env.example" ]]; then
    cp "$PROJECT_ROOT/.env.example" "$ENV_FILE"
    echo "[setup] Created .env from .env.example"
  else
    touch "$ENV_FILE"
    echo "[setup] Created empty .env"
  fi
fi

# --- Admin password ---
echo "Set the admin password for the web dashboard."
echo "(This password will be hashed — it is never stored in plaintext.)"
echo ""
read -s -p "Admin password: " ADMIN_PW
echo ""
read -s -p "Confirm password: " ADMIN_PW2
echo ""

if [[ "$ADMIN_PW" != "$ADMIN_PW2" ]]; then
  echo "[ERROR] Passwords do not match. Aborting."
  exit 1
fi

if [[ ${#ADMIN_PW} -lt 8 ]]; then
  echo "[ERROR] Password must be at least 8 characters. Aborting."
  exit 1
fi

# --- Optional viewer password ---
echo ""
read -p "Set a read-only viewer password? (y/N): " SET_VIEWER
VIEWER_HASH=""
if [[ "${SET_VIEWER,,}" == "y" ]]; then
  read -s -p "Viewer password: " VIEWER_PW
  echo ""
  VIEWER_HASH=$(python3 -c "
import sys; sys.path.insert(0, '$PROJECT_ROOT')
from scripts.security import hash_password
print(hash_password('$VIEWER_PW'))
")
fi

# --- Generate secrets ---
SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ADMIN_HASH=$(python3 -c "
import sys; sys.path.insert(0, '$PROJECT_ROOT')
from scripts.security import hash_password
print(hash_password('$ADMIN_PW'))
")

echo ""
echo "[setup] Generated session secret and password hash."

# --- Write to .env ---
# Helper: set a key in .env (update existing or append)
set_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    # Update existing line
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  elif grep -q "^# *${key}=" "$ENV_FILE" 2>/dev/null; then
    # Uncomment and set
    sed -i "s|^# *${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    # Append
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

set_env "UI_ADMIN_PASSWORD_HASH" "$ADMIN_HASH"
set_env "UI_SESSION_SECRET" "$SESSION_SECRET"
set_env "UI_SESSION_TTL_S" "3600"

if [[ -n "$VIEWER_HASH" ]]; then
  set_env "UI_VIEWER_PASSWORD_HASH" "$VIEWER_HASH"
fi

# --- File permissions ---
chmod 600 "$ENV_FILE"
echo "[setup] Set .env permissions to 600 (owner-only)."

# --- Optional TLS ---
echo ""
read -p "Generate a self-signed TLS certificate? (y/N): " GEN_TLS
if [[ "${GEN_TLS,,}" == "y" ]]; then
  bash "$PROJECT_ROOT/scripts/generate_cert.sh"
  TLS_DIR="$PROJECT_ROOT/runtime/tls"
  set_env "UI_TLS_CERT" "$TLS_DIR/cert.pem"
  set_env "UI_TLS_KEY" "$TLS_DIR/key.pem"
fi

echo ""
echo "============================================"
echo "  Security setup complete!"
echo "============================================"
echo ""
echo "Auth:     Enabled (admin + ${VIEWER_HASH:+viewer}${VIEWER_HASH:-no viewer})"
echo "Password: Hashed with PBKDF2-SHA256 (600k iterations)"
echo "Session:  ${UI_SESSION_TTL_S:-3600}s TTL"
echo ""
echo "Start the UI server with:  python scripts/ui_server.py"
