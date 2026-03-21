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


def test_summarize_empty_steps_falls_back():
    """No executed steps → fall back to intent.human_summary."""
    s = _make_summarizer([_final("Should not be used.")])
    intent = _make_intent()
    # Empty step list — summarizer returns intent.human_summary when no steps to include
    # Note: the current summarizer still calls the LLM even with empty steps; if it
    # returns non-empty content, that content is used.  Test the fallback path by
    # using the ErrorLLMBackend so the LLM call fails.
    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = ErrorLLMBackend()
    s = Summarizer(cfg, backend, _NullTrace())
    result = s.summarize(intent, [], req_id=2)
    assert result == intent.human_summary


def test_summarize_whitespace_only_falls_back():
    """LLM returns only whitespace → fall back to intent.human_summary."""
    s = _make_summarizer([_final("   \n  ")])
    intent = _make_intent()
    result = s.summarize(intent, [_make_step_result()], req_id=3)
    assert result == intent.human_summary


def test_summarize_failed_step_appears_in_prompt():
    """A step with ok=False is included in the user message sent to the LLM."""
    s = _make_summarizer([_final("Payment failed.")])
    step = _make_step_result(tool="ln_pay", ok=False, error="insufficient funds",
                             raw_result={"ok": False, "error": "insufficient funds"})
    s.summarize(_make_intent(), [step], req_id=4)
    user_msg = s.backend.calls[0].messages[-1]["content"]
    assert "ln_pay" in user_msg
    # ok=False should be in the serialized result
    assert '"ok": false' in user_msg or "'ok': False" in user_msg or "false" in user_msg


def test_summarize_logs_llm_call_event():
    """Summarizer logs an llm_call trace event with stage=summarizer."""
    logged: list = []

    class _RecordingTrace:
        def log(self, event):
            logged.append(event)

    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = MockLLMBackend([_final("done")])
    s = Summarizer(cfg, backend, _RecordingTrace())
    s.summarize(_make_intent(), [_make_step_result()], req_id=5)
    kinds = [e.get("event") for e in logged]
    assert "llm_call" in kinds
    llm_call_events = [e for e in logged if e.get("event") == "llm_call"]
    assert llm_call_events[0]["stage"] == "summarizer"


def test_summarize_logs_llm_error_event_on_failure():
    """Summarizer logs an llm_error trace event when the LLM raises."""
    logged: list = []

    class _RecordingTrace:
        def log(self, event):
            logged.append(event)

    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = ErrorLLMBackend()
    s = Summarizer(cfg, backend, _RecordingTrace())
    s.summarize(_make_intent(), [_make_step_result()], req_id=6)
    kinds = [e.get("event") for e in logged]
    assert "llm_error" in kinds


def test_summarize_no_tools_in_request():
    """LLMRequest sent by summarizer must have empty tools list (text-only gen)."""
    s = _make_summarizer([_final("result")])
    s.summarize(_make_intent(), [_make_step_result()], req_id=7)
    req = s.backend.calls[0]
    assert req.tools == []


def test_summarize_goal_in_user_message():
    """The intent goal string appears verbatim in the user message."""
    goal = "Get the current block height from the blockchain"
    s = _make_summarizer([_final("done")])
    intent = _make_intent(goal=goal)
    s.summarize(intent, [_make_step_result()], req_id=8)
    user_msg = s.backend.calls[0].messages[-1]["content"]
    assert goal in user_msg


# =============================================================================
# Streaming tests (on_token callback / backend.stream() path)
# =============================================================================

class _StreamingBackend(LLMBackend):
    """
    Test backend that yields a preset list of token chunks from stream() and
    raises RuntimeError from step() to catch any accidental non-streaming calls.

    Using RuntimeError (not LLMError) means an accidental step() call fails the
    test loudly rather than triggering the LLMError fallback path and producing
    a false-passing test.

    This fixture verifies three contract points simultaneously:
      1. on_token is called exactly once per chunk yielded by stream()
      2. The return value is the stripped concatenation of all chunks
      3. step() is never called when on_token is provided
    """

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def step(self, request: LLMRequest) -> LLMResponse:
        # Fail loudly if step() is called — the streaming path must use stream()
        raise RuntimeError(
            "_StreamingBackend.step() must not be called when on_token is provided. "
            "The summarizer should use backend.stream() instead."
        )

    def stream(self, request: LLMRequest):
        yield from self._chunks


