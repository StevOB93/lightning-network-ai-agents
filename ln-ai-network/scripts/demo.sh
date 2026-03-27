#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# LN-AI DEMO SCRIPT
#
# One-command E2E demo: boots the full stack, sets up the Lightning network,
# submits a payment prompt, and waits for the pipeline to complete.
#
# Usage:
#   ./scripts/demo.sh                          # full demo (boot + network + prompt)
#   ./scripts/demo.sh --skip-setup             # skip boot + network (stack already running)
#   ./scripts/demo.sh --prompt "your prompt"   # override the default payment prompt
#   ./scripts/demo.sh --nodes 3                # boot 3 nodes instead of 2
#
# The script opens the Web UI in your browser so you can watch the pipeline
# stages execute in real time.
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Defaults ─────────────────────────────────────────────────────────────────

NODE_COUNT=2
SKIP_SETUP=false
PROMPT="Create an invoice for 100000 millisatoshis on node-2, then pay it from node-1."
UI_HOST="${UI_HOST:-127.0.0.1}"
UI_PORT="${UI_PORT:-8008}"
UI_URL="http://${UI_HOST}:${UI_PORT}"
POLL_INTERVAL=2        # seconds between completion checks
POLL_TIMEOUT="${POLL_TIMEOUT:-120}"       # max seconds to wait for pipeline result
READY_TIMEOUT=180      # max seconds to wait for system + UI readiness

# Track background PIDs so the cleanup trap can kill them on interrupt
_BG_PIDS=()
_cleanup() {
  if [[ ${#_BG_PIDS[@]} -gt 0 ]]; then
    for pid in "${_BG_PIDS[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
  fi
}
trap _cleanup EXIT INT TERM

# ── Argument parsing ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-setup)   SKIP_SETUP=true; shift ;;
    --prompt)       PROMPT="$2"; shift 2 ;;
    --nodes)        NODE_COUNT="$2"; shift 2 ;;
    --timeout)      POLL_TIMEOUT="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
LN-AI Demo Script — one-command E2E demo

Usage:
  ./scripts/demo.sh [OPTIONS]

Options:
  --skip-setup       Skip system boot and network setup (use if stack is already running)
  --prompt "..."     Override the default payment prompt
  --nodes N          Number of Lightning nodes (default: 2)
  --timeout SEC      Max seconds to wait for pipeline result (default: 120)
  -h, --help         Show this help

Environment:
  UI_HOST            UI server host (default: 127.0.0.1)
  UI_PORT            UI server port (default: 8008)

Examples:
  ./scripts/demo.sh                                          # full E2E demo
  ./scripts/demo.sh --skip-setup                             # prompt only (stack running)
  ./scripts/demo.sh --prompt "What is the balance of node-1?"
  ./scripts/demo.sh --nodes 3
EOF
      exit 0
      ;;
    *) echo "[ERROR] Unknown argument: $1"; exit 2 ;;
  esac
done

# Adapt default prompt if user didn't override and NODE_COUNT > 2
if [[ "$PROMPT" == "Create an invoice for 100000 millisatoshis on node-2, then pay it from node-1." && "$NODE_COUNT" -gt 2 ]]; then
  PROMPT="Create an invoice for 100000 millisatoshis on node-${NODE_COUNT}, then pay it from node-1."
fi

# ── Helpers ──────────────────────────────────────────────────────────────────

_ts() { date "+%H:%M:%S"; }

info()  { echo "[$(_ts)] [INFO]  $*"; }
ok()    { echo "[$(_ts)] [OK]    $*"; }
fail()  { echo "[$(_ts)] [FAIL]  $*" >&2; }
banner() {
  echo ""
  echo "══════════════════════════════════════════════════════════════"
  echo "  $*"
  echo "══════════════════════════════════════════════════════════════"
  echo ""
}

# wait_for_url URL TIMEOUT_SEC LABEL
#   Polls a URL until it returns HTTP 200. Exits 1 on timeout.
wait_for_url() {
  local url="$1" timeout="$2" label="$3"
  local elapsed=0
  info "Waiting for $label ($url) ..."
  while ! curl -sf "$url" >/dev/null 2>&1; do
    sleep 2
    elapsed=$((elapsed + 2))
    if [[ $elapsed -ge $timeout ]]; then
      fail "$label did not become ready within ${timeout}s"
      exit 1
    fi
  done
  ok "$label is ready (${elapsed}s)"
}

# ── Phase 1: Boot the stack ─────────────────────────────────────────────────

if [[ "$SKIP_SETUP" == "false" ]]; then
  banner "Phase 1/4 — Booting system ($NODE_COUNT nodes)"
  bash "$PROJECT_ROOT/scripts/1.start.sh" "$NODE_COUNT"
  ok "System boot complete"
else
  info "Skipping system boot (--skip-setup)"
fi

# ── Phase 2: Wait for UI readiness ──────────────────────────────────────────

banner "Phase 2/4 — Waiting for UI server"
wait_for_url "${UI_URL}/api/status" "$READY_TIMEOUT" "UI server"

# ── Phase 3: Set up Lightning network ───────────────────────────────────────

