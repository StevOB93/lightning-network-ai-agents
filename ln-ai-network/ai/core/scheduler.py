from __future__ import annotations

# =============================================================================
# DeterministicScheduler — drift-free fixed-interval tick scheduling
#
# Problem with naive `time.sleep(cadence)`:
#   Each sleep starts from "now" — any time spent executing between ticks is
#   not accounted for. Over many ticks, this causes cumulative drift:
#     tick 0: t=0, tick 1: t=cadence + work_time, tick N: t=N*(cadence+work_time)
#   With a 500ms cadence and 10ms of per-tick work, after 1000 ticks the loop
#   is running ~10s behind schedule.
#
# This scheduler fixes drift by targeting absolute times:
#   tick k is always scheduled for t0 + k * cadence_s
#
#   If a tick runs late (because the previous iteration took longer than cadence),
#   the next sleep is shortened to compensate. If an iteration runs very late
#   (longer than one full cadence), sleep_s is clamped to 0 so the next tick
#   fires immediately — the scheduler catches up without sleeping.
#
# Usage:
#   sched = DeterministicScheduler(tick_ms=500)
#   while True:
#       sched.wait_next_tick()   # blocks until the next tick is due
#       process_inbox_message()
#
# Thread safety: this class is not thread-safe. Intended for use in a single
# background loop thread.
# =============================================================================

import time


class DeterministicScheduler:
    """
    Fixed-interval scheduler that eliminates cumulative drift.

    Tick k fires at t0 + k * cadence_s (monotonic time), where t0 is the
    moment this scheduler was constructed. If an iteration overruns its
    cadence window, the scheduler immediately proceeds to the next tick
    rather than sleeping for a full cadence.
    """

    def __init__(self, tick_ms: int) -> None:
        if tick_ms <= 0:
            raise ValueError("tick_ms must be > 0")
        self._cadence_s = tick_ms / 1000.0
        self._t0 = time.monotonic()   # reference epoch for all tick calculations
        self._k = 0                   # next tick index (incremented after each wait)

    def next_tick_time(self) -> float:
        """Return the monotonic timestamp when the next tick is due."""
        return self._t0 + (self._k * self._cadence_s)

    def wait_next_tick(self) -> None:
        """
        Block until the next tick is due, then advance the tick counter.

        If the current time is already past the next tick time (overrun),
        sleep_s is clamped to 0 so execution continues immediately.
        The tick counter is always incremented regardless of overrun so that
        subsequent ticks target the correct absolute times.
        """
        target = self.next_tick_time()
        now = time.monotonic()
        sleep_s = max(0.0, target - now)
        if sleep_s > 0:
            time.sleep(sleep_s)
        self._k += 1
