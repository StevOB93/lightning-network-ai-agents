#!/usr/bin/env bash
#
# discover_nodes.sh
#
# Discovers all running Lightning nodes under LN_RUNTIME
# and emits a JSON map suitable for automation and AI control.
#
# This script is READ-ONLY.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

echo "{"

FIRST=1

for LN_DIR in "$LN_RUNTIME/lightning"/node-*; do
  [ -d "$LN_DIR" ] || continue

  NODE_NAME="$(basename "$LN_DIR")"

  if ! lightning-cli --lightning-dir="$LN_DIR" getinfo >/dev/null 2>&1; then
    continue
  fi

  INFO="$(lightning-cli --lightning-dir="$LN_DIR" getinfo)"
  NODE_ID="$(echo "$INFO" | jq -r '.id')"
  ALIAS="$(echo "$INFO" | jq -r '.alias')"
  ADDR="$(echo "$INFO" | jq -r '.binding[0].address + ":" + (.binding[0].port|tostring)')"

  if [ "$FIRST" -eq 0 ]; then
    echo ","
  fi
  FIRST=0

  cat <<EOF
  "$NODE_NAME": {
    "id": "$NODE_ID",
    "alias": "$ALIAS",
    "address": "$ADDR"
  }
EOF
done

echo
echo "}"
