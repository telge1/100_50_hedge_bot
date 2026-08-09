"""Load all-wave fade events with existing failure annotations."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade import EXTRA_WAVE_COLS, SYMBOL, WAVE_DIR
from orderbook_analyse.fractal_cycle_phase_failure import WAVE_COLS
from orderbook_analyse.fractal_cycle_phase_failure.events import local_failure_mask


def _bool(s: pd.Series) -> pd.Series:
    return s.map(
        lambda x: True
        if str(x).lower() in ("1", "true", "yes")
        else (False if str(x).lower() in ("0", "false", "no") else False)
    )


def load_all_waves(tf: str, wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    path = Path(wave_dir) / f"waves_{tf}.csv"
    header = pd.read_csv(path, nrows=0).columns.tolist()
    want = list(dict.fromkeys([*WAVE_COLS, *EXTRA_WAVE_COLS]))
    cols = [c for c in want if c in header]
    df = pd.read_csv(path, usecols=cols)
    for c in ("end_available_at", "start_available_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    for c in ("rsi_end_gt_50", "rsi_end_lt_50", "inefficient_flag"):
        if c in df.columns:
            df[c] = _bool(df[c])
    df = df.sort_values("end_available_at").reset_index(drop=True)
    df["wave_i"] = np.arange(len(df), dtype=np.int64)
    df["timeframe"] = tf
    df["symbol"] = SYMBOL

    # previous wave fields (immediate prior row = chronological prior wave)
    df["prev_direction"] = df["direction"].shift(1)
    df["prev_directional_efficiency"] = df["directional_efficiency"].shift(1)
    df["prev_signed_price_move_pct"] = df["signed_price_move_pct"].shift(1)
    if "n_bars" in df.columns:
        df["prev_n_bars"] = df["n_bars"].shift(1)
    else:
        df["prev_n_bars"] = np.nan
    df["prev_duration_min"] = (
        (df["end_available_at"] - df["start_available_at"]).dt.total_seconds() / 60.0
    ).shift(1)

    fail_up, fail_dn = local_failure_mask(df)
    df["is_failed"] = (fail_up | fail_dn).to_numpy()
    df["failure_type"] = np.where(
        fail_up,
        "FAILED_UP_WAVE",
        np.where(fail_dn, "FAILED_DOWN_WAVE", "NON_FAILED"),
    )
    df["wave_group"] = np.where(df["is_failed"], "FAILED", "NON_FAILED")

    # All-wave fade hypothesis
    df["expected_reversal"] = np.where(df["direction"].astype(str) == "UP", "DOWN", "UP")
    df["side"] = np.where(df["expected_reversal"] == "DOWN", "SHORT", "LONG")
    df["confirmation_available_at"] = df["end_available_at"]

    # previous opposite relational groups (fixed, no optimized threshold)
    prev_opp = (
        df["prev_direction"].notna()
        & (df["prev_direction"].astype(str) != df["direction"].astype(str))
    )
    cur_e = df["directional_efficiency"].astype(float)
    prev_e = df["prev_directional_efficiency"].astype(float)
    rel = pd.Series("NO_PREV_OPPOSITE", index=df.index, dtype=object)
    both = prev_opp & np.isfinite(cur_e) & np.isfinite(prev_e)
    rel.loc[both & (cur_e < prev_e)] = "CURRENT_WEAKER_THAN_PREVIOUS"
    rel.loc[both & (cur_e > prev_e)] = "CURRENT_STRONGER_THAN_PREVIOUS"
    rel.loc[both & (cur_e == prev_e)] = "SIMILAR"
    df["prev_rel_efficiency"] = rel

    # EMA context buckets (existing labels only)
    pve = df["price_vs_ema20_end"].astype(str)
    e9 = df["ema9_vs_ema20_end"].astype(str)
    ema = pd.Series("MIXED", index=df.index, dtype=object)
    ema.loc[(pve == "ABOVE") & (e9 == "BULL")] = "EMA_BULL"
    ema.loc[(pve == "BELOW") & (e9 == "BEAR")] = "EMA_BEAR"
    df["ema_context"] = ema

    # RSI buckets
    rsi = df["rsi_end"].astype(float)
    rb = pd.Series("NA", index=df.index, dtype=object)
    rb.loc[rsi < 40] = "lt40"
    rb.loc[(rsi >= 40) & (rsi < 50)] = "40_50"
    rb.loc[(rsi >= 50) & (rsi <= 60)] = "50_60"
    rb.loc[rsi > 60] = "gt60"
    df["rsi_bucket"] = rb
    df["rsi_delta_sign"] = np.where(
        df["rsi_delta"].astype(float) > 0,
        "POS",
        np.where(df["rsi_delta"].astype(float) < 0, "NEG", "ZERO"),
    )

    # duration buckets by n_bars
    nb = df["n_bars"].astype(float) if "n_bars" in df.columns else pd.Series(np.nan, index=df.index)
    dur = pd.Series("NA", index=df.index, dtype=object)
    dur.loc[(nb >= 1) & (nb <= 2)] = "1-2"
    dur.loc[(nb >= 3) & (nb <= 4)] = "3-4"
    dur.loc[(nb >= 5) & (nb <= 8)] = "5-8"
    dur.loc[(nb >= 9) & (nb <= 16)] = "9-16"
    dur.loc[nb > 16] = ">16"
    df["duration_bucket"] = dur

    df["stoch_path"] = (
        df["stoch_zone_start"].astype(str) + "->" + df["stoch_zone_end"].astype(str)
    )
    return df.reset_index(drop=True)
