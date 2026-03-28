# x402 — HTTP 402 Payment Required for Lightning Micropayments

## Overview

x402 integrates the HTTP 402 Payment Required status code with Lightning Network micropayments. It enables pay-per-request APIs: a server gates endpoints behind Lightning invoices, and clients pay automatically before retrying.

This implementation provides **two integration layers**:

1. **Server-side paywall** (`scripts/x402.py`) — the web dashboard gates selected API endpoints behind Lightning payments
2. **Client-side auto-pay** (`ai/controllers/executor.py`) — the AI pipeline executor detects 402 responses, pays the embedded invoice, and retries the tool call

## Architecture

```
Client Request (no payment)
    ↓
[Rate Limit] → [Auth] → [RBAC] → [x402 Paywall]
    ↓                                    ↓
 (pass)                          402 + BOLT11 invoice
    ↓                                    ↓
[Route Handler]               Client pays via ln_pay
    ↓                                    ↓
  Response                  Client retries + X-Payment-Preimage header
                                         ↓
                            [Verify SHA256(preimage) = payment_hash] → [Route Handler]
```

## How It Works

### 1. Client requests a paywalled endpoint

```
POST /api/ask HTTP/1.1
Content-Type: application/json

{"text": "check my balance"}
```

### 2. Server returns 402 with a BOLT11 invoice

```json
HTTP/1.1 402 Payment Required
Content-Type: application/json

{
  "status": 402,
  "title": "Payment Required",
  "bolt11": "lnbcrt1000n1pjq...",
  "amount_msat": 1000000,
  "payment_hash": "abc123...",
  "memo": "x402 payment for POST /api/ask",
  "expires_at": 1710872400
}
```

### 3. Client pays the invoice

The client pays the BOLT11 invoice via their Lightning node. Upon successful payment, they receive the **payment preimage** — a 32-byte value whose SHA256 hash equals the `payment_hash`.

### 4. Client retries with proof of payment

```
POST /api/ask HTTP/1.1
Content-Type: application/json
X-Payment-Preimage: deadbeef0123456789...

{"text": "check my balance"}
```

### 5. Server verifies and serves the request

The server computes `SHA256(preimage)` and checks it matches a stored invoice's `payment_hash`. If valid, the request proceeds normally.

## Configuration

All x402 settings are in `.env`:

```bash
# Enable the payment gateway
X402_ENABLED=1

# Which Lightning node creates invoices (default: 1)
X402_INVOICE_NODE=1

# Invoice expiry in seconds (default: 600 = 10 minutes)
X402_INVOICE_EXPIRY_S=600

# Per-endpoint prices in millisatoshis
X402_ASK_COST_MSAT=1000000       # 1,000 sats per query
X402_NETWORK_COST_MSAT=100000    # 100 sats per network view
X402_TRACE_COST_MSAT=50000       # 50 sats per trace access
```

### Executor auto-pay (client-side)

```bash
# Enable the AI executor to auto-pay 402 invoices
EXECUTOR_X402_AUTO_PAY=1

# Which node pays (should differ from the invoice node)
EXECUTOR_X402_PAY_NODE=2

# Safety cap — refuse to auto-pay above this amount
EXECUTOR_X402_MAX_AMOUNT_MSAT=100000000  # 100,000 sats
```

## Components

| File | Purpose |
|------|---------|
| `scripts/x402.py` | Core middleware: `InvoiceStore`, `X402Paywall`, `verify_preimage()`, `extract_x402()` |
| `scripts/ui_server.py` | Server integration: x402 check in `do_GET`/`do_POST` after RBAC |
| `ai/controllers/executor.py` | Client integration: auto-pay in the tool retry loop |
| `mcp/ln_mcp_server.py` | New tools: `ln_listinvoices`, `ln_waitinvoice` |
| `ai/tools.py` | Tool registration for the new invoice tools |
| `web/app.js` | Payment overlay UI, 402 response handler |
| `web/index.html` | Payment overlay HTML, x402 status indicator |
| `web/styles.css` | Payment overlay styles |
| `ai/tests/test_x402.py` | 33 tests covering all components |

## API Spec

