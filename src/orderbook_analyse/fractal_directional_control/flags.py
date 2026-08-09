"""Fixed logical flags for directional control / CCI (no threshold search)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_directional_control import CCI_STRONG, WEAK_PRICE_ABS


def _eq(series: pd.Series, val: str) -> pd.Series:
    return series.astype(str) == val


def regime_bull_1d(df: pd.DataFrame) -> pd.Series:
    """Primary 1d bullish context (fixed)."""
    return (
        _eq(df.get("d1_direction", pd.Series(index=df.index)), "UP")
        | (
            df.get("d1_rsi_end_gt_50", pd.Series(False, index=df.index)).fillna(False).astype(bool)
            & _eq(df.get("d1_ema9_vs_ema20_end", pd.Series(index=df.index)), "BULL")
        )
    )


def regime_bear_1d(df: pd.DataFrame) -> pd.Series:
    return (
        _eq(df.get("d1_direction", pd.Series(index=df.index)), "DOWN")
        | (
            (~df.get("d1_rsi_end_gt_50", pd.Series(True, index=df.index)).fillna(True).astype(bool))
            & _eq(df.get("d1_ema9_vs_ema20_end", pd.Series(index=df.index)), "BEAR")
        )
    )


def ctx_bull_4h1h(df: pd.DataFrame) -> pd.Series:
    return (
        _eq(df.get("h4_direction", pd.Series(index=df.index)), "UP")
        | _eq(df.get("h1_direction", pd.Series(index=df.index)), "UP")
        | (
            _eq(df.get("h4_ema9_vs_ema20_end", pd.Series(index=df.index)), "BULL")
            & _eq(df.get("h4_price_vs_ema20_end", pd.Series(index=df.index)), "ABOVE")
        )
        | (
            _eq(df.get("h1_ema9_vs_ema20_end", pd.Series(index=df.index)), "BULL")
            & _eq(df.get("h1_price_vs_ema20_end", pd.Series(index=df.index)), "ABOVE")
        )
    )


def ctx_bear_4h1h(df: pd.DataFrame) -> pd.Series:
    return (
        _eq(df.get("h4_direction", pd.Series(index=df.index)), "DOWN")
        | _eq(df.get("h1_direction", pd.Series(index=df.index)), "DOWN")
        | (
            _eq(df.get("h4_ema9_vs_ema20_end", pd.Series(index=df.index)), "BEAR")
            & _eq(df.get("h4_price_vs_ema20_end", pd.Series(index=df.index)), "BELOW")
        )
        | (
            _eq(df.get("h1_ema9_vs_ema20_end", pd.Series(index=df.index)), "BEAR")
            & _eq(df.get("h1_price_vs_ema20_end", pd.Series(index=df.index)), "BELOW")
        )
    )


def ctx_bearish_or_weak(df: pd.DataFrame) -> pd.Series:
    """4h/1h bearish or weak (not clearly bullish)."""
    h4_weak = ~_eq(df.get("h4_direction", pd.Series(index=df.index)), "UP")
    h1_weak = ~_eq(df.get("h1_direction", pd.Series(index=df.index)), "UP")
    return regime_bear_1d(df) & h4_weak & h1_weak


def ctx_bullish_or_weak(df: pd.DataFrame) -> pd.Series:
    h4_weak = ~_eq(df.get("h4_direction", pd.Series(index=df.index)), "DOWN")
    h1_weak = ~_eq(df.get("h1_direction", pd.Series(index=df.index)), "DOWN")
    return regime_bull_1d(df) & h4_weak & h1_weak


def inefficient_down_in_bull(df: pd.DataFrame) -> pd.Series:
    """BULL CONTROL setup: DOWN wave fails under bullish regime/context."""
    bull = regime_bull_1d(df) | ctx_bull_4h1h(df)
    down = _eq(df["direction"], "DOWN")
    rsi_ok = df["rsi_end_gt_50"].fillna(False).astype(bool)
    ema_ok = _eq(df["ema9_vs_ema20_end"], "BULL") & _eq(df["price_vs_ema20_end"], "ABOVE")
    # signed <= 0 => price did not fall with DOWN (or rose)
    price_fail = df["signed_price_move_pct"].astype(float) <= 0.0
    barely = df["price_move_pct"].astype(float).abs() <= WEAK_PRICE_ABS
    return bull & down & rsi_ok & ema_ok & (price_fail | barely)


def inefficient_up_in_bear(df: pd.DataFrame) -> pd.Series:
    """BEAR CONTROL setup: UP wave fails under bearish regime/context."""
    bear = regime_bear_1d(df) | ctx_bear_4h1h(df)
    up = _eq(df["direction"], "UP")
    rsi_ok = df["rsi_end_lt_50"].fillna(False).astype(bool)
    ema_ok = _eq(df["ema9_vs_ema20_end"], "BEAR") & _eq(df["price_vs_ema20_end"], "BELOW")
    price_fail = df["signed_price_move_pct"].astype(float) <= 0.0
    barely = df["price_move_pct"].astype(float).abs() <= WEAK_PRICE_ABS
    return bear & up & rsi_ok & ema_ok & (price_fail | barely)


def realign_bear_setup(df: pd.DataFrame) -> pd.Series:
    """1d bear + 4h/1h not UP; inefficient UP; next is DOWN."""
    setup = ctx_bearish_or_weak(df)
    up = _eq(df["direction"], "UP")
    rsi_ok = df["rsi_end_lt_50"].fillna(False).astype(bool)
    ema_ok = _eq(df["ema9_vs_ema20_end"], "BEAR")
    weak_eff = df["directional_efficiency"].astype(float) <= 0.0
    weak_price = df["signed_price_move_pct"].astype(float) <= 0.0
    return setup & up & rsi_ok & ema_ok & (weak_eff | weak_price)


def realign_bull_setup(df: pd.DataFrame) -> pd.Series:
    setup = ctx_bullish_or_weak(df)
    down = _eq(df["direction"], "DOWN")
    rsi_ok = df["rsi_end_gt_50"].fillna(False).astype(bool)
    ema_ok = _eq(df["ema9_vs_ema20_end"], "BULL")
    weak_eff = df["directional_efficiency"].astype(float) <= 0.0
    weak_price = df["signed_price_move_pct"].astype(float) <= 0.0
    return setup & down & rsi_ok & ema_ok & (weak_eff | weak_price)


def cci_extreme_for_wave(df: pd.DataFrame) -> pd.Series:
    """Relevant CCI extreme at wave end for turn analysis."""
    up = _eq(df["direction"], "UP")
    # UP end → positive extreme; DOWN end → |negative| extreme
    pos = df["cci_strongest_pos"].astype(float)
    neg = df["cci_strongest_neg"].astype(float).abs()
    return pd.Series(np.where(up, pos, neg), index=df.index)


def cci_bucket(abs_extreme: pd.Series) -> pd.Series:
    x = abs_extreme.astype(float)
    out = pd.Series("na", index=x.index)
    out[(x >= 0) & (x < 100)] = "lt100"
    out[(x >= 100) & (x < 150)] = "100_150"
    out[(x >= 150) & (x < 200)] = "150_200"
    out[(x >= 200) & (x < 300)] = "200_300"
    out[x >= 300] = "gt300"
    out[~np.isfinite(x)] = "na"
    return out


def bear_reversal_candidate(df: pd.DataFrame, *, require_strong_cci: bool) -> pd.Series:
    up = _eq(df["direction"], "UP")
    weak = (df["signed_price_move_pct"].astype(float) <= 0.0) | (
        df["price_move_pct"].astype(float).abs() <= WEAK_PRICE_ABS
    )
    rsi_ok = df["rsi_end_lt_50"].fillna(False).astype(bool)
    ema_ok = _eq(df["ema9_vs_ema20_end"], "BEAR")
    base = up & weak & rsi_ok & ema_ok
    if not require_strong_cci:
        return base
    extreme = df["cci_strongest_pos"].astype(float) >= CCI_STRONG
    return base & extreme


def bull_reversal_candidate(df: pd.DataFrame, *, require_strong_cci: bool) -> pd.Series:
    down = _eq(df["direction"], "DOWN")
    weak = (df["signed_price_move_pct"].astype(float) <= 0.0) | (
        df["price_move_pct"].astype(float).abs() <= WEAK_PRICE_ABS
    )
    rsi_ok = df["rsi_end_gt_50"].fillna(False).astype(bool)
    ema_ok = _eq(df["ema9_vs_ema20_end"], "BULL")
    base = down & weak & rsi_ok & ema_ok
    if not require_strong_cci:
        return base
    extreme = df["cci_strongest_neg"].astype(float).abs() >= CCI_STRONG
    return base & extreme


def summarize_next_moves(sub: pd.DataFrame, *, label: str, timeframe: str) -> dict:
    nxt = sub[sub["has_next_opp"] == True]  # noqa: E712
    signed = nxt["next_opp_signed_price_move_pct"].astype(float)
    fav = nxt["next_opp_favorable_move_pct"].astype(float)
    adv = nxt["next_opp_adverse_move_pct"].astype(float)
    eff = nxt["next_opp_directional_efficiency"].astype(float)
    n = int(len(nxt))
    pos_share = float((signed > 0).mean()) if n else None
    return {
        "label": label,
        "timeframe": timeframe,
        "n": n,
        "n_setup": int(len(sub)),
        "small_sample": n < 30,
        "median_next_signed_price_move_pct": float(signed.median()) if n else None,
        "mean_next_signed_price_move_pct": float(signed.mean()) if n else None,
        "median_next_favorable_move_pct": float(fav.median()) if n else None,
        "median_next_adverse_move_pct": float(adv.median()) if n else None,
        "mean_next_directional_efficiency": float(eff.mean()) if n and eff.notna().any() else None,
        "share_next_signed_positive": pos_share,
    }
