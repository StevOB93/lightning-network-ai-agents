from __future__ import annotations

import threading


class ConcurrencyGate:
    """
    Simple deterministic concurrency gate.
    (Even if you run a single-thread loop today, this enforces future scaling constraints.)
    """

    def __init__(self, max_in_flight: int) -> None:
        self._sem = threading.Semaphore(max(1, int(max_in_flight)))

    def acquire(self, blocking: bool = False) -> bool:
        return self._sem.acquire(blocking=blocking)

    def release(self) -> None:
        self._sem.release()