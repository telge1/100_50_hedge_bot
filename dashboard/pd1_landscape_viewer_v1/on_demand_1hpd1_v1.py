"""On-demand 1h PD1 landscape window (CH 1m → 1h engine, no frozen pool publications)."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.service import (
    _candles_from_packed,
    _timeframe_seconds,
    load_candles,
)
from research_charts.short_pool_oracle_v1 import parse_decision_iso
from research_charts.trp_import import ensure_trp_on_path

from .historical_window_v1 import (
    MAX_EXPORT_HOURS,
    SYMBOL,
    WARMUP_HOURS,
    LandscapeWindowError,
    _causal_decision_close,
    _clip_segments_to_window,
    _iter_5m,
    _iso,
    _load_style,
    _origin_logical_v2_key,
    _parse_utc,
    _resample_5m_from_candles,
)
from research_charts.clickhouse_source import ClickHouseResearchCandleSource

logger = logging.getLogger(__name__)

ON_DEMAND_PIPELINE_LINE_ID = "on_demand_ch1m_1hpd1_v1"
BUILDER_VERSION_V1 = "pd1_landscape_on_demand_1hpd1_v2"
LIFETIME_BUILDER_VERSION = "pd1_on_demand_1hpd1_lifetime_v2"
POOL_DETAIL = 1
POOL_TIMEFRAME = "1h"
SLOT_MINUTES = 60


def on_demand_1hpd1_cache_identity() -> dict[str, Any]:
    cfg = build_live_micro_lld_v2_config(POOL_DETAIL)
    return {
        "symbol": SYMBOL,
        "timeframe": POOL_TIMEFRAME,
        "pool_detail": POOL_DETAIL,
        "pipeline_line_id": ON_DEMAND_PIPELINE_LINE_ID,
        "config_hash": config_hash(cfg),
        "builder_version": BUILDER_VERSION_V1,
    }


def _ch_symbol_bounds(symbol: str = SYMBOL) -> tuple[datetime, datetime]:
    src = ClickHouseResearchCandleSource()
    meta = {m["symbol"]: m for m in src.list_symbol_meta()}
    sym = str(symbol).upper()
    if sym not in meta:
        raise LandscapeWindowError(
            "OUT_OF_SCOPE",
            "no ClickHouse 1m coverage for symbol",
            {"symbol": sym},
        )
    lo = parse_decision_iso(meta[sym]["first_time_iso"])
    hi = parse_decision_iso(meta[sym]["last_time_iso"])
    return lo, hi


def on_demand_1hpd1_capabilities_scope() -> dict[str, Any]:
    ident = on_demand_1hpd1_cache_identity()
    lo, hi = _ch_symbol_bounds()
    return {
        "pool_detail": POOL_DETAIL,
        "symbol": SYMBOL,
        "timeframe": POOL_TIMEFRAME,
        "pipeline_line_id": ident["pipeline_line_id"],
        "config_hash": ident["config_hash"],
        "data_available_from": _iso(lo),
        "data_available_to": _iso(hi),
        "membership_slot_minutes": SLOT_MINUTES,
        "source": "on_demand_ch1m",
        "builder_version": BUILDER_VERSION_V1,
    }


def _pool_row_engine(m: dict[str, Any]) -> dict[str, Any]:
    lower = m.get("lower_bound")
    if lower is None:
        lower = m.get("bottom_price")
    upper = m.get("upper_bound")
    if upper is None:
        upper = m.get("top_price")
    return {
        "pool_id": m["pool_id"],
        "side": m.get("side"),
        "bottom": float(lower),
        "top": float(upper),
        "strength": m.get("strength"),
        "strength_class": m.get("strength_class") or "Weak",
        "origin": _origin_logical_v2_key(m["pool_id"]),
        "known_at": m.get("known_at"),
    }


def _active_members_at(raw_pools: list[dict[str, Any]], dc: datetime) -> list[dict[str, Any]]:
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.as_of import pools_as_of

    out: list[dict[str, Any]] = []
    for snap in pools_as_of(raw_pools, dc):
        if not snap.get("active", True):
            continue
        tf = str(snap.get("timeframe") or "")
        if tf and tf != POOL_TIMEFRAME:
            continue
        if not str(snap.get("pool_id", "")).startswith(f"lldv2:{SYMBOL}:{POOL_TIMEFRAME}:"):
            continue
        out.append(_pool_row_engine(snap))
    return out


def _build_lifetimes_on_demand(
    grid: list[datetime],
    raw_pools: list[dict[str, Any]],
    slot_minutes: int,
) -> list[dict[str, Any]]:
    """ACTIVE intervals: membership is constant between slot DCs; segment start = as-of DC (not grid win_start)."""
    by_pool: dict[str, dict[str, Any]] = {}
    windows: list[tuple[datetime, datetime, datetime, list[dict]]] = []
    win_start = grid[0]
    prev_dc: datetime | None = None
    for dt in grid:
        dc = _causal_decision_close(dt, slot_minutes)
        if prev_dc is not None and dc != prev_dc:
            windows.append(
                (
                    win_start,
                    dt - timedelta(minutes=5),
                    prev_dc,
                    _active_members_at(raw_pools, prev_dc),
                )
            )
            win_start = dt
        prev_dc = dc
    if prev_dc is not None:
        windows.append((win_start, grid[-1], prev_dc, _active_members_at(raw_pools, prev_dc)))

    for _w0, w1, dc_asof, members in windows:
        seg_start = _iso(dc_asof)
        seg_end = _iso(w1)
        for m in members:
            pid = m["pool_id"]
            row = {k: v for k, v in m.items() if k not in ("bottom", "top")}
            row["lower_bound"] = m["bottom"]
            row["upper_bound"] = m["top"]
            if pid not in by_pool:
                by_pool[pid] = {
                    **_pool_row_engine(row),
                    "segments": [[seg_start, seg_end]],
                }
            else:
                pl = by_pool[pid]
                pl.update(
                    {
                        "bottom": float(m["bottom"]),
                        "top": float(m["top"]),
                        "strength": m.get("strength"),
                        "strength_class": m.get("strength_class") or "Weak",
                    }
                )
                last = pl["segments"][-1]
                last_end = parse_decision_iso(last[1])
                if last[1] == seg_start or last_end + timedelta(minutes=5) >= parse_decision_iso(seg_start):
                    last[1] = seg_end
                else:
                    pl["segments"].append([seg_start, seg_end])
    return sorted(by_pool.values(), key=lambda x: x["pool_id"])


def _load_engine_candles(vis_from: datetime, vis_to: datetime) -> tuple[list, dict[str, Any]]:
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.as_of import filter_candles_closed_at_or_before

    cfg = build_live_micro_lld_v2_config(POOL_DETAIL)
    warmup_bars = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
    tf_sec = _timeframe_seconds(POOL_TIMEFRAME)
    from_unix = int(vis_from.timestamp())
    to_unix = int(vis_to.timestamp())
    load_start = max(0, from_unix - warmup_bars * tf_sec)

    packed = load_candles(SYMBOL, POOL_TIMEFRAME, start=load_start, end=to_unix, limit=50000)
    if not packed:
        raise LandscapeWindowError("NO_CANDLES", "load_candles returned empty packed")
    if not bool(packed.get("strict_complete_buckets")):
        raise LandscapeWindowError(
            "STRICT_BUCKETS",
            "aggregated candles missing strict_complete_buckets",
            {"timeframe": POOL_TIMEFRAME},
        )
    candles = _candles_from_packed(packed)
    if not candles:
        raise LandscapeWindowError("NO_CANDLES", "no 1h candles after pack")
    candles = filter_candles_closed_at_or_before(candles, vis_to, POOL_TIMEFRAME)
    if not candles:
        raise LandscapeWindowError("NO_CANDLES", "no closed 1h candles at or before to_utc")
    meta = {
        "warmup_bars": warmup_bars,
        "load_start_unix": load_start,
        "candle_count": len(candles),
        "strict_complete_buckets": True,
    }
    return candles, meta


def build_landscape_window_on_demand_1hpd1_v1(
    from_utc: str | datetime,
    to_utc: str | datetime,
    *,
    pool_detail: int = POOL_DETAIL,
    compute_mode: str = "exact_origin_v1",
) -> dict[str, Any]:
    pd = int(pool_detail)
    if pd != POOL_DETAIL:
        raise LandscapeWindowError(
            "INVALID_SCOPE",
            "1h on-demand supports PD1 only",
            {"pool_detail": pd, "allowed": [POOL_DETAIL]},
        )

    t0 = time.perf_counter()
    vis_from = _parse_utc(from_utc)
    vis_to = _parse_utc(to_utc)
    if vis_from > vis_to:
        raise LandscapeWindowError("INVALID_REQUEST", "from_utc must be <= to_utc")
    span_h = (vis_to - vis_from).total_seconds() / 3600.0
    if span_h > MAX_EXPORT_HOURS + 1e-6:
        raise LandscapeWindowError(
            "INVALID_REQUEST",
            f"window exceeds {MAX_EXPORT_HOURS}h export limit",
            {"requested_hours": span_h, "max_export_hours": MAX_EXPORT_HOURS},
        )

    data_lo, data_hi = _ch_symbol_bounds()
    if vis_from < data_lo or vis_to > data_hi:
        raise LandscapeWindowError(
            "OUT_OF_SCOPE",
            "request outside ClickHouse 1m coverage",
            {
                "data_available_from": _iso(data_lo),
                "data_available_to": _iso(data_hi),
                "requested_from": _iso(vis_from),
                "requested_to": _iso(vis_to),
            },
        )

    ident = on_demand_1hpd1_cache_identity()
    context_from = max(data_lo, vis_from - timedelta(hours=WARMUP_HOURS))

    t_eng = time.perf_counter()
    candles, candle_meta = _load_engine_candles(vis_from, vis_to)
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.engine import run_live_micro_lld_v2

    cfg = build_live_micro_lld_v2_config(POOL_DETAIL)
    result = run_live_micro_lld_v2(candles, cfg)
    raw_pools = [p.to_dict() for p in result.pools]
    t_eng_done = time.perf_counter()

    grid = _iter_5m(context_from, vis_to)
    t_life = time.perf_counter()
    pools_full = _build_lifetimes_on_demand(grid, raw_pools, SLOT_MINUTES)
    pools = _clip_segments_to_window(pools_full, vis_from, vis_to)
    t_life_done = time.perf_counter()

    t_ch = time.perf_counter()
    src = ClickHouseResearchCandleSource()
    bars_list = src.get_candles(SYMBOL, "5m", start=context_from, end=vis_to + timedelta(minutes=5))
    bars = _resample_5m_from_candles(bars_list)
    t_ch_done = time.perf_counter()

    vis_grid = [t for t in grid if vis_from <= t <= vis_to]
    price_rows = [
        {
            "t": _iso(t),
            "o": float(bars[t]["open"]),
            "h": float(bars[t]["high"]),
            "l": float(bars[t]["low"]),
            "c": float(bars[t]["close"]),
        }
        for t in vis_grid
        if t in bars
    ]

    fill, borders = _load_style()
    payload = {
        "meta": {
            "period_start": _iso(vis_from),
            "period_end": _iso(vis_to),
            "pool_count": len(pools),
            "FILL_BY_CLASS": fill,
            "side_borders": borders,
            "anchors": [],
        },
        "prices": price_rows,
        "pools": pools,
        "events": [],
        "clusters_by_dc": {},
        "provenance": {
            "pipeline_line_id": ident["pipeline_line_id"],
            "config_hash": ident["config_hash"],
            "pool_detail": POOL_DETAIL,
            "timeframe": POOL_TIMEFRAME,
            "membership_slot_minutes": SLOT_MINUTES,
            "context_from": _iso(context_from),
            "remaining": False,
            "compute_mode": str(compute_mode or "exact_origin_v1"),
            "source": "on_demand_ch1m_1hpd1_v1",
            "builder_version": BUILDER_VERSION_V1,
            "lifetime_builder_version": LIFETIME_BUILDER_VERSION,
            "engine_pool_count": len(raw_pools),
            "strict_complete_buckets": True,
            "candle_meta": candle_meta,
            "membership_loader": "pools_as_of_per_1h_dc",
        },
        "window": {
            "from_utc": _iso(vis_from),
            "to_utc": _iso(vis_to),
            "context_from_utc": _iso(context_from),
        },
    }
    t_done = time.perf_counter()
    payload["timing_ms"] = {
        "engine_and_candles": round((t_eng_done - t_eng) * 1000, 1),
        "build_lifetimes": round((t_life_done - t_life) * 1000, 1),
        "clickhouse_candles_5m_prices": round((t_ch_done - t_ch) * 1000, 1),
        "total": round((t_done - t0) * 1000, 1),
    }
    json.dumps(payload)
    logger.info(
        "[on_demand_1hpd1] pools=%s engine_pools=%s total_ms=%s",
        len(pools),
        len(raw_pools),
        payload["timing_ms"]["total"],
    )
    return payload
