#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# x402 END-TO-END DEMO
#
# Demonstrates the full HTTP 402 Payment Required flow:
#
#   Phase 1: Boot system + set up Lightning network
#   Phase 2: Auto-pay below threshold (silent)
#   Phase 3: Approval flow above threshold (approve)
#   Phase 4: Approval flow above threshold (deny)
#   Phase 5: Verify trace events
#
# Usage:
#   ./scripts/demo_x402.sh                      # full demo
#   ./scripts/demo_x402.sh --skip-setup         # skip boot (stack already running)
#
# Prerequisites: ./scripts/0.install.sh completed, .env configured with an
# LLM backend.
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_COUNT=2
SKIP_SETUP=false
UI_HOST="${UI_HOST:-127.0.0.1}"
UI_PORT="${UI_PORT:-8008}"
UI_URL="http://${UI_HOST}:${UI_PORT}"
POLL_INTERVAL=2
POLL_TIMEOUT="${POLL_TIMEOUT:-120}"
READY_TIMEOUT=180
RUNTIME_DIR="$PROJECT_ROOT/runtime/agent"

_BG_PIDS=()
_cleanup() {
  if [[ ${#_BG_PIDS[@]} -gt 0 ]]; then
    for pid in "${_BG_PIDS[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
  fi
}
trap _cleanup EXIT INT TERM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-setup) SKIP_SETUP=true; shift ;;
    --nodes)      NODE_COUNT="$2"; shift 2 ;;
    *)            echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────

info()  { printf '\033[1;34m[x402]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[x402]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[x402]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[x402]\033[0m %s\n' "$*"; exit 1; }

wait_for_ui() {
  local elapsed=0
  info "Waiting for UI server at $UI_URL ..."
  while ! curl -sf "$UI_URL/api/status" > /dev/null 2>&1; do
    sleep 2
    elapsed=$((elapsed + 2))
    if [[ $elapsed -ge $READY_TIMEOUT ]]; then
      fail "UI server did not become ready within ${READY_TIMEOUT}s"
    fi
  done
  ok "UI server ready."
}

submit_prompt() {
  local prompt="$1"
  info "Submitting prompt: $prompt"
  curl -sf -X POST "$UI_URL/api/ask" \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"$prompt\"}" > /dev/null
}

wait_for_result() {
  local elapsed=0
  while true; do
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
    if [[ $elapsed -ge $POLL_TIMEOUT ]]; then
      warn "Pipeline did not complete within ${POLL_TIMEOUT}s"
      return 1
    fi
    local result
    result=$(curl -sf "$UI_URL/api/pipeline_result" 2>/dev/null || echo "")
    if [[ -n "$result" ]] && echo "$result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = d.get('result')
if r and r.get('success') is not None:
    sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
      return 0
    fi
  done
}

check_trace_event() {
  local event_name="$1"
  curl -sf "$UI_URL/api/trace" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
events = data.get('events', [])
for e in events:
    if e.get('event') == '$event_name':
        sys.exit(0)
sys.exit(1)
" 2>/dev/null
}

wait_for_pending() {
  local elapsed=0
  info "Waiting for x402 approval request..."
  while [[ ! -f "$RUNTIME_DIR/x402_pending.json" ]]; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [[ $elapsed -ge 60 ]]; then
      warn "No approval request appeared within 60s"
      return 1
    fi
  done
  ok "Approval request detected:"
  python3 -m json.tool "$RUNTIME_DIR/x402_pending.json" 2>/dev/null || cat "$RUNTIME_DIR/x402_pending.json"
}

respond_to_approval() {
  local approved="$1"
  info "Responding to approval: approved=$approved"
  curl -sf -X POST "$UI_URL/api/x402_approve" \
    -H "Content-Type: application/json" \
    -d "{\"approved\": $approved}" > /dev/null
}

# ── Phase 1: Boot + Network Setup ───────────────────────────────────────────

if [[ "$SKIP_SETUP" == false ]]; then
  info "Phase 1: Booting system with $NODE_COUNT nodes and x402 enabled..."

  # Ensure x402 is enabled in .env
  cd "$PROJECT_ROOT"
  # shellcheck source=/dev/null
  [[ -f .env ]] && source .env

  # Append x402 config if not already present
  if ! grep -q "X402_ENABLED=1" .env 2>/dev/null; then
    cat >> .env << 'ENVEOF'

