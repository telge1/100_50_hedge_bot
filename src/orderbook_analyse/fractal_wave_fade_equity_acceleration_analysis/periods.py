"""Half-year period labeling."""

from __future__ import annotations

import pandas as pd


def halfyear_label(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    y = int(t.year)
    h = "H1" if t.month <= 6 else "H2"
    return f"{y}-{h}"


def period_bounds(label: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    y, h = label.split("-")
    year = int(y)
    if h == "H1":
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        end = pd.Timestamp(f"{year}-06-30 23:59:59", tz="UTC")
    else:
        start = pd.Timestamp(f"{year}-07-01", tz="UTC")
        end = pd.Timestamp(f"{year}-12-31 23:59:59", tz="UTC")
    return start, end


def months_in_period(
    label: str,
    *,
    data_start: pd.Timestamp,
    data_end: pd.Timestamp,
) -> float:
    """Covered months within [data_start, data_end] ∩ period (fractional OK)."""
    ps, pe = period_bounds(label)
    a = max(ps, pd.Timestamp(data_start))
    b = min(pe, pd.Timestamp(data_end))
    if b < a:
        return 0.0
    days = (b - a).total_seconds() / 86400.0 + 1.0 / 86400.0
    return max(days / 30.4375, 1e-9)


def assign_periods(trades: pd.DataFrame) -> pd.DataFrame:
    out = trades.copy()
    out["exit_time"] = pd.to_datetime(out["exit_time"], utc=True)
    out["entry_time"] = pd.to_datetime(out["entry_time"], utc=True)
    out["period"] = out["exit_time"].map(halfyear_label)
    return out
