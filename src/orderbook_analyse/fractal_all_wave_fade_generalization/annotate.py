"""Annotate waves with frozen fade labels (no logic changes)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade import EXTRA_WAVE_COLS
from orderbook_analyse.fractal_all_wave_fade_generalization import APT_IS_RESULTS
from orderbook_analyse.fractal_cycle_phase_failure import WAVE_COLS
from orderbook_analyse.fractal_cycle_phase_failure.events import local_failure_mask


def _bool(s: pd.Series) -> pd.Series:
    return s.map(
        lambda x: True
        if str(x).lower() in ("1", "true", "yes")
        else (False if str(x).lower() in ("0", "false", "no") else False)
    )


def load_frozen_quantile_edges(
    is_events_path: Path | None = None,
) -> dict[tuple[str, str, str], dict[float, float]]:
    """Quartile edges from APT IS all_wave_events (frozen, not recomputed on OOS)."""
    path = is_events_path or (APT_IS_RESULTS / "all_wave_events.csv")
    ev = pd.read_csv(
        path,
        usecols=["timeframe", "direction", "directional_efficiency", "signed_price_move_pct"],
    )
    edges: dict[tuple[str, str, str], dict[float, float]] = {}
    for (tf, d), g in ev.groupby(["timeframe", "direction"]):
        for col in ("directional_efficiency", "signed_price_move_pct"):
            s = g[col].astype(float).dropna()
            q = s.quantile([0.25, 0.5, 0.75])
            edges[(str(tf), str(d), col)] = {0.25: float(q.loc[0.25]), 0.5: float(q.loc[0.5]), 0.75: float(q.loc[0.75])}
    return edges


def assign_frozen_quartile(series: pd.Series, edge: dict[float, float]) -> pd.Series:
    q25, q50, q75 = edge[0.25], edge[0.5], edge[0.75]
    out = pd.Series("NA", index=series.index, dtype=object)
    v = series.astype(float)
    out.loc[v.notna() & (v <= q25)] = "Q1"
    out.loc[v.notna() & (v > q25) & (v <= q50)] = "Q2"
    out.loc[v.notna() & (v > q50) & (v <= q75)] = "Q3"
    out.loc[v.notna() & (v > q75)] = "Q4"
    return out


def annotate_waves_df(
    waves: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    quantile_edges: dict[tuple[str, str, str], dict[float, float]],
) -> pd.DataFrame:
    """Same annotation as fractal_all_wave_fade.events.load_all_waves, plus frozen Q labels."""
    if waves is None or waves.empty:
        return pd.DataFrame()

    want = list(dict.fromkeys([*WAVE_COLS, *EXTRA_WAVE_COLS]))
    cols = [c for c in want if c in waves.columns]
    df = waves[cols].copy()
    for c in ("end_available_at", "start_available_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    for c in ("rsi_end_gt_50", "rsi_end_lt_50", "inefficient_flag"):
        if c in df.columns:
            df[c] = _bool(df[c])
    df = df.sort_values("end_available_at").reset_index(drop=True)
    df["wave_i"] = np.arange(len(df), dtype=np.int64)
    df["timeframe"] = timeframe
    df["symbol"] = symbol

    df["prev_direction"] = df["direction"].shift(1)
    df["prev_directional_efficiency"] = df["directional_efficiency"].shift(1)
    df["prev_signed_price_move_pct"] = df["signed_price_move_pct"].shift(1)
    df["prev_n_bars"] = df["n_bars"].shift(1) if "n_bars" in df.columns else np.nan

    fail_up, fail_dn = local_failure_mask(df)
    df["is_failed"] = (fail_up | fail_dn).to_numpy()
    df["failure_type"] = np.where(
        fail_up,
        "FAILED_UP_WAVE",
        np.where(fail_dn, "FAILED_DOWN_WAVE", "NON_FAILED"),
    )
    df["wave_group"] = np.where(df["is_failed"], "FAILED", "NON_FAILED")
    df["expected_reversal"] = np.where(df["direction"].astype(str) == "UP", "DOWN", "UP")
    df["side"] = np.where(df["expected_reversal"] == "DOWN", "SHORT", "LONG")
    df["confirmation_available_at"] = df["end_available_at"]

    prev_opp = df["prev_direction"].notna() & (
        df["prev_direction"].astype(str) != df["direction"].astype(str)
    )
    cur_e = df["directional_efficiency"].astype(float)
    prev_e = df["prev_directional_efficiency"].astype(float)
    rel = pd.Series("NO_PREV_OPPOSITE", index=df.index, dtype=object)
    both = prev_opp & np.isfinite(cur_e) & np.isfinite(prev_e)
    rel.loc[both & (cur_e < prev_e)] = "CURRENT_WEAKER_THAN_PREVIOUS"
    rel.loc[both & (cur_e > prev_e)] = "CURRENT_STRONGER_THAN_PREVIOUS"
    rel.loc[both & (cur_e == prev_e)] = "SIMILAR"
    df["prev_rel_efficiency"] = rel

    pve = df["price_vs_ema20_end"].astype(str)
    e9 = df["ema9_vs_ema20_end"].astype(str)
    ema = pd.Series("MIXED", index=df.index, dtype=object)
    ema.loc[(pve == "ABOVE") & (e9 == "BULL")] = "EMA_BULL"
    ema.loc[(pve == "BELOW") & (e9 == "BEAR")] = "EMA_BEAR"
    df["ema_context"] = ema

    rsi = df["rsi_end"].astype(float)
    rb = pd.Series("NA", index=df.index, dtype=object)
    rb.loc[rsi < 40] = "lt40"
    rb.loc[(rsi >= 40) & (rsi < 50)] = "40_50"
    rb.loc[(rsi >= 50) & (rsi <= 60)] = "50_60"
    rb.loc[rsi > 60] = "gt60"
    df["rsi_bucket"] = rb

    df["stoch_path"] = (
        df["stoch_zone_start"].astype(str) + "->" + df["stoch_zone_end"].astype(str)
    )

    # Frozen APT-IS quartile labels
    eff_q = pd.Series("NA", index=df.index, dtype=object)
    size_q = pd.Series("NA", index=df.index, dtype=object)
    for direction in ("UP", "DOWN"):
        m = df["direction"].astype(str) == direction
        e_key = (timeframe, direction, "directional_efficiency")
        s_key = (timeframe, direction, "signed_price_move_pct")
        if e_key in quantile_edges:
            eff_q.loc[m] = assign_frozen_quartile(
                df.loc[m, "directional_efficiency"], quantile_edges[e_key]
            )
        if s_key in quantile_edges:
            size_q.loc[m] = assign_frozen_quartile(
                df.loc[m, "signed_price_move_pct"], quantile_edges[s_key]
            )
    df["eff_quantile"] = eff_q
    df["size_quantile"] = size_q
    return df
