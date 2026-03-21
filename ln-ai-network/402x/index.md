# 402x — HTTP 402 Payment Required Integration

Placeholder for future work integrating Lightning Network micropayments into HTTP request flows via the 402 Payment Required status code.

The 402 flow allows a server to reject an HTTP request with a Lightning invoice, which the client pays automatically before retrying — enabling pay-per-request APIs without subscriptions or accounts.

## Planned scope

- Middleware that issues BOLT11 invoices on 402 responses
- Client interceptor that detects 402, pays via the local Lightning node, and retries with proof of payment
- Integration with the AI agent's `ln_pay` tool for automated payment handling

## Status

Not yet implemented. This directory is a placeholder.