if [[ "$SKIP_SETUP" == "false" ]]; then
  banner "Phase 3/4 — Setting up Lightning network"

  # Source env.sh for LIGHTNING_BASE, BITCOIN_* vars needed to start nodes.
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env.sh"

  # Infra boot only starts node-1. Start nodes 2..N so network_test.sh can
  # fund them, connect them, and open channels.
  RPC_USER="${BITCOIN_RPC_USER:-lnrpc}"
  RPC_PASS="${BITCOIN_RPC_PASSWORD:-lnrpcpass}"
  for i in $(seq 2 "$NODE_COUNT"); do
    NODE_DIR="$LIGHTNING_BASE/node-$i"
    if lightning-cli --network=regtest --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; then
      info "node-$i already running"
      continue
    fi
    info "Starting node-$i..."
    mkdir -p "$NODE_DIR"
    LN_PORT=$((${LIGHTNING_BASE_PORT:-9735} + i - 1))
    lightningd \
      --network=regtest \
      --lightning-dir="$NODE_DIR" \
      --addr="127.0.0.1:$LN_PORT" \
      --bitcoin-rpcconnect=127.0.0.1 \
      --bitcoin-rpcport="${BITCOIN_RPC_PORT:-18443}" \
      --bitcoin-rpcuser="$RPC_USER" \
      --bitcoin-rpcpassword="$RPC_PASS" \
      --bitcoin-datadir="$BITCOIN_DIR" \
      --log-file="$NODE_DIR/lightningd.log" &
    # Wait for RPC readiness
    until lightning-cli --network=regtest --lightning-dir="$NODE_DIR" getinfo >/dev/null 2>&1; do
      sleep 1
    done
    ok "node-$i started (port $LN_PORT)"
  done

  bash "$PROJECT_ROOT/scripts/network_test.sh" "$NODE_COUNT"
  ok "Lightning network ready (${NODE_COUNT} nodes, linear topology, channels open)"
else
  info "Skipping network setup (--skip-setup)"
fi

# ── Phase 4: Submit prompt and wait for result ──────────────────────────────

banner "Phase 4/4 — Submitting prompt to pipeline"
info "Prompt: $PROMPT"

# POST the prompt and extract the assigned message ID
RESPONSE="$(curl -sf -X POST "${UI_URL}/api/ask" \
  -H "Content-Type: application/json" \
  -d "$(printf '{"text": %s}' "$(echo "$PROMPT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')")")"

MSG_ID="$(echo "$RESPONSE" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["msg"]["id"])' 2>/dev/null)"
if [[ -z "$MSG_ID" ]]; then
  echo "[FATAL] Failed to submit prompt — no message ID returned."
  echo "[FATAL] Response was: $RESPONSE"
  exit 1
fi
ok "Prompt queued (message ID: $MSG_ID)"

# Try to open the browser so the user can watch live
_browser_opened=false
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$UI_URL" >/dev/null 2>&1 && _browser_opened=true
elif command -v open >/dev/null 2>&1; then
  open "$UI_URL" >/dev/null 2>&1 && _browser_opened=true
elif command -v wslview >/dev/null 2>&1; then
  wslview "$UI_URL" >/dev/null 2>&1 && _browser_opened=true
fi
if [[ "$_browser_opened" == "false" ]]; then
  info "Open the Web UI manually: $UI_URL"
fi

# Poll for pipeline completion
info "Waiting for pipeline to complete (timeout: ${POLL_TIMEOUT}s) ..."
ELAPSED=0
RESULT=""

while [[ $ELAPSED -lt $POLL_TIMEOUT ]]; do
  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  # Fetch latest pipeline result
  RAW="$(curl -sf "${UI_URL}/api/pipeline_result" 2>/dev/null || echo "{}")"

  # Check if the result matches our request_id
  MATCH="$(echo "$RAW" | python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
    r = data.get('result') or {}
    if str(r.get('request_id')) == '$MSG_ID':
        print('yes')
    else:
        print('no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")"

  if [[ "$MATCH" == "yes" ]]; then
    RESULT="$RAW"
    break
  fi
done

# ── Results ──────────────────────────────────────────────────────────────────

banner "Demo Results"

if [[ -z "$RESULT" ]]; then
  fail "Pipeline did not complete within ${POLL_TIMEOUT}s"
  info "Check the Web UI at $UI_URL for details"
  exit 1
fi

# Extract and display the result
python3 -c "
import json, sys, textwrap

data = json.loads(sys.stdin.read())
r = data.get('result', {})

success = r.get('success', False)
status = 'SUCCESS' if success else 'FAILED'
summary = r.get('content', r.get('human_summary', '(no summary)'))

print(f'  Status:  {status}')
print(f'  Request: #{r.get(\"request_id\", \"?\")}')
print()
print('  Summary:')
for line in textwrap.wrap(summary, width=70):
    print(f'    {line}')
print()

steps = r.get('step_results', [])
if steps:
    print(f'  Steps executed: {len(steps)}')
    for i, s in enumerate(steps, 1):
        tool = s.get('tool', '?')
        ok_flag = 'ok' if s.get('ok', s.get('success', False)) else 'FAIL'
        print(f'    {i}. {tool} [{ok_flag}]')
    print()
" <<< "$RESULT"

# Final status
SUCCESS="$(echo "$RESULT" | python3 -c "
import json, sys
r = json.loads(sys.stdin.read()).get('result', {})
print('true' if r.get('success', False) else 'false')
")"

if [[ "$SUCCESS" == "true" ]]; then
  ok "Demo completed successfully!"
else
  fail "Demo completed with errors (see summary above)"
fi

info "Web UI: $UI_URL"
echo ""
