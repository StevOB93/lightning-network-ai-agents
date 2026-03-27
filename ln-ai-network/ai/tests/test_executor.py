"""
Tests for ai/controllers/executor.py

Strategy:
  - MockMCPClient returns preset results from a queue and records all calls.
  - _NullTrace discards all events (no file I/O in tests).
  - No real MCP, LLM, or network connections are made.

Test groups:
  Happy path            — successful tool calls produce correct StepResult fields.
  Error policies        — on_error="abort" raises ExecutorError; "skip" marks result skipped.
  Retry behavior        — on_error="retry" re-calls MCP up to max_retries times.
  Placeholder resolution — "$stepN.result.payload.key" is resolved from prior step raw_result.
  Args normalization    — _normalize_tool_args coerces node="1" to node=1 before MCP call.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from ai.controllers.executor import Executor, ExecutorConfig, ExecutorError
from ai.models import ExecutionPlan, IntentBlock, PlanStep


# =============================================================================
# Mock infrastructure
# =============================================================================

class _NullTrace:
    def reset(self, header: Dict[str, Any]) -> None:
        pass
    def log(self, event: Dict[str, Any]) -> None:
        pass


class MockMCPClient:
    """Returns responses from a pre-set queue. Records all calls."""
    def __init__(self, responses: List[Any]) -> None:
        self._queue = list(responses)
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Any:
        self.calls.append((tool, args or {}))
        if not self._queue:
            raise RuntimeError("MockMCPClient: no more responses queued")
        return self._queue.pop(0)


def _ok_result(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"result": {"ok": True, "payload": payload or {}}}


def _err_result(msg: str = "tool failed") -> Dict[str, Any]:
    return {"result": {"ok": False, "error": msg}}


def _make_executor(responses: List[Any]) -> Executor:
    cfg = ExecutorConfig()
    mcp = MockMCPClient(responses)
    return Executor(cfg, mcp, _NullTrace())


def _make_intent() -> IntentBlock:
    return IntentBlock(
        goal="Test",
        intent_type="noop",
        context={},
        success_criteria=[],
        clarifications_needed=[],
        human_summary="Test",
        raw_prompt="test",
    )


def _make_step(
    step_id: int = 1,
    tool: str = "network_health",
    args: Dict[str, Any] | None = None,
    on_error: str = "abort",
    max_retries: int = 0,
    depends_on: List[int] | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool=tool,
        args=args or {},
        expected_outcome="done",
        depends_on=depends_on or [],
        on_error=on_error,
        max_retries=max_retries,
    )


def _make_plan(steps: List[PlanStep]) -> ExecutionPlan:
    return ExecutionPlan(steps=steps, plan_rationale="test", intent=_make_intent())


# =============================================================================
# Happy path
# =============================================================================

def test_execute_empty_plan():
    e = _make_executor([])
    results = e.execute(_make_plan([]), req_id=1)
    assert results == []


def test_execute_single_step_success():
    e = _make_executor([_ok_result({"status": "healthy"})])
    results = e.execute(_make_plan([_make_step()]), req_id=2)
    assert len(results) == 1
    r = results[0]
    assert r.ok is True
    assert r.tool == "network_health"
    assert r.skipped is False
    assert r.retries_used == 0


def test_execute_multi_step_success():
    e = _make_executor([_ok_result(), _ok_result()])
    steps = [_make_step(1, "network_health"), _make_step(2, "btc_getblockchaininfo")]
    results = e.execute(_make_plan(steps), req_id=3)
    assert len(results) == 2
    assert all(r.ok for r in results)


def test_execute_records_mcp_calls():
    e = _make_executor([_ok_result()])
    e.execute(_make_plan([_make_step(1, "ln_getinfo", {"node": 1})]), req_id=4)
    assert e.mcp.calls[0] == ("ln_getinfo", {"node": 1})  # type: ignore[attr-defined]


# =============================================================================
# Error policies
# =============================================================================

def test_abort_on_error_raises_executor_error():
    e = _make_executor([_err_result("something broke")])
    with pytest.raises(ExecutorError, match="something broke"):
        e.execute(_make_plan([_make_step(on_error="abort")]), req_id=5)


def test_skip_on_error_returns_skipped_result():
    e = _make_executor([_err_result("not critical")])
    results = e.execute(_make_plan([_make_step(on_error="skip")]), req_id=6)
    assert len(results) == 1
    r = results[0]
    assert r.ok is False
    assert r.skipped is True


def test_skip_does_not_raise():
    """A skipped step must not stop execution of subsequent steps."""
    e = _make_executor([_err_result(), _ok_result()])
    steps = [_make_step(1, on_error="skip"), _make_step(2, "btc_getblockchaininfo")]
    results = e.execute(_make_plan(steps), req_id=7)
    assert results[0].skipped is True
    assert results[1].ok is True


def test_abort_stops_after_first_failure():
    e = _make_executor([_err_result(), _ok_result()])
    steps = [_make_step(1, on_error="abort"), _make_step(2)]
    with pytest.raises(ExecutorError):
        e.execute(_make_plan(steps), req_id=8)
    # Second step should never have been called
    assert len(e.mcp.calls) == 1  # type: ignore[attr-defined]


# =============================================================================
# Retry behavior
# =============================================================================

def test_retry_succeeds_on_second_attempt():
    e = _make_executor([_err_result(), _ok_result({"done": True})])
    step = _make_step(1, "network_health", on_error="retry", max_retries=1)
    results = e.execute(_make_plan([step]), req_id=9)
    assert results[0].ok is True
    assert results[0].retries_used == 1


def test_retry_exhausted_returns_failed():
    e = _make_executor([_err_result(), _err_result()])
    step = _make_step(1, "network_health", on_error="retry", max_retries=1)
    with pytest.raises(ExecutorError):
        e.execute(_make_plan([step]), req_id=10)
    assert len(e.mcp.calls) == 2  # type: ignore[attr-defined]


# =============================================================================
# Placeholder resolution
# =============================================================================

def test_placeholder_resolved_from_prior_step():
    """$step1.result.payload.bolt11 is filled from step 1's raw_result."""
    bolt11 = "lnbc100n1test"
    step1_result = _ok_result({"bolt11": bolt11})
    step2_result = _ok_result({"status": "paid"})

    e = _make_executor([step1_result, step2_result])

    step1 = _make_step(1, "ln_invoice", {"node": 1, "amount_msat": 1000, "label": "x", "description": "y"})
    step2 = _make_step(
        2, "ln_pay",
        {"from_node": 1, "bolt11": "$step1.result.payload.bolt11"},
    )
    results = e.execute(_make_plan([step1, step2]), req_id=11)

    assert results[1].ok is True
    # Confirm the placeholder was resolved before calling MCP
    _, call_args = e.mcp.calls[1]  # type: ignore[attr-defined]
    assert call_args["bolt11"] == bolt11


