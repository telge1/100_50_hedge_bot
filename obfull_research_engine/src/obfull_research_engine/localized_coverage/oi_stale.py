"""Localize OI stale intervals without discarding whole hours."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env

from ..interval_coverage import _q
from ..timeparse import format_utc_z
from . import OI_HARD_AGE_MS


def localize_oi_stale(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    hard_age_ms: int = OI_HARD_AGE_MS,
) -> dict[str, Any]:
    """Find wall-clock intervals where OI age would exceed hard_age for analysis seconds.

    Uses orderbook_analysis.open_interest_5s. For each 1s analysis second S in [start,end),
    OI is valid if there exists a bucket with bucket_time <= S and (S - bucket_time)*1000 <= hard_age_ms.

    Returns coalesced stale/missing intervals (seconds with no valid OI).
    """
    load_clickhouse_env()
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    # Load OI with lookback
    look = start - timedelta(milliseconds=hard_age_ms + 5000)
    hs = look.strftime("%Y-%m-%d %H:%M:%S")
    he = end.strftime("%Y-%m-%d %H:%M:%S")
    raw = _q(
        "SELECT toUnixTimestamp(bucket_time) bt, open_interest "
        "FROM orderbook_analysis.open_interest_5s "
        f"WHERE symbol='{symbol}' AND bucket_time>='{hs}' AND bucket_time<'{he}' "
        "ORDER BY bt FORMAT TSV"
    )
    buckets: list[tuple[int, float]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        buckets.append((int(float(parts[0])), float(parts[1])))

    start_u = int(start.timestamp())
    end_u = int(end.timestamp())
    hard_s = hard_age_ms / 1000.0

    stale_seconds: list[int] = []
    j = 0
    last_bt: int | None = None
    for s in range(start_u, end_u):
        while j < len(buckets) and buckets[j][0] <= s:
            last_bt = buckets[j][0]
            j += 1
        if last_bt is None or (s - last_bt) > hard_s:
            stale_seconds.append(s)

    # Coalesce
    intervals: list[dict[str, Any]] = []
    if stale_seconds:
        a = b = stale_seconds[0]
        for x in stale_seconds[1:]:
            if x == b + 1:
                b = x
            else:
                intervals.append(_iv(a, b + 1))
                a = b = x
        intervals.append(_iv(a, b + 1))

    return {
        "symbol": symbol,
        "start": format_utc_z(start),
        "end": format_utc_z(end),
        "oi_hard_age_ms": hard_age_ms,
        "n_oi_buckets_loaded": len(buckets),
        "n_stale_seconds": len(stale_seconds),
        "stale_intervals": intervals,
        "stage_policy": "OI optional context — flag OI_CONTEXT_UNAVAILABLE; do not block whole window",
    }


def _iv(a_u: int, b_u: int) -> dict[str, Any]:
    a = datetime.fromtimestamp(a_u, tz=timezone.utc)
    b = datetime.fromtimestamp(b_u, tz=timezone.utc)
    return {
        "start": format_utc_z(a),
        "end": format_utc_z(b),
        "duration_seconds": b_u - a_u,
        "reason": "OI_STALE",
    }
