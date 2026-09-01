"""Causal MTF snapshots via Gold aggregation and indicators."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import DASHBOARD_ROOT, SNAPSHOT_TFS

import sys

if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))

from research.stoch_fade_trade_context_analysis.pipeline import (
    aggregate_complete,
    enrich_frame,
    last_closed_index,
    snapshot_row,
    to_utc,
    to_utc_ns,
)

SNAP_FIELDS = (
    "source_bar_open",
    "source_bar_close",
    "available_at",
    "available_at_le_entry",
    "snapshot_missing",
    "open",
    "high",
    "low",
    "close",
    "rsi",
    "stoch_rsi_raw",
    "stoch_k",
    "stoch_d",
    "stoch_k_minus_d",
    "stoch_k_prev",
    "stoch_d_prev",
    "cross_up",
    "cross_down",
    "stoch_phase",
    "ema20",
    "ema50",
    "ema200",
    "ema_stack",
    "ema_trend",
    "close_vs_ema20",
    "close_vs_ema50",
    "close_vs_ema200",
    "ema20_slope_3",
    "ema50_slope_3",
    "range20_pos_close",
)


def build_tf_frames(c1m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    one = c1m.copy()
    one["timestamp"] = pd.to_datetime(one["open_time"], utc=True)
    one["available_at"] = pd.to_datetime(one["close_time"], utc=True)
    frames["1m"] = enrich_frame(one)
    as_of = to_utc(c1m["close_time"].iloc[-1])
    for tf in SNAPSHOT_TFS:
        if tf == "1m":
            continue
        agg, _audit = aggregate_complete(c1m, tf, as_of=as_of)
        frames[tf] = enrich_frame(agg)
    return frames


def snapshots_for_trade(
    frames: dict[str, pd.DataFrame],
    avail: dict[str, np.ndarray],
    *,
    entry: pd.Timestamp,
    entry_price: float,
    direction: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lookahead = False
    for tf in SNAPSHOT_TFS:
        snap = snapshot_row(
            tf=tf,
            frame=frames[tf],
            avail=avail[tf],
            entry=entry,
            entry_price=entry_price,
            direction=direction,
        )
        if snap.get("available_at_le_entry") is False:
            lookahead = True
        for field in SNAP_FIELDS:
            out[f"tf_{tf}_{field}"] = snap.get(field)
    out["lookahead"] = lookahead
    return out
