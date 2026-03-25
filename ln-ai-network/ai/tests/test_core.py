"""Tests for ai/core/ modules: config, scheduler, rate_limiter, backoff, concurrency.

Strategy:
  - All tests are unit tests with no external dependencies.
  - Time-sensitive tests (backoff, rate limiter) use monkeypatching or
    very short delays (1 ms) to keep the suite fast.
"""
from __future__ import annotations

import time

import pytest

from ai.core.backoff import DeterministicBackoff
from ai.core.concurrency import ConcurrencyGate
from ai.core.config import AgentConfig
from ai.core.rate_limiter import DualRateLimiter
from ai.core.scheduler import DeterministicScheduler
from ai.core.token_estimation import HeuristicTokenEstimator


# =============================================================================
# AgentConfig
# =============================================================================

class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig()
        assert cfg.tick_ms == 500
        assert cfg.llm_rpm == 30
        assert cfg.llm_tpm == 60_000
        assert cfg.llm_temperature == 0.2
        assert cfg.max_history_messages == 6
        assert cfg.backoff_base_ms == 1_000
        assert cfg.circuit_breaker_after == 6

    def test_from_env_picks_up_overrides(self, monkeypatch):
        monkeypatch.setenv("AGENT_TICK_MS", "250")
        monkeypatch.setenv("LLM_RPM", "60")
        monkeypatch.setenv("LLM_TEMPERATURE", "0.5")
        cfg = AgentConfig.from_env()
        assert cfg.tick_ms == 250
        assert cfg.llm_rpm == 60
        assert cfg.llm_temperature == 0.5

    def test_from_env_uses_defaults_for_missing_vars(self, monkeypatch):
        # Remove any env vars that might be set in the environment
        for var in ("AGENT_TICK_MS", "LLM_RPM", "LLM_TPM"):
            monkeypatch.delenv(var, raising=False)
        cfg = AgentConfig.from_env()
        assert cfg.tick_ms == 500
        assert cfg.llm_rpm == 30

    def test_frozen(self):
        cfg = AgentConfig()
        with pytest.raises(Exception):  # FrozenInstanceError
            cfg.tick_ms = 999  # type: ignore


# =============================================================================
# DeterministicScheduler
# =============================================================================

class TestDeterministicScheduler:
    def test_rejects_zero_tick_ms(self):
        with pytest.raises(ValueError):
            DeterministicScheduler(0)

    def test_rejects_negative_tick_ms(self):
        with pytest.raises(ValueError):
            DeterministicScheduler(-1)

    def test_wait_advances_tick_counter(self):
        sched = DeterministicScheduler(1)  # 1 ms cadence
        assert sched._k == 0
        sched.wait_next_tick()
        assert sched._k == 1

    def test_overrun_does_not_sleep(self):
        """If we're already past the next tick time, wait_next_tick returns immediately."""
        sched = DeterministicScheduler(1)
        # Burn some time so next tick is already overdue
        time.sleep(0.01)
        t0 = time.monotonic()
        sched.wait_next_tick()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.005  # Should return almost immediately


# =============================================================================
# DualRateLimiter
# =============================================================================

class TestDualRateLimiter:
    def test_allowed_fresh_limiter(self):
        lim = DualRateLimiter(rpm=30, tpm=60_000, min_interval_ms=0)
        assert lim.allowed(100) is True

    def test_spend_then_allowed_respects_min_interval(self):
        lim = DualRateLimiter(rpm=30, tpm=60_000, min_interval_ms=500)
        lim.spend(100)
        # Immediately after spending, min_interval hasn't elapsed
        assert lim.allowed(100) is False

    def test_spend_reduces_tpm_bucket(self):
        lim = DualRateLimiter(rpm=10_000, tpm=100, min_interval_ms=0)
        lim.spend(90)
        # Only 10 tokens left; a 20-token request should be rejected
        assert lim.allowed(20) is False
        # A 5-token request should be allowed
        assert lim.allowed(5) is True

    def test_reconcile_drains_extra_tokens(self):
        lim = DualRateLimiter(rpm=10_000, tpm=100, min_interval_ms=0)
        lim.spend(50)
        # We estimated 50 but used 80 — extra 30 should be drained
        lim.reconcile_actual(80, 50)
        # Now only ~20 tokens remain; a 25-token request should be rejected
        assert lim.allowed(25) is False

    def test_reconcile_no_op_when_under_estimate(self):
        lim = DualRateLimiter(rpm=10_000, tpm=100, min_interval_ms=0)
        lim.spend(50)
        before = lim._tok.tokens
        lim.reconcile_actual(30, 50)  # actual < estimate → no drain
        assert lim._tok.tokens == before  # unchanged


