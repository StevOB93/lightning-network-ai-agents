from __future__ import annotations

# =============================================================================
# GuardedBackend — wraps any LLMBackend with rate limiting, backoff, and
# concurrency gating built from AgentConfig values.
#
# Every backend.step() call in the pipeline passes through GuardedBackend,
# which enforces three independent protections before touching the provider:
#
#   1. DeterministicBackoff / circuit breaker
#      If previous calls raised TransientAPIError or RateLimitError, execution
#      blocks here until the computed backoff delay (or circuit-open window)
#      expires. This prevents hammering a degraded provider.
#
#   2. ConcurrencyGate
#      A semaphore limits the number of simultaneous in-flight LLM calls.
#      Default max_in_flight=1 enforces serial calls — safe with the current
#      single-threaded pipeline. Increase via LLM_MAX_IN_FLIGHT if the
#      executor ever runs plan steps in parallel across multiple threads.
#
#   3. DualRateLimiter (RPM + TPM + minimum interval)
#      A pre-call token estimate checks that both the request-per-minute and
#      token-per-minute budgets have capacity. If not, execution spins in a
#      short sleep loop until capacity is available. After the call completes,
#      reconcile_actual() corrects the TPM bucket for underestimates.
#
# Error handling:
#   TransientAPIError / RateLimitError  → note_failure() (triggers backoff)
#   AuthError / PermanentAPIError       → re-raise immediately (no backoff)
#   Success                             → note_success() + reconcile tokens
#
# Usage (pipeline.py):
#   cfg = AgentConfig.from_env()
#   backend = GuardedBackend(create_backend_for_role("translator"), cfg)
# =============================================================================

import time
from typing import Iterable, Optional

from ai.core.backoff import DeterministicBackoff
from ai.core.concurrency import ConcurrencyGate
from ai.core.config import AgentConfig
from ai.core.rate_limiter import DualRateLimiter
from ai.core.token_estimation import HeuristicTokenEstimator
from ai.llm.base import (
    AuthError,
    LLMBackend,
    LLMRequest,
    LLMResponse,
    PermanentAPIError,
    RateLimitError,
    TokenEstimator,
    TransientAPIError,
)


class GuardedBackend(LLMBackend):
    """
    Decorator that adds rate limiting, exponential backoff, and concurrency
    control to any LLMBackend implementation.

    All three guards are built from a single AgentConfig instance, so the
    entire pipeline shares the same tuning parameters (env vars).

    Each GuardedBackend instance maintains independent state — wrapping the
    translator, planner, and summarizer backends separately means each stage
    has its own rate-limit buckets and backoff counters. This is intentional:
    a summarizer rate-limit should not block the translator from starting the
    next query.
    """

    def __init__(self, inner: LLMBackend, cfg: AgentConfig) -> None:
        self._inner = inner
        self._backoff = DeterministicBackoff(
            base_ms=cfg.backoff_base_ms,
            max_ms=cfg.backoff_max_ms,
            jitter_ms=cfg.backoff_jitter_ms,
            circuit_breaker_after=cfg.circuit_breaker_after,
            circuit_breaker_open_ms=cfg.circuit_breaker_open_ms,
        )
        self._gate = ConcurrencyGate(cfg.llm_max_in_flight)
        self._limiter = DualRateLimiter(
            rpm=cfg.llm_rpm,
            tpm=cfg.llm_tpm,
            min_interval_ms=cfg.llm_min_interval_ms,
        )
        # Prefer a provider-specific estimator; fall back to the heuristic.
        self._estimator: TokenEstimator = inner.token_estimator() or HeuristicTokenEstimator()

    def stream(self, request: LLMRequest) -> Iterable[str]:
        """
        Stream text tokens through the inner backend with concurrency and
        backoff guards applied.

        Rate limiting is not pre-applied (token count is unknown upfront for
        streaming), but the concurrency gate and circuit breaker are enforced
        so that streaming calls respect the same backoff state as step() calls.
        """
        while self._backoff.blocked():
            now = time.monotonic()
            wake = max(
                self._backoff.state.blocked_until,
                self._backoff.state.circuit_open_until,
            )
            time.sleep(max(0.05, min(wake - now, 1.0)))

        req_id = id(request)
        self._gate.acquire(blocking=True)
        try:
            yield from self._inner.stream(request)
            self._backoff.note_success()
        except (TransientAPIError, RateLimitError) as exc:
            retry_after = getattr(exc, "retry_after_s", None)
            self._backoff.note_failure(req_id, retry_after_s=retry_after)
            raise
        except (AuthError, PermanentAPIError):
            # Caller bugs — no backoff. Re-raise immediately without touching state.
            raise
        except Exception:
            self._backoff.note_failure(req_id)
            raise
        finally:
            self._gate.release()

    def token_estimator(self) -> Optional[TokenEstimator]:
        """Delegate to the inner backend's estimator (or heuristic fallback)."""
        return self._estimator

    def step(self, request: LLMRequest) -> LLMResponse:
        """
        Execute one LLM step with all three guards applied in sequence.

        Blocks if:
          - The backoff window has not yet elapsed (previous transient error)
          - The circuit breaker is open (too many consecutive failures)
          - No concurrency slot is available (llm_max_in_flight reached)
          - The RPM or TPM budget is exhausted (rate limiter not ready)
        """
        # ── 1. Wait for backoff / circuit breaker ────────────────────────────
        # Poll in 50ms increments up to 1s per wake to keep the loop responsive.
        while self._backoff.blocked():
            now = time.monotonic()
            wake = max(
                self._backoff.state.blocked_until,
                self._backoff.state.circuit_open_until,
            )
            time.sleep(max(0.05, min(wake - now, 1.0)))

        # ── 2. Acquire concurrency slot (blocking) ────────────────────────────
        self._gate.acquire(blocking=True)
        try:
            # ── 3. Wait for rate limiter ──────────────────────────────────────
            # Estimate total tokens (prompt + max output) for the TPM budget.
            est_tokens = (
                self._estimator.estimate_prompt_tokens(request.messages, request.tools)
                + request.max_output_tokens
            )
            while not self._limiter.allowed(est_tokens):
                time.sleep(0.05)
            self._limiter.spend(est_tokens)

            # ── 4. Make the actual LLM call ───────────────────────────────────
            # Use id(request) as the step_id for deterministic jitter — it
            # gives a unique-per-call value without requiring callers to pass
            # an explicit ID.
            req_id = id(request)
            try:
                resp = self._inner.step(request)
            except (TransientAPIError, RateLimitError) as exc:
                retry_after = getattr(exc, "retry_after_s", None)
                self._backoff.note_failure(req_id, retry_after_s=retry_after)
                raise
            except (AuthError, PermanentAPIError):
                # Caller bugs — no backoff. Re-raise immediately without touching state.
                raise

            # ── 5. Record success and reconcile actual token usage ─────────────
            self._backoff.note_success()
            if resp.usage:
                self._limiter.reconcile_actual(resp.usage.total_tokens, est_tokens)

            return resp
        finally:
            self._gate.release()