# x402 demo configuration
X402_ENABLED=1
X402_INVOICE_NODE=1
X402_ASK_COST_MSAT=1000
EXECUTOR_X402_AUTO_PAY=1
EXECUTOR_X402_PAY_NODE=2
EXECUTOR_X402_MAX_AMOUNT_MSAT=100000000
X402_APPROVAL_THRESHOLD_MSAT=50000
X402_APPROVAL_TIMEOUT_S=60
ENVEOF
    info "x402 configuration added to .env"
  fi

  # Boot the system
  bash scripts/1.start.sh "$NODE_COUNT"
  wait_for_ui

  # Set up the Lightning network
  info "Setting up Lightning network topology..."
  bash scripts/network_test.sh "$NODE_COUNT"
  ok "Phase 1 complete: system booted and network ready."
else
  info "Phase 1: Skipping setup (--skip-setup)."
  wait_for_ui
fi

echo ""
echo "================================================================"
echo "  PHASE 2: Auto-Pay Below Threshold"
echo "================================================================"
echo ""

info "x402 price: 1,000 msat | Threshold: 50,000 msat"
info "Amount is below threshold → executor will auto-pay silently."

submit_prompt "Show the balance of node 1"

if wait_for_result; then
  if check_trace_event "x402_paid"; then
    ok "Auto-pay verified! Trace shows x402_paid event."
  else
    warn "Pipeline completed but no x402_paid event found in trace."
    warn "(This is expected if the endpoint was not paywalled or if x402 is disabled.)"
  fi
else
  warn "Pipeline did not complete in time."
fi

echo ""
echo "================================================================"
echo "  PHASE 3: Approval Flow — Approve"
echo "================================================================"
echo ""

# Lower the threshold so the 1,000 msat price triggers approval
info "Lowering approval threshold to 500 msat..."
curl -sf -X POST "$UI_URL/api/config" \
  -H "Content-Type: application/json" \
  -d '{"X402_APPROVAL_THRESHOLD_MSAT": "500"}' > /dev/null

# Restart agent so the new threshold takes effect
info "Restarting agent to apply new threshold..."
curl -sf -X POST "$UI_URL/api/restart" \
  -H "Content-Type: application/json" \
  -d '{}' > /dev/null 2>&1 || true
sleep 5

submit_prompt "Show the balance of node 2"

if wait_for_pending; then
  respond_to_approval true

  if wait_for_result; then
    if check_trace_event "x402_approved"; then
      ok "Approval flow verified! Trace shows x402_approved event."
    fi
    if check_trace_event "x402_paid"; then
      ok "Payment confirmed! Trace shows x402_paid event."
    fi
  else
    warn "Pipeline did not complete after approval."
  fi
else
  warn "Approval request was not generated. The agent may not have hit the x402 paywall."
  warn "This can happen if the pipeline routes around the paywalled endpoint."
fi

echo ""
echo "================================================================"
echo "  PHASE 4: Approval Flow — Deny"
echo "================================================================"
echo ""

submit_prompt "List channels on node 1"

if wait_for_pending; then
  respond_to_approval false

  if wait_for_result; then
    if check_trace_event "x402_denied"; then
      ok "Denial flow verified! Trace shows x402_denied event."
    fi
  else
    # Pipeline may fail (expected when denied)
    if check_trace_event "x402_denied"; then
      ok "Denial flow verified! Trace shows x402_denied event."
    fi
  fi
else
  warn "Approval request was not generated for denial phase."
fi

echo ""
echo "================================================================"
echo "  PHASE 5: Verification Summary"
echo "================================================================"
echo ""

info "Checking trace for x402 events..."
EVENTS=("x402_payment" "x402_paid" "x402_approval_requested" "x402_approved" "x402_denied")
for evt in "${EVENTS[@]}"; do
  if check_trace_event "$evt"; then
    ok "  $evt  [found]"
  else
    warn "  $evt  [not found]"
  fi
done

echo ""
ok "x402 demo complete!"
echo ""
info "Open $UI_URL to see the full trace log and pipeline results."