def test_placeholder_bad_path_aborts():
    """Placeholder that can't be navigated causes ExecutorError (on_error=abort)."""
    e = _make_executor([_ok_result({})])  # step1 has no "bolt11" in payload

    step1 = _make_step(1, "network_health")
    step2 = _make_step(
        2, "ln_pay",
        {"from_node": 1, "bolt11": "$step1.result.payload.bolt11"},
        on_error="abort",
    )

    with pytest.raises(ExecutorError, match="Placeholder"):
        e.execute(_make_plan([step1, step2]), req_id=12)


def test_placeholder_bad_path_skip():
    """Placeholder failure on a skip step returns skipped result."""
    e = _make_executor([_ok_result({})])

    step1 = _make_step(1, "network_health")
    step2 = _make_step(
        2, "ln_pay",
        {"from_node": 1, "bolt11": "$step1.result.payload.bolt11"},
        on_error="skip",
    )

    results = e.execute(_make_plan([step1, step2]), req_id=13)
    assert results[1].skipped is True


# =============================================================================
# Args normalization
# =============================================================================

def test_int_coercion_applied():
    """node passed as string "1" should be coerced to int 1."""
    e = _make_executor([_ok_result()])
    step = _make_step(1, "ln_getinfo", {"node": "1"})
    e.execute(_make_plan([step]), req_id=14)
    _, call_args = e.mcp.calls[0]  # type: ignore[attr-defined]
    assert call_args["node"] == 1


# =============================================================================
# Topological sort / depends_on enforcement
# =============================================================================

def test_depends_on_reorders_steps():
    """Steps provided out of order are sorted by depends_on before execution."""
    from ai.controllers.executor import _topological_sort
    step1 = _make_step(1, "ln_getinfo")
    step2 = _make_step(2, "ln_listfunds", depends_on=[1])
    # Provide in reverse order — sort should fix this
    ordered = _topological_sort([step2, step1])
    assert [s.step_id for s in ordered] == [1, 2]


