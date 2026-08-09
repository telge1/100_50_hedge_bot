"""Fast 1m BE50 path books (numpy) wrapping July simulate logic."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_july_2026.simulate import (
    simulate_be50_trade,
    trade_levels,
)


def prepare_book(c1m: pd.DataFrame) -> dict[str, Any]:
    ts = pd.to_datetime(c1m["timestamp"], utc=True)
    return {
        "df": c1m.reset_index(drop=True),
        "ts_ns": ts.astype("int64").to_numpy(),
    }


def simulate_be50_trade_fast(tr: pd.Series, book: dict[str, Any], levels: dict[str, float]) -> dict[str, Any]:
    """Slice the preloaded book around the trade then reuse causal simulator."""
    et = pd.Timestamp(tr["entry_time"])
    if et.tzinfo is None:
        et = et.tz_localize("UTC")
    else:
        et = et.tz_convert("UTC")
    xt_cap = pd.Timestamp(tr["exit_time"])
    if xt_cap.tzinfo is None:
        xt_cap = xt_cap.tz_localize("UTC")
    else:
        xt_cap = xt_cap.tz_convert("UTC")
    xt_cap = xt_cap + pd.Timedelta(days=10)

    ts_ns = book["ts_ns"]
    i0 = int(np.searchsorted(ts_ns, et.value, side="left"))
    i1 = int(np.searchsorted(ts_ns, xt_cap.value, side="right"))
    sub = book["df"].iloc[i0:i1].reset_index(drop=True)
    return simulate_be50_trade(tr, sub, levels)


__all__ = ["prepare_book", "simulate_be50_trade_fast", "trade_levels"]
