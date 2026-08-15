"""O(n) HTF aggregation using the same inspect_bucket completeness rules."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Sequence


def aggregate_1m_to_timeframe(
    candles_1m: Sequence[Any],
    timeframe: str,
    *,
    as_of: datetime | None = None,
    require_complete: bool = True,
) -> list[Any]:
    from signal_generator.timeframes import (
        bucket_start,
        ensure_utc,
        inspect_bucket,
    )

    if not candles_1m:
        return []
    if not require_complete:
        from signal_generator.timeframes import aggregate_1m_to_timeframe as sg_aggregate

        return sg_aggregate(
            candles_1m, timeframe, as_of=as_of, require_complete=False
        )

    sorted_bars = sorted((ensure_utc(c.open_time), c) for c in candles_1m)
    if as_of is None:
        as_of = ensure_utc(sorted_bars[-1][1].close_time)
    else:
        as_of = ensure_utc(as_of)

    groups: dict[datetime, list] = defaultdict(list)
    for ot, bar in sorted_bars:
        groups[bucket_start(ot, timeframe)].append(bar)

    out: list[Any] = []
    for start in sorted(groups):
        insp = inspect_bucket(
            groups[start], timeframe=timeframe, bucket_open=start, as_of=as_of
        )
        if insp.bar is not None:
            out.append(insp.bar)
    return out


def audit_htf_buckets(
    candles_1m: Sequence[Any],
    timeframe: str,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    from signal_generator.timeframes import bucket_start, ensure_utc, inspect_bucket

    if not candles_1m:
        return {
            "timeframe": timeframe,
            "complete_count": 0,
            "incomplete_discarded": 0,
            "not_closed_discarded": 0,
            "first_complete_bucket": None,
            "last_complete_bucket": None,
            "complete_open_hours_utc": [],
            "unexpected_4h_hours": [],
            "gap_count": 0,
        }
    sorted_bars = sorted((ensure_utc(c.open_time), c) for c in candles_1m)
    as_of = ensure_utc(as_of or sorted_bars[-1][1].close_time)
    groups: dict[datetime, list] = defaultdict(list)
    for ot, bar in sorted_bars:
        groups[bucket_start(ot, timeframe)].append(bar)
    complete: list[datetime] = []
    incomplete = 0
    not_closed = 0
    for start in sorted(groups):
        insp = inspect_bucket(
            groups[start], timeframe=timeframe, bucket_open=start, as_of=as_of
        )
        if insp.bar is not None:
            complete.append(ensure_utc(insp.bar.open_time))
        elif insp.status.value == "NOT_CLOSED":
            not_closed += 1
        else:
            incomplete += 1
    hours = sorted({t.hour for t in complete})
    unexpected = []
    if timeframe == "4h":
        unexpected = [h for h in hours if h not in {0, 4, 8, 12, 16, 20}]
    gaps = 0
    if len(complete) >= 2:
        step = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}[timeframe]
        for a, b in zip(complete, complete[1:]):
            delta = int((b - a).total_seconds() // 60)
            if delta != step:
                gaps += 1
    return {
        "timeframe": timeframe,
        "complete_count": len(complete),
        "incomplete_discarded": incomplete,
        "not_closed_discarded": not_closed,
        "first_complete_bucket": complete[0].isoformat().replace("+00:00", "Z") if complete else None,
        "last_complete_bucket": complete[-1].isoformat().replace("+00:00", "Z") if complete else None,
        "complete_open_hours_utc": hours,
        "unexpected_4h_hours": unexpected,
        "gap_count": gaps,
    }
