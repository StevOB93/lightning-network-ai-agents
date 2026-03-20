"""
Tests for ai/controllers/translator.py

Strategy:
  - MockLLMBackend returns preset LLMResponse objects from a queue.
  - _NullTrace discards all events (no file I/O in tests).
  - No real LLM API calls or network connections are made.

Test groups:
  Happy path    — valid JSON responses produce correct IntentBlock fields.
  Retry         — bad JSON on first attempt triggers retry; exhausted retries raise TranslatorError.
  Validation    — missing/invalid fields exhaust retries and raise TranslatorError.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from ai.llm.base import LLMBackend, LLMRequest, LLMResponse, TransientAPIError
from ai.controllers.translator import Translator, TranslatorConfig, TranslatorError
from ai.models import IntentBlock


# =============================================================================
# Mock infrastructure
# =============================================================================

class _NullTrace:
    """Trace logger that discards all events (test stub)."""
    def reset(self, header: Dict[str, Any]) -> None:
        pass
    def log(self, event: Dict[str, Any]) -> None:
        pass


def _final(content: str) -> LLMResponse:
    return LLMResponse(type="final", tool_calls=[], content=content, reasoning=None, usage=None)


class MockLLMBackend(LLMBackend):
    """Returns responses from a pre-set queue. Raises if queue is empty."""
    def __init__(self, responses: List[LLMResponse]) -> None:
        self._queue = list(responses)
        self.calls: List[LLMRequest] = []

    def step(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._queue:
            raise RuntimeError("MockLLMBackend: no more responses queued")
        return self._queue.pop(0)


def _make_translator(responses: List[LLMResponse], max_retries: int = 2) -> Translator:
    cfg = TranslatorConfig(max_output_tokens=512, temperature=0.1, max_retries=max_retries)
    backend = MockLLMBackend(responses)
    return Translator(cfg, backend, _NullTrace())


def _valid_intent_json(**overrides) -> str:
    base = {
        "goal": "Check network health",
        "intent_type": "noop",
        "context": {},
        "success_criteria": ["network_health returns ok"],
        "clarifications_needed": [],
        "human_summary": "You want me to check network health.",
    }
    base.update(overrides)
    return json.dumps(base)


# =============================================================================
# Happy path
# =============================================================================

def test_translate_returns_intent_block():
    t = _make_translator([_final(_valid_intent_json())])
    result = t.translate("check network health", req_id=1)
    assert isinstance(result, IntentBlock)
    assert result.goal == "Check network health"
    assert result.intent_type == "noop"
    assert result.raw_prompt == "check network health"


def test_translate_payment_intent():
    payload = _valid_intent_json(
        goal="Pay node 2 with 1000 sat",
        intent_type="pay_invoice",
        context={"from_node": 1, "to_node": 2, "amount_sat": 1000},
        success_criteria=["payment sent", "preimage received"],
        human_summary="I'll send 1000 sat from node 1 to node 2.",
    )
    t = _make_translator([_final(payload)])
    result = t.translate("pay node 2 with 1000 sat", req_id=2)
    assert result.intent_type == "pay_invoice"
    assert result.context["from_node"] == 1
    assert result.context["amount_sat"] == 1000
    assert len(result.success_criteria) == 2


def test_translate_open_channel_intent():
    payload = _valid_intent_json(
        goal="Open a 500000 sat channel from node 1 to node 2",
        intent_type="open_channel",
        context={"from_node": 1, "to_node": 2, "amount_sat": 500000},
        human_summary="Opening a 500k sat channel between node 1 and 2.",
    )
    t = _make_translator([_final(payload)])
    result = t.translate("open channel 500000 sat from node1 to node2", req_id=3)
    assert result.intent_type == "open_channel"
    assert result.context["amount_sat"] == 500000


def test_translate_strips_markdown_fences():
    """LLM sometimes wraps JSON in ```json ... ```"""
    raw = "```json\n" + _valid_intent_json() + "\n```"
    t = _make_translator([_final(raw)])
    result = t.translate("check network health", req_id=4)
    assert isinstance(result, IntentBlock)


def test_translate_human_summary_preserved():
    t = _make_translator([_final(_valid_intent_json(human_summary="I'll check the network for you."))])
    result = t.translate("how is the network?", req_id=5)
    assert result.human_summary == "I'll check the network for you."


def test_unknown_intent_type_falls_back_to_freeform():
    payload = _valid_intent_json(intent_type="fly_to_moon")
    t = _make_translator([_final(payload)])
    result = t.translate("fly to the moon", req_id=6)
    assert result.intent_type == "freeform"


# =============================================================================
# Retry behavior
# =============================================================================

def test_retry_on_bad_json_then_success():
    """First response is not JSON; second is valid."""
    good = _valid_intent_json(goal="Retry succeeded")
    t = _make_translator([
        _final("This is not JSON at all."),
        _final(good),
    ], max_retries=2)
    result = t.translate("anything", req_id=7)
    assert result.goal == "Retry succeeded"
    assert len(t.backend.calls) == 2  # type: ignore[attr-defined]


def test_retry_exhausted_raises_translator_error():
    """All responses are invalid JSON — should raise TranslatorError."""
    t = _make_translator([
        _final("bad"),
        _final("also bad"),
        _final("still bad"),
    ], max_retries=2)
    with pytest.raises(TranslatorError):
        t.translate("anything", req_id=8)


def test_llm_error_raises_translator_error():
    class ErrorBackend(LLMBackend):
        def step(self, request: LLMRequest) -> LLMResponse:
            raise TransientAPIError("connection reset")

    cfg = TranslatorConfig()
    t = Translator(cfg, ErrorBackend(), _NullTrace())
    with pytest.raises(TranslatorError, match="LLM error"):
        t.translate("anything", req_id=9)


# =============================================================================
# Validation
# =============================================================================

def test_missing_goal_field_raises_on_retry_exhaustion():
    payload = json.dumps({
        "intent_type": "noop",
        "context": {},
        "success_criteria": [],
        "clarifications_needed": [],
        "human_summary": "ok",
    })
    t = _make_translator([_final(payload)] * 3, max_retries=2)
    with pytest.raises(TranslatorError):
        t.translate("anything", req_id=10)


def test_non_dict_json_raises_on_retry():
    t = _make_translator([_final("[1, 2, 3]")] * 3, max_retries=2)
    with pytest.raises(TranslatorError):
        t.translate("anything", req_id=11)


def test_raw_prompt_stored_in_intent():
    t = _make_translator([_final(_valid_intent_json())])
    result = t.translate("my original text", req_id=12)
    assert result.raw_prompt == "my original text"