def test_depends_on_multi_level():
    """Three-step chain: 3 depends on 2, 2 depends on 1."""
    from ai.controllers.executor import _topological_sort
    step1 = _make_step(1)
    step2 = _make_step(2, depends_on=[1])
    step3 = _make_step(3, depends_on=[2])
    ordered = _topological_sort([step3, step1, step2])
    assert [s.step_id for s in ordered] == [1, 2, 3]


def test_depends_on_circular_raises():
    """Circular dependency raises ValueError."""
    from ai.controllers.executor import _topological_sort
    step1 = _make_step(1, depends_on=[2])
    step2 = _make_step(2, depends_on=[1])
    with pytest.raises(ValueError, match="Circular"):
        _topological_sort([step1, step2])


def test_depends_on_unknown_step_raises():
    """Reference to a non-existent step_id raises ValueError."""
    from ai.controllers.executor import _topological_sort
    step1 = _make_step(1, depends_on=[99])
    with pytest.raises(ValueError, match="Unknown step_id"):
        _topological_sort([step1])


def test_execute_invalid_dependency_graph_raises_executor_error():
    """Circular dependency at execute() time raises ExecutorError (not ValueError)."""
    e = _make_executor([])
    step1 = _make_step(1, depends_on=[2])
    step2 = _make_step(2, depends_on=[1])
    with pytest.raises(ExecutorError, match="Invalid plan dependency graph"):
        e.execute(_make_plan([step1, step2]), req_id=15)


# =============================================================================
# _compute_levels and parallel execution
# =============================================================================

def test_compute_levels_no_deps():
    """Steps with no dependencies all land in wave 0."""
    from ai.controllers.executor import _compute_levels
    steps = [_make_step(1), _make_step(2), _make_step(3)]
    levels = _compute_levels(steps)
    assert len(levels) == 1
    assert {s.step_id for s in levels[0]} == {1, 2, 3}


def test_compute_levels_chain():
    """A linear chain produces one step per wave."""
    from ai.controllers.executor import _compute_levels
    step1 = _make_step(1)
    step2 = _make_step(2, depends_on=[1])
    step3 = _make_step(3, depends_on=[2])
    levels = _compute_levels([step1, step2, step3])
    assert len(levels) == 3
    assert levels[0][0].step_id == 1
    assert levels[1][0].step_id == 2
    assert levels[2][0].step_id == 3


def test_compute_levels_diamond():
    """Diamond: step1 → {step2, step3} → step4."""
    from ai.controllers.executor import _compute_levels
    step1 = _make_step(1)
    step2 = _make_step(2, depends_on=[1])
    step3 = _make_step(3, depends_on=[1])
    step4 = _make_step(4, depends_on=[2, 3])
    levels = _compute_levels([step1, step2, step3, step4])
    assert len(levels) == 3
    assert levels[0][0].step_id == 1
    assert {s.step_id for s in levels[1]} == {2, 3}
    assert levels[2][0].step_id == 4


def test_parallel_execution_independent_steps():
    """Steps with no depends_on run in parallel; both MCP calls are made."""
    step1 = _make_step(1, "ln_getinfo", {"node": 1})
    step2 = _make_step(2, "ln_listfunds", {"node": 2})
    # Two responses, one per step
    e = Executor(ExecutorConfig(max_workers=2), MockMCPClient([_ok_result(), _ok_result()]), _NullTrace())
    results = e.execute(_make_plan([step1, step2]), req_id=16)
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert {r.step_id for r in results} == {1, 2}


def test_parallel_wave_both_results_collected_before_failure_check():
    """Both steps in a wave execute before the failure is raised (wave semantics)."""
    step1 = _make_step(1, on_error="abort")
    step2 = _make_step(2, on_error="abort")
    # step1 fails, step2 succeeds — with parallel execution both run before the check
    e = Executor(ExecutorConfig(max_workers=2), MockMCPClient([_err_result(), _ok_result()]), _NullTrace())
    with pytest.raises(ExecutorError):
        e.execute(_make_plan([step1, step2]), req_id=17)
    # Both were called (parallel wave ran to completion before failure raised)
    assert len(e.mcp.calls) == 2  # type: ignore[attr-defined]
