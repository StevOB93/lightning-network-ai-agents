"""x402 — HTTP 402 Payment Required middleware for Lightning micropayments.

Provides server-side paywall functionality: endpoints can be gated behind
Lightning invoices.  A client that receives a 402 response pays the embedded
BOLT11 invoice and retries with the payment preimage in the
``X-Payment-Preimage`` header.  The middleware verifies ``SHA256(preimage)``
matches the invoice's ``payment_hash`` and grants access.

All primitives use Python stdlib only (hashlib, threading, time).  Invoice
creation is delegated to a caller-supplied ``create_invoice`` callback so this
module has no direct dependency on the MCP server.

Design follows the same composable, thread-safe patterns as ``security.py``.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Preimage verification (pure crypto — no network calls)
# ---------------------------------------------------------------------------

def verify_preimage(payment_hash: str, preimage: str) -> bool:
    """Return True if ``SHA256(preimage) == payment_hash`` (both hex-encoded).

    This is the standard Lightning proof-of-payment check: the payer receives
    the preimage upon successful payment, and SHA256(preimage) equals the
    payment_hash embedded in the original invoice.
    """
    try:
        preimage_bytes = bytes.fromhex(preimage)
        expected = bytes.fromhex(payment_hash)
    except (ValueError, TypeError):
        return False
    actual = hashlib.sha256(preimage_bytes).digest()
    return actual == expected


# ---------------------------------------------------------------------------
# 402 response formatting
# ---------------------------------------------------------------------------

def create_x402_response(
    bolt11: str,
    amount_msat: int,
    payment_hash: str,
    memo: str = "",
    expires_at: int = 0,
) -> dict[str, Any]:
    """Build the JSON body for an HTTP 402 response.

    The response includes everything a client needs to pay and retry:
    the BOLT11 invoice string, the amount, the payment hash (for later
    preimage verification), and an optional human-readable memo.
    """
    return {
        "status": 402,
        "title": "Payment Required",
        "bolt11": bolt11,
        "amount_msat": amount_msat,
        "payment_hash": payment_hash,
        "memo": memo,
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Invoice store (thread-safe in-memory)
# ---------------------------------------------------------------------------

@dataclass
class PendingInvoice:
    """An invoice issued by the paywall, awaiting payment."""
    label: str
    bolt11: str
    payment_hash: str
    amount_msat: int
    endpoint: str
    created_ts: float
    expires_ts: float
    paid: bool = False


class InvoiceStore:
    """Thread-safe in-memory store for x402 invoices.

    Invoices are keyed by ``payment_hash`` so the preimage header can be
    matched back to the original invoice in O(1).  Expired invoices are
    pruned lazily every ``prune_interval`` lookups.
    """

    def __init__(self, prune_interval: int = 50) -> None:
        self._invoices: dict[str, PendingInvoice] = {}
        self._lock = threading.Lock()
        self._ops = 0
        self._prune_interval = prune_interval

    def add(self, inv: PendingInvoice) -> None:
        with self._lock:
            self._invoices[inv.payment_hash] = inv

    def lookup(self, payment_hash: str) -> PendingInvoice | None:
        with self._lock:
            self._ops += 1
            if self._ops % self._prune_interval == 0:
                self._prune_expired()
            return self._invoices.get(payment_hash)

    def mark_paid(self, payment_hash: str) -> None:
        with self._lock:
            inv = self._invoices.get(payment_hash)
            if inv:
                inv.paid = True

    def remove(self, payment_hash: str) -> None:
        with self._lock:
            self._invoices.pop(payment_hash, None)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._invoices)

    def _prune_expired(self) -> None:
        """Remove expired invoices. Called under lock."""
        now = time.time()
        expired = [h for h, inv in self._invoices.items() if inv.expires_ts < now]
        for h in expired:
            del self._invoices[h]


# ---------------------------------------------------------------------------
# X402 paywall middleware
# ---------------------------------------------------------------------------

@dataclass
class X402Result:
    """Outcome of an x402 paywall check."""
    allowed: bool
    status_code: int = 200
    response_body: dict[str, Any] = field(default_factory=dict)


# Type alias for the invoice creation callback.
# Signature: (node, amount_msat, label, description) -> dict
#   On success: {"ok": True, "payload": {"bolt11": "lnbcrt...", "payment_hash": "abc..."}}
#   On failure: {"ok": False, "error": "..."}
CreateInvoiceFn = Callable[[int, int, str, str], dict[str, Any]]


class X402Paywall:
    """Middleware that gates HTTP endpoints behind Lightning payments.

    Usage::

        paywall = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000, "GET /api/network": 100},
            node=1,
            invoice_expiry_s=600,
            create_invoice=my_ln_invoice_fn,
        )

        result = paywall.check("POST", "/api/ask", request_headers)
        if not result.allowed:
            send_response(result.status_code, result.response_body)
            return
    """

    def __init__(
        self,
        endpoint_prices: dict[str, int],
        node: int,
        invoice_expiry_s: int,
        create_invoice: CreateInvoiceFn,
    ) -> None:
        self._prices = endpoint_prices          # "METHOD /path" → cost in msat
        self._node = node
        self._expiry_s = invoice_expiry_s
        self._create_invoice = create_invoice
        self._store = InvoiceStore()

    @property
    def store(self) -> InvoiceStore:
        return self._store

    def check(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
    ) -> X402Result:
        """Check whether the request should be allowed, paywalled, or rejected.

        Returns an ``X402Result``:
        - ``allowed=True`` if the endpoint is free or a valid preimage was provided
        - ``allowed=False, status_code=402`` if payment is required
        - ``allowed=False, status_code=400`` if the preimage is invalid
        """
        clean_path = path.split("?")[0]
        key = f"{method.upper()} {clean_path}"
        cost_msat = self._prices.get(key)

        # Free endpoint — pass through
        if cost_msat is None:
            return X402Result(allowed=True)

        # Check for payment preimage header
        preimage = headers.get("X-Payment-Preimage") or headers.get("x-payment-preimage") or ""
        preimage = preimage.strip()

        if preimage:
            return self._verify_payment(preimage)

        # No preimage — issue a new invoice
        return self._issue_invoice(key, cost_msat)

    def _verify_payment(self, preimage: str) -> X402Result:
        """Verify a preimage against stored invoices."""
        # Compute payment_hash from the preimage
        try:
            preimage_bytes = bytes.fromhex(preimage)
        except (ValueError, TypeError):
            return X402Result(
                allowed=False, status_code=400,
                response_body={"error": "Invalid preimage format (expected hex)"},
            )

        payment_hash = hashlib.sha256(preimage_bytes).hexdigest()
        inv = self._store.lookup(payment_hash)

        if inv is None:
            return X402Result(
                allowed=False, status_code=400,
                response_body={"error": "Unknown payment hash — no matching invoice"},
            )

        if inv.expires_ts < time.time():
            self._store.remove(payment_hash)
            return X402Result(
                allowed=False, status_code=400,
                response_body={"error": "Invoice expired — request a new one"},
            )

        # Valid payment — mark as paid and allow
        self._store.mark_paid(payment_hash)
        return X402Result(allowed=True)

    def _issue_invoice(self, endpoint_key: str, cost_msat: int) -> X402Result:
        """Create a Lightning invoice and return a 402 response."""
        label = f"x402-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        description = f"x402 payment for {endpoint_key}"

        result = self._create_invoice(
            self._node, cost_msat, label, description,
        )

        if not result.get("ok"):
            # Invoice creation failed — return 503 rather than blocking the user
            return X402Result(
                allowed=False, status_code=503,
                response_body={"error": f"Payment system unavailable: {result.get('error', 'unknown')}"},
            )

        payload = result.get("payload", {})
        bolt11 = payload.get("bolt11", "")
        payment_hash = payload.get("payment_hash", "")
        expires_at = int(time.time()) + self._expiry_s

        # Store the invoice for later preimage verification
        self._store.add(PendingInvoice(
            label=label,
            bolt11=bolt11,
            payment_hash=payment_hash,
            amount_msat=cost_msat,
            endpoint=endpoint_key,
            created_ts=time.time(),
            expires_ts=expires_at,
        ))

        return X402Result(
            allowed=False,
            status_code=402,
            response_body=create_x402_response(
                bolt11=bolt11,
                amount_msat=cost_msat,
                payment_hash=payment_hash,
                memo=description,
                expires_at=expires_at,
            ),
        )


# ---------------------------------------------------------------------------
# Helper for executor-side 402 detection
# ---------------------------------------------------------------------------

def extract_x402(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Check if an MCP tool response looks like a 402 payment-required response.

    Returns ``{"bolt11": ..., "amount_msat": ..., "payment_hash": ...}`` if the
    response appears to be a 402, or ``None`` otherwise.
    """
    # Check nested under "result" (standard MCP wire format)
    body = raw.get("result", raw)
    if isinstance(body, dict) and body.get("status") == 402 and "bolt11" in body:
        return {
            "bolt11": body["bolt11"],
            "amount_msat": body.get("amount_msat", 0),
            "payment_hash": body.get("payment_hash", ""),
        }
    return None
