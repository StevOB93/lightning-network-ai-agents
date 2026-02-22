from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class BackoffState:
    attempt: int = 0
    blocked_until: float = 0.0
    circuit_open_until: float = 0.0
    consecutive_failures: int = 0


class DeterministicBackoff:
    """
    Deterministic exponential backoff with a deterministic "jitter" derived from step_id.
    Supports Retry-After override (when provided by the backend).
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
        now = time.monotonic()
        return now < max(self.state.blocked_until, self.state.circuit_open_until)

    def note_success(self) -> None:
        self.state.attempt = 0
        self.state.consecutive_failures = 0
        self.state.blocked_until = 0.0
        # do not forcibly close circuit early; leave it time-based
        # (but if it’s already expired, blocked() will return False)

    def note_failure(self, step_id: int, retry_after_s: Optional[float] = None) -> None:
        now = time.monotonic()
        self.state.attempt += 1
        self.state.consecutive_failures += 1

        exp = min(self._max_ms, self._base_ms * (2 ** (self.state.attempt - 1)))
        jitter = (hash(step_id) % (self._jitter_ms + 1)) if self._jitter_ms > 0 else 0
        delay_s = (exp + jitter) / 1000.0

        if retry_after_s is not None:
            delay_s = max(delay_s, float(retry_after_s))

        self.state.blocked_until = max(self.state.blocked_until, now + delay_s)

        # Circuit breaker
        if self.state.consecutive_failures >= self._cb_after:
            self.state.circuit_open_until = max(self.state.circuit_open_until, now + self._cb_open_s)
            # reset attempt so we don't grow without bound
            self.state.attempt = 0
            self.state.consecutive_failures = 0