"""
Tests for ai/controllers/summarizer.py

Strategy:
  - MockLLMBackend returns preset LLMResponse objects from a queue.
  - ErrorLLMBackend always raises LLMError (tests the fallback path).
  - _NullTrace discards all events (no file I/O in tests).
  - No real LLM API calls or network connections are made.

Test groups:
  Core behavior — LLM content is returned as-is when non-empty.
  Fallback      — LLM error or empty content falls back to intent.human_summary.
  Prompt shape  — tool result data (tool name, raw values) appears in the user message.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from ai.llm.base import LLMBackend, LLMError, LLMRequest, LLMResponse
from ai.controllers.summarizer import Summarizer, SummarizerConfig
from ai.models import IntentBlock, StepResult


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


class ErrorLLMBackend(LLMBackend):
    def step(self, request: LLMRequest) -> LLMResponse:
        raise LLMError("LLM unavailable")


def _make_intent(**overrides) -> IntentBlock:
    base = {
        "goal": "Get current balance",
        "intent_type": "noop",
        "context": {"node": 1},
        "success_criteria": [],
        "clarifications_needed": [],
        "human_summary": "Checking your balance.",
        "raw_prompt": "what is my current balance?",
    }
    base.update(overrides)
    return IntentBlock(**base)


def _make_step_result(**overrides) -> StepResult:
    base = {
        "step_id": 1,
        "tool": "ln_getinfo",
        "args": {"node": 1},
        "ok": True,
        "error": None,
        "raw_result": {"ok": True, "payload": {"id": "02aa", "alias": "node-1", "num_channels": 2}},
        "retries_used": 0,
        "skipped": False,
    }
    base.update(overrides)
    return StepResult(**base)


def _make_summarizer(responses: List[LLMResponse]) -> Summarizer:
    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = MockLLMBackend(responses)
    return Summarizer(cfg, backend, _NullTrace())


# =============================================================================
# Tests
# =============================================================================

def test_summarize_returns_llm_content():
    expected = "Node 1 has 2 channels and alias 'node-1'."
    s = _make_summarizer([_final(expected)])
    result = s.summarize(_make_intent(), [_make_step_result()], req_id=1)
    assert result == expected


def test_summarize_fallback_on_error():
    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = ErrorLLMBackend()
    s = Summarizer(cfg, backend, _NullTrace())
    intent = _make_intent()
    result = s.summarize(intent, [_make_step_result()], req_id=1)
    assert result == intent.human_summary


def test_summarize_fallback_on_empty_content():
    s = _make_summarizer([_final("")])
    intent = _make_intent()
    result = s.summarize(intent, [_make_step_result()], req_id=1)
    assert result == intent.human_summary


def test_summarize_includes_tool_results_in_prompt():
    s = _make_summarizer([_final("Answer.")])
    step = _make_step_result(tool="ln_listfunds", raw_result={"ok": True, "payload": {"funds": 50000}})
    s.summarize(_make_intent(), [step], req_id=1)

    # Verify the user message sent to LLM contains tool result data
    backend = s.backend
    assert len(backend.calls) == 1
    user_msg = backend.calls[0].messages[-1]["content"]
    assert "ln_listfunds" in user_msg
    assert "50000" in user_msg


def test_summarize_multiple_steps():
    expected = "Blockchain is at block 1200. Node 1 has 2 channels."
    s = _make_summarizer([_final(expected)])
    steps = [
        _make_step_result(step_id=1, tool="btc_getblockchaininfo", raw_result={"ok": True, "payload": {"blocks": 1200}}),
        _make_step_result(step_id=2, tool="ln_getinfo", raw_result={"ok": True, "payload": {"num_channels": 2}}),
    ]
    result = s.summarize(_make_intent(), steps, req_id=1)
    assert result == expected