### 402 Response Body

| Field | Type | Description |
|-------|------|-------------|
| `status` | `int` | Always `402` |
| `title` | `string` | `"Payment Required"` |
| `bolt11` | `string` | BOLT11 invoice string |
| `amount_msat` | `int` | Price in millisatoshis |
| `payment_hash` | `string` | Hex-encoded SHA256 hash for verification |
| `memo` | `string` | Human-readable description |
| `expires_at` | `int` | Unix timestamp when the invoice expires |

### Request Header

| Header | Description |
|--------|-------------|
| `X-Payment-Preimage` | Hex-encoded 32-byte preimage from a successful payment |

## Human Approval for Large Payments

When `EXECUTOR_X402_AUTO_PAY=1`, the executor silently pays invoices below the **approval threshold**. Invoices above the threshold pause the pipeline and request human approval via the web dashboard.

### Payment Decision Flow

```
amount_msat <= X402_APPROVAL_THRESHOLD_MSAT   → auto-pay silently
amount_msat >  X402_APPROVAL_THRESHOLD_MSAT
            <= EXECUTOR_X402_MAX_AMOUNT_MSAT   → pause, request human approval
amount_msat >  EXECUTOR_X402_MAX_AMOUNT_MSAT   → refuse (safety cap)
```

### Configuration

```bash
# Payments above 50k sats require human approval (default)
X402_APPROVAL_THRESHOLD_MSAT=50000000

# How long the agent waits for approval before timing out (default: 120s)
X402_APPROVAL_TIMEOUT_S=120
```

Both values are configurable in the Settings tab of the web dashboard.

### How It Works

1. The executor encounters a 402 response with `amount_msat > threshold`
2. It writes `runtime/agent/x402_pending.json` with the invoice details
3. The UI server detects the file and pushes an `x402_approval` SSE event
4. The browser shows an approval modal with the amount, tool name, and BOLT11 invoice
5. The user clicks **Approve** or **Deny**
6. The browser POSTs to `/api/x402_approve` with `{"approved": true/false}`
7. The UI server writes `runtime/agent/x402_response.json`
8. The executor reads the response, pays (if approved) or aborts (if denied)

### Trace Events

| Event | When |
|-------|------|
| `x402_payment` | Auto-pay initiated (below threshold) |
| `x402_paid` | Payment succeeded |
| `x402_approval_requested` | Approval modal shown to user |
| `x402_approved` | User approved the payment |
| `x402_denied` | User denied the payment |
| `x402_approval_timeout` | User did not respond within the timeout |

## Limitations

- **Regtest only** — all payments use regtest Bitcoin, not real money
- **In-memory invoice store** — pending invoices are lost on server restart
- **No hold invoices** — uses standard BOLT11 invoices, not hold invoices with conditional settlement
- **Single-server** — designed for the research harness, not a distributed payment gateway
- **No refunds** — once paid, there is no automated refund mechanism

## Demo Walkthrough

### Quick Demo (auto-pay only)

1. Start the system: `./scripts/1.start.sh 2`
2. Run the network test: `./scripts/network_test.sh 2`
3. Enable x402 in `.env`:
   ```
   X402_ENABLED=1
   X402_ASK_COST_MSAT=1000000
   EXECUTOR_X402_AUTO_PAY=1
   EXECUTOR_X402_PAY_NODE=2
   ```
4. Start the UI server: `python scripts/ui_server.py`
5. Open http://127.0.0.1:8008 — the x402 indicator appears in the status bar
6. Submit a prompt — the agent auto-pays the 402 invoice and completes the query
7. Check the trace log for `x402_payment` and `x402_paid` events

### Full E2E Demo (auto-pay + approval + denial)

Run the automated demo script:

```bash
./scripts/demo_x402.sh
```

This runs five phases:
1. **Boot** — starts the system with x402 enabled
2. **Auto-pay** — submits a prompt with a low price (below threshold), verifies auto-pay
3. **Approval (approve)** — lowers the threshold, submits a prompt, approves the payment
4. **Approval (deny)** — submits another prompt, denies the payment
5. **Verification** — checks the trace log for all expected x402 events