# =============================================================================
# DeterministicBackoff
# =============================================================================

class TestDeterministicBackoff:
    def _make(self, **kw):
        defaults = dict(base_ms=10, max_ms=100, jitter_ms=0,
                        circuit_breaker_after=3, circuit_breaker_open_ms=100)
        defaults.update(kw)
        return DeterministicBackoff(**defaults)

    def test_not_blocked_initially(self):
        b = self._make()
        assert b.blocked() is False

    def test_blocked_after_failure(self):
        b = self._make(base_ms=500)
        b.note_failure(step_id=1)
        assert b.blocked() is True

    def test_note_success_clears_block(self):
        b = self._make(base_ms=1)
        b.note_failure(step_id=1)
        b.note_success()
        # After success the per-failure delay is cleared
        assert b.state.attempt == 0
        assert b.state.consecutive_failures == 0
        assert b.state.blocked_until == 0.0

    def test_circuit_breaker_opens_after_n_failures(self):
        b = self._make(base_ms=1, circuit_breaker_after=3, circuit_breaker_open_ms=5000)
        for i in range(3):
            b.note_failure(step_id=i)
        assert b.state.circuit_open_until > time.monotonic()

    def test_retry_after_respected(self):
        b = self._make(base_ms=1)
        b.note_failure(step_id=1, retry_after_s=10.0)
        # blocked_until should be roughly now + 10s
        assert b.state.blocked_until > time.monotonic() + 9.0


# =============================================================================
# ConcurrencyGate
# =============================================================================

class TestConcurrencyGate:
    def test_acquire_non_blocking_succeeds_when_free(self):
        gate = ConcurrencyGate(max_in_flight=1)
        assert gate.acquire(blocking=False) is True
        gate.release()

    def test_acquire_non_blocking_fails_when_full(self):
        gate = ConcurrencyGate(max_in_flight=1)
        gate.acquire(blocking=False)
        assert gate.acquire(blocking=False) is False
        gate.release()

    def test_release_allows_second_acquire(self):
        gate = ConcurrencyGate(max_in_flight=1)
        gate.acquire(blocking=False)
        gate.release()
        assert gate.acquire(blocking=False) is True
        gate.release()

    def test_max_in_flight_respected(self):
        gate = ConcurrencyGate(max_in_flight=3)
        assert gate.acquire(blocking=False) is True
        assert gate.acquire(blocking=False) is True
        assert gate.acquire(blocking=False) is True
        assert gate.acquire(blocking=False) is False
        gate.release()
        gate.release()
        gate.release()

    def test_zero_max_clamped_to_one(self):
        """max_in_flight=0 is clamped to 1 — a gate with no slots would deadlock."""
        gate = ConcurrencyGate(max_in_flight=0)
        assert gate.acquire(blocking=False) is True
        gate.release()


# =============================================================================
# HeuristicTokenEstimator
# =============================================================================

class TestHeuristicTokenEstimator:
    def test_empty_input_returns_minimum(self):
        est = HeuristicTokenEstimator()
        result = est.estimate_prompt_tokens([], [])
        assert result >= 1

    def test_longer_content_yields_more_tokens(self):
        est = HeuristicTokenEstimator()
        short = est.estimate_prompt_tokens(
            [{"role": "user", "content": "hi"}], []
        )
        long = est.estimate_prompt_tokens(
            [{"role": "user", "content": "x" * 1000}], []
        )
        assert long > short

    def test_tools_schema_adds_tokens(self):
        est = HeuristicTokenEstimator()
        without = est.estimate_prompt_tokens([{"role": "user", "content": "hi"}], [])
        tools = [{"type": "function", "function": {"name": "ln_getinfo", "parameters": {}}}]
        with_tools = est.estimate_prompt_tokens([{"role": "user", "content": "hi"}], tools)
        assert with_tools > without
