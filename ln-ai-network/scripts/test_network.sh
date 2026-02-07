#!/usr/bin/env bash
#
# test_network.sh
#
# End-to-end functional test for the Lightning regtest network.
#
# Confirms:
#  - all nodes are reachable
#  - all nodes are on regtest
#  - peers are connected
#  - channels exist
#  - a routed payment succeeds
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../env.sh"

echo "=============================="
echo " Lightning Network Test Suite "
echo "=============================="

#######################################
# Discover nodes
#######################################
DISCOVERY_JSON="$("$SCRIPT_DIR/discover_nodes.sh")"
NODE_NAMES=($(echo "$DISCOVERY_JSON" | jq -r 'keys[]'))

NODE_COUNT="${#NODE_NAMES[@]}"

if [ "$NODE_COUNT" -lt 2 ]; then
  echo "[FAIL] Need at least 2 nodes to test routing"
  exit 1
fi

echo "[INFO] Discovered $NODE_COUNT nodes"

#######################################
# 1. RPC + regtest verification
#######################################
echo "[TEST] Verifying Lightning RPC + regtest network..."

for NODE in "${NODE_NAMES[@]}"; do
  LN_DIR="$LN_RUNTIME/lightning/$NODE"

  INFO="$(lightning-cli --lightning-dir="$LN_DIR" getinfo)"

  NETWORK="$(echo "$INFO" | jq -r '.network')"

  if [ "$NETWORK" != "regtest" ]; then
    echo "[FAIL] $NODE is not on regtest"
    exit 1
  fi
done

echo "[PASS] All nodes report network=regtest"

#######################################
# 2. Peer connectivity check
#######################################
echo "[TEST] Verifying peer connections..."

for NODE in "${NODE_NAMES[@]}"; do
  LN_DIR="$LN_RUNTIME/lightning/$NODE"

  PEERS="$(lightning-cli --lightning-dir="$LN_DIR" listpeers | jq '.peers | length')"

  if [ "$PEERS" -eq 0 ]; then
    echo "[FAIL] $NODE has zero peers"
    exit 1
  fi
done

echo "[PASS] All nodes have peers"

#######################################
# 3. Channel existence check
#######################################
echo "[TEST] Verifying channels..."

TOTAL_CHANNELS=0

for NODE in "${NODE_NAMES[@]}"; do
  LN_DIR="$LN_RUNTIME/lightning/$NODE"

  COUNT="$(lightning-cli --lightning-dir="$LN_DIR" listchannels | jq '.channels | length')"
  TOTAL_CHANNELS=$((TOTAL_CHANNELS + COUNT))
done

if [ "$TOTAL_CHANNELS" -eq 0 ]; then
  echo "[FAIL] No channels detected"
  exit 1
fi

echo "[PASS] Channels exist across network"

#######################################
# 4. End-to-end payment test
#######################################
echo "[TEST] Performing routed payment..."

SENDER="${NODE_NAMES[0]}"
RECEIVER="${NODE_NAMES[$((NODE_COUNT - 1))]}"

SENDER_DIR="$LN_RUNTIME/lightning/$SENDER"
RECEIVER_DIR="$LN_RUNTIME/lightning/$RECEIVER"

INVOICE_JSON="$(lightning-cli --lightning-dir="$RECEIVER_DIR" invoice 1000 test-invoice test-desc)"
BOLT11="$(echo "$INVOICE_JSON" | jq -r '.bolt11')"

PAY_RESULT="$(lightning-cli --lightning-dir="$SENDER_DIR" pay "$BOLT11" 2>/dev/null || true)"

STATUS="$(echo "$PAY_RESULT" | jq -r '.status // empty')"

if [ "$STATUS" != "complete" ]; then
  echo "[FAIL] Payment failed"
  exit 1
fi

echo "[PASS] Payment routed successfully"

#######################################
# Final Result
#######################################
echo
echo "=============================="
echo "  ALL TESTS PASSED ✅"
echo "=============================="
echo
echo "Network is fully functional:"
echo " - regtest chain"
echo " - peer discovery"
echo " - channels active"
echo " - routing works"
echo
