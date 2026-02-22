from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    capacity: float
    refill_per_s: float
    tokens: float
    last_t: float

    def refill(self, now: float) -> None:
        dt = max(0.0, now - self.last_t)
        self.tokens = min(self.capacity, self.tokens + dt * self.refill_per_s)
        self.last_t = now

    def can_spend(self, amount: float, now: float) -> bool:
        self.refill(now)
        return self.tokens >= amount

    def spend(self, amount: float, now: float) -> None:
        self.refill(now)
        if self.tokens < amount:
            raise RuntimeError("Bucket underflow (logic error)")
        self.tokens -= amount


class DualRateLimiter:
    """
    Enforces:
    - Requests per minute (RPM)
    - Tokens per minute (TPM)
    - Minimum interval between requests (ms)

    Provider-agnostic.
    """

    def __init__(self, rpm: int, tpm: int, min_interval_ms: int) -> None:
        now = time.monotonic()

        rpm = max(1, int(rpm))
        tpm = max(1, int(tpm))
        min_interval_ms = max(0, int(min_interval_ms))

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
        self._next_allowed_time = now

    def allowed(self, estimated_total_tokens: int) -> bool:
        now = time.monotonic()
        if now < self._next_allowed_time:
            return False
        if not self._req.can_spend(1.0, now):
            return False
        if not self._tok.can_spend(float(max(1, estimated_total_tokens)), now):
            return False
        return True

    def spend(self, estimated_total_tokens: int) -> None:
        now = time.monotonic()
        self._req.spend(1.0, now)
        self._tok.spend(float(max(1, estimated_total_tokens)), now)
        self._next_allowed_time = max(self._next_allowed_time, now + self._min_interval_s)

    def reconcile_actual(self, actual_total_tokens: int, estimated_total_tokens: int) -> None:
        """
        Optional: if actual tokens > estimate, you can "spend the difference" to reduce future burstiness.
        If actual <= estimate, do nothing (you already reserved enough).
        """
        diff = int(actual_total_tokens) - int(estimated_total_tokens)
        if diff <= 0:
            return
        now = time.monotonic()
        # Spend extra if available; if not, bucket will refill naturally and future calls will be blocked.
        if self._tok.can_spend(float(diff), now):
            self._tok.spend(float(diff), now)