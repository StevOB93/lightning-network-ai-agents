"""Tests for ai.llm.guarded_backend.GuardedBackend.

Strategy:
  - Inner backend is a MagicMock so no real LLM calls are made.
  - Tests verify that the guard correctly:
      * Passes successful responses through unchanged
      * Calls note_failure() on TransientAPIError / RateLimitError
      * Calls note_success() on a successful call
      * Re-raises AuthError and PermanentAPIError without touching backoff
      * Reconciles actual token usage via DualRateLimiter.reconcile_actual()
      * Releases the concurrency gate even when an exception is raised
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai.core.config import AgentConfig
from ai.llm.base import (
    AuthError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    PermanentAPIError,
    RateLimitError,
    TransientAPIError,
)
from ai.llm.guarded_backend import GuardedBackend


# =============================================================================
# Helpers
# =============================================================================

def _make_cfg(**overrides) -> AgentConfig:
    """Return an AgentConfig with rate limits wide open for fast tests."""
    return AgentConfig(
        llm_rpm=10_000,
        llm_tpm=10_000_000,
        llm_min_interval_ms=0,
        llm_max_in_flight=1,
        backoff_base_ms=1,
        backoff_max_ms=10,
        backoff_jitter_ms=0,
        circuit_breaker_after=100,
        circuit_breaker_open_ms=1,
        **overrides,
    )


def _make_request() -> LLMRequest:
    return LLMRequest(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_output_tokens=64,
        temperature=0.0,
    )


def _final_response(content: str = "ok") -> LLMResponse:
    return LLMResponse(
        type="final",
        tool_calls=[],
        content=content,
        reasoning=None,
        usage=LLMUsage(prompt_tokens=10, output_tokens=5, total_tokens=15),
    )


def _make_guarded(inner):
    return GuardedBackend(inner, _make_cfg())


# =============================================================================
# Happy path
# =============================================================================

def test_successful_response_passed_through():
    inner = MagicMock()
    inner.step.return_value = _final_response("hello")
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    resp = g.step(_make_request())
    assert resp.content == "hello"
    assert resp.type == "final"


def test_note_success_called_after_successful_step():
    inner = MagicMock()
    inner.step.return_value = _final_response()
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with patch.object(g._backoff, "note_success") as mock_success:
        g.step(_make_request())
        mock_success.assert_called_once()


def test_reconcile_actual_called_with_usage():
    inner = MagicMock()
    inner.step.return_value = _final_response()
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with patch.object(g._limiter, "reconcile_actual") as mock_rec:
        g.step(_make_request())
        # Called with actual total (15) and estimated
        mock_rec.assert_called_once()
        actual_arg = mock_rec.call_args[0][0]
        assert actual_arg == 15


def test_no_reconcile_when_usage_is_none():
    inner = MagicMock()
    inner.step.return_value = LLMResponse(
        type="final", tool_calls=[], content="hi", reasoning=None, usage=None
    )
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with patch.object(g._limiter, "reconcile_actual") as mock_rec:
        g.step(_make_request())
        mock_rec.assert_not_called()


# =============================================================================
# Error handling
# =============================================================================

def test_transient_error_calls_note_failure():
    inner = MagicMock()
    inner.step.side_effect = TransientAPIError("server exploded")
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with patch.object(g._backoff, "note_failure") as mock_fail:
        with pytest.raises(TransientAPIError):
            g.step(_make_request())
        mock_fail.assert_called_once()


def test_rate_limit_error_calls_note_failure_with_retry_after():
    inner = MagicMock()
    inner.step.side_effect = RateLimitError("429", retry_after_s=5.0)
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with patch.object(g._backoff, "note_failure") as mock_fail:
        with pytest.raises(RateLimitError):
            g.step(_make_request())
        _, kwargs = mock_fail.call_args
        assert kwargs.get("retry_after_s") == 5.0


def test_auth_error_does_not_call_note_failure():
    inner = MagicMock()
    inner.step.side_effect = AuthError("bad key")
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with patch.object(g._backoff, "note_failure") as mock_fail:
        with pytest.raises(AuthError):
            g.step(_make_request())
        mock_fail.assert_not_called()


def test_permanent_error_does_not_call_note_failure():
    inner = MagicMock()
    inner.step.side_effect = PermanentAPIError("context too long")
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with patch.object(g._backoff, "note_failure") as mock_fail:
        with pytest.raises(PermanentAPIError):
            g.step(_make_request())
        mock_fail.assert_not_called()


# =============================================================================
# Concurrency gate
# =============================================================================

def test_gate_released_on_success():
    inner = MagicMock()
    inner.step.return_value = _final_response()
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    g.step(_make_request())
    # Gate should be available again (acquire non-blocking returns True)
    assert g._gate.acquire(blocking=False) is True
    g._gate.release()


def test_gate_released_on_exception():
    inner = MagicMock()
    inner.step.side_effect = TransientAPIError("boom")
    inner.token_estimator.return_value = None
    g = _make_guarded(inner)
    with pytest.raises(TransientAPIError):
        g.step(_make_request())
    # Gate must be released even after an exception
    assert g._gate.acquire(blocking=False) is True
    g._gate.release()


# =============================================================================
# Token estimator delegation
# =============================================================================

def test_uses_inner_token_estimator_if_available():
    """If the inner backend provides a token estimator, GuardedBackend uses it."""
    inner = MagicMock()
    inner.step.return_value = _final_response()
    custom_estimator = MagicMock()
    custom_estimator.estimate_prompt_tokens.return_value = 99
    inner.token_estimator.return_value = custom_estimator
    g = GuardedBackend(inner, _make_cfg())
    assert g._estimator is custom_estimator


def test_falls_back_to_heuristic_when_no_estimator():
    """Inner backend returning None for token_estimator → heuristic is used."""
    from ai.core.token_estimation import HeuristicTokenEstimator
    inner = MagicMock()
    inner.token_estimator.return_value = None
    g = GuardedBackend(inner, _make_cfg())
    assert isinstance(g._estimator, HeuristicTokenEstimator)
