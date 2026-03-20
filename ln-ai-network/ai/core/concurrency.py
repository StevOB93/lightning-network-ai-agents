from __future__ import annotations

# =============================================================================
# ConcurrencyGate — limits the number of simultaneous in-flight LLM calls
#
# Wraps a threading.Semaphore so callers can acquire/release slots without
# importing threading directly. max_in_flight=1 (the default) enforces
# a strict serial call pattern — consistent with the current single-threaded
# inbox loop.
#
# If the agent is ever parallelized (e.g. processing multiple inbox messages
# concurrently), increase LLM_MAX_IN_FLIGHT to match the provider's burst
# allowance. The rest of the rate-limiting infrastructure (DualRateLimiter,
# DeterministicBackoff) is already designed to handle concurrent callers.
#
# Usage:
#   gate = ConcurrencyGate(max_in_flight=1)
#   if gate.acquire(blocking=False):
#       try:
#           result = backend.step(request)
#       finally:
#           gate.release()
#   else:
#       # Already at capacity — skip or queue
# =============================================================================

import threading


class ConcurrencyGate:
    """
    Semaphore-based gate that limits concurrent in-flight LLM calls.

    acquire(blocking=False) returns True if a slot was taken, False if full.
    acquire(blocking=True)  blocks until a slot is available (use with care
    in async contexts — prefer non-blocking + back-pressure at the caller).
    """

    def __init__(self, max_in_flight: int) -> None:
        # Clamp to 1 minimum — a gate with 0 slots would block forever
        self._sem = threading.Semaphore(max(1, int(max_in_flight)))

    def acquire(self, blocking: bool = False) -> bool:
        """
        Try to acquire a concurrency slot.

        blocking=False (default): returns immediately. True = slot acquired,
          False = all slots are taken.
        blocking=True: waits until a slot is freed. Always returns True.
        """
        return self._sem.acquire(blocking=blocking)

    def release(self) -> None:
        """Release a previously acquired slot."""
        self._sem.release()
