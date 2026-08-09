"""Build 15m failure episodes and join causal MTF cycle context."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure import (
    SYMBOL,
    TF_PREFIX,
    WAVE_COLS,
    WAVE_DIR,
)
from orderbook_analyse.fractal_cycle_phase_failure.phase import (
    cycle_phase_from_wave,
    turning_flags,
)
from orderbook_analyse.fractal_directional_control.flags import (
    inefficient_down_in_bull,
    inefficient_up_in_bear,
)
from orderbook_analyse.fractal_directional_control.load_join import asof_last_completed


def _bool(s: pd.Series) -> pd.Series:
    return s.map(
        lambda x: True
        if str(x).lower() in ("1", "true", "yes")
        else (False if str(x).lower() in ("0", "false", "no") else False)
    )


def load_waves(tf: str, wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    path = Path(wave_dir) / f"waves_{tf}.csv"
    header = pd.read_csv(path, nrows=0).columns.tolist()
    cols = [c for c in WAVE_COLS if c in header]
    df = pd.read_csv(path, usecols=cols)
    for c in ("end_available_at", "start_available_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    for c in ("rsi_end_gt_50", "rsi_end_lt_50", "inefficient_flag"):
        if c in df.columns:
            df[c] = _bool(df[c])
    df = df.sort_values("end_available_at").reset_index(drop=True)
    df["wave_i"] = np.arange(len(df), dtype=np.int64)
    df["prev_direction"] = df["direction"].shift(1)
    df["prev_directional_efficiency"] = df["directional_efficiency"].shift(1)
    df["prev_signed_price_move_pct"] = df["signed_price_move_pct"].shift(1)
    return df


def local_failure_mask(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    up = df["direction"].astype(str) == "UP"
    dn = df["direction"].astype(str) == "DOWN"
    signed = df["signed_price_move_pct"].astype(float)
    eff = df["directional_efficiency"].astype(float)
    ineff = df["inefficient_flag"].fillna(False).astype(bool)
    weak = (signed <= 0.0) | (eff <= 0.0) | ineff
    return up & weak, dn & weak


def build_failure_events(wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    """One row per failed 15m wave episode."""
    w15 = load_waves("15m", wave_dir)
    fail_up, fail_dn = local_failure_mask(w15)
    mask = fail_up | fail_dn
    events = w15.loc[mask].copy()
    events["failure_type"] = np.where(fail_up.loc[mask], "FAILED_UP_WAVE", "FAILED_DOWN_WAVE")
    events["decision_time"] = events["end_available_at"]
    events["symbol"] = SYMBOL
    events["expected_reversal"] = np.where(
        events["failure_type"] == "FAILED_UP_WAVE", "DOWN", "UP"
    )
    events["M15_cycle_phase"] = cycle_phase_from_wave(events)
    tu, td = turning_flags(events)
    events["M15_turning_up"] = tu.to_numpy()
    events["M15_turning_down"] = td.to_numpy()

    rename = {c: f"M15_{c}" for c in WAVE_COLS if c in events.columns}
    events = events.rename(columns=rename)
    return events.reset_index(drop=True)


def attach_context(events: pd.DataFrame, wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    times = events["decision_time"].to_numpy(dtype="datetime64[ns]")
    frames = [events.reset_index(drop=True)]

    for tf in ("1d", "4h", "1h", "1M", "1w", "5m", "1m"):
        print(f"[join] {tf}", flush=True)
        waves = load_waves(tf, wave_dir)
        prefix = TF_PREFIX[tf]
        cols = [c for c in WAVE_COLS if c in waves.columns]
        joined = asof_last_completed(waves, times, cols, prefix)
        tmp = pd.DataFrame(
            {
                "direction": joined[f"{prefix}_direction"],
                "stoch_zone_end": joined[f"{prefix}_stoch_zone_end"],
                "stoch_zone_start": joined[f"{prefix}_stoch_zone_start"],
            }
        )
        joined[f"{prefix}_cycle_phase"] = cycle_phase_from_wave(tmp)
        tu, td = turning_flags(tmp)
        joined[f"{prefix}_turning_up"] = tu.to_numpy()
        joined[f"{prefix}_turning_down"] = td.to_numpy()
        frames.append(joined)

    out = pd.concat(frames, axis=1)

    ctrl = pd.DataFrame(
        {
            "direction": out["M15_direction"],
            "signed_price_move_pct": out["M15_signed_price_move_pct"],
            "price_move_pct": out["M15_price_move_pct"],
            "directional_efficiency": out["M15_directional_efficiency"],
            "rsi_end_gt_50": out["M15_rsi_end_gt_50"],
            "rsi_end_lt_50": out["M15_rsi_end_lt_50"],
            "ema9_vs_ema20_end": out["M15_ema9_vs_ema20_end"],
            "price_vs_ema20_end": out["M15_price_vs_ema20_end"],
            "d1_direction": out["D1_direction"],
            "d1_rsi_end_gt_50": out["D1_rsi_end_gt_50"],
            "d1_ema9_vs_ema20_end": out["D1_ema9_vs_ema20_end"],
            "h4_direction": out["H4_direction"],
            "h4_ema9_vs_ema20_end": out["H4_ema9_vs_ema20_end"],
            "h4_price_vs_ema20_end": out["H4_price_vs_ema20_end"],
            "h1_direction": out["H1_direction"],
            "h1_ema9_vs_ema20_end": out["H1_ema9_vs_ema20_end"],
            "h1_price_vs_ema20_end": out["H1_price_vs_ema20_end"],
        }
    )
    out["flag_inefficient_up_in_bear"] = inefficient_up_in_bear(ctrl).fillna(False).astype(bool)
    out["flag_inefficient_down_in_bull"] = inefficient_down_in_bull(ctrl).fillna(False).astype(bool)

    # Union: keep local failures; also keep control-flag waves if any were not local
    # (local already selected; flags are annotations for analysis)
    return out


def attach_relative_weakness(events: pd.DataFrame) -> pd.DataFrame:
    out = events.copy()
    prev_dir = out["prev_direction"].astype(str)
    prev_eff = out["prev_directional_efficiency"].astype(float)
    cur_eff = out["M15_directional_efficiency"].astype(float)
    weak = pd.Series(False, index=out.index)
    m_up = out["failure_type"] == "FAILED_UP_WAVE"
    weak.loc[m_up] = (
        (prev_dir.loc[m_up] == "DOWN")
        & np.isfinite(prev_eff.loc[m_up])
        & np.isfinite(cur_eff.loc[m_up])
        & (prev_eff.loc[m_up] > cur_eff.loc[m_up])
    ).to_numpy()
    m_dn = out["failure_type"] == "FAILED_DOWN_WAVE"
    weak.loc[m_dn] = (
        (prev_dir.loc[m_dn] == "UP")
        & np.isfinite(prev_eff.loc[m_dn])
        & np.isfinite(cur_eff.loc[m_dn])
        & (prev_eff.loc[m_dn] > cur_eff.loc[m_dn])
    ).to_numpy()
    out["relative_wave_weakness"] = weak
    return out


def micro_diagnostic(df: pd.DataFrame) -> pd.Series:
    """aligned / counter / mixed vs expected reversal (5m+1m)."""
    exp = df["expected_reversal"].astype(str)
    d5 = df["M5_direction"].astype(str)
    d1 = df["M1m_direction"].astype(str)
    out = pd.Series("mixed", index=df.index, dtype=object)
    aligned = ((exp == "UP") & (d5 == "UP") & (d1 == "UP")) | (
        (exp == "DOWN") & (d5 == "DOWN") & (d1 == "DOWN")
    )
    counter = ((exp == "UP") & (d5 == "DOWN") & (d1 == "DOWN")) | (
        (exp == "DOWN") & (d5 == "UP") & (d1 == "UP")
    )
    out.loc[aligned] = "aligned"
    out.loc[counter] = "counter"
    return out
