"""Forward price and wall hold/break outcome evaluation."""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from orderbook_analyse.wall_toxicity_audit.types import OutcomeParams


@dataclass
class ForwardPathMetrics:
    reference_price: float | None
    forward_end_price: float | None
    forward_return_bps: float | None
    mfe_up_bps: float | None
    mae_down_bps: float | None
    time_to_mfe_up_seconds: float | None
    time_to_mae_down_seconds: float | None
    max_price: float | None
    min_price: float | None
    data_coverage_seconds: float
    forward_data_complete: bool
    n_price_samples: int


@dataclass
class WallRoleOutcome:
    """Ask=resistance / Bid=support semantics for one horizon."""

    side: str
    resistance_held: bool | None = None
    resistance_broken: bool | None = None
    breakout_attempted: bool | None = None
    breakout_accepted: bool | None = None
    breakout_failed: bool | None = None
    rejection_down_bps: float | None = None
    breakout_up_bps: float | None = None
    support_held: bool | None = None
    support_broken: bool | None = None
    breakdown_attempted: bool | None = None
    breakdown_accepted: bool | None = None
    breakdown_failed: bool | None = None
    rejection_up_bps: float | None = None
    breakdown_down_bps: float | None = None
    time_to_first_touch_seconds: float | None = None
    time_to_break_seconds: float | None = None
    time_above_wall_seconds: float = 0.0
    time_below_wall_seconds: float = 0.0
    accepted_above_wall: bool | None = None
    accepted_below_wall: bool | None = None
    held: bool | None = None
    broken: bool | None = None
    acceptance: bool | None = None
    failed_break: bool | None = None


