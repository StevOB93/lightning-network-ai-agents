from __future__ import annotations

# =============================================================================
# DeterministicBackoff — exponential backoff with deterministic jitter and
# an integrated circuit breaker.
#
# Design goals:
#   1. Reproducible behavior: jitter is hash(step_id) % jitter_ms rather than
#      random.random(), so the same step always gets the same jitter value.
#      This makes retry behavior predictable in tests and logs.
#
#   2. Retry-After support: if the provider returns a Retry-After header value,
#      note_failure() accepts it as retry_after_s and uses max(backoff, retry_after)
#      so we never come back before the provider says it's ready.
#
#   3. Circuit breaker: after N consecutive failures, the circuit "opens" for
#      a fixed duration. This prevents futile rapid retries when the provider
#      is genuinely down. The attempt counter resets when the breaker opens,
#      so subsequent failures restart the exponential sequence.
#
# Usage:
#   backoff = DeterministicBackoff(base_ms=1000, max_ms=30000, jitter_ms=250,
#                                   circuit_breaker_after=6, circuit_breaker_open_ms=60000)
#   while True:
#       if backoff.blocked():
#           continue   # or sleep until unblocked
#       try:
#           result = make_llm_call(step_id)
#           backoff.note_success()
#           break
#       except TransientAPIError as e:
#           backoff.note_failure(step_id, retry_after_s=e.retry_after)
# =============================================================================

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class BackoffState:
    """
    Mutable state tracked across retry attempts.

    Fields:
      attempt             — number of failures since last success (or breaker open)
      blocked_until       — monotonic time before which new calls are blocked
      circuit_open_until  — monotonic time while circuit is open (blocks all calls)
      consecutive_failures — total failures without any success; drives breaker
    """
    attempt: int = 0
    blocked_until: float = 0.0
    circuit_open_until: float = 0.0
    consecutive_failures: int = 0


class DeterministicBackoff:
    """
    Exponential backoff with deterministic jitter and an integrated circuit breaker.

    Parameters:
      base_ms                — base delay for the first retry (milliseconds)
      max_ms                 — delay cap; backoff never exceeds this
      jitter_ms              — max additional ms added deterministically via hash(step_id)
      circuit_breaker_after  — consecutive failures that trigger the breaker
      circuit_breaker_open_ms — how long the circuit stays open (milliseconds)
    """

    def __init__(
        self,
        base_ms: int,
        max_ms: int,
        jitter_ms: int,
        circuit_breaker_after: int,
        circuit_breaker_open_ms: int,
    ) -> None:
        self._base_ms = max(1, int(base_ms))
        self._max_ms = max(1, int(max_ms))
        self._jitter_ms = max(0, int(jitter_ms))
        self._cb_after = max(1, int(circuit_breaker_after))
        self._cb_open_s = max(1, int(circuit_breaker_open_ms)) / 1000.0
        self.state = BackoffState()

    def blocked(self) -> bool:
        """
        Return True if a new call should NOT be made right now.

        Blocked when either:
          - The per-failure delay has not yet elapsed (blocked_until), or
          - The circuit breaker is open (circuit_open_until).
        Both are measured against monotonic time.
        """
        now = time.monotonic()
        return now < max(self.state.blocked_until, self.state.circuit_open_until)

    def note_success(self) -> None:
        """
        Record a successful call. Resets attempt counter and failure streak.

        Does NOT forcibly close an open circuit — if the circuit opened, it
        stays open until circuit_open_until expires by wall time. This prevents
        a single lucky success from immediately re-enabling a degraded provider.
        """
        self.state.attempt = 0
        self.state.consecutive_failures = 0
        self.state.blocked_until = 0.0

    def note_failure(self, step_id: int, retry_after_s: Optional[float] = None) -> None:
        """
        Record a failed call and compute the next blocked_until time.

        Backoff formula:
          delay = min(max_ms, base_ms * 2^(attempt-1)) + hash_jitter(step_id)

        If retry_after_s is provided (e.g. from a 429 Retry-After header),
        the actual delay is max(backoff_delay, retry_after_s).

        Circuit breaker: when consecutive_failures reaches cb_after, sets
        circuit_open_until and resets the attempt counter so subsequent
        failures start a fresh exponential sequence from the beginning.
        """
        now = time.monotonic()
        self.state.attempt += 1
        self.state.consecutive_failures += 1

        # Exponential delay, capped at max_ms
        exp = min(self._max_ms, self._base_ms * (2 ** (self.state.attempt - 1)))

        # Deterministic jitter: same step_id always produces the same offset.
        # hash() is Python's built-in hash — consistent within a process.
        jitter = (hash(step_id) % (self._jitter_ms + 1)) if self._jitter_ms > 0 else 0
        delay_s = (exp + jitter) / 1000.0

        # Provider-specified retry hint takes precedence when longer than backoff
        if retry_after_s is not None:
            delay_s = max(delay_s, float(retry_after_s))

        # Use max() to never shorten an existing blocked_until from a prior failure
        self.state.blocked_until = max(self.state.blocked_until, now + delay_s)

        # Open circuit breaker after N consecutive failures
        if self.state.consecutive_failures >= self._cb_after:
            self.state.circuit_open_until = max(
                self.state.circuit_open_until, now + self._cb_open_s
            )
            # Reset attempt so the next sequence starts from base_ms, not max_ms
            self.state.attempt = 0
            self.state.consecutive_failures = 0
