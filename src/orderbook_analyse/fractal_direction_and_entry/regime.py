"""Fixed hierarchical regime classification (no threshold search)."""

from __future__ import annotations

import pandas as pd

from orderbook_analyse.fractal_direction_and_entry import WEAK_PRICE_ABS, TF_PREFIX


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


def structure_bull(df: pd.DataFrame, tf: str) -> pd.Series:
    """EMA + RSI structural bull (persistent strength / structure)."""
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_flag = _bool(df[_p(tf, "rsi_end_gt_50")]) | (rsi > 50.0)
    return (
        rsi_flag
        & _eq(df[_p(tf, "price_vs_ema20_end")], "ABOVE")
        & _eq(df[_p(tf, "ema9_vs_ema20_end")], "BULL")
    )


def structure_bear(df: pd.DataFrame, tf: str) -> pd.Series:
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_flag = _bool(df[_p(tf, "rsi_end_lt_50")]) | (rsi < 50.0)
    return (
        rsi_flag
        & _eq(df[_p(tf, "price_vs_ema20_end")], "BELOW")
        & _eq(df[_p(tf, "ema9_vs_ema20_end")], "BEAR")
    )


def wave_up_efficient(df: pd.DataFrame, tf: str) -> pd.Series:
    return (
        _eq(df[_p(tf, "direction")], "UP")
        & (df[_p(tf, "signed_price_move_pct")].astype(float) > 0.0)
        & (df[_p(tf, "directional_efficiency")].astype(float) > 0.0)
    )


def wave_down_efficient(df: pd.DataFrame, tf: str) -> pd.Series:
    return (
        _eq(df[_p(tf, "direction")], "DOWN")
        & (df[_p(tf, "signed_price_move_pct")].astype(float) > 0.0)
        & (df[_p(tf, "directional_efficiency")].astype(float) > 0.0)
    )


def wave_down_inefficient(df: pd.DataFrame, tf: str) -> pd.Series:
    """DOWN wave with weak/negative price follow-through."""
    down = _eq(df[_p(tf, "direction")], "DOWN")
    signed = df[_p(tf, "signed_price_move_pct")].astype(float)
    eff = df[_p(tf, "directional_efficiency")].astype(float)
    move = df[_p(tf, "price_move_pct")].astype(float).abs()
    return down & ((signed <= 0.0) | (eff <= 0.0) | (move <= WEAK_PRICE_ABS))


def wave_up_inefficient(df: pd.DataFrame, tf: str) -> pd.Series:
    up = _eq(df[_p(tf, "direction")], "UP")
    signed = df[_p(tf, "signed_price_move_pct")].astype(float)
    eff = df[_p(tf, "directional_efficiency")].astype(float)
    move = df[_p(tf, "price_move_pct")].astype(float).abs()
    return up & ((signed <= 0.0) | (eff <= 0.0) | (move <= WEAK_PRICE_ABS))


def bull_controlled(df: pd.DataFrame, tf: str) -> pd.Series:
    """
    Bull-controlled TF:
    - UP waves show price impact, OR DOWN waves are inefficient
    - RSI predominantly bullish (>50)
    - EMA structure bullish
    """
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_bull = _bool(df[_p(tf, "rsi_end_gt_50")]) | (rsi > 50.0)
    ema_bull = _eq(df[_p(tf, "price_vs_ema20_end")], "ABOVE") & _eq(
        df[_p(tf, "ema9_vs_ema20_end")], "BULL"
    )
    wave_ok = wave_up_efficient(df, tf) | wave_down_inefficient(df, tf)
    return wave_ok & rsi_bull & ema_bull


def bear_controlled(df: pd.DataFrame, tf: str) -> pd.Series:
    rsi = df[_p(tf, "rsi_end")].astype(float)
    rsi_bear = _bool(df[_p(tf, "rsi_end_lt_50")]) | (rsi < 50.0)
    ema_bear = _eq(df[_p(tf, "price_vs_ema20_end")], "BELOW") & _eq(
        df[_p(tf, "ema9_vs_ema20_end")], "BEAR"
    )
    wave_ok = wave_down_efficient(df, tf) | wave_up_inefficient(df, tf)
    return wave_ok & rsi_bear & ema_bear


def htf_bull_1d(df: pd.DataFrame) -> pd.Series:
    """1D is primary HTF regime. Soft 'improving' via rsi_delta > 0 allowed."""
    d = "1d"
    avail = df[_p(d, "end_available_at")].notna()
    improving = (df[_p(d, "rsi_delta")].astype(float) > 0.0) & (
        _bool(df[_p(d, "rsi_end_gt_50")]) | (df[_p(d, "rsi_end")].astype(float) > 50.0)
    ) & ~structure_bear(df, d)
    return avail & (
        bull_controlled(df, d) | structure_bull(df, d) | wave_up_efficient(df, d) | improving
    )


def htf_bear_1d(df: pd.DataFrame) -> pd.Series:
    d = "1d"
    avail = df[_p(d, "end_available_at")].notna()
    deteriorating = (df[_p(d, "rsi_delta")].astype(float) < 0.0) & (
        _bool(df[_p(d, "rsi_end_lt_50")]) | (df[_p(d, "rsi_end")].astype(float) < 50.0)
    ) & ~structure_bull(df, d)
    return avail & (
        bear_controlled(df, d)
        | structure_bear(df, d)
        | wave_down_efficient(df, d)
        | deteriorating
    )


