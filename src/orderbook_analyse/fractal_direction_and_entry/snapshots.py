"""Causal 5m decision grid + multi-TF wave as-of joins."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_direction_and_entry import (
    ALL_JOIN_TFS,
    SYMBOL,
    TF_PREFIX,
    WAVE_DIR,
    WAVE_FEATURE_COLS,
)
from orderbook_analyse.fractal_directional_control.load_join import asof_last_completed


def _bool_map(s: pd.Series) -> pd.Series:
    return s.map(
        lambda x: True
        if str(x).lower() in ("1", "true", "yes")
        else (False if str(x).lower() in ("0", "false", "no") else False)
    )


def load_waves(tf: str, wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    path = Path(wave_dir) / f"waves_{tf}.csv"
    usecols = [c for c in WAVE_FEATURE_COLS if True]
    # ensure columns exist
    header = pd.read_csv(path, nrows=0).columns.tolist()
    cols = [c for c in usecols if c in header]
    df = pd.read_csv(path, usecols=cols)
    for c in ("end_available_at", "start_available_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    for c in ("rsi_end_gt_50", "rsi_end_lt_50", "inefficient_flag"):
        if c in df.columns:
            df[c] = _bool_map(df[c])
    return df.sort_values("end_available_at").reset_index(drop=True)


def load_decision_grid_5m(*, symbol: str = SYMBOL) -> pd.DataFrame:
    """Every closed 5m candle; decision_time = available_at (close)."""
    raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe="5m")
    df = raw.rename(columns={"available_at": "decision_time"}).copy()
    df["decision_time"] = pd.to_datetime(df["decision_time"], utc=True)
    df = df.sort_values("decision_time").drop_duplicates("decision_time").reset_index(drop=True)
    df["symbol"] = symbol
    df["grid_i"] = np.arange(len(df), dtype=np.int64)
    return df


def attach_tf_states(grid: pd.DataFrame, wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    times = grid["decision_time"].to_numpy(dtype="datetime64[ns]")
    frames = [grid.reset_index(drop=True)]
    for tf in ALL_JOIN_TFS:
        print(f"[join] {tf}", flush=True)
        waves = load_waves(tf, wave_dir)
        prefix = TF_PREFIX[tf]
        cols = [c for c in WAVE_FEATURE_COLS if c in waves.columns]
        joined = asof_last_completed(waves, times, cols, prefix)
        frames.append(joined)
    return pd.concat(frames, axis=1)
