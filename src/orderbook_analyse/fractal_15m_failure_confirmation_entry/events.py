"""Load confirmation events and 1m path helpers."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_confirmation_entry import (
    EARLY_SNAPSHOTS,
    FAILURE_EVENTS,
    SYMBOL,
    WAVE_15M,
    WAVE_DIR,
)
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf


def load_confirmation_events() -> pd.DataFrame:
    fe = pd.read_csv(FAILURE_EVENTS)
    fe["decision_time"] = pd.to_datetime(fe["decision_time"], utc=True)
    fe["confirmation_available_at"] = fe["decision_time"]
    fe["side"] = np.where(fe["failure_type"] == "FAILED_UP_WAVE", "SHORT", "LONG")
    fe["expected_reversal"] = np.where(fe["side"] == "SHORT", "DOWN", "UP")

    w = pd.read_csv(
        WAVE_15M,
        usecols=[
            "start_available_at",
            "end_available_at",
            "n_bars",
            "favorable_move_pct",
            "adverse_move_pct",
            "price_move_pct",
            "stoch_delta",
        ],
    )
    w["start_available_at"] = pd.to_datetime(w["start_available_at"], utc=True)
    w["end_available_at"] = pd.to_datetime(w["end_available_at"], utc=True)
    w["wave_i"] = np.arange(len(w), dtype=np.int64)
    w = w.rename(
        columns={
            "start_available_at": "wave_start_available_at",
            "end_available_at": "wave_end_available_at",
            "favorable_move_pct": "M15_favorable_move_pct",
            "adverse_move_pct": "M15_adverse_move_pct",
            "price_move_pct": "M15_price_move_pct",
            "stoch_delta": "M15_stoch_delta",
            "n_bars": "M15_n_bars",
        }
    )
    out = fe.merge(w, on="wave_i", how="left")
    out["wave_duration_min"] = (
        out["wave_end_available_at"] - out["wave_start_available_at"]
    ).dt.total_seconds() / 60.0

    # persistence known by confirmation (from early-detection snapshots)
    snap = pd.read_csv(
        EARLY_SNAPSHOTS,
        usecols=["wave_i", "max_partial_fail_streak_1m", "is_later_failure"],
    )
    pers = (
        snap[snap["is_later_failure"]]
        .groupby("wave_i", sort=False)["max_partial_fail_streak_1m"]
        .max()
        .rename("partial_fail_streak_1m")
    )
    out = out.merge(pers, left_on="wave_i", right_index=True, how="left")
    out["symbol"] = SYMBOL
    return out.reset_index(drop=True)


def load_1m_ohlcv(*, symbol: str = SYMBOL) -> pd.DataFrame:
    raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe="1m")
    df = raw.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    return df


def load_micro_waves(tf: str) -> pd.DataFrame:
    path = Path(WAVE_DIR) / f"waves_{tf}.csv"
    df = pd.read_csv(
        path,
        usecols=[
            "direction",
            "signed_price_move_pct",
            "directional_efficiency",
            "rsi_end",
            "stoch_zone_end",
            "stoch_state_end",
            "end_available_at",
            "start_available_at",
        ],
    )
    for c in ("end_available_at", "start_available_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df.sort_values("end_available_at").reset_index(drop=True)


def first_open_after(
    open_times: np.ndarray,
    opens: np.ndarray,
    decision_time: np.datetime64,
) -> tuple[int, float, np.datetime64]:
    """First 1m bar with open_time > decision_time; return (idx, open, open_time)."""
    j = int(np.searchsorted(open_times, decision_time, side="right"))
    if j >= len(open_times):
        return -1, np.nan, np.datetime64("NaT")
    return j, float(opens[j]), open_times[j]
