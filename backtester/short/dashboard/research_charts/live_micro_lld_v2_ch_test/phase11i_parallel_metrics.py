"""Phase 11I helpers — parallelism overlap (unit-testable, no I/O)."""

from __future__ import annotations


def max_simultaneous_workers(intervals: list[tuple[float, float]]) -> int:
    """Given (start, end) wall timestamps, return peak concurrent count."""
    if not intervals:
        return 0
    events: list[tuple[float, int]] = []
    for a, b in intervals:
        if b < a:
            a, b = b, a
        events.append((a, 1))
        events.append((b, -1))
    events.sort(key=lambda x: (x[0], -x[1]))
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def parallel_wall_seconds(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    start = min(a for a, _ in intervals)
    end = max(b for _, b in intervals)
    return max(0.0, end - start)
