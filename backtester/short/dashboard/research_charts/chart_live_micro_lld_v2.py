"""Research adapter for Live Micro LLD V2 (pool_ralf.pine) — not connected to scanner."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .service import _candles_from_packed, _timeframe_seconds, load_candles
from .short_pool_oracle_v1 import parse_decision_iso
from .trp_import import ensure_trp_on_path

LIVE_MICRO_LLD_V2_LAYER = "live_micro_lld_v2"
LIVE_MICRO_LLD_V2_NAMESPACE = "LIVE_MICRO_LLD_V2"
SUPPORTED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "1h", "4h"})


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_close(value: datetime | str) -> datetime:
    if isinstance(value, str):
        return _ensure_utc(parse_decision_iso(value))
    return _ensure_utc(value)


def _pool_overlaps_window(
    pool: dict[str, Any],
    from_unix: int,
    to_unix: int,
) -> bool:
    known = pool.get("known_at")
    if not known:
        return False
    known_unix = int(_ensure_utc(parse_decision_iso(known)).timestamp())
    break_at = pool.get("break_at")
    end_unix = to_unix
    if break_at:
        end_unix = min(end_unix, int(_ensure_utc(parse_decision_iso(break_at)).timestamp()))
    start_unix = max(from_unix, known_unix)
    return start_unix <= to_unix and known_unix <= to_unix and end_unix >= from_unix


def pools_to_zones(
    pools: list[dict[str, Any]],
    *,
    from_unix: int,
    to_unix: int,
    pool_types: set[str] | None = None,
    strength_classes: set[str] | None = None,
) -> list[dict[str, Any]]:
    from indicators.live_micro_lld_v2.styles import BORDER_LOWER, BORDER_UPPER, FILL_BY_CLASS

    zones: list[dict[str, Any]] = []
    for p in pools:
        if pool_types and p.get("pool_type") not in pool_types:
            continue
        sc = str(p.get("strength_class") or "")
        if strength_classes and sc not in strength_classes:
            continue
        if not _pool_overlaps_window(p, from_unix, to_unix):
            continue
        known_unix = int(_ensure_utc(parse_decision_iso(p["known_at"])).timestamp())
        break_at = p.get("break_at")
        end_unix = to_unix
        if break_at:
            end_unix = min(end_unix, int(_ensure_utc(parse_decision_iso(break_at)).timestamp()))
        start = max(from_unix, known_unix)
        if end_unix < start:
            continue
        side = str(p.get("side") or "")
        border = BORDER_UPPER if side == "upper" else BORDER_LOWER
        fill = FILL_BY_CLASS.get(sc, "#88888833")
        pid = str(p["pool_id"])
        zones.append(
            {
                "id": f"live-micro-v2:{pid}",
                "pool_id": pid,
                "namespace": LIVE_MICRO_LLD_V2_NAMESPACE,
                "type": "zone",
                "side": side,
                "pool_type": p.get("pool_type"),
                "strength_class": sc,
                "lower_price": float(p["bottom_price"]),
                "upper_price": float(p["top_price"]),
                "bottom_price": float(p["bottom_price"]),
                "top_price": float(p["top_price"]),
                "start_timestamp": int(start),
                "end_timestamp": int(end_unix),
                "style": {"color": border, "fillColor": fill, "lineWidth": 2},
                "metadata": {
                    "source": "live_micro_lld_v2",
                    "pool_type": p.get("pool_type"),
                    "strength": p.get("strength"),
                    "strength_class": sc,
                    "early_timing": p.get("early_timing"),
                },
            }
        )
    return zones


def _remaining_zones(
    zones: list[dict[str, Any]],
    pools: list[dict[str, Any]],
    candles: list[Any],
    *,
    timeframe: str,
    to_unix: int,
) -> list[dict[str, Any]]:
    """Unconsumed rectangles for the zones already selected. Engine output is unchanged."""
    from indicators.live_micro_lld_v2.availability import bar_close

    from .live_micro_lld_v2_ch_test.phase11o_candle_ranges import CandleRangeIndex
    from .live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
        remaining_from_penetration,
    )

    rows: list[tuple[int, float, float]] = []
    for candle in candles:
        close_unix = int(bar_close(_ensure_utc(candle.timestamp), timeframe).timestamp())
        rows.append((close_unix, float(candle.high), float(candle.low)))
    rows.sort()
    deduped: list[tuple[int, float, float]] = []
    for row in rows:
        if deduped and deduped[-1][0] == row[0]:
            deduped[-1] = row
        else:
            deduped.append(row)
    if not deduped:
        return []
    index = CandleRangeIndex(
        [row[0] for row in deduped],
        [row[1] for row in deduped],
        [row[2] for row in deduped],
    )
    by_id = {str(pool.get("pool_id")): pool for pool in pools}
    remaining: list[dict[str, Any]] = []
    for zone in zones:
        pool = by_id.get(str(zone.get("pool_id")))
        known = pool.get("known_at") if pool else None
        if not known:
            continue
        known_unix = int(_ensure_utc(parse_decision_iso(str(known))).timestamp())
        span = index.eligible_index_span(known_unix, int(to_unix))
        if span is None:
            pen_high = None
            pen_low = None
        else:
            pen_high, pen_low = index.query(span[0], span[1])
        try:
            rem_top, rem_bottom, _frac, fully, _ph, _pl = remaining_from_penetration(
                str(zone.get("side") or ""),
                float(zone["bottom_price"]),
                float(zone["top_price"]),
                pen_high,
                pen_low,
            )
        except Exception:
            continue
        if fully or rem_top - rem_bottom <= 1e-12:
            continue
        copy = dict(zone)
        copy["id"] = f"live-micro-v2-rem:{zone['pool_id']}"
        copy["lower_price"] = rem_bottom
        copy["upper_price"] = rem_top
        copy["bottom_price"] = rem_bottom
        copy["top_price"] = rem_top
        copy["metadata"] = dict(zone.get("metadata") or {})
        copy["metadata"]["layer"] = "remaining"
        remaining.append(copy)
    return remaining


def chart_live_micro_lld_v2(
    symbol: str,
    from_close: datetime | str,
    to_close: datetime | str,
    *,
    timeframe: str = "15m",
    pool_detail: int = 5,
    pool_types: list[str] | None = None,
    strength_classes: list[str] | None = None,
    include_remaining: bool = False,
) -> dict[str, Any]:
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.as_of import (
        filter_candles_closed_at_or_before,
        pools_as_of,
    )
    from indicators.live_micro_lld_v2.config import LiveMicroLldV2Config
    from indicators.live_micro_lld_v2.engine import run_live_micro_lld_v2

    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "15m").strip()
    if tf not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {tf}")

    from_dt = _parse_close(from_close)
    to_dt = _parse_close(to_close)
    if from_dt > to_dt:
        raise ValueError("from_close must be <= to_close")

    from_unix = int(from_dt.timestamp())
    to_unix = int(to_dt.timestamp())

    cfg = LiveMicroLldV2Config(pool_detail=int(pool_detail))
    warmup_bars = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
    tf_sec = _timeframe_seconds(tf)
    load_start = max(0, from_unix - warmup_bars * tf_sec)
    packed = load_candles(sym, tf, start=load_start, end=to_unix, limit=50000)
    candles = _candles_from_packed(packed)
    if not candles:
        raise ValueError("no candles in window")

    candles = filter_candles_closed_at_or_before(candles, to_dt, tf)
    if not candles:
        raise ValueError("no closed candles at or before to_close")

    result = run_live_micro_lld_v2(candles, cfg)
    raw_pools = [p.to_dict() for p in result.pools]
    pool_dicts = pools_as_of(raw_pools, to_dt)
    pt_set = set(pool_types) if pool_types else None
    sc_set = set(strength_classes) if strength_classes else None
    zones = pools_to_zones(
        pool_dicts,
        from_unix=from_unix,
        to_unix=to_unix,
        pool_types=pt_set,
        strength_classes=sc_set,
    )
    remaining_zones: list[dict[str, Any]] = []
    if include_remaining and zones:
        remaining_zones = _remaining_zones(
            zones,
            pool_dicts,
            candles,
            timeframe=tf,
            to_unix=to_unix,
        )
        zones = zones + remaining_zones

    return {
        "layer": LIVE_MICRO_LLD_V2_LAYER,
        "namespace": LIVE_MICRO_LLD_V2_NAMESPACE,
        "symbol": sym,
        "timeframe": tf,
        "pool_detail": int(pool_detail),
        "from_close": from_dt.isoformat(),
        "to_close": to_dt.isoformat(),
        "as_of_close": to_dt.isoformat(),
        "pool_count": len({z["pool_id"] for z in zones}),
        "segment_count": len(zones),
        "remaining_count": len(remaining_zones),
        "zones": zones,
    }