def _slice_series(
    series: Sequence[tuple[datetime, float]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    """Inclusive start, exclusive end — no future beyond ``end``."""
    if not series:
        return []
    ts_list = [t for t, _ in series]
    i0 = bisect.bisect_left(ts_list, start)
    i1 = bisect.bisect_left(ts_list, end)
    return list(series[i0:i1])


def price_at_or_after(
    series: Sequence[tuple[datetime, float]], when: datetime
) -> tuple[datetime, float] | None:
    if not series:
        return None
    ts_list = [t for t, _ in series]
    i = bisect.bisect_left(ts_list, when)
    if i >= len(series):
        return None
    return series[i]


def evaluate_forward_path(
    series: Sequence[tuple[datetime, float]],
    *,
    reference_ts: datetime,
    horizon_seconds: float,
    params: OutcomeParams,
) -> ForwardPathMetrics:
    end_ts = reference_ts + timedelta(seconds=horizon_seconds)
    start_px = price_at_or_after(series, reference_ts)
    window = _slice_series(series, reference_ts, end_ts)
    if start_px is None or not window:
        return ForwardPathMetrics(
            reference_price=None if start_px is None else start_px[1],
            forward_end_price=None,
            forward_return_bps=None,
            mfe_up_bps=None,
            mae_down_bps=None,
            time_to_mfe_up_seconds=None,
            time_to_mae_down_seconds=None,
            max_price=None,
            min_price=None,
            data_coverage_seconds=0.0,
            forward_data_complete=False,
            n_price_samples=0,
        )
    ref_price = float(start_px[1])
    # Ensure reference sample is included even if bisect left skipped equals
    if window[0][0] > reference_ts:
        window = [start_px] + window
    prices = [p for _, p in window]
    max_p = max(prices)
    min_p = min(prices)
    end_price = prices[-1]
    ret_bps = (end_price - ref_price) / ref_price * 10_000.0 if ref_price > 0 else None
    ups = [(p - ref_price) / ref_price * 10_000.0 for p in prices]
    downs = [(p - ref_price) / ref_price * 10_000.0 for p in prices]
    mfe_up = max(ups)
    mae_down = min(downs)
    i_mfe = ups.index(mfe_up)
    i_mae = downs.index(mae_down)
    coverage = (window[-1][0] - window[0][0]).total_seconds()
    complete = coverage >= horizon_seconds * params.min_forward_coverage_ratio and len(window) >= 2
    return ForwardPathMetrics(
        reference_price=ref_price,
        forward_end_price=end_price,
        forward_return_bps=ret_bps,
        mfe_up_bps=mfe_up,
        mae_down_bps=mae_down,
        time_to_mfe_up_seconds=(window[i_mfe][0] - reference_ts).total_seconds(),
        time_to_mae_down_seconds=(window[i_mae][0] - reference_ts).total_seconds(),
        max_price=max_p,
        min_price=min_p,
        data_coverage_seconds=coverage,
        forward_data_complete=complete,
        n_price_samples=len(window),
    )


def _wall_edges(band_low: float, band_high: float) -> tuple[float, float]:
    return float(band_low), float(band_high)


def evaluate_wall_role(
    series: Sequence[tuple[datetime, float]],
    *,
    reference_ts: datetime,
    horizon_seconds: float,
    side: str,
    band_low: float,
    band_high: float,
    params: OutcomeParams,
) -> WallRoleOutcome:
    """Ask wall = resistance; Bid wall = support."""
    end_ts = reference_ts + timedelta(seconds=horizon_seconds)
    window = _slice_series(series, reference_ts, end_ts)
    out = WallRoleOutcome(side=str(side).lower())
    if len(window) < 2:
        return out

    low, high = _wall_edges(band_low, band_high)
    touch_frac = params.touch_bps / 10_000.0
    break_frac = params.break_bps / 10_000.0
    side_l = out.side

    if side_l in {"ask", "sell"}:
        # Resistance near high edge of ask band.
        wall = high
        touch_level = wall * (1.0 - touch_frac)
        break_level = wall * (1.0 + break_frac)
    else:
        wall = low
        touch_level = wall * (1.0 + touch_frac)
        break_level = wall * (1.0 - break_frac)

    first_touch_ts: datetime | None = None
    first_break_ts: datetime | None = None
    time_above = 0.0
    time_below = 0.0
    max_above_bps = 0.0
    max_below_bps = 0.0
    max_rej_down = 0.0
    max_rej_up = 0.0
    beyond_streak_start: datetime | None = None
    accepted = False
    returned_after_break = False

    for i, (ts, px) in enumerate(window):
        dt = 0.0
        if i + 1 < len(window):
            dt = (window[i + 1][0] - ts).total_seconds()
        if side_l in {"ask", "sell"}:
            if px >= touch_level and first_touch_ts is None:
                first_touch_ts = ts
            if px > break_level:
                if first_break_ts is None:
                    first_break_ts = ts
                    beyond_streak_start = ts
                time_above += dt
                max_above_bps = max(max_above_bps, (px - wall) / wall * 10_000.0)
                if (
                    beyond_streak_start is not None
                    and (ts - beyond_streak_start).total_seconds() >= params.acceptance_seconds
                ):
                    accepted = True
            else:
                beyond_streak_start = None
                if first_break_ts is not None and px < wall:
                    if (ts - first_break_ts).total_seconds() <= params.failed_break_return_seconds:
                        returned_after_break = True
                if first_touch_ts is not None and px < touch_level:
                    max_rej_down = max(
                        max_rej_down, (touch_level - px) / wall * 10_000.0
                    )
            if px < wall:
                time_below += dt
        else:
            if px <= touch_level and first_touch_ts is None:
                first_touch_ts = ts
            if px < break_level:
                if first_break_ts is None:
                    first_break_ts = ts
                    beyond_streak_start = ts
                time_below += dt
                max_below_bps = max(max_below_bps, (wall - px) / wall * 10_000.0)
                if (
                    beyond_streak_start is not None
                    and (ts - beyond_streak_start).total_seconds() >= params.acceptance_seconds
                ):
                    accepted = True
            else:
                beyond_streak_start = None
                if first_break_ts is not None and px > wall:
                    if (ts - first_break_ts).total_seconds() <= params.failed_break_return_seconds:
                        returned_after_break = True
                if first_touch_ts is not None and px > touch_level:
                    max_rej_up = max(max_rej_up, (px - touch_level) / wall * 10_000.0)
            if px > wall:
                time_above += dt

    touched = first_touch_ts is not None
    broken = first_break_ts is not None
    failed = bool(broken and returned_after_break and not accepted)
    held = bool(touched and not broken)

    t_touch = (
        None
        if first_touch_ts is None
        else (first_touch_ts - reference_ts).total_seconds()
    )
    t_break = (
        None
        if first_break_ts is None
        else (first_break_ts - reference_ts).total_seconds()
    )

    out.time_to_first_touch_seconds = t_touch
    out.time_to_break_seconds = t_break
    out.time_above_wall_seconds = time_above
    out.time_below_wall_seconds = time_below
    out.held = held if touched else None
    out.broken = broken
    out.acceptance = accepted if broken else None
    out.failed_break = failed if broken else None

    if side_l in {"ask", "sell"}:
        out.resistance_held = held if touched else None
        out.resistance_broken = broken
        out.breakout_attempted = broken
        out.breakout_accepted = accepted if broken else None
        out.breakout_failed = failed if broken else None
        out.rejection_down_bps = max_rej_down if touched else None
        out.breakout_up_bps = max_above_bps if broken else None
        out.accepted_above_wall = accepted if broken else None
    else:
        out.support_held = held if touched else None
        out.support_broken = broken
        out.breakdown_attempted = broken
        out.breakdown_accepted = accepted if broken else None
        out.breakdown_failed = failed if broken else None
        out.rejection_up_bps = max_rej_up if touched else None
        out.breakdown_down_bps = max_below_bps if broken else None
        out.accepted_below_wall = accepted if broken else None
    return out


def outcome_row(
    *,
    sequence_id: str,
    symbol: str,
    side: str,
    reference_point: str,
    reference_ts: datetime,
    horizon_seconds: int,
    path: ForwardPathMetrics,
    role: WallRoleOutcome,
) -> dict[str, Any]:
    row = {
        "wall_sequence_id": sequence_id,
        "symbol": symbol,
        "side": side,
        "reference_point": reference_point,
        "reference_ts": reference_ts.isoformat(),
        "horizon_seconds": horizon_seconds,
        **{k: getattr(path, k) for k in path.__dataclass_fields__},
    }
    for k in role.__dataclass_fields__:
        if k == "side":
            continue
        row[k] = getattr(role, k)
    return row
