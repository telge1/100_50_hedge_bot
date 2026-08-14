"""Per-symbol coverage records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .candles import FiveMinuteSeries, ensure_utc
from .config import UNIVERSE_HISTORY_START


def coverage_row(
    symbol: str,
    series: FiveMinuteSeries,
    *,
    entry_count: int,
    query_start: datetime | None = None,
    query_end: datetime | None = None,
    database_history_start: datetime | None = None,
    database_history_end: datetime | None = None,
) -> dict[str, Any]:
    universe = ensure_utc(UNIVERSE_HISTORY_START)
    db_start = database_history_start or series.history_start
    db_end = database_history_end or series.history_end
    listing_limited = bool(db_start and ensure_utc(db_start) > universe)
    if series.one_minute_rows <= 0:
        status = "NO_CANDLES"
    elif series.duplicate_one_minute_rows:
        status = "DUPLICATES"
    elif series.missing_one_minute_rows:
        status = "GAPS"
    elif listing_limited:
        status = "LISTING_LIMITED"
    else:
        status = "OK"

    def _iso(ts):
        if ts is None:
            return None
        return ensure_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "symbol": symbol.upper(),
        "query_start": _iso(query_start),
        "query_end": _iso(query_end),
        "database_history_start": _iso(db_start),
        "database_history_end": _iso(db_end),
        "history_start": _iso(series.history_start),
        "history_end": _iso(series.history_end),
        "entry_count": int(entry_count),
        "one_minute_rows": int(series.one_minute_rows),
        "missing_one_minute_rows": int(series.missing_one_minute_rows),
        "duplicate_one_minute_rows": int(series.duplicate_one_minute_rows),
        "complete_five_minute_buckets": int(len(series.bars)),
        "dropped_incomplete_five_minute_buckets": int(series.dropped_incomplete_five_minute_buckets),
        "listing_limited": listing_limited,
        "coverage_status": status,
    }
