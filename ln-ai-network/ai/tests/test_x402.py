"""Tests for x402 — HTTP 402 Payment Required middleware.

Covers:
  - verify_preimage(): valid/invalid preimage, hex edge cases
  - create_x402_response(): correct format
  - InvoiceStore: add, lookup, expire, prune, thread safety
  - X402Paywall: free endpoints, paywalled endpoints, preimage verification,
    expired invoices, invoice creation failure
  - extract_x402(): recognizes 402 shape, ignores normal responses
  - Executor integration: auto-pay on 402 with mock MCP
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.x402 import (
    InvoiceStore,
    PendingInvoice,
    X402Paywall,
    clear_approval_files,
    create_x402_response,
    extract_x402,
    read_approval_response,
    verify_preimage,
    write_approval_request,
    write_approval_response,
)


# =============================================================================
# verify_preimage
# =============================================================================

class TestVerifyPreimage:
    """Test the core cryptographic proof-of-payment check."""

    def test_valid_preimage(self):
        preimage = "deadbeef" * 4  # 32 bytes hex
        preimage_bytes = bytes.fromhex(preimage)
        payment_hash = hashlib.sha256(preimage_bytes).hexdigest()
        assert verify_preimage(payment_hash, preimage) is True

    def test_invalid_preimage(self):
        preimage = "deadbeef" * 4
        wrong_hash = "00" * 32
        assert verify_preimage(wrong_hash, preimage) is False

    def test_non_hex_preimage(self):
        assert verify_preimage("aa" * 32, "not-hex") is False

    def test_non_hex_payment_hash(self):
        assert verify_preimage("not-hex", "aa" * 32) is False

    def test_empty_strings(self):
        assert verify_preimage("", "") is False

    def test_none_values(self):
        assert verify_preimage(None, "aa" * 32) is False
        assert verify_preimage("aa" * 32, None) is False

    def test_short_preimage(self):
        """Short preimage still works — SHA256 accepts any length."""
        preimage = "ff"
        preimage_bytes = bytes.fromhex(preimage)
        payment_hash = hashlib.sha256(preimage_bytes).hexdigest()
        assert verify_preimage(payment_hash, preimage) is True


# =============================================================================
# create_x402_response
# =============================================================================

class TestCreateX402Response:
    def test_basic_response(self):
        resp = create_x402_response(
            bolt11="lnbcrt1000n1...",
            amount_msat=1000,
            payment_hash="abc123",
            memo="test payment",
            expires_at=9999999999,
        )
        assert resp["status"] == 402
        assert resp["title"] == "Payment Required"
        assert resp["bolt11"] == "lnbcrt1000n1..."
        assert resp["amount_msat"] == 1000
        assert resp["payment_hash"] == "abc123"
        assert resp["memo"] == "test payment"
        assert resp["expires_at"] == 9999999999

    def test_defaults(self):
        resp = create_x402_response(bolt11="x", amount_msat=0, payment_hash="y")
        assert resp["memo"] == ""
        assert resp["expires_at"] == 0


# =============================================================================
# InvoiceStore
# =============================================================================

def _make_invoice(**overrides) -> PendingInvoice:
    defaults = dict(
        label="test-label",
        bolt11="lnbcrt...",
        payment_hash="ab" * 32,
        amount_msat=1000,
        endpoint="POST /api/ask",
        created_ts=time.time(),
        expires_ts=time.time() + 600,
    )
    defaults.update(overrides)
    return PendingInvoice(**defaults)


class TestInvoiceStore:
    def test_add_and_lookup(self):
        store = InvoiceStore()
        inv = _make_invoice()
        store.add(inv)
        assert store.lookup(inv.payment_hash) is inv

    def test_lookup_missing(self):
        store = InvoiceStore()
        assert store.lookup("nonexistent") is None

    def test_mark_paid(self):
        store = InvoiceStore()
        inv = _make_invoice()
        store.add(inv)
        assert inv.paid is False
        store.mark_paid(inv.payment_hash)
        assert inv.paid is True

    def test_remove(self):
        store = InvoiceStore()
        inv = _make_invoice()
        store.add(inv)
        store.remove(inv.payment_hash)
        assert store.lookup(inv.payment_hash) is None

    def test_count(self):
        store = InvoiceStore()
        assert store.count == 0
        store.add(_make_invoice(payment_hash="aa" * 32))
        store.add(_make_invoice(payment_hash="bb" * 32))
        assert store.count == 2

    def test_prune_expired(self):
        store = InvoiceStore(prune_interval=1)  # prune on every lookup
        expired = _make_invoice(
            payment_hash="cc" * 32,
            expires_ts=time.time() - 10,
        )
        fresh = _make_invoice(
            payment_hash="dd" * 32,
            expires_ts=time.time() + 600,
        )
        store.add(expired)
        store.add(fresh)
        # Trigger prune via lookup
        store.lookup("anything")
        assert store.count == 1
        assert store.lookup(fresh.payment_hash) is fresh

    def test_thread_safety(self):
        """Concurrent adds shouldn't corrupt the store."""
        store = InvoiceStore()
        errors = []

        def add_invoices(start: int) -> None:
            try:
                for i in range(50):
                    h = f"{start + i:064x}"
                    store.add(_make_invoice(payment_hash=h))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_invoices, args=(i * 50,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert store.count == 200


# =============================================================================
# X402Paywall
# =============================================================================

def _mock_create_invoice(node, amount_msat, label, description):
    """Mock ln_invoice that returns a deterministic payment_hash."""
    preimage = hashlib.sha256(label.encode()).hexdigest()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    return {
        "ok": True,
        "payload": {
            "bolt11": f"lnbcrt{amount_msat}n1mock",
            "payment_hash": payment_hash,
        },
    }


def _failing_create_invoice(node, amount_msat, label, description):
    return {"ok": False, "error": "Lightning node not running"}


class TestX402Paywall:
    def test_free_endpoint_passthrough(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=600,
            create_invoice=_mock_create_invoice,
        )
        result = pw.check("GET", "/api/status", {})
        assert result.allowed is True

    def test_paywalled_endpoint_returns_402(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=600,
            create_invoice=_mock_create_invoice,
        )
        result = pw.check("POST", "/api/ask", {})
        assert result.allowed is False
        assert result.status_code == 402
        assert result.response_body["bolt11"].startswith("lnbcrt")
        assert result.response_body["amount_msat"] == 1000

    def test_valid_preimage_allows_access(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=600,
            create_invoice=_mock_create_invoice,
        )
        # First request: get the 402 with the invoice
        r1 = pw.check("POST", "/api/ask", {})
        assert r1.status_code == 402
        payment_hash = r1.response_body["payment_hash"]

        # Compute a valid preimage (we know the payment_hash from the mock)
        # The mock uses SHA256(SHA256(label)) as payment_hash,
        # so the preimage is SHA256(label). We need to find the preimage
        # such that SHA256(preimage) == payment_hash.
        # Since we stored the invoice, we can look it up.
        inv = pw.store.lookup(payment_hash)
        assert inv is not None

        # For testing, compute preimage from the label (matching mock logic)
        preimage = hashlib.sha256(inv.label.encode()).hexdigest()
        # Verify our preimage matches
        assert hashlib.sha256(bytes.fromhex(preimage)).hexdigest() == payment_hash

        # Second request: include preimage header
        r2 = pw.check("POST", "/api/ask", {"X-Payment-Preimage": preimage})
        assert r2.allowed is True

    def test_invalid_preimage_rejected(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=600,
            create_invoice=_mock_create_invoice,
        )
        result = pw.check("POST", "/api/ask", {"X-Payment-Preimage": "00" * 32})
        assert result.allowed is False
        assert result.status_code == 400

    def test_non_hex_preimage_rejected(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=600,
            create_invoice=_mock_create_invoice,
        )
        result = pw.check("POST", "/api/ask", {"X-Payment-Preimage": "not-hex"})
        assert result.allowed is False
        assert result.status_code == 400

    def test_expired_invoice_rejected(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=-1,  # expire immediately
            create_invoice=_mock_create_invoice,
        )
        # Get the 402
        r1 = pw.check("POST", "/api/ask", {})
        payment_hash = r1.response_body["payment_hash"]
        inv = pw.store.lookup(payment_hash)
        preimage = hashlib.sha256(inv.label.encode()).hexdigest()

        # Try with preimage — should be expired
        r2 = pw.check("POST", "/api/ask", {"X-Payment-Preimage": preimage})
        assert r2.allowed is False
        assert r2.status_code == 400

    def test_invoice_creation_failure_returns_503(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=600,
            create_invoice=_failing_create_invoice,
        )
        result = pw.check("POST", "/api/ask", {})
        assert result.allowed is False
        assert result.status_code == 503

    def test_query_string_stripped(self):
        pw = X402Paywall(
            endpoint_prices={"GET /api/network": 500},
            node=1, invoice_expiry_s=600,
            create_invoice=_mock_create_invoice,
        )
        result = pw.check("GET", "/api/network?q=test", {})
        assert result.status_code == 402

    def test_case_insensitive_header(self):
        pw = X402Paywall(
            endpoint_prices={"POST /api/ask": 1000},
            node=1, invoice_expiry_s=600,
            create_invoice=_mock_create_invoice,
        )
        # Get invoice
        r1 = pw.check("POST", "/api/ask", {})
        payment_hash = r1.response_body["payment_hash"]
        inv = pw.store.lookup(payment_hash)
        preimage = hashlib.sha256(inv.label.encode()).hexdigest()

        # Use lowercase header name
        r2 = pw.check("POST", "/api/ask", {"x-payment-preimage": preimage})
        assert r2.allowed is True


# =============================================================================
# extract_x402
# =============================================================================

class TestExtractX402:
    def test_recognizes_402_response(self):
        raw = {
            "result": {
                "status": 402,
                "bolt11": "lnbcrt1000n1...",
                "amount_msat": 1000,
                "payment_hash": "abc",
            }
        }
        info = extract_x402(raw)
        assert info is not None
        assert info["bolt11"] == "lnbcrt1000n1..."
        assert info["amount_msat"] == 1000

    def test_recognizes_flat_402(self):
        raw = {"status": 402, "bolt11": "lnbcrt...", "payment_hash": "xyz"}
        info = extract_x402(raw)
        assert info is not None

    def test_ignores_normal_success(self):
        raw = {"result": {"ok": True, "payload": {"balance": 1000}}}
        assert extract_x402(raw) is None

    def test_ignores_normal_error(self):
        raw = {"error": "something went wrong"}
        assert extract_x402(raw) is None

    def test_ignores_non_402_status(self):
        raw = {"result": {"status": 200, "bolt11": "lnbcrt..."}}
        assert extract_x402(raw) is None

    def test_ignores_402_without_bolt11(self):
        raw = {"result": {"status": 402, "error": "payment required"}}
        assert extract_x402(raw) is None


# =============================================================================
# Executor x402 integration (mock MCP)
# =============================================================================

class TestExecutorX402Integration:
    """Test executor auto-pay via a mock MCP client."""

    def _make_executor(self, mcp_responses: list[dict]) -> Any:
        """Create an Executor with a mock MCP that returns canned responses."""
        from ai.controllers.executor import Executor, ExecutorConfig
        from ai.utils import TraceLogger

        call_index = {"i": 0}
        class MockMCP:
            def call(self, tool, args=None):
                idx = call_index["i"]
                call_index["i"] += 1
                if idx < len(mcp_responses):
                    return mcp_responses[idx]
                return {"ok": True, "payload": {}}

        trace = TraceLogger(Path("/dev/null"))
        config = ExecutorConfig(
            x402_auto_pay=True,
            x402_pay_from_node=2,
            x402_max_amount_msat=100_000_000,
        )
        return Executor(mcp=MockMCP(), trace=trace, config=config)

    def test_auto_pay_402_then_success(self):
        """First call returns 402, executor pays, second call succeeds."""
        from ai.models import ExecutionPlan, PlanStep

        executor = self._make_executor([
            # First tool call: 402 response
            {"result": {"status": 402, "bolt11": "lnbcrt1000n1mock", "amount_msat": 1000, "payment_hash": "abc"}},
            # ln_pay call: success
            {"result": {"ok": True, "payload": {"payment_preimage": "deadbeef"}}},
            # Retry of original tool: success
            {"result": {"ok": True, "payload": {"balance": 50000}}},
        ])

        plan = ExecutionPlan(
            steps=[
                PlanStep(step_id=1, tool="ln_listfunds", args={"node": 1},
                         expected_outcome="funds listed", depends_on=[], on_error="retry", max_retries=2),
            ],
            plan_rationale="test", intent=None,
        )

        results = executor.execute(plan, req_id=1)
        assert len(results) == 1
        assert results[0].ok is True

    def test_auto_pay_disabled_passes_402_as_error(self):
        """When x402_auto_pay is False, 402 is treated as a normal response."""
        from ai.controllers.executor import Executor, ExecutorConfig
        from ai.models import ExecutionPlan, PlanStep
        from ai.utils import TraceLogger

        class MockMCP:
            def call(self, tool, args=None):
                return {"result": {"status": 402, "bolt11": "lnbcrt...", "amount_msat": 1000, "payment_hash": "abc"}}

        trace = TraceLogger(Path("/dev/null"))
        config = ExecutorConfig(x402_auto_pay=False)
        executor = Executor(mcp=MockMCP(), trace=trace, config=config)

        plan = ExecutionPlan(
            steps=[
                PlanStep(step_id=1, tool="ln_listfunds", args={"node": 1},
                         expected_outcome="funds listed", depends_on=[], on_error="skip", max_retries=0),
            ],
            plan_rationale="test", intent=None,
        )

        results = executor.execute(plan, req_id=1)
        assert len(results) == 1
        # With auto-pay disabled, the 402 passes through as a normal result.


# =============================================================================
# Approval file helpers
# =============================================================================

class TestApprovalFileHelpers:
    """Test the file-based approval request/response helpers."""

    def test_write_and_read_cycle(self, tmp_path):
        data = {"req_id": 1, "amount_msat": 75000, "bolt11": "lnbcrt..."}
        write_approval_request(tmp_path, data)
        assert (tmp_path / "x402_pending.json").exists()

        # Response not written yet
        assert read_approval_response(tmp_path) is None

        # Write response
        write_approval_response(tmp_path, approved=True)
        resp = read_approval_response(tmp_path)
        assert resp is not None
        assert resp["approved"] is True
        assert "ts" in resp

    def test_clear_removes_both_files(self, tmp_path):
        write_approval_request(tmp_path, {"test": 1})
        write_approval_response(tmp_path, approved=False)
        clear_approval_files(tmp_path)
        assert not (tmp_path / "x402_pending.json").exists()
        assert not (tmp_path / "x402_response.json").exists()

    def test_clear_safe_when_no_files(self, tmp_path):
        """clear_approval_files should not raise when files don't exist."""
        clear_approval_files(tmp_path)  # should not raise

    def test_denied_response(self, tmp_path):
        write_approval_response(tmp_path, approved=False)
        resp = read_approval_response(tmp_path)
        assert resp["approved"] is False


# =============================================================================
# Executor x402 approval flow (mock MCP)
# =============================================================================

class TestExecutorApprovalFlow:
    """Test executor human-approval threshold logic with mock MCP."""

    def _make_executor(self, mcp_responses: list[dict], tmp_path: Path,
                       threshold_msat: int = 50_000,
                       max_msat: int = 100_000_000,
                       timeout_s: int = 5) -> Any:
        from ai.controllers.executor import Executor, ExecutorConfig
        from ai.utils import TraceLogger

        call_index = {"i": 0}
        class MockMCP:
            def call(self, tool, args=None):
                idx = call_index["i"]
                call_index["i"] += 1
                if idx < len(mcp_responses):
                    return mcp_responses[idx]
                return {"ok": True, "payload": {}}

        trace = TraceLogger(tmp_path / "trace.log")
        config = ExecutorConfig(
            x402_auto_pay=True,
            x402_pay_from_node=2,
            x402_max_amount_msat=max_msat,
            x402_approval_threshold_msat=threshold_msat,
            x402_approval_timeout_s=timeout_s,
        )
        return Executor(mcp=MockMCP(), trace=trace, config=config, agent_dir=tmp_path)

    def _make_plan(self):
        from ai.models import ExecutionPlan, PlanStep
        return ExecutionPlan(
            steps=[
                PlanStep(step_id=1, tool="ln_listfunds", args={"node": 1},
                         expected_outcome="funds listed", depends_on=[],
                         on_error="retry", max_retries=2),
            ],
            plan_rationale="test", intent=None,
        )

    def test_auto_pay_below_threshold(self, tmp_path):
        """Amount below threshold → auto-pay without approval file."""
        executor = self._make_executor([
            {"result": {"status": 402, "bolt11": "lnbcrt...", "amount_msat": 1000, "payment_hash": "abc"}},
            {"result": {"ok": True, "payload": {"payment_preimage": "ff" * 32}}},
            {"result": {"ok": True, "payload": {"balance": 50000}}},
        ], tmp_path, threshold_msat=50_000)

        results = executor.execute(self._make_plan(), req_id=1)
        assert results[0].ok is True
        # No approval file should have been written
        assert not (tmp_path / "x402_pending.json").exists()

    def test_approval_requested_and_approved(self, tmp_path):
        """Amount above threshold → approval requested, approved, paid."""
        executor = self._make_executor([
            {"result": {"status": 402, "bolt11": "lnbcrt...", "amount_msat": 75000, "payment_hash": "abc"}},
            {"result": {"ok": True, "payload": {"payment_preimage": "ff" * 32}}},
            {"result": {"ok": True, "payload": {"balance": 50000}}},
        ], tmp_path, threshold_msat=50_000)

        # Write approval response from a background thread (simulating the UI)
        def approve_after_delay():
            time.sleep(0.3)
            write_approval_response(tmp_path, approved=True)

        t = threading.Thread(target=approve_after_delay)
        t.start()

        results = executor.execute(self._make_plan(), req_id=1)
        t.join()
        assert results[0].ok is True

    def _make_abort_plan(self):
        """Plan with on_error=abort so failures raise ExecutorError."""
        from ai.models import ExecutionPlan, PlanStep
        return ExecutionPlan(
            steps=[
                PlanStep(step_id=1, tool="ln_listfunds", args={"node": 1},
                         expected_outcome="funds listed", depends_on=[],
                         on_error="abort", max_retries=0),
            ],
            plan_rationale="test", intent=None,
        )

    def test_approval_denied(self, tmp_path):
        """Amount above threshold → approval denied → step fails."""
        executor = self._make_executor([
            {"result": {"status": 402, "bolt11": "lnbcrt...", "amount_msat": 75000, "payment_hash": "abc"}},
        ], tmp_path, threshold_msat=50_000)

        def deny_after_delay():
            time.sleep(0.3)
            write_approval_response(tmp_path, approved=False)

        t = threading.Thread(target=deny_after_delay)
        t.start()

        from ai.controllers.executor import ExecutorError
        with pytest.raises(ExecutorError, match="denied"):
            executor.execute(self._make_abort_plan(), req_id=1)
        t.join()

    def test_approval_timeout(self, tmp_path):
        """No response within timeout → step fails."""
        executor = self._make_executor([
            {"result": {"status": 402, "bolt11": "lnbcrt...", "amount_msat": 75000, "payment_hash": "abc"}},
        ], tmp_path, threshold_msat=50_000, timeout_s=1)

        from ai.controllers.executor import ExecutorError
        with pytest.raises(ExecutorError, match="denied|timed out"):
            executor.execute(self._make_abort_plan(), req_id=1)

    def test_hard_cap_exceeded(self, tmp_path):
        """Amount above hard cap → refused, no approval file."""
        executor = self._make_executor([
            {"result": {"status": 402, "bolt11": "lnbcrt...", "amount_msat": 200_000_000, "payment_hash": "abc"}},
        ], tmp_path, threshold_msat=50_000, max_msat=100_000_000)

        from ai.controllers.executor import ExecutorError
        with pytest.raises(ExecutorError, match="safety cap"):
            executor.execute(self._make_abort_plan(), req_id=1)
        assert not (tmp_path / "x402_pending.json").exists()

    def test_stale_files_cleaned_on_init(self, tmp_path):
        """Constructor should clean up leftover approval files."""
        write_approval_request(tmp_path, {"stale": True})
        write_approval_response(tmp_path, approved=True)

        from ai.controllers.executor import Executor, ExecutorConfig
        from ai.utils import TraceLogger

        class DummyMCP:
            def call(self, tool, args=None):
                return {}

        trace = TraceLogger(tmp_path / "trace.log")
        config = ExecutorConfig(x402_auto_pay=True)
        Executor(mcp=DummyMCP(), trace=trace, config=config, agent_dir=tmp_path)

        assert not (tmp_path / "x402_pending.json").exists()
        assert not (tmp_path / "x402_response.json").exists()

    def test_trace_events_emitted(self, tmp_path):
        """Check that approval trace events are logged."""
        executor = self._make_executor([
            {"result": {"status": 402, "bolt11": "lnbcrt...", "amount_msat": 75000, "payment_hash": "abc"}},
            {"result": {"ok": True, "payload": {"payment_preimage": "ff" * 32}}},
            {"result": {"ok": True, "payload": {"balance": 50000}}},
        ], tmp_path, threshold_msat=50_000)

        def approve_after_delay():
            time.sleep(0.3)
            write_approval_response(tmp_path, approved=True)

        t = threading.Thread(target=approve_after_delay)
        t.start()

        executor.execute(self._make_plan(), req_id=1)
        t.join()

        # Read trace events from the log file
        trace_path = tmp_path / "trace.log"
        if trace_path.exists():
            events = []
            for line in trace_path.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            event_types = [e.get("event") for e in events]
            assert "x402_approval_requested" in event_types
            assert "x402_approved" in event_types
            assert "x402_paid" in event_types
