"""
Tests for ai/controllers/planner.py

Strategy:
  - MockLLMBackend returns preset LLMResponse objects from a queue.
  - _NullTrace discards all events (no file I/O in tests).
  - No real LLM API calls or network connections are made.

Test groups:
  Happy path    — valid JSON responses produce correct ExecutionPlan fields.
  Retry         — bad JSON on first attempt triggers retry; exhausted retries raise PlannerError.
  Validation    — unknown tools, missing required args, duplicate step_ids, invalid on_error
                  all exhaust retries and raise PlannerError.

Note on placeholder args: steps may contain "$step1.result.payload.X" references as args.
The Planner validates tool names and required args but treats "$..." values as valid
placeholders — the Executor resolves them at runtime.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from ai.llm.base import LLMBackend, LLMRequest, LLMResponse, TransientAPIError
from ai.controllers.planner import Planner, PlannerConfig, PlannerError
from ai.models import ExecutionPlan, IntentBlock, PlanStep


# =============================================================================
# Mock infrastructure
# =============================================================================

class _NullTrace:
    def reset(self, header: Dict[str, Any]) -> None:
        pass
    def log(self, event: Dict[str, Any]) -> None:
        pass


def _final(content: str) -> LLMResponse:
    return LLMResponse(type="final", tool_calls=[], content=content, reasoning=None, usage=None)


class MockLLMBackend(LLMBackend):
    def __init__(self, responses: List[LLMResponse]) -> None:
        self._queue = list(responses)
        self.calls: List[LLMRequest] = []

    def step(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._queue:
            raise RuntimeError("MockLLMBackend: no more responses queued")
        return self._queue.pop(0)


def _make_planner(responses: List[LLMResponse], max_retries: int = 2) -> Planner:
    cfg = PlannerConfig(max_output_tokens=1024, temperature=0.1, max_retries=max_retries)
    backend = MockLLMBackend(responses)
    return Planner(cfg, backend, _NullTrace())


def _make_intent(intent_type: str = "noop", context: Dict[str, Any] | None = None) -> IntentBlock:
    return IntentBlock(
        goal="Test goal",
        intent_type=intent_type,
        context=context or {},
        success_criteria=["done"],
        clarifications_needed=[],
        human_summary="Test",
        raw_prompt="test",
    )


def _plan_json(steps: List[Dict[str, Any]] | None = None, rationale: str = "Test plan") -> str:
    return json.dumps({
        "plan_rationale": rationale,
        "steps": steps if steps is not None else [],
    })


def _ln_getinfo_step(step_id: int = 1) -> Dict[str, Any]:
    return {
        "step_id": step_id,
        "tool": "ln_getinfo",
        "args": {"node": 1},
        "expected_outcome": "Get node info",
        "depends_on": [],
        "on_error": "abort",
        "max_retries": 0,
    }


def _ln_pay_step(step_id: int = 2) -> Dict[str, Any]:
    return {
        "step_id": step_id,
        "tool": "ln_pay",
        "args": {"from_node": 1, "bolt11": "$step1.result.payload.bolt11"},
        "expected_outcome": "Payment sent",
        "depends_on": [1],
        "on_error": "abort",
        "max_retries": 0,
    }


# =============================================================================
# Happy path
# =============================================================================

def test_plan_returns_execution_plan():
    p = _make_planner([_final(_plan_json([_ln_getinfo_step()]))])
    result = p.plan(_make_intent("noop"), req_id=1)
    assert isinstance(result, ExecutionPlan)
    assert len(result.steps) == 1
    assert result.steps[0].tool == "ln_getinfo"


def test_plan_empty_steps_for_noop():
    p = _make_planner([_final(_plan_json([]))])
    result = p.plan(_make_intent("noop"), req_id=2)
    assert result.steps == []
    assert result.plan_rationale == "Test plan"


def test_plan_rationale_preserved():
    p = _make_planner([_final(_plan_json([], rationale="Doing nothing needed."))])
    result = p.plan(_make_intent(), req_id=3)
    assert result.plan_rationale == "Doing nothing needed."


def test_plan_intent_back_reference():
    intent = _make_intent("open_channel")
    p = _make_planner([_final(_plan_json([]))])
    result = p.plan(intent, req_id=4)
    assert result.intent is intent


def test_plan_strips_markdown_fences():
    raw = "```json\n" + _plan_json([_ln_getinfo_step()]) + "\n```"
    p = _make_planner([_final(raw)])
    result = p.plan(_make_intent(), req_id=5)
    assert len(result.steps) == 1


def test_plan_multi_step():
    steps = [_ln_getinfo_step(1), _ln_pay_step(2)]
    p = _make_planner([_final(_plan_json(steps))])
    result = p.plan(_make_intent("pay_invoice"), req_id=6)
    assert len(result.steps) == 2
    assert result.steps[1].args["bolt11"] == "$step1.result.payload.bolt11"


def test_plan_placeholder_args_accepted():
    """Placeholder values in required args must not cause a validation error."""
    step = {
        "step_id": 1,
        "tool": "ln_pay",
        "args": {"from_node": 1, "bolt11": "$step0.result.payload.bolt11"},
        "expected_outcome": "Pay invoice",
        "depends_on": [],
        "on_error": "abort",
        "max_retries": 0,
    }
    p = _make_planner([_final(_plan_json([step]))])
    result = p.plan(_make_intent("pay_invoice"), req_id=7)
    assert result.steps[0].args["bolt11"] == "$step0.result.payload.bolt11"


# =============================================================================
# Retry behavior
# =============================================================================

def test_retry_on_bad_json_then_success():
    good = _plan_json([_ln_getinfo_step()])
    p = _make_planner([_final("not json at all"), _final(good)], max_retries=2)
    result = p.plan(_make_intent(), req_id=8)
    assert len(result.steps) == 1
    assert len(p.backend.calls) == 2  # type: ignore[attr-defined]


def test_retry_exhausted_raises_planner_error():
    p = _make_planner([_final("bad")] * 3, max_retries=2)
    with pytest.raises(PlannerError):
        p.plan(_make_intent(), req_id=9)


def test_llm_error_raises_planner_error():
    class ErrorBackend(LLMBackend):
        def step(self, request: LLMRequest) -> LLMResponse:
            raise TransientAPIError("connection reset")

    p = Planner(PlannerConfig(), ErrorBackend(), _NullTrace())
    with pytest.raises(PlannerError, match="LLM error"):
        p.plan(_make_intent(), req_id=10)


# =============================================================================
# Validation
# =============================================================================

def test_unknown_tool_raises():
    step = {
        "step_id": 1,
        "tool": "ln_fly_to_moon",
        "args": {},
        "expected_outcome": "Whatever",
        "depends_on": [],
        "on_error": "abort",
        "max_retries": 0,
    }
    p = _make_planner([_final(_plan_json([step]))] * 3, max_retries=2)
    with pytest.raises(PlannerError, match="unknown tool"):
        p.plan(_make_intent(), req_id=11)


def test_missing_required_arg_raises():
    # ln_getinfo requires "node"
    step = {
        "step_id": 1,
        "tool": "ln_getinfo",
        "args": {},
        "expected_outcome": "Get node info",
        "depends_on": [],
        "on_error": "abort",
        "max_retries": 0,
    }
    p = _make_planner([_final(_plan_json([step]))] * 3, max_retries=2)
    with pytest.raises(PlannerError, match="missing required arg"):
        p.plan(_make_intent(), req_id=12)


def test_invalid_on_error_raises():
    step = {
        "step_id": 1,
        "tool": "network_health",
        "args": {},
        "expected_outcome": "Check health",
        "depends_on": [],
        "on_error": "explode",
        "max_retries": 0,
    }
    p = _make_planner([_final(_plan_json([step]))] * 3, max_retries=2)
    with pytest.raises(PlannerError, match="invalid on_error"):
        p.plan(_make_intent(), req_id=13)


def test_duplicate_step_id_raises():
    steps = [_ln_getinfo_step(1), _ln_getinfo_step(1)]
    p = _make_planner([_final(_plan_json(steps))] * 3, max_retries=2)
    with pytest.raises(PlannerError, match="duplicate step_id"):
        p.plan(_make_intent(), req_id=14)


def test_non_dict_json_raises():
    p = _make_planner([_final("[1, 2, 3]")] * 3, max_retries=2)
    with pytest.raises(PlannerError):
        p.plan(_make_intent(), req_id=15)
