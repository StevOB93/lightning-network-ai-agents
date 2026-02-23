from __future__ import annotations

import time


class DeterministicScheduler:
    """
    Drift-free scheduler: tick k occurs at t0 + k * cadence.
    Uses monotonic time for determinism and stability.
    """

    def __init__(self, tick_ms: int) -> None:
        if tick_ms <= 0:
            raise ValueError("tick_ms must be > 0")
        self._cadence_s = tick_ms / 1000.0
        self._t0 = time.monotonic()
        self._k = 0

    def next_tick_time(self) -> float:
        return self._t0 + (self._k * self._cadence_s)

    def wait_next_tick(self) -> None:
        target = self.next_tick_time()
        now = time.monotonic()
        sleep_s = max(0.0, target - now)
        if sleep_s > 0:
            time.sleep(sleep_s)
        self._k += 1