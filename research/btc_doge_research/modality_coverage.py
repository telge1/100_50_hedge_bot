"""Modality-scoped coverage assessment for BTC/DOGE full-history backfill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .clickhouse import connect, rows
from .contracts import ALLOWED_SYMBOLS, sanitize_json, stable_hash
from .full_history_contracts import (
    EXPECTED_CANDLES,
    EXPECTED_OI,
    PILOT_DAY_STR,
    SEGMENT_MISSING,
    SEGMENT_ORDERING_AMBIGUOUS,
    SEGMENT_PARTIAL,
    SEGMENT_READY,
    ordering_ambiguous_for_day,
)
from .full_history_inventory import _ch_day_metrics
from .ob200_segments import build_ob200_segments_from_discovery, ob_inventory_gaps_for_day
from .source_discovery import build_source_discovery


def _day_range(start: datetime, end: datetime):
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        yield day
        day += timedelta(days=1)


def _ch_modality_segment(
    symbol: str,
    day: datetime,
    modality: str,
    ch: dict[str, Any],
) -> dict[str, Any]:
    day_end = day + timedelta(days=1)
    if modality == "PUBLIC_TRADES":
        count = ch["trade_count"]
        expected = None
        complete = count > 0
    elif modality == "OPEN_INTEREST":
        count = ch["oi_count"]
        expected = EXPECTED_OI
        complete = count == expected and ch["oi_unique"] == expected
    elif modality == "CANDLES":
        count = ch["candle_count"]
        expected = EXPECTED_CANDLES
        complete = count == expected
    elif modality == "LIQUIDATIONS":
        count = ch["liq_count"]
        expected = None
        complete = True
    else:
        raise ValueError(modality)
    if count == 0 and modality != "LIQUIDATIONS":
        status = SEGMENT_MISSING
    elif complete:
        status = SEGMENT_READY
    else:
        status = SEGMENT_PARTIAL
    ambiguous = ordering_ambiguous_for_day(symbol, day) if modality == "PUBLIC_TRADES" else []
    if ambiguous and status == SEGMENT_READY:
        status = SEGMENT_ORDERING_AMBIGUOUS
    return {
        "symbol": symbol,
        "modality": modality,
        "segment_start": day,
        "segment_end": day_end,
        "producer_id": "CLICKHOUSE_CANONICAL",
        "source": f"clickhouse_{modality.lower()}",
        "source_fingerprint": stable_hash({"symbol": symbol, "day": day.strftime("%Y-%m-%d"), "modality": modality}),
        "expected_rows": expected if expected is not None else count,
        "actual_rows": count,
        "status": status,
        "exclusion_reason": "" if status in (SEGMENT_READY, SEGMENT_PARTIAL, SEGMENT_ORDERING_AMBIGUOUS) else "NO_SOURCE_ROWS",
        "ordering_ambiguous_count": len(ambiguous),
    }


def _profile_segment(symbol: str, day: datetime, trade_status: str) -> dict[str, Any]:
    day_end = day + timedelta(days=1)
    if trade_status not in (SEGMENT_READY, SEGMENT_ORDERING_AMBIGUOUS):
        status = SEGMENT_MISSING
        reason = "TRADES_NOT_READY"
    else:
        status = SEGMENT_READY
        reason = ""
    row = {
        "symbol": symbol,
        "segment_start": day,
        "segment_end": day_end,
        "producer_id": "DERIVED_FROM_TRADES",
        "source": "derived_tpo_volume",
        "source_fingerprint": stable_hash({"symbol": symbol, "day": day.strftime("%Y-%m-%d"), "profiles": True}),
        "status": status,
        "exclusion_reason": reason,
    }
    tpo = {**row, "modality": "TPO_PROFILE", "expected_rows": 1, "actual_rows": 0}
    vol = {**row, "modality": "VOLUME_PROFILE", "expected_rows": 1, "actual_rows": 0}
    return tpo, vol


def build_modality_coverage(
    discovery: dict[str, Any] | None = None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    discovery = discovery or build_source_discovery()
    start = start or datetime(2026, 7, 19, tzinfo=timezone.utc)
    end = end or datetime(2026, 9, 1, tzinfo=timezone.utc)
    ob_by_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in discovery["ob200_files"]:
        if item.get("zero_duration"):
            continue
        ob_by_day.setdefault((item["symbol"], item["utc_day"]), []).append(item)
    ob_segments = build_ob200_segments_from_discovery(
        [f for f in discovery["ob200_files"] if not f.get("zero_duration")]
    )
    client = connect()
    segments: list[dict[str, Any]] = []
    try:
        for symbol in sorted(ALLOWED_SYMBOLS):
            for day in _day_range(start, end):
                day_str = day.strftime("%Y-%m-%d")
                if day_str == PILOT_DAY_STR:
                    continue
                ch = _ch_day_metrics(client, symbol, day)
                for modality in ("PUBLIC_TRADES", "LIQUIDATIONS", "OPEN_INTEREST", "CANDLES"):
                    seg = _ch_modality_segment(symbol, day, modality, ch)
                    segments.append(seg)
                trade_status = next(
                    s["status"] for s in segments
                    if s["symbol"] == symbol and s["segment_start"] == day and s["modality"] == "PUBLIC_TRADES"
                )
                segments.extend(_profile_segment(symbol, day, trade_status))
                day_files = ob_by_day.get((symbol, day_str), [])
                segments.extend(ob_inventory_gaps_for_day(symbol, day, day_files))
                for seg in ob_segments:
                    if seg["symbol"] != symbol:
                        continue
                    seg_start = seg["segment_start"]
                    if isinstance(seg_start, str):
                        seg_start = datetime.fromisoformat(seg_start.replace("Z", "+00:00"))
                    if seg_start.date() == day.date():
                        segments.append(seg)
    finally:
        client.close()
    out = []
    for s in segments:
        if isinstance(s.get("segment_start"), datetime):
            s = {**s, "segment_start": s["segment_start"].isoformat().replace("+00:00", "Z")}
        if isinstance(s.get("segment_end"), datetime):
            s = {**s, "segment_end": s["segment_end"].isoformat().replace("+00:00", "Z")}
        if isinstance(s.get("file_start"), datetime):
            s = {**s, "file_start": s["file_start"].isoformat().replace("+00:00", "Z")}
        if isinstance(s.get("file_end"), datetime):
            s = {**s, "file_end": s["file_end"].isoformat().replace("+00:00", "Z")}
        out.append(sanitize_json(s))
    return out


def coverage_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_modality: dict[str, dict[str, int]] = {}
    for seg in segments:
        by_status[seg["status"]] = by_status.get(seg["status"], 0) + 1
        mod = by_modality.setdefault(seg["modality"], {})
        mod[seg["status"]] = mod.get(seg["status"], 0) + 1
    eligible = [s for s in segments if s["status"] in (SEGMENT_READY, SEGMENT_PARTIAL, SEGMENT_ORDERING_AMBIGUOUS)]
    return {
        "total_segments": len(segments),
        "eligible_segments": len(eligible),
        "by_status": by_status,
        "by_modality": by_modality,
    }
