"""Reference-level resolution with explicit causality rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import FORMULA_ID
from .models import EdgeSide, ReferenceLevel, ReferenceSource
from .time_windows import as_utc


ResolveMpFn = Callable[..., dict[str, Any]]


def resolve_strategy_reference(
    *,
    decision_time_utc: datetime,
    edge_side: EdgeSide,
    reference_price: float | None = None,
    reference_known_as_of_utc: datetime | None = None,
    mp_profile: dict[str, Any] | None = None,
) -> ReferenceLevel:
    decision = as_utc(decision_time_utc)
    if reference_price is not None:
        known = (
            None
            if reference_known_as_of_utc is None
            else as_utc(reference_known_as_of_utc)
        )
        causal = known is not None and known <= decision
        return ReferenceLevel(
            price=float(reference_price),
            side=edge_side,
            source=ReferenceSource.MANUAL_OVERRIDE,
            known_as_of_utc=known,
            causal_reference=causal,
            formula_id=FORMULA_ID if causal else None,
        )

    if mp_profile is None:
        raise ValueError("STOP_XRAY_MP_PROFILE_REQUIRED")
    # Expected shape from adapter: tpo vah/val + window bounds
    tpo = mp_profile.get("tpo") or {}
    va = tpo.get("value_area") or {}
    if edge_side == EdgeSide.UPPER:
        price = float(va["vah"])
    else:
        price = float(va["val"])
    win_start = as_utc(mp_profile["window_start"])
    win_end = as_utc(mp_profile["window_end"])
    return ReferenceLevel(
        price=price,
        side=edge_side,
        source=ReferenceSource.MARKET_PROFILE_30M,
        known_as_of_utc=win_end,
        causal_reference=True,
        profile_window_start=win_start,
        profile_window_end=win_end,
        formula_id=FORMULA_ID,
    )


def resolve_manual_reference(
    *,
    analysis_start: datetime,
    reference_price: float | None,
    reference_side: EdgeSide | None,
    reference_known_as_of_utc: datetime | None,
) -> ReferenceLevel:
    if reference_price is None:
        return ReferenceLevel(
            price=None,
            side=None,
            source=ReferenceSource.NONE,
            known_as_of_utc=None,
            causal_reference=False,
        )
    known = (
        None
        if reference_known_as_of_utc is None
        else as_utc(reference_known_as_of_utc)
    )
    start = as_utc(analysis_start)
    if known is not None and known < start:
        return ReferenceLevel(
            price=float(reference_price),
            side=reference_side,
            source=ReferenceSource.MANUAL_OVERRIDE,
            known_as_of_utc=known,
            causal_reference=True,
            formula_id=None,
        )
    return ReferenceLevel(
        price=float(reference_price),
        side=reference_side,
        source=ReferenceSource.MANUAL_FORENSIC,
        known_as_of_utc=known,
        causal_reference=False,
        formula_id=None,
    )


def wall_threshold_for_strategy(
    *,
    decision_time: datetime,
    pre_window_minutes: int,
    local_band_usd: float,
    quantile: float,
    max_rank: int,
) -> dict[str, Any]:
    decision = as_utc(decision_time)
    start = decision - timedelta(minutes=int(pre_window_minutes))
    return {
        "method": "causal_pre_decision_q95",
        "window_start": start,
        "window_end": decision,
        "known_as_of_utc": decision,
        "causal": True,
        "quantile": quantile,
        "local_band_usd": local_band_usd,
        "max_rank": max_rank,
    }


def wall_threshold_for_manual(
    *,
    start: datetime,
    end: datetime,
    local_band_usd: float,
    quantile: float,
    max_rank: int,
) -> dict[str, Any]:
    return {
        "method": "forensic_window_q95",
        "window_start": as_utc(start),
        "window_end": as_utc(end),
        "known_as_of_utc": as_utc(end),
        "causal": False,
        "quantile": quantile,
        "local_band_usd": local_band_usd,
        "max_rank": max_rank,
    }
