"""Composite-input wrapper. The Phase 11U-B adapter module is not modified."""

from __future__ import annotations

from datetime import datetime, timezone

from ..phase11u_b.adapter import (
    READY,
    AdapterStop,
    candle_window_status,
    check_membership,
    project_active_pools,
    resolve_publication,
)
from .multi_file import CompositeCandleCache, FilePart


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_composite_remaining(
    source,
    parts: list[FilePart],
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    decision_close: datetime,
    cache: CompositeCandleCache | None = None,
) -> dict:
    """Resolve one published snapshot, then project from the composite candle input."""
    if pool_detail < 1 or pool_detail > 10:
        raise AdapterStop("INVALID_REQUEST", "pool_detail must be 1..10")
    dc = decision_close.astimezone(timezone.utc)
    hits = source.find(symbol=symbol, timeframe=timeframe, pool_detail=int(pool_detail), decision_close=dc)
    snapshot_id, fingerprint, lines = resolve_publication(hits)
    members = source.members(snapshot_id)
    catalog = source.catalog(snapshot_id)
    check_membership(snapshot_id, fingerprint, members, catalog)
    holder = cache or CompositeCandleCache()
    loaded = holder.get(parts, timeframe)
    if loaded.clickhouse_candle_reads != 0:
        raise AdapterStop("INVALID_REQUEST", "clickhouse candle fallback")
    status = candle_window_status(
        range_start=loaded.range_start,
        range_end_exclusive=loaded.range_end_exclusive,
        decision_close=dc,
        known_ats=[row.known_at for row in catalog],
        timeframe=timeframe,
    )
    if status != READY:
        raise AdapterStop(status, "composite candle window does not cover this close")
    projected = project_active_pools(catalog, loaded.rows, decision_close=dc, timeframe=timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "pool_detail": int(pool_detail),
        "decision_close": _iso(dc),
        "published_snapshot_id": snapshot_id,
        "pipeline_line_ids": list(lines),
        "composite_input_id": loaded.composite_id,
        "immutable_parts": [dict(part) for part in loaded.parts],
        "coverage": READY,
        "active_count": len(projected["active"]),
        "remaining_segment_count": len(projected["remaining"]),
        "fully_traversed_active_ids": projected["fully_traversed_active_ids"],
        "active": projected["active"],
        "remaining": projected["remaining"],
    }
