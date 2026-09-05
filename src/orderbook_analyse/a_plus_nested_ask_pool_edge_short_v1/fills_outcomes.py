"""Fill detection and outcome simulation (causal, short-only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import _utc_naive

from .config import OUTCOME_HORIZON_MINUTES, ROUNDTRIP_COST_PCT_BASELINE


@dataclass
class FillResult:
    fill_at: datetime | None
    fill_price: float | None
    first_touch_at: datetime | None
    same_bar_ambiguous: bool
    status: str  # FILLED | NO_FILL | SAME_BAR_SEQUENCE_AMBIGUOUS | ORDER_NEVER_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_at": None if self.fill_at is None else self.fill_at.isoformat(),
            "fill_price": self.fill_price,
            "first_touch_at": None if self.first_touch_at is None else self.first_touch_at.isoformat(),
            "same_bar_ambiguous": self.same_bar_ambiguous,
            "status": self.status,
        }


def detect_short_limit_fill(
    df_1m: pd.DataFrame,
    *,
    entry_price: float,
    order_active_at: datetime,
    child_available_at: datetime,
    horizon_end: datetime | None = None,
) -> FillResult:
    """Fill when a later closed 1m bar's high reaches entry after order_active_at.

    If the birth bar of the child pool already touched entry before available_at
    and no later bar re-touches, mark SAME_BAR_SEQUENCE_AMBIGUOUS (no strict fill).
    """
    active = _utc_naive(order_active_at)
    avail = _utc_naive(child_available_at)
    assert active >= avail

    df = df_1m.sort_values("open_time").copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    if getattr(df["open_time"].dt, "tz", None) is not None:
        df["open_time"] = df["open_time"].dt.tz_convert("UTC").dt.tz_localize(None)

    # Birth-bar ambiguity: bar that closes at available_at may have touched during formation
    birth_open = avail - timedelta(minutes=1)
    birth = df[df["open_time"] == birth_open]
    birth_touched = False
    if not birth.empty and float(birth.iloc[0]["high"]) >= entry_price:
        birth_touched = True

    end = _utc_naive(horizon_end) if horizon_end else None
    after = df[df["open_time"] + pd.Timedelta(minutes=1) > active]
    if end is not None:
        after = after[after["open_time"] + pd.Timedelta(minutes=1) <= end]

    for _, row in after.iterrows():
        bar_close = _utc_naive(row["open_time"].to_pydatetime()) + timedelta(minutes=1)
        if bar_close <= active:
            continue
        if float(row["high"]) >= entry_price:
            return FillResult(
                fill_at=bar_close,
                fill_price=entry_price,
                first_touch_at=bar_close,
                same_bar_ambiguous=False,
                status="FILLED",
            )

    if birth_touched:
        return FillResult(
            fill_at=None,
            fill_price=None,
            first_touch_at=avail,
            same_bar_ambiguous=True,
            status="SAME_BAR_SEQUENCE_AMBIGUOUS",
        )
    return FillResult(
        fill_at=None,
        fill_price=None,
        first_touch_at=None,
        same_bar_ambiguous=False,
        status="NO_FILL",
    )


def _first_touch_outcome(
    df_1m: pd.DataFrame,
    *,
    fill_at: datetime,
    entry: float,
    stop: float,
    target: float,
    horizon_minutes: int = OUTCOME_HORIZON_MINUTES,
) -> dict[str, Any]:
    fill_n = _utc_naive(fill_at)
    end = fill_n + timedelta(minutes=horizon_minutes)
    df = df_1m.sort_values("open_time").copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    if getattr(df["open_time"].dt, "tz", None) is not None:
        df["open_time"] = df["open_time"].dt.tz_convert("UTC").dt.tz_localize(None)

    after = df[(df["open_time"] + pd.Timedelta(minutes=1) > fill_n) & (df["open_time"] < end)]
    mfe = 0.0
    mae = 0.0
    for _, row in after.iterrows():
        h, l = float(row["high"]), float(row["low"])
        mfe = max(mfe, entry - l)  # short favorable
        mae = max(mae, h - entry)
        hit_tp = l <= target
        hit_sl = h >= stop
        bar_close = _utc_naive(row["open_time"].to_pydatetime()) + timedelta(minutes=1)
        if hit_tp and hit_sl:
            return {
                "result": "AMBIGUOUS",
                "exit_at": bar_close.isoformat(),
                "exit_price": None,
                "mfe": mfe,
                "mae": mae,
                "hold_minutes": (bar_close - fill_n).total_seconds() / 60.0,
            }
        if hit_sl:
            return {
                "result": "SL_FIRST",
                "exit_at": bar_close.isoformat(),
                "exit_price": stop,
                "mfe": mfe,
                "mae": mae,
                "hold_minutes": (bar_close - fill_n).total_seconds() / 60.0,
            }
        if hit_tp:
            return {
                "result": "TP_FIRST",
                "exit_at": bar_close.isoformat(),
                "exit_price": target,
                "mfe": mfe,
                "mae": mae,
                "hold_minutes": (bar_close - fill_n).total_seconds() / 60.0,
            }
    return {
        "result": "NEITHER",
        "exit_at": end.isoformat(),
        "exit_price": None,
        "mfe": mfe,
        "mae": mae,
        "hold_minutes": horizon_minutes,
    }


def pnl_from_short(
    *,
    entry: float,
    exit_price: float | None,
    result: str,
    stop: float,
    target: float,
    cost_pct: float = ROUNDTRIP_COST_PCT_BASELINE,
) -> dict[str, Any]:
    risk = stop - entry
    if exit_price is None:
        gross_pnl_pct = 0.0
        if result == "NEITHER":
            # mark flat / no close — treat as 0 for expectancy tables
            gross_pnl_pct = 0.0
        exit_used = entry
    else:
        exit_used = exit_price
        gross_pnl_pct = (entry - exit_used) / entry * 100.0

    net_pnl_pct = gross_pnl_pct - cost_pct
    gross_r = None
    net_r = None
    if risk > 0:
        if result == "TP_FIRST":
            gross_r = (entry - target) / risk
        elif result == "SL_FIRST":
            gross_r = (entry - stop) / risk
        elif exit_price is not None:
            gross_r = (entry - exit_price) / risk
        if gross_r is not None:
            # approximate net R by subtracting cost in R units
            cost_in_price = entry * cost_pct / 100.0
            net_r = (gross_r * risk - cost_in_price) / risk
    return {
        "gross_pnl_pct": gross_pnl_pct,
        "net_pnl_pct": net_pnl_pct,
        "gross_r": gross_r,
        "net_r": net_r,
        "cost_pct": cost_pct,
        "fees_slippage_pct": cost_pct,
    }


def evaluate_target_variants(
    df_1m: pd.DataFrame,
    *,
    fill_at: datetime,
    entry: float,
    stop: float,
    bid_info: dict[str, Any],
    cost_pct: float = ROUNDTRIP_COST_PCT_BASELINE,
) -> list[dict[str, Any]]:
    risk = stop - entry
    bids = bid_info.get("bid_pools") or []
    variants: list[tuple[str, float | None]] = []

    if bids:
        first = bids[0]
        variants.append(("A_first_bid_near_edge", float(first["upper_edge"])))
        variants.append(("B_first_bid_midpoint", float(first["midpoint"])))
        if len(bids) > 1:
            # next larger = farthest among top few by width*strength proxy → use second nearest
            variants.append(("C_next_larger_bid_near_edge", float(bids[1]["upper_edge"])))
        else:
            variants.append(("C_next_larger_bid_near_edge", None))
    else:
        variants.extend(
            [
                ("A_first_bid_near_edge", None),
                ("B_first_bid_midpoint", None),
                ("C_next_larger_bid_near_edge", None),
            ]
        )

    if risk > 0:
        variants.append(("D_1R", entry - 1.0 * risk))
        variants.append(("E_2R", entry - 2.0 * risk))
        variants.append(("F_3R", entry - 3.0 * risk))
    else:
        variants.extend([("D_1R", None), ("E_2R", None), ("F_3R", None)])

    out: list[dict[str, Any]] = []
    for name, target in variants:
        if target is None or target >= entry:
            out.append(
                {
                    "target_variant": name,
                    "target_price": target,
                    "result": "NO_TARGET",
                    "mfe": None,
                    "mae": None,
                    "hold_minutes": None,
                    **pnl_from_short(
                        entry=entry, exit_price=None, result="NO_TARGET", stop=stop, target=entry, cost_pct=cost_pct
                    ),
                }
            )
            continue
        touch = _first_touch_outcome(df_1m, fill_at=fill_at, entry=entry, stop=stop, target=target)
        pnl = pnl_from_short(
            entry=entry,
            exit_price=touch["exit_price"],
            result=touch["result"],
            stop=stop,
            target=target,
            cost_pct=cost_pct,
        )
        out.append({"target_variant": name, "target_price": target, **touch, **pnl})
    return out