def soft_htf_confirm_bull(df: pd.DataFrame) -> pd.Series:
    """1W/1M soft confirm — never veto alone; only strengthens."""
    w_ok = df[_p("1w", "end_available_at")].isna() | structure_bull(df, "1w") | ~structure_bear(
        df, "1w"
    )
    m_ok = df[_p("1M", "end_available_at")].isna() | structure_bull(df, "1M") | ~structure_bear(
        df, "1M"
    )
    return w_ok & m_ok


def soft_htf_confirm_bear(df: pd.DataFrame) -> pd.Series:
    w_ok = df[_p("1w", "end_available_at")].isna() | structure_bear(df, "1w") | ~structure_bull(
        df, "1w"
    )
    m_ok = df[_p("1M", "end_available_at")].isna() | structure_bear(df, "1M") | ~structure_bull(
        df, "1M"
    )
    return w_ok & m_ok


def soft_htf_conflict_bull(df: pd.DataFrame) -> pd.Series:
    """Weekly clearly bearish while claiming bull — blocks STRONG only."""
    return df[_p("1w", "end_available_at")].notna() & structure_bear(df, "1w")


def soft_htf_conflict_bear(df: pd.DataFrame) -> pd.Series:
    return df[_p("1w", "end_available_at")].notna() & structure_bull(df, "1w")


def classify_direction_state(df: pd.DataFrame) -> pd.Series:
    """
    Hierarchical state (documented; fixed; no optimization).

    STRONG_BULL:
      1D bullish AND NOT 1D bearish
      AND 4h bull_controlled AND 1h bull_controlled
      AND no clear 1W structural bear conflict

    BULL:
      1D bullish AND NOT 1D bearish
      AND operative bull bias:
        (bull_controlled(4h) OR bull_controlled(1h)
         OR (structure_bull(4h) AND structure_bull(1h)))
      AND NOT (bear_controlled(4h) AND bear_controlled(1h))
      AND not already STRONG_BULL

    STRONG_BEAR / BEAR: mirror

    MIXED: residual / conflicts
    """
    h_bull = htf_bull_1d(df)
    h_bear = htf_bear_1d(df)

    bc4 = bull_controlled(df, "4h")
    bc1 = bull_controlled(df, "1h")
    be4 = bear_controlled(df, "4h")
    be1 = bear_controlled(df, "1h")
    sb4 = structure_bull(df, "4h")
    sb1 = structure_bull(df, "1h")
    se4 = structure_bear(df, "4h")
    se1 = structure_bear(df, "1h")

    op_bull = (bc4 | bc1 | (sb4 & sb1)) & ~(be4 & be1)
    op_bear = (be4 | be1 | (se4 & se1)) & ~(bc4 & bc1)

    strong_bull = (
        h_bull & ~h_bear & bc4 & bc1 & ~soft_htf_conflict_bull(df)
    )
    strong_bear = (
        h_bear & ~h_bull & be4 & be1 & ~soft_htf_conflict_bear(df)
    )

    bull = h_bull & ~h_bear & op_bull & ~strong_bull
    bear = h_bear & ~h_bull & op_bear & ~strong_bear

    # If both bull and bear claim (pathological), force MIXED
    conflict = (bull | strong_bull) & (bear | strong_bear)

    out = pd.Series("MIXED", index=df.index, dtype=object)
    out.loc[bear] = "BEAR"
    out.loc[bull] = "BULL"
    out.loc[strong_bear] = "STRONG_BEAR"
    out.loc[strong_bull] = "STRONG_BULL"
    out.loc[conflict] = "MIXED"
    # No 1D available → MIXED
    no_d1 = df[_p("1d", "end_available_at")].isna()
    out.loc[no_d1] = "MIXED"
    return out


REGIME_RULES_DOC = """
Direction state rules (fixed a priori; no optimization):

bull_controlled(TF):
  (UP & signed_price_move_pct>0 & directional_efficiency>0)
  OR (DOWN & (signed<=0 OR eff<=0 OR |price_move|<=0.02))
  AND (rsi_end>50) AND (price ABOVE EMA20) AND (EMA9>EMA20 BULL)

bear_controlled(TF): mirror

HTF (1D primary):
  htf_bull = bull_controlled(1d) OR structure_bull(1d) OR up_efficient(1d)
             OR (rsi_delta>0 & rsi_end>50 & not structure_bear)
  htf_bear = mirror
  1W/1M: soft confirm only; 1W structural conflict blocks STRONG_* only

Operative (4h/1h):
  op_bull = bull_controlled(4h|1h) OR (structure_bull both)
            AND NOT (bear_controlled both)

STRONG_BULL = htf_bull & ~htf_bear & bull_controlled(4h) & bull_controlled(1h)
              & ~1W_structure_bear
BULL = htf_bull & ~htf_bear & op_bull & ~STRONG_BULL
STRONG_BEAR / BEAR = mirror
MIXED = residual, conflicts, or missing 1D
CCI is carried in snapshots but NEVER used in classification or entry.
"""
