"""Ask-band liquidity distribution timeline (exact + band views)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt, _apply_change, _apply_reset, _floor_bucket
from ..timeparse import format_utc_z
from . import (
    EPSILON,
    EXACT_BAND_TICKS,
    MIN_RELEVANT_ASK_QTY,
    ORIGINAL_WALL_PRICE,
    TICK_SIZE,
    analysis_band_bounds,
)
from .generations import AskWallGeneration
from .segments import EpochSegment, iter_merged_events


def _centroid(asks: dict[float, float], low: float, high: float) -> float | None:
    mass = 0.0
    acc = 0.0
    for px, q in asks.items():
        if q and low - 1e-12 <= float(px) <= high + 1e-12:
            mass += float(q)
            acc += float(px) * float(q)
    if mass <= EPSILON:
        return None
    return acc / mass


def _nearest_relevant(asks: dict[float, float], *, mid: float | None, min_qty: float, low: float, high: float) -> tuple[float | None, float | None]:
    cands = [
        (float(px), float(q))
        for px, q in asks.items()
        if q and float(q) >= min_qty and low - 1e-12 <= float(px) <= high + 1e-12
    ]
    if not cands:
        return None, None
    if mid is None:
        px, q = min(cands, key=lambda x: x[0])
        return px, q
    px, q = min(cands, key=lambda x: abs(x[0] - mid))
    return px, q


def build_liquidity_timeline(
    *,
    payload: dict[str, Any],
    segment: EpochSegment,
    generations: list[AskWallGeneration],
    wall_price: float = ORIGINAL_WALL_PRICE,
    bucket_ms: int = 100,
) -> list[dict[str, Any]]:
    band_low, band_high = analysis_band_bounds(wall_price=wall_price)
    exact_low = wall_price - EXACT_BAND_TICKS * TICK_SIZE
    exact_high = wall_price + EXACT_BAND_TICKS * TICK_SIZE

    bids = dict(payload["initial_bids"])
    asks = dict(payload["initial_asks"])
    epoch = int(payload.get("initial_replay_epoch") or 0)
    rows: list[dict[str, Any]] = []
    last_emit: datetime | None = None
    centroid0: float | None = None
    front0: float | None = None

    def alive_count(ts: datetime) -> int:
        n = 0
        for g in generations:
            if int(g.replay_epoch) != segment.replay_epoch:
                continue
            st = _as_dt(g.start_time)
            en = _as_dt(g.end_time) if g.end_time else segment.coverage_end
            if st <= ts < en or (g.end_time is None and st <= ts <= en):
                if g.end_time is None and ts <= en:
                    n += 1
                elif g.end_time and st <= ts < _as_dt(g.end_time):
                    n += 1
        return n

    def emit(ts: datetime) -> None:
        nonlocal centroid0, front0
        if epoch != segment.replay_epoch:
            return
        if ts < segment.coverage_start or ts > segment.coverage_end:
            return
        mid = 0.5 * (max(bids) + min(asks)) if bids and asks else None
        total = sum(float(q) for px, q in asks.items() if q and band_low - 1e-12 <= float(px) <= band_high + 1e-12)
        total_exact = sum(
            float(q) for px, q in asks.items() if q and exact_low - 1e-12 <= float(px) <= exact_high + 1e-12
        )
        cent = _centroid(asks, band_low, band_high)
        nearest_px, nearest_q = _nearest_relevant(
            asks, mid=mid, min_qty=MIN_RELEVANT_ASK_QTY, low=band_low, high=band_high
        )
        qty_above = sum(
            float(q) for px, q in asks.items() if q and float(px) > wall_price + 1e-12 and float(px) <= band_high + 1e-12
        )
        qty_at = sum(float(q) for px, q in asks.items() if q and abs(float(px) - wall_price) <= 1e-9)
        if centroid0 is None and cent is not None:
            centroid0 = cent
        if front0 is None and nearest_px is not None:
            front0 = nearest_px
        c_shift = None if cent is None or centroid0 is None else (cent - centroid0) / TICK_SIZE
        f_shift = None if nearest_px is None or front0 is None else (nearest_px - front0) / TICK_SIZE
        dwd = None
        if mid is not None and total > EPSILON:
            dwd = sum(
                float(q) * ((float(px) - mid) / TICK_SIZE)
                for px, q in asks.items()
                if q and band_low - 1e-12 <= float(px) <= band_high + 1e-12
            ) / total
        rows.append(
            {
                "timestamp": format_utc_z(ts),
                "replay_epoch": epoch,
                "coverage_ok": True,
                "midprice": mid,
                "best_ask": min(asks) if asks else None,
                "best_bid": max(bids) if bids else None,
                "total_ask_qty": total,
                "total_ask_qty_exact_band": total_exact,
                "ask_liquidity_centroid": cent,
                "nearest_relevant_ask_price": nearest_px,
                "nearest_relevant_ask_qty": nearest_q,
                "number_of_active_ask_generations": alive_count(ts),
                "quantity_above_original_wall": qty_above,
                "quantity_at_original_wall": qty_at,
                "centroid_shift_ticks": c_shift,
                "front_wall_shift_ticks": f_shift,
                "depth_weighted_distance_from_mid": dwd,
                "band_low": band_low,
                "band_high": band_high,
                "exact_band_low": exact_low,
                "exact_band_high": exact_high,
            }
        )

    for et, _o, _k, _i, kind, pev in iter_merged_events(payload):
        if et > segment.coverage_end:
            break
        if kind == "reset":
            _apply_reset(bids, asks, pev)
        else:
            _apply_change(bids, asks, pev)
        if pev.get("replay_epoch") is not None:
            epoch = int(pev["replay_epoch"])
        if et < segment.coverage_start:
            continue
        if epoch != segment.replay_epoch:
            # stop emitting for this segment (no bridge)
            if et >= segment.coverage_start:
                break
            continue
        # emit on 100ms grid
        b = _floor_bucket(et, bucket_ms)
        if last_emit is None or b > last_emit:
            emit(et if et >= segment.coverage_start else segment.coverage_start)
            last_emit = b

    # final emit at coverage end
    emit(segment.coverage_end)
    return rows
