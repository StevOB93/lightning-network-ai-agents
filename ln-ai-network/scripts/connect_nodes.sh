#!/usr/bin/env bash
#
# connect_nodes.sh
#
# Connects Lightning nodes using an explicit topology.
#
# USAGE:
#   ./connect_nodes.sh mesh
#   ./connect_nodes.sh star
#   ./connect_nodes.sh ring
#

set -euo pipefail

TOPOLOGY="${1:-mesh}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

DISCOVERY_JSON="$("$SCRIPT_DIR/discover_nodes.sh")"

NODE_NAMES=($(echo "$DISCOVERY_JSON" | jq -r 'keys[]'))

connect() {
  local SRC="$1"
  local DST="$2"

  SRC_DIR="$LN_RUNTIME/lightning/$SRC"

  DST_ID="$(echo "$DISCOVERY_JSON" | jq -r ".\"$DST\".id")"
  DST_ADDR="$(echo "$DISCOVERY_JSON" | jq -r ".\"$DST\".address")"

  lightning-cli --lightning-dir="$SRC_DIR" connect "$DST_ID@$DST_ADDR" >/dev/null 2>&1 || true
}

case "$TOPOLOGY" in
  mesh)
    for A in "${NODE_NAMES[@]}"; do
      for B in "${NODE_NAMES[@]}"; do
        [ "$A" != "$B" ] && connect "$A" "$B"
      done
    done
    ;;
  star)
    for NODE in "${NODE_NAMES[@]}"; do
      [ "$NODE" != "node-1" ] && connect "node-1" "$NODE"
    done
    ;;
  ring)
    COUNT="${#NODE_NAMES[@]}"
    for ((i=0; i<COUNT; i++)); do
      NEXT=$(( (i + 1) % COUNT ))
      connect "${NODE_NAMES[$i]}" "${NODE_NAMES[$NEXT]}"
    done
    ;;
  *)
    echo "Unknown topology: $TOPOLOGY"
    exit 1
    ;;
esac

echo "[SUCCESS] Nodes connected using '$TOPOLOGY' topology"
