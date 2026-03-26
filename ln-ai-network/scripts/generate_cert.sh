#!/usr/bin/env bash
# Generate a self-signed TLS certificate for development/demo use.
# The cert and key are written to runtime/tls/ (gitignored).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TLS_DIR="$PROJECT_ROOT/runtime/tls"
mkdir -p "$TLS_DIR"

CERT="$TLS_DIR/cert.pem"
KEY="$TLS_DIR/key.pem"

if [[ -f "$CERT" && -f "$KEY" ]]; then
  echo "[generate_cert] Certificate already exists at $CERT"
  echo "[generate_cert] Delete and re-run to regenerate."
  exit 0
fi

openssl req -x509 \
  -newkey rsa:2048 \
  -keyout "$KEY" \
  -out "$CERT" \
  -days 365 \
  -nodes \
  -subj "/CN=localhost"

chmod 600 "$KEY"
chmod 644 "$CERT"

echo "[generate_cert] Self-signed certificate generated:"
echo "  Cert: $CERT"
echo "  Key:  $KEY"
echo ""
echo "Add these to your .env to enable HTTPS:"
echo "  UI_TLS_CERT=$CERT"
echo "  UI_TLS_KEY=$KEY"
