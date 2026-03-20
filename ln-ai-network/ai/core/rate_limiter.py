from __future__ import annotations

# =============================================================================
# DualRateLimiter — enforces RPM + TPM + minimum interval simultaneously
#
# Uses the token bucket algorithm for both RPM and TPM:
#   - Each bucket has a capacity equal to the full-minute allowance.
#   - Tokens refill at rate = capacity / 60 per second (continuous).
#   - A request "spends" 1 token from the request bucket and N tokens from
#     the token bucket, where N = estimated_total_tokens.
#
# The minimum interval adds a hard floor between calls — even if both buckets
# have tokens, calls cannot come faster than min_interval_ms apart. This
# prevents back-to-back rapid-fire calls that could briefly exceed the
# provider's burst tolerance.
#
# Usage pattern (caller's responsibility to poll):
#
#   limiter = DualRateLimiter(rpm=30, tpm=60_000, min_interval_ms=1000)
#   est = estimator.estimate_prompt_tokens(messages, tools) + max_output_tokens
#   if limiter.allowed(est):
#       limiter.spend(est)
#       response = backend.step(request)
#       limiter.reconcile_actual(response.usage.total_tokens, est)
#
# reconcile_actual() corrects for underestimates: if the actual response used
# more tokens than the estimate, it drains the extra from the TPM bucket.
# Overestimates are not refunded (conservative by design).
# =============================================================================

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    """
    Single token bucket: continuous refill up to capacity, spend on demand.

    refill() is called before every can_spend/spend check to add tokens
    proportional to elapsed time since the last refill.
    """
    capacity: float        # maximum tokens (= full-minute allowance)
    refill_per_s: float    # tokens added per second (= capacity / 60)
    tokens: float          # current available tokens
    last_t: float          # monotonic timestamp of last refill

    def refill(self, now: float) -> None:
        """Add tokens proportional to elapsed time; clamp to capacity."""
        dt = max(0.0, now - self.last_t)
        self.tokens = min(self.capacity, self.tokens + dt * self.refill_per_s)
        self.last_t = now

    def can_spend(self, amount: float, now: float) -> bool:
        """True if the bucket has enough tokens after refilling."""
        self.refill(now)
        return self.tokens >= amount

    def spend(self, amount: float, now: float) -> None:
        """Deduct tokens after refilling. Raises on underflow (logic error)."""
        self.refill(now)
        if self.tokens < amount:
            raise RuntimeError("Bucket underflow (logic error)")
        self.tokens -= amount


class DualRateLimiter:
    """
    Enforces three constraints simultaneously:
      1. Requests per minute (RPM) — token bucket, capacity = rpm
      2. Tokens per minute (TPM)  — token bucket, capacity = tpm
      3. Minimum interval          — hard floor between consecutive calls

    Provider-agnostic: works with OpenAI, Gemini, Ollama, or any provider
    that has RPM/TPM limits.

    All three constraints must pass for allowed() to return True.
    spend() must be called immediately after allowed() returns True and before
    the actual LLM call. Do not call spend() if allowed() returned False.
    """

    def __init__(self, rpm: int, tpm: int, min_interval_ms: int) -> None:
        now = time.monotonic()

        # Clamp inputs to sensible minimums (avoids division by zero)
        rpm = max(1, int(rpm))
        tpm = max(1, int(tpm))
        min_interval_ms = max(0, int(min_interval_ms))

        # Both buckets start full — a fresh agent can use its full allocation immediately
        self._req = _Bucket(
            capacity=float(rpm),
            refill_per_s=float(rpm) / 60.0,
            tokens=float(rpm),
            last_t=now,
        )
        self._tok = _Bucket(
            capacity=float(tpm),
            refill_per_s=float(tpm) / 60.0,
            tokens=float(tpm),
            last_t=now,
        )
        self._min_interval_s = min_interval_ms / 1000.0
        self._next_allowed_time = now  # monotonic time before which calls are forbidden

    def allowed(self, estimated_total_tokens: int) -> bool:
        """
        Return True if all three constraints are satisfied right now.

        Does NOT consume any tokens — call spend() if proceeding with the call.
        estimated_total_tokens should include both prompt and expected output tokens.
        """
        now = time.monotonic()
        # Minimum interval check
        if now < self._next_allowed_time:
            return False
        # RPM check (costs exactly 1 request token)
        if not self._req.can_spend(1.0, now):
            return False
        # TPM check (costs estimated_total_tokens)
        if not self._tok.can_spend(float(max(1, estimated_total_tokens)), now):
            return False
        return True

    def spend(self, estimated_total_tokens: int) -> None:
        """
        Consume one request + estimated_total_tokens from the buckets.

        Also advances next_allowed_time by min_interval_s from now.
        Call this immediately before the LLM API call.
        """
        now = time.monotonic()
        self._req.spend(1.0, now)
        self._tok.spend(float(max(1, estimated_total_tokens)), now)
        self._next_allowed_time = max(self._next_allowed_time, now + self._min_interval_s)

    def reconcile_actual(self, actual_total_tokens: int, estimated_total_tokens: int) -> None:
        """
        Correct the TPM bucket when actual usage exceeded the estimate.

        If actual > estimated, the extra tokens are drained from the bucket now
        (reducing how quickly the bucket refills to usable levels).
        If actual <= estimated, nothing happens — the estimate was conservative,
        which is fine. We do not refund over-estimates.

        Call this after receiving the LLM response with real usage data.
        """
        diff = int(actual_total_tokens) - int(estimated_total_tokens)
        if diff <= 0:
            return
        now = time.monotonic()
        # Best-effort: spend extra only if tokens are available.
        # If the bucket is already near-empty, the natural refill slowdown
        # will handle backpressure on the next call anyway.
        if self._tok.can_spend(float(diff), now):
            self._tok.spend(float(diff), now)
