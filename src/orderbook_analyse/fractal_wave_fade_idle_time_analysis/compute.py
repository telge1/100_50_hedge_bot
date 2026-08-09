"""Compute idle gaps and capacity statistics."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.periods import (
    halfyear_label,
    months_in_period,
)


BUCKETS: list[tuple[str, float, float]] = [
    ("<15min", 0.0, 15.0),
    ("15-30min", 15.0, 30.0),
    ("30-60min", 30.0, 60.0),
    ("1-2h", 60.0, 120.0),
    ("2-3h", 120.0, 180.0),
    ("3-6h", 180.0, 360.0),
    ("6-12h", 360.0, 720.0),
    ("12-24h", 720.0, 1440.0),
    ("1-2d", 1440.0, 2880.0),
    ("2-3d", 2880.0, 4320.0),
    (">3d", 4320.0, float("inf")),
]

CUM_THRESHOLDS_MIN = {
    "within_15min": 15.0,
    "within_30min": 30.0,
    "within_1h": 60.0,
    "within_3h": 180.0,
    "within_6h": 360.0,
    "within_12h": 720.0,
    "within_24h": 1440.0,
}


def load_trades(path) -> pd.DataFrame:
    t = pd.read_csv(path)
    t["entry_time"] = pd.to_datetime(t["entry_time"], utc=True)
    t["exit_time"] = pd.to_datetime(t["exit_time"], utc=True)
    t = t.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    return t


def build_gaps(trades: pd.DataFrame) -> pd.DataFrame:
    if len(trades) < 2:
        return pd.DataFrame()
    prev = trades.iloc[:-1].reset_index(drop=True)
    nxt = trades.iloc[1:].reset_index(drop=True)
    idle_min = (nxt["entry_time"] - prev["exit_time"]).dt.total_seconds() / 60.0
    if (idle_min <= 0).any():
        bad = int((idle_min <= 0).sum())
        raise AssertionError(f"found {bad} non-positive idle gaps (overlap / same-minute entry)")

    gaps = pd.DataFrame(
        {
            "gap_id": np.arange(1, len(nxt) + 1, dtype=int),
            "previous_trade_id": prev["trade_id"].astype(int).values,
            "previous_symbol": prev["symbol"].astype(str).values,
            "previous_side": prev["side"].astype(str).values,
            "previous_exit_time": prev["exit_time"].values,
            "previous_exit_reason": prev["exit_reason"].astype(str).values,
            "next_trade_id": nxt["trade_id"].astype(int).values,
            "next_symbol": nxt["symbol"].astype(str).values,
            "next_side": nxt["side"].astype(str).values,
            "next_entry_time": nxt["entry_time"].values,
            "next_first_signal_tf": nxt["first_signal_tf"].astype(str).values,
            "idle_minutes": idle_min.astype(float).values,
        }
    )
    gaps["idle_hours"] = gaps["idle_minutes"] / 60.0
    gaps["idle_days"] = gaps["idle_hours"] / 24.0
    gaps["previous_exit_time"] = pd.to_datetime(gaps["previous_exit_time"], utc=True)
    gaps["next_entry_time"] = pd.to_datetime(gaps["next_entry_time"], utc=True)
    gaps["period"] = gaps["next_entry_time"].map(halfyear_label)
    gaps["month"] = gaps["next_entry_time"].dt.strftime("%Y-%m")
    return gaps


def _pctiles(x: np.ndarray) -> dict[str, float]:
    qs = [25, 50, 75, 90, 95, 99]
    out = {f"p{q}": float(np.percentile(x, q)) for q in qs}
    out["mean"] = float(np.mean(x))
    out["median"] = float(np.median(x))
    out["min"] = float(np.min(x))
    out["max"] = float(np.max(x))
    return out


def basic_stats(gaps: pd.DataFrame) -> dict[str, Any]:
    m = gaps["idle_minutes"].astype(float).to_numpy()
    h = gaps["idle_hours"].astype(float).to_numpy()
    pm = _pctiles(m)
    ph = _pctiles(h)
    return {
        "n_gaps": int(len(gaps)),
        "minutes": pm,
        "hours": ph,
    }


def bucket_stats(gaps: pd.DataFrame) -> pd.DataFrame:
    m = gaps["idle_minutes"].astype(float).to_numpy()
    n = len(m)
    rows = []
    for label, lo, hi in BUCKETS:
        if hi == float("inf"):
            mask = m >= lo
        else:
            mask = (m >= lo) & (m < hi)
        c = int(mask.sum())
        rows.append({"bucket": label, "n": c, "share_pct": float(100.0 * c / n) if n else None})
    return pd.DataFrame(rows)


def cumulative_within(gaps: pd.DataFrame) -> dict[str, Any]:
    m = gaps["idle_minutes"].astype(float).to_numpy()
    n = len(m)
    out = {}
    for name, thr in CUM_THRESHOLDS_MIN.items():
        c = int((m < thr).sum())  # "within X" = strictly before X? user said "innerhalb" → typically <=
        # use <= for "within"
        c = int((m <= thr).sum())
        out[name] = {"n": c, "share_pct": float(100.0 * c / n) if n else None}
    return out


def longest_idle(gaps: pd.DataFrame, n: int = 25) -> pd.DataFrame:
    cols = [
        "previous_trade_id",
        "previous_symbol",
        "previous_side",
        "previous_exit_time",
        "previous_exit_reason",
        "next_trade_id",
        "next_symbol",
        "next_side",
        "next_entry_time",
        "next_first_signal_tf",
        "idle_minutes",
        "idle_hours",
        "idle_days",
    ]
    return gaps.nlargest(n, "idle_minutes")[cols].reset_index(drop=True)


def by_halfyear(gaps: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    data_start = trades["entry_time"].min()
    data_end = trades["exit_time"].max()
    rows = []
    for period, g in gaps.groupby("period", sort=True):
        h = g["idle_hours"].astype(float)
        n_tr = int((trades["exit_time"].map(halfyear_label) == period).sum())
        # also count by entry period for trades in that block
        n_tr_entry = int((trades["entry_time"].map(halfyear_label) == period).sum())
        rows.append(
            {
                "period": period,
                "trades": n_tr_entry,
                "n_gaps": int(len(g)),
                "months_covered": months_in_period(
                    period, data_start=data_start, data_end=data_end
                ),
                "mean_idle_hours": float(h.mean()),
                "median_idle_hours": float(h.median()),
                "p90_idle_hours": float(np.percentile(h, 90)),
                "max_idle_hours": float(h.max()),
                "share_idle_lt_1h": float((h < 1.0).mean()),
                "share_idle_lt_3h": float((h < 3.0).mean()),
                "share_idle_lt_6h": float((h < 6.0).mean()),
            }
        )
    return pd.DataFrame(rows)


def by_month(gaps: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    t = trades.copy()
    t["month"] = t["entry_time"].dt.strftime("%Y-%m")
    trade_counts = t.groupby("month").size().rename("trades")
    rows = []
    for month, g in gaps.groupby("month", sort=True):
        h = g["idle_hours"].astype(float)
        rows.append(
            {
                "month": month,
                "trades": int(trade_counts.get(month, 0)),
                "n_gaps": int(len(g)),
                "mean_idle_hours": float(h.mean()),
                "median_idle_hours": float(h.median()),
                "max_idle_hours": float(h.max()),
            }
        )
    return pd.DataFrame(rows)


def time_budget(trades: pd.DataFrame, gaps: pd.DataFrame) -> dict[str, Any]:
    hold_min = (trades["exit_time"] - trades["entry_time"]).dt.total_seconds() / 60.0
    hold_min = hold_min.astype(float)
    idle_min = gaps["idle_minutes"].astype(float)

    t0 = trades["entry_time"].min()
    t1 = trades["exit_time"].max()
    total_min = (t1 - t0).total_seconds() / 60.0

    time_in_market = float(hold_min.sum())
    flat_idle = float(idle_min.sum())
    # residual: before first entry already at t0; after last exit included in span end
    # Check: holding + idle should ≈ total (first entry to last exit)
    accounted = time_in_market + flat_idle
    residual = float(total_min - accounted)

    return {
        "backtest_start": t0,
        "backtest_end": t1,
        "total_span_minutes": float(total_min),
        "total_span_hours": float(total_min / 60.0),
        "total_span_days": float(total_min / 60.0 / 24.0),
        "time_in_market_minutes": time_in_market,
        "time_in_market_hours": time_in_market / 60.0,
        "flat_idle_minutes": flat_idle,
        "flat_idle_hours": flat_idle / 60.0,
        "time_in_market_pct": float(100.0 * time_in_market / total_min) if total_min else None,
        "flat_idle_pct": float(100.0 * flat_idle / total_min) if total_min else None,
        "accounted_minutes": accounted,
        "residual_minutes": residual,
        "mean_holding_hours": float(hold_min.mean() / 60.0),
        "median_holding_hours": float(hold_min.median() / 60.0),
        "mean_idle_hours": float(idle_min.mean() / 60.0),
        "median_idle_hours": float(idle_min.median() / 60.0),
        "holding_time_over_total": float(time_in_market / total_min) if total_min else None,
        "idle_time_over_total": float(flat_idle / total_min) if total_min else None,
    }


def capacity_assessment(budget: dict[str, Any], cum: dict[str, Any]) -> dict[str, Any]:
    flat_pct = budget.get("flat_idle_pct") or 0.0
    tim_pct = budget.get("time_in_market_pct") or 0.0
    within_6 = cum.get("within_6h", {}).get("share_pct") or 0.0
    within_24 = cum.get("within_24h", {}).get("share_pct") or 0.0
    med_idle = budget.get("median_idle_hours") or 0.0

    # Long droughts vs short fragmented waits
    mostly_short_waits = within_24 >= 95 and med_idle <= 3.0

    if flat_pct >= 70:
        level = "HIGH_UNUSED_TIME"
    elif flat_pct >= 35:
        level = "MODERATE_UNUSED_TIME"
    else:
        level = "LOW_UNUSED_TIME"

    if mostly_short_waits and flat_pct < 50:
        note = (
            f"About {flat_pct:.0f}% of calendar time is flat, but waits are short "
            f"(median idle ~{med_idle:.1f}h; {within_6:.0f}% of next entries within 6h, "
            f"{within_24:.0f}% within 24h). Extra independent symbols could fill some of "
            f"these gaps and raise utilization, yet the headroom is fragmented — not "
            f"multi-day drought capacity. This is not a claim that N coins ⇒ N× profit."
        )
        if flat_pct < 40:
            level = "MODERATE_FRAGMENTED_IDLE"
    elif flat_pct >= 70:
        note = (
            "Most calendar time is flat. Additional independent symbols could "
            "theoretically raise capital utilization, but profits would not scale "
            "linearly with coin count (correlation, shared regimes, global risk)."
        )
    else:
        note = (
            "Substantial flat time exists; multi-symbol expansion has room on a "
            "time-utilization basis only — not a profit multiplier claim."
        )

    return {
        "unused_time_level": level,
        "flat_idle_pct": flat_pct,
        "time_in_market_pct": tim_pct,
        "share_next_trade_within_6h": within_6,
        "share_next_trade_within_24h": within_24,
        "descriptive_note": note,
        "disclaimer": (
            "No multi-coin simulation. Unused flat time ≠ guaranteed extra edge. "
            "N coins do not imply N× profit."
        ),
    }
