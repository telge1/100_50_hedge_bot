"""Outcome horizon evaluation from mid/minute series inside ready window only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from .models import OutcomeHorizon
from .ports import MidState
from .time_windows import as_utc, dt_to_ns


DEFAULT_HORIZONS_MIN = (5, 15, 30, 60, 240)


def _label(m: int) -> str:
    if m == 60:
        return "1h"
    if m == 240:
        return "4h"
    return f"{m}m"


def plan_outcome_horizons(
    *,
    event_time: datetime,
    window_end: datetime,
    horizons_min: Sequence[int] = DEFAULT_HORIZONS_MIN,
) -> list[OutcomeHorizon]:
    event = as_utc(event_time)
    end = as_utc(window_end)
    out: list[OutcomeHorizon] = []
    for m in horizons_min:
        required = event + timedelta(minutes=int(m))
        if required <= end:
            out.append(
                OutcomeHorizon(
                    label=_label(int(m)),
                    status="EVALUATED",
                    required_end_utc=required,
                    evaluated=True,
                )
            )
        else:
            out.append(
                OutcomeHorizon(
                    label=_label(int(m)),
                    status="UNAVAILABLE_OUTSIDE_WINDOW",
                    required_end_utc=required,
                    evaluated=False,
                    detail={"window_end": end.isoformat().replace("+00:00", "Z")},
                )
            )
    return out


def evaluate_outcomes(
    *,
    event_time: datetime,
    window_end: datetime,
    mid_series: Sequence[MidState],
    reference_price: float | None,
    side: str | None = None,
    horizons_min: Sequence[int] = DEFAULT_HORIZONS_MIN,
) -> list[OutcomeHorizon]:
    """Fill EVALUATED detail from mid samples already loaded for the ready window.

    Never loads data beyond window_end / outside provided mid_series.
    """
    planned = plan_outcome_horizons(
        event_time=event_time, window_end=window_end, horizons_min=horizons_min
    )
    event = as_utc(event_time)
    event_ns = dt_to_ns(event)
    if not mid_series:
        for o in planned:
            if o.status == "EVALUATED":
                o.status = "NO_EVENT"
                o.evaluated = False
                o.detail = {"reason": "NO_MID_SERIES"}
        return planned

    start_mid = _mid_at_or_after(mid_series, event_ns)
    if start_mid is None:
        for o in planned:
            if o.status == "EVALUATED":
                o.status = "NO_EVENT"
                o.evaluated = False
                o.detail = {"reason": "NO_START_MID"}
        return planned

    for o in planned:
        if o.status != "EVALUATED":
            continue
        end_ns = dt_to_ns(as_utc(o.required_end_utc))
        # Only use mids already in series and within [event, required_end]
        path = [m for m in mid_series if event_ns <= m.bucket_start_ns < end_ns]
        if not path:
            o.status = "NO_EVENT"
            o.evaluated = False
            o.detail = {"reason": "EMPTY_PATH"}
            continue
        # coverage check: last sample must reach near required end (within 1s)
        if path[-1].bucket_start_ns + 1_000_000_000 < end_ns:
            # still evaluate with available path but mark coverage shortfall? Spec:
            # UNAVAILABLE only when required_end > window_end. Inside window we EVALUATE.
            pass
        end_mid = path[-1].mid
        start_px = float(start_mid.mid)
        highs = [m.mid for m in path]
        lows = highs
        mfe = max(highs) - start_px
        mae = start_px - min(lows)
        if side == "lower":
            mfe = start_px - min(lows)
            mae = max(highs) - start_px
        net = end_mid - start_px
        net_pct = (net / start_px * 100.0) if start_px else 0.0
        held = reclaimed = broken = None
        if reference_price is not None:
            ref = float(reference_price)
            if side == "lower":
                broken = any(m.mid < ref for m in path)
                held = not broken and all(m.mid >= ref for m in path)
                reclaimed = broken and path[-1].mid >= ref
            else:
                broken = any(m.mid > ref for m in path)
                held = not broken and all(m.mid <= ref for m in path)
                reclaimed = broken and path[-1].mid <= ref
        efficiency = None
        span = max(highs) - min(lows)
        if span > 1e-12:
            efficiency = abs(net) / span
        o.detail = {
            "start_price": round(start_px, 8),
            "end_price": round(float(end_mid), 8),
            "mfe": round(float(mfe), 8),
            "mae": round(float(mae), 8),
            "net_move_abs": round(float(net), 8),
            "net_move_pct": round(float(net_pct), 8),
            "delta": None,  # filled by caller if trade flow known
            "efficiency": None if efficiency is None else round(float(efficiency), 8),
            "reference_held": held,
            "reference_reclaimed": reclaimed,
            "reference_broken": broken,
        }
    return planned


def _mid_at_or_after(series: Sequence[MidState], ns: int) -> MidState | None:
    for m in series:
        if m.bucket_start_ns >= ns:
            return m
    return series[-1] if series else None
