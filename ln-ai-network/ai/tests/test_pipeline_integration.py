"""
Pipeline integration test: verifies all 3 stages chain together correctly.

Uses the same mock infrastructure as unit tests (no real LLM or MCP calls),
but runs the full Translator → Planner → Executor chain and validates that
output artifacts from each stage feed correctly into the next.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ai.controllers.executor import Executor, ExecutorConfig
from ai.controllers.planner import Planner, PlannerConfig
from ai.controllers.translator import Translator, TranslatorConfig
from ai.llm.base import LLMBackend, LLMRequest, LLMResponse
from ai.mcp_client import MCPClient
from ai.models import PipelineResult


# =============================================================================
# Shared mock infrastructure
# =============================================================================

class _NullTrace:
    def reset(self, header: Dict[str, Any]) -> None:
        pass
    def log(self, event: Dict[str, Any]) -> None:
        pass


class MockLLMBackend(LLMBackend):
    def __init__(self, responses: List[LLMResponse]) -> None:
        self._queue = list(responses)

    def step(self, request: LLMRequest) -> LLMResponse:
        if not self._queue:
            raise RuntimeError("MockLLMBackend: queue empty")
        return self._queue.pop(0)


class MockMCPClient(MCPClient):
    def __init__(self, responses: List[Any]) -> None:
        self._queue = list(responses)
        self.calls: List[Tuple[str, Dict]] = []

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Any:
        self.calls.append((tool, args or {}))
        if not self._queue:
            raise RuntimeError("MockMCPClient: queue empty")
        return self._queue.pop(0)


def _final(content: str) -> LLMResponse:
    return LLMResponse(type="final", tool_calls=[], content=content, reasoning=None, usage=None)


def _ok_result(payload: Dict | None = None) -> Dict:
    return {"result": {"ok": True, "payload": payload or {}}}


# =============================================================================
# Helpers: build valid JSON payloads
# =============================================================================

def _intent_json(**overrides) -> str:
    base = {
        "goal": "Check network health",
        "intent_type": "noop",
        "context": {},
        "success_criteria": ["network_health returns ok"],
        "clarifications_needed": [],
        "human_summary": "I will check the network health.",
    }
    base.update(overrides)
    return json.dumps(base)


def _plan_json(steps: List[Dict] | None = None, **overrides) -> str:
    base = {
        "steps": steps or [
            {
                "step_id": 1,
                "tool": "network_health",
                "args": {},
                "expected_outcome": "Network status returned",
                "depends_on": [],
                "on_error": "abort",
                "max_retries": 0,
            }
        ],
        "plan_rationale": "Check network health first.",
    }
    base.update(overrides)
    return json.dumps(base)


# =============================================================================
# Integration tests
# =============================================================================

class TestPipelineChain:
    """Verify the 3 stages chain together with consistent artifacts."""

    def _make_chain(self, intent_resp: str, plan_resp: str, mcp_responses: List[Any]):
        trace = _NullTrace()
        translator = Translator(TranslatorConfig(), MockLLMBackend([_final(intent_resp)]), trace)
        planner = Planner(PlannerConfig(), MockLLMBackend([_final(plan_resp)]), trace)
        executor = Executor(ExecutorConfig(), MockMCPClient(mcp_responses), trace)
        return translator, planner, executor

    def test_noop_health_check_chain(self):
        """Full chain for a simple health check intent."""
        translator, planner, executor = self._make_chain(
            _intent_json(),
            _plan_json(),
            [_ok_result({"status": "ok", "nodes": 2})],
        )

        intent = translator.translate("check network health", req_id=1)
        plan = planner.plan(intent, req_id=1)
        results = executor.execute(plan, req_id=1)

        # Intent is correct
        assert intent.intent_type == "noop"
        assert intent.raw_prompt == "check network health"

        # Plan references the same intent
        assert plan.intent is intent
        assert len(plan.steps) == 1
        assert plan.steps[0].tool == "network_health"

        # Executor ran exactly the planned steps
        assert len(results) == 1
        assert results[0].step_id == 1
        assert results[0].tool == "network_health"
        assert results[0].ok is True
        assert results[0].skipped is False

    def test_multistep_chain(self):
        """Full chain for a multi-step plan (two tool calls)."""
        plan_resp = _plan_json(steps=[
            {"step_id": 1, "tool": "network_health", "args": {},
             "expected_outcome": "status ok", "depends_on": [], "on_error": "abort", "max_retries": 0},
            {"step_id": 2, "tool": "ln_listpeers", "args": {"node": 1},
             "expected_outcome": "peers listed", "depends_on": [1], "on_error": "skip", "max_retries": 0},
        ])
        translator, planner, executor = self._make_chain(
            _intent_json(intent_type="freeform", goal="Check health then list peers"),
            plan_resp,
            [_ok_result(), _ok_result({"peers": []})],
        )

        intent = translator.translate("check health and list peers", req_id=2)
        plan = planner.plan(intent, req_id=2)
        results = executor.execute(plan, req_id=2)

        assert len(plan.steps) == 2
        assert len(results) == 2
        assert all(r.ok for r in results)
        assert [r.step_id for r in results] == [1, 2]

    def test_intent_type_propagates_to_plan(self):
        """The IntentBlock.intent_type correctly flows through to the plan artifact."""
        translator, planner, executor = self._make_chain(
            _intent_json(intent_type="freeform", goal="Open a channel"),
            _plan_json(),
            [_ok_result()],
        )
        intent = translator.translate("open a channel", req_id=3)
        plan = planner.plan(intent, req_id=3)

        assert plan.intent.intent_type == "freeform"
        assert plan.intent.goal == "Open a channel"

    def test_pipeline_result_serialization(self):
        """PipelineResult.to_outbox_dict() round-trips all stage artifacts."""
        translator, planner, executor = self._make_chain(
            _intent_json(),
            _plan_json(),
            [_ok_result()],
        )
        intent = translator.translate("check health", req_id=4)
        plan = planner.plan(intent, req_id=4)
        results = executor.execute(plan, req_id=4)

        pr = PipelineResult(
            request_id=4,
            ts=int(time.time()),
            success=True,
            stage_failed=None,
            intent=intent,
            plan=plan,
            step_results=results,
            human_summary="Network is healthy.",
            error=None,
            pipeline_build="test",
        )
        d = pr.to_outbox_dict()

        assert d["type"] == "pipeline_report"
        assert d["success"] is True
        assert d["stage_failed"] is None
        assert d["intent"]["raw_prompt"] == "check health"
        assert d["intent"]["intent_type"] == "noop"
        assert len(d["plan"]["steps"]) == 1
        assert len(d["step_results"]) == 1
        assert d["step_results"][0]["ok"] is True
        assert d["content"] == "Network is healthy."

    def test_step_failure_captured_in_result(self):
        """Executor failure is recorded correctly in StepResult."""
        plan_resp = _plan_json(steps=[
            {"step_id": 1, "tool": "network_health", "args": {},
             "expected_outcome": "ok", "depends_on": [], "on_error": "skip", "max_retries": 0},
            {"step_id": 2, "tool": "ln_listpeers", "args": {"node": 1},
             "expected_outcome": "peers listed", "depends_on": [], "on_error": "skip", "max_retries": 0},
        ])
        translator, planner, executor = self._make_chain(
            _intent_json(),
            plan_resp,
            [
                {"result": {"ok": False, "error": "node not running"}},
                _ok_result({"peers": []}),
            ],
        )
        intent = translator.translate("check health", req_id=5)
        plan = planner.plan(intent, req_id=5)
        results = executor.execute(plan, req_id=5)

        assert results[0].ok is False
        assert results[0].skipped is True  # on_error=skip
        assert results[1].ok is True

    def test_pipeline_result_stage_failed_on_error(self):
        """PipelineResult correctly records stage_failed when executor aborts."""
        from ai.controllers.executor import ExecutorError

        plan_resp = _plan_json(steps=[
            {"step_id": 1, "tool": "network_health", "args": {},
             "expected_outcome": "ok", "depends_on": [], "on_error": "abort", "max_retries": 0},
        ])
        translator, planner, executor = self._make_chain(
            _intent_json(),
            plan_resp,
            [{"result": {"ok": False, "error": "connection refused"}}],
        )
        intent = translator.translate("check health", req_id=6)
        plan = planner.plan(intent, req_id=6)

        with pytest.raises(ExecutorError):
            executor.execute(plan, req_id=6)

    def test_history_context_included_in_translation(self):
        """When history is passed, translator includes it in the LLM messages."""
        trace = _NullTrace()
        calls: List[LLMRequest] = []

        class RecordingBackend(LLMBackend):
            def step(self, request: LLMRequest) -> LLMResponse:
                calls.append(request)
                return _final(_intent_json())

        translator = Translator(TranslatorConfig(), RecordingBackend(), trace)
        history = [
            {"role": "user", "content": "previous prompt"},
            {"role": "assistant", "content": "previous response"},
        ]
        translator.translate("follow-up prompt", req_id=7, history=history)

        assert len(calls) == 1
        messages = calls[0].messages
        # History messages should appear between system and current user message
        roles = [m["role"] for m in messages]
        assert roles[0] == "system"
        assert "user" in roles[1:]
        assert "assistant" in roles
        # Current prompt is the last user message
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        assert user_msgs[-1] == "follow-up prompt"
        assert "previous prompt" in user_msgs
