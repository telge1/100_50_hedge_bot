"""Remaining pool heatmap projection v1 — does not mutate LiquidityPoolV2."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from data.models import Candle, ensure_utc
from indicators.live_micro_lld_v2.as_of import filter_candles_closed_at_or_before
from indicators.live_micro_lld_v2.availability import bar_close
from indicators.live_micro_lld_v2.models import LiquidityPoolV2

from .remaining_pool_projection_v1_config import (
    DEFAULT_SEGMENT_COUNT,
    FIRST_ELIGIBLE_CONSUMPTION_RULE,
    PROJECTION_VERSION,
    STRENGTH_CLASS_COLORS,
)


class RemainingPoolProjectionError(Exception):
    """Fail-closed projection error."""


@dataclass(frozen=True)
class RemainingPoolSegmentV1:
    original_segment_index: int
    segment_bottom: float
    segment_top: float
    visible_bottom: float
    visible_top: float
    visible_fraction: float
    consumed_fraction: float
    segment_visible: bool
    strength_class: str
    display_color: str


@dataclass(frozen=True)
class RemainingPoolProjectionV1:
    pool_id: str
    symbol: str
    timeframe: str
    side: str
    pool_type: str
    original_top: float
    original_bottom: float
    remaining_top: float
    remaining_bottom: float
    penetration_high: Optional[float]
    penetration_low: Optional[float]
    remaining_fraction: float
    fully_traversed: bool
    engine_active: bool
    remaining_visible: bool
    strength: float
    strength_class: str
    known_at: datetime
    break_at: Optional[datetime]
    decision_close: datetime
    first_eligible_consumption_close: datetime
    projection_version: str
    candle_input_reference: str
    segments: tuple[RemainingPoolSegmentV1, ...] = field(default_factory=tuple)


def _clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def validate_pool_bounds(bottom: float, top: float) -> None:
    b, t = float(bottom), float(top)
    if t < b:
        raise RemainingPoolProjectionError(f"invalid pool bounds bottom={b} top={t}")


def eligible_candles_for_consumption(
    candles: Sequence[Candle],
    *,
    known_at: datetime,
    decision_close: datetime,
    timeframe: str,
) -> list[Candle]:
    known = ensure_utc(known_at)
    dc = ensure_utc(decision_close)
    out: list[Candle] = []
    for c in candles:
        close = bar_close(ensure_utc(c.timestamp), timeframe)
        if close <= known:
            continue
        if close > dc:
            continue
        out.append(c)
    return out


def compute_penetration_upper(eligible: Sequence[Candle]) -> Optional[float]:
    if not eligible:
        return None
    return max(float(c.high) for c in eligible)


def compute_penetration_lower(eligible: Sequence[Candle]) -> Optional[float]:
    if not eligible:
        return None
    return min(float(c.low) for c in eligible)


def compute_remaining_bounds_upper(
    bottom: float,
    top: float,
    penetration_high: Optional[float],
) -> tuple[float, float, float, bool]:
    validate_pool_bounds(bottom, top)
    b, t = float(bottom), float(top)
    height = t - b
    if height <= 0.0:
        return t, b, 0.0, True
    if penetration_high is None:
        return t, b, 1.0, False
    pen = float(penetration_high)
    if pen < b:
        return t, b, 1.0, False
    if pen >= t:
        return t, t, 0.0, True
    rem_b = pen
    frac = (t - rem_b) / height
    return t, rem_b, frac, False


def compute_remaining_bounds_lower(
    bottom: float,
    top: float,
    penetration_low: Optional[float],
) -> tuple[float, float, float, bool]:
    validate_pool_bounds(bottom, top)
    b, t = float(bottom), float(top)
    height = t - b
    if height <= 0.0:
        return t, b, 0.0, True
    if penetration_low is None:
        return t, b, 1.0, False
    pen = float(penetration_low)
    if pen > t:
        return t, b, 1.0, False
    if pen <= b:
        return b, b, 0.0, True
    rem_t = pen
    frac = (rem_t - b) / height
    return rem_t, b, frac, False


def remaining_from_penetration(
    side: str,
    bottom: float,
    top: float,
    penetration_high: Optional[float],
    penetration_low: Optional[float],
) -> tuple[float, float, float, bool, Optional[float], Optional[float]]:
    """Same bounds as project_pool_v1, given precomputed eligible extremes."""
    s = str(side).lower()
    if s == "upper":
        rem_t, rem_b, frac, full = compute_remaining_bounds_upper(bottom, top, penetration_high)
        return rem_t, rem_b, frac, full, penetration_high, None
    if s == "lower":
        rem_t, rem_b, frac, full = compute_remaining_bounds_lower(bottom, top, penetration_low)
        return rem_t, rem_b, frac, full, None, penetration_low
    raise RemainingPoolProjectionError(f"unknown side {side}")


def build_fixed_segments(
    *,
    original_bottom: float,
    original_top: float,
    remaining_bottom: float,
    remaining_top: float,
    strength_class: str,
    segment_count: int = DEFAULT_SEGMENT_COUNT,
) -> tuple[RemainingPoolSegmentV1, ...]:
    validate_pool_bounds(original_bottom, original_top)
    b0, t0 = float(original_bottom), float(original_top)
    height = t0 - b0
    if height <= 0.0:
        return tuple()
    n = int(segment_count)
    if n < 1:
        raise RemainingPoolProjectionError("segment_count must be >= 1")
    color = STRENGTH_CLASS_COLORS.get(strength_class, "#888888")
    step = height / n
    rb, rt = float(remaining_bottom), float(remaining_top)
    segments: list[RemainingPoolSegmentV1] = []
    for i in range(n):
        seg_b = b0 + i * step
        seg_t = b0 + (i + 1) * step if i < n - 1 else t0
        vis_b = max(seg_b, rb)
        vis_t = min(seg_t, rt)
        if vis_t <= vis_b:
            segments.append(
                RemainingPoolSegmentV1(
                    original_segment_index=i,
                    segment_bottom=seg_b,
                    segment_top=seg_t,
                    visible_bottom=vis_b,
                    visible_top=vis_t,
                    visible_fraction=0.0,
                    consumed_fraction=1.0,
                    segment_visible=False,
                    strength_class=strength_class,
                    display_color=color,
                )
            )
            continue
        seg_h = seg_t - seg_b
        vis_frac = (vis_t - vis_b) / seg_h if seg_h > 0 else 0.0
        segments.append(
            RemainingPoolSegmentV1(
                original_segment_index=i,
                segment_bottom=seg_b,
                segment_top=seg_t,
                visible_bottom=vis_b,
                visible_top=vis_t,
                visible_fraction=vis_frac,
                consumed_fraction=1.0 - vis_frac,
                segment_visible=True,
                strength_class=strength_class,
                display_color=color,
            )
        )
    return tuple(segments)


def engine_active_at_close(pool: LiquidityPoolV2, decision_close: datetime) -> bool:
    """Prefix-end engine membership (includes box_amount eviction, not break_at alone)."""
    dc = ensure_utc(decision_close)
    if ensure_utc(pool.known_at) > dc:
        return False
    return bool(pool.active)


def first_eligible_consumption_close(
    candles: Sequence[Candle],
    *,
    known_at: datetime,
    decision_close: datetime,
    timeframe: str,
) -> Optional[datetime]:
    known = ensure_utc(known_at)
    dc = ensure_utc(decision_close)
    first: Optional[datetime] = None
    for c in candles:
        close = bar_close(ensure_utc(c.timestamp), timeframe)
        if close <= known:
            continue
        if close > dc:
            break
        if first is None or close < first:
            first = close
    return first


@dataclass
class PoolConsumptionLedger:
    """Monotonic wick penetration per pool_id across chronological closes."""

    _pen_high: dict[str, float] = field(default_factory=dict)
    _pen_low: dict[str, float] = field(default_factory=dict)
    _bounds: dict[str, tuple[float, float]] = field(default_factory=dict)

    def bounds_key(self, pool: LiquidityPoolV2) -> tuple[float, float]:
        return (float(pool.bottom_price), float(pool.top_price))

    def prior_for(self, pool: LiquidityPoolV2) -> tuple[Optional[float], Optional[float]]:
        pid = pool.pool_id
        bk = self.bounds_key(pool)
        if pid in self._bounds and self._bounds[pid] != bk:
            raise RemainingPoolProjectionError(
                f"pool_id {pid} reappeared with different bounds {self._bounds[pid]} vs {bk}"
            )
        return self._pen_high.get(pid), self._pen_low.get(pid)

    def record(self, projection: RemainingPoolProjectionV1) -> None:
        pid = projection.pool_id
        bk = (projection.original_bottom, projection.original_top)
        if pid in self._bounds and self._bounds[pid] != bk:
            raise RemainingPoolProjectionError(f"ledger bounds conflict for {pid}")
        self._bounds[pid] = bk
        if projection.penetration_high is not None:
            prev = self._pen_high.get(pid)
            self._pen_high[pid] = (
                max(prev, projection.penetration_high) if prev is not None else projection.penetration_high
            )
        if projection.penetration_low is not None:
            prev = self._pen_low.get(pid)
            self._pen_low[pid] = (
                min(prev, projection.penetration_low) if prev is not None else projection.penetration_low
            )


def project_pool_v1(
    pool: LiquidityPoolV2,
    candles: Sequence[Candle],
    *,
    decision_close: datetime,
    candle_input_reference: str,
    segment_count: int = DEFAULT_SEGMENT_COUNT,
    prior_penetration_high: Optional[float] = None,
    prior_penetration_low: Optional[float] = None,
) -> RemainingPoolProjectionV1:
    """Pure function: no mutation of pool; monotonic merge with prior penetration if given."""
    dc = ensure_utc(decision_close)
    known = ensure_utc(pool.known_at)
    if known > dc:
        raise RemainingPoolProjectionError(f"pool {pool.pool_id} not known at {dc.isoformat()}")

    prefix = filter_candles_closed_at_or_before(candles, dc, pool.timeframe)
    eligible = eligible_candles_for_consumption(
        prefix,
        known_at=known,
        decision_close=dc,
        timeframe=pool.timeframe,
    )
    fec = first_eligible_consumption_close(
        prefix, known_at=known, decision_close=dc, timeframe=pool.timeframe
    )

    side = str(pool.side).lower()
    b, t = float(pool.bottom_price), float(pool.top_price)

    if side == "upper":
        pen = compute_penetration_upper(eligible)
        if prior_penetration_high is not None:
            pen = max(pen, prior_penetration_high) if pen is not None else prior_penetration_high
        rem_t, rem_b, frac, full = compute_remaining_bounds_upper(b, t, pen)
        pen_h, pen_l = pen, None
    elif side == "lower":
        pen = compute_penetration_lower(eligible)
        if prior_penetration_low is not None:
            pen = min(pen, prior_penetration_low) if pen is not None else prior_penetration_low
        rem_t, rem_b, frac, full = compute_remaining_bounds_lower(b, t, pen)
        pen_h, pen_l = None, pen
    else:
        raise RemainingPoolProjectionError(f"unknown side {pool.side}")

    eng_active = engine_active_at_close(pool, dc)
    rem_vis = eng_active and not full and frac > 0.0

    segs = build_fixed_segments(
        original_bottom=b,
        original_top=t,
        remaining_bottom=rem_b,
        remaining_top=rem_t,
        strength_class=str(pool.strength_class),
        segment_count=segment_count,
    )

    return RemainingPoolProjectionV1(
        pool_id=pool.pool_id,
        symbol=pool.symbol,
        timeframe=pool.timeframe,
        side=side,
        pool_type=str(pool.pool_type),
        original_top=t,
        original_bottom=b,
        remaining_top=rem_t,
        remaining_bottom=rem_b,
        penetration_high=pen_h,
        penetration_low=pen_l,
        remaining_fraction=frac,
        fully_traversed=full,
        engine_active=eng_active,
        remaining_visible=rem_vis,
        strength=float(pool.strength),
        strength_class=str(pool.strength_class),
        known_at=known,
        break_at=pool.break_at,
        decision_close=dc,
        first_eligible_consumption_close=fec or known,
        projection_version=PROJECTION_VERSION,
        candle_input_reference=candle_input_reference,
        segments=segs,
    )


def apply_strength_class_filter(
    projections: Sequence[RemainingPoolProjectionV1],
    allowed: frozenset[str],
) -> list[RemainingPoolProjectionV1]:
    return [p for p in projections if p.strength_class in allowed]


def project_pools_from_engine_result(
    result,
    candles: Sequence[Candle],
    *,
    decision_close: datetime,
    candle_input_reference: str,
    segment_count: int = DEFAULT_SEGMENT_COUNT,
    ledger: Optional[PoolConsumptionLedger] = None,
) -> list[RemainingPoolProjectionV1]:
    dc = ensure_utc(decision_close)
    out: list[RemainingPoolProjectionV1] = []
    for pool in result.pools:
        if ensure_utc(pool.known_at) > dc:
            continue
        prior_h, prior_l = (None, None)
        if ledger is not None:
            prior_h, prior_l = ledger.prior_for(pool)
        proj = project_pool_v1(
            pool,
            candles,
            decision_close=dc,
            candle_input_reference=candle_input_reference,
            segment_count=segment_count,
            prior_penetration_high=prior_h,
            prior_penetration_low=prior_l,
        )
        if ledger is not None:
            ledger.record(proj)
        out.append(proj)
    return out


def projection_to_dict(p: RemainingPoolProjectionV1) -> dict[str, Any]:
    return {
        "pool_id": p.pool_id,
        "original_top": p.original_top,
        "original_bottom": p.original_bottom,
        "remaining_top": p.remaining_top,
        "remaining_bottom": p.remaining_bottom,
        "remaining_fraction": p.remaining_fraction,
        "fully_traversed": p.fully_traversed,
        "engine_active": p.engine_active,
        "remaining_visible": p.remaining_visible,
        "penetration_high": p.penetration_high,
        "penetration_low": p.penetration_low,
        "first_eligible_consumption_rule": FIRST_ELIGIBLE_CONSUMPTION_RULE,
        "segment_count": len(p.segments),
    }
