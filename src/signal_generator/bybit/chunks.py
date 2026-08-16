"""Time-chunk helpers for resumable history backfills."""

from __future__ import annotations

from datetime import datetime, timedelta

from signal_generator.bybit.history import ensure_utc


def iter_time_chunks(
    start: datetime,
    end: datetime,
    *,
    chunk_days: int = 7,
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into contiguous half-open chunks of ``chunk_days``.

    The final chunk may be shorter. Empty range yields [].
    """
    start = ensure_utc(start)
    end = ensure_utc(end)
    if end <= start:
        return []
    if chunk_days <= 0:
        raise ValueError("chunk_days must be > 0")
    step = timedelta(days=chunk_days)
    chunks: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks
