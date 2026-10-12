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


def chart_live_micro_lld_v2(
    symbol: str,
    from_close: datetime | str,
    to_close: datetime | str,
    *,
    timeframe: str = "15m",
    pool_detail: int = 5,
    pool_types: list[str] | None = None,
    strength_classes: list[str] | None = None,
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
        "zones": zones,
    }