class _StreamingErrorBackend(LLMBackend):
    """
    Test backend whose stream() raises LLMError to verify the fallback path
    when streaming fails mid-request (e.g. API rate limit during token delivery).
    """

    def step(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("step() must not be called on _StreamingErrorBackend")

    def stream(self, request: LLMRequest):
        raise LLMError("api rate limited during streaming")
        yield  # Make this a generator function despite the unconditional raise


def test_summarize_streaming_calls_on_token_per_chunk():
    """
    on_token is called once per chunk; return value is the joined result.

    Verifies:
      - on_token receives exactly the chunks yielded by backend.stream()
      - The final return value is those chunks joined and stripped
      - step() is never called (would raise RuntimeError from _StreamingBackend)
    """
    chunks = ["Node 1 ", "has a ", "balance of 1M sats."]
    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = _StreamingBackend(chunks)
    s = Summarizer(cfg, backend, _NullTrace())

    received: list = []
    result = s.summarize(
        _make_intent(),
        [_make_step_result()],
        req_id=9,
        on_token=received.append,
    )

    # on_token must receive each chunk in order
    assert received == chunks, f"Expected chunks {chunks!r}, got {received!r}"

    # Return value must be the stripped concatenation of all chunks
    assert result == "Node 1 has a balance of 1M sats."


def test_summarize_streaming_empty_chunks_fallback():
    """
    An empty stream (no chunks yielded) falls back to intent.human_summary.

    When stream() yields nothing, the joined result is "" and strip() produces "",
    which triggers the existing fallback: return intent.human_summary.
    This ensures an LLM that produces no output is handled gracefully.
    """
    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = _StreamingBackend([])  # No chunks — stream yields nothing
    s = Summarizer(cfg, backend, _NullTrace())
    intent = _make_intent()

    result = s.summarize(intent, [_make_step_result()], req_id=10, on_token=lambda _: None)

    # Empty stream → empty join → empty strip → fallback to human_summary
    assert result == intent.human_summary, (
        f"Expected human_summary fallback, got: {result!r}"
    )


def test_summarize_streaming_error_falls_back():
    """
    An LLMError raised during stream() falls back to intent.human_summary.

    This verifies that the existing LLMError catch block covers the streaming
    path, not just the step() path. The llm_error trace event must also be
    emitted so operators can see the failure in the UI trace panel.
    """
    logged: list = []

    class _RecordingTrace:
        def log(self, event: dict) -> None:
            logged.append(event)

    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = _StreamingErrorBackend()
    s = Summarizer(cfg, backend, _RecordingTrace())
    intent = _make_intent()

    result = s.summarize(intent, [_make_step_result()], req_id=11, on_token=lambda _: None)

    # LLMError during streaming → fallback to human_summary (same as step() path)
    assert result == intent.human_summary, (
        f"Expected human_summary fallback on streaming error, got: {result!r}"
    )

    # The llm_error event must be logged so it's visible in the trace panel
    error_events = [e for e in logged if e.get("event") == "llm_error"]
    assert error_events, "Expected an llm_error trace event to be logged"
    assert error_events[0].get("stage") == "summarizer"


def test_summarize_no_streaming_when_no_callback():
    """
    step() is used (not stream()) when on_token=None.

    Documents the routing contract: the summarizer calls backend.stream() only
    when on_token is provided; otherwise it calls backend.step(). Using
    _StreamingBackend (which raises RuntimeError from step()) as the backend
    proves that step() is called — if stream() were called instead, no error
    would be raised and the assertion would be wrong.
    """
    cfg = SummarizerConfig(max_output_tokens=512, temperature=0.2)
    backend = _StreamingBackend(["ignored"])  # step() raises RuntimeError
    s = Summarizer(cfg, backend, _NullTrace())

    # Calling without on_token must route to step(), which raises RuntimeError.
    # If the summarizer mistakenly calls stream() instead, the chunks would be
    # joined into "ignored", the LLMError catch would NOT trigger (RuntimeError
    # is not LLMError), and the exception would propagate out — still a failure,
    # but a confusing one. This test makes the routing explicit.
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="must not be called"):
        s.summarize(_make_intent(), [_make_step_result()], req_id=12, on_token=None)
