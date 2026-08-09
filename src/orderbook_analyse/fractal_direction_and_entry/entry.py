"""Fractal counterwave-failure + LTF realign entries (after regime)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_direction_and_entry import TF_PREFIX
from orderbook_analyse.fractal_direction_and_entry.regime import (
    structure_bear,
    structure_bull,
    wave_down_efficient,
    wave_up_efficient,
    wave_down_inefficient,
    wave_up_inefficient,
)


def _eq(s: pd.Series, val: str) -> pd.Series:
    return s.astype(str) == val


def _bool(s: pd.Series) -> pd.Series:
    return s.map(
        lambda x: True
        if str(x).lower() in ("1", "true", "yes")
        else (False if str(x).lower() in ("0", "false", "no") else False)
    ).fillna(False)


def _p(tf: str, col: str) -> str:
    return f"{TF_PREFIX[tf]}_{col}"


def counterwave_fail_long(df: pd.DataFrame) -> pd.Series:
    """15m DOWN inefficient with RSI/EMA still bullish."""
    tf = "15m"
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_ok = _bool(df[_p(tf, "rsi_end_gt_50")]) | (rsi > 50.0)
    ema_ok = structure_bull(df, tf)
    return wave_down_inefficient(df, tf) & rsi_ok & ema_ok


def counterwave_fail_short(df: pd.DataFrame) -> pd.Series:
    tf = "15m"
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_ok = _bool(df[_p(tf, "rsi_end_lt_50")]) | (rsi < 50.0)
    ema_ok = structure_bear(df, tf)
    return wave_up_inefficient(df, tf) & rsi_ok & ema_ok


def realign_up(df: pd.DataFrame, tf: str) -> pd.Series:
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_ok = _bool(df[_p(tf, "rsi_end_gt_50")]) | (rsi > 50.0)
    return wave_up_efficient(df, tf) & rsi_ok & structure_bull(df, tf)


def realign_down(df: pd.DataFrame, tf: str) -> pd.Series:
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_ok = _bool(df[_p(tf, "rsi_end_lt_50")]) | (rsi < 50.0)
    return wave_down_efficient(df, tf) & rsi_ok & structure_bear(df, tf)


def flag_entries(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    bull_reg = out["direction_state"].isin(["BULL", "STRONG_BULL"])
    bear_reg = out["direction_state"].isin(["BEAR", "STRONG_BEAR"])

    cw_l = counterwave_fail_long(out)
    cw_s = counterwave_fail_short(out)
    r_up = realign_up(out, "1m") | realign_up(out, "5m")
    r_dn = realign_down(out, "1m") | realign_down(out, "5m")

    out["cw_fail_long"] = cw_l
    out["cw_fail_short"] = cw_s
    out["realign_up"] = r_up
    out["realign_down"] = r_dn

    out["long_raw"] = bull_reg & cw_l & r_up
    out["short_raw"] = bear_reg & cw_s & r_dn

    # Baselines components
    out["baseline_bull_regime"] = bull_reg
    out["baseline_bear_regime"] = bear_reg
    out["baseline_bull_cw_no_realign"] = bull_reg & cw_l & ~r_up
    out["baseline_bear_cw_no_realign"] = bear_reg & cw_s & ~r_dn
    out["baseline_realign_up_no_htf"] = r_up & ~bull_reg
    out["baseline_realign_down_no_htf"] = r_dn & ~bear_reg

    # Episode key = 15m wave end_available_at (same counterwave episode)
    out["episode_key_15m"] = out[_p("15m", "end_available_at")].astype(str)
    return out


def dedupe_entry_episodes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Episode definition:
      An entry episode is identified by (side, 15m_wave.end_available_at).
      Within the same 15m as-of wave, only the FIRST 5m bar where the full
      entry condition becomes true is kept as an independent signal.
      A new signal of the same side is allowed only after the 15m as-of wave
      changes (new end_available_at).
    """
    out = df.copy()
    out["long_entry"] = False
    out["short_entry"] = False

    # vectorized first-True per group
    long_idx = out.index[out["long_raw"].fillna(False)]
    if len(long_idx):
        keys = out.loc[long_idx, "episode_key_15m"]
        first = ~keys.duplicated(keep="first")
        out.loc[long_idx[first.to_numpy()], "long_entry"] = True

    short_idx = out.index[out["short_raw"].fillna(False)]
    if len(short_idx):
        keys = out.loc[short_idx, "episode_key_15m"]
        first = ~keys.duplicated(keep="first")
        out.loc[short_idx[first.to_numpy()], "short_entry"] = True

    return out


EPISODE_DOC = """
Entry episode dedupe:
  key = (side, M15_end_available_at)
  Keep only the first 5m decision_time where the full entry fires
  for that 15m counterwave episode. No new same-side signal until
  the causal 15m as-of wave changes.
"""
