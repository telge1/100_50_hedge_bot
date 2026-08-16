"""Stochastic K/D-cross wave segmentation — freeze fractal_cycle_wave_analysis/waves.py."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from signal_generator.strategy.wave_fade.parameters import (
    INEFFICIENT_ABS_PRICE_PCT,
    MIN_ABS_STOCH_DELTA,
    MIN_WAVE_BARS,
)


def segment_stoch_waves(df: pd.DataFrame, *, min_bars: int = MIN_WAVE_BARS) -> pd.DataFrame:
    """Segment completed Stoch waves between K/D crosses (vectorized core)."""
    if df.empty or "stoch_k" not in df.columns:
        return pd.DataFrame()

    work = df.reset_index(drop=True)
    bull = work["stoch_bullish_cross"].fillna(False).to_numpy(dtype=bool)
    bear = work["stoch_bearish_cross"].fillna(False).to_numpy(dtype=bool)
    event_idx = np.flatnonzero(bull | bear)
    if len(event_idx) < 2:
        return pd.DataFrame()
    event_dir = np.where(
        bull[event_idx] & ~bear[event_idx],
        1,
        np.where(bear[event_idx] & ~bull[event_idx], -1, 0),
    )
    keep = event_dir != 0
    event_idx = event_idx[keep]
    event_dir = event_dir[keep]
    if len(event_idx) < 2:
        return pd.DataFrame()

    starts = event_idx[:-1]
    start_dirs = event_dir[:-1]
    ends = event_idx[1:] - 1
    flip = start_dirs != event_dir[1:]
    starts, start_dirs, ends = starts[flip], start_dirs[flip], ends[flip]
    n_bars = ends - starts + 1
    ok = (n_bars >= min_bars) & (ends >= starts)
    starts, ends, start_dirs, n_bars = starts[ok], ends[ok], start_dirs[ok], n_bars[ok]
    if len(starts) == 0:
        return pd.DataFrame()

    close = work["close"].to_numpy(dtype=float)
    high = work["high"].to_numpy(dtype=float)
    low = work["low"].to_numpy(dtype=float)
    k = work["stoch_k"].to_numpy(dtype=float)
    rsi = work["rsi"].to_numpy(dtype=float)
    cci = work["cci"].to_numpy(dtype=float)
    ema9 = work["ema9"].to_numpy(dtype=float)
    ema20 = work["ema20"].to_numpy(dtype=float)
    ema100 = work["ema100"].to_numpy(dtype=float)
    ema400 = work["ema400"].to_numpy(dtype=float)
    ts = work["timestamp"].to_numpy()
    avail = work["available_at"].to_numpy()
    zone = work["stoch_zone"].to_numpy()
    pve = work["price_vs_ema20"].to_numpy()
    e9v = work["ema9_vs_ema20"].to_numpy()

    rows: list[dict[str, Any]] = []
    for s, e, d, nb in zip(starts, ends, start_dirs, n_bars):
        direction = "UP" if d > 0 else "DOWN"
        start_px = float(close[s])
        end_px = float(close[e])
        price_move_pct = (end_px / start_px - 1.0) * 100.0 if start_px else np.nan
        wave_high = float(np.nanmax(high[s : e + 1]))
        wave_low = float(np.nanmin(low[s : e + 1]))
        if direction == "UP":
            favorable = (wave_high / start_px - 1.0) * 100.0 if start_px else np.nan
            adverse = (wave_low / start_px - 1.0) * 100.0 if start_px else np.nan
            signed_move = price_move_pct
        else:
            favorable = (wave_low / start_px - 1.0) * 100.0 if start_px else np.nan
            adverse = (wave_high / start_px - 1.0) * 100.0 if start_px else np.nan
            signed_move = -price_move_pct

        k0, k1 = float(k[s]), float(k[e])
        stoch_delta = k1 - k0
        rsi0, rsi1 = float(rsi[s]), float(rsi[e])
        rsi_slice = rsi[s : e + 1]
        rsi_gt50_share = (
            float(np.nanmean(rsi_slice > 50.0)) if np.isfinite(rsi_slice).any() else np.nan
        )
        cci_slice = cci[s : e + 1]
        cci0, cci1 = float(cci[s]), float(cci[e])
        cci_min = float(np.nanmin(cci_slice)) if np.isfinite(cci_slice).any() else np.nan
        cci_max = float(np.nanmax(cci_slice)) if np.isfinite(cci_slice).any() else np.nan
        abs_stoch = abs(stoch_delta) if np.isfinite(stoch_delta) else np.nan
        efficiency = (
            float(signed_move) / abs_stoch
            if np.isfinite(signed_move) and np.isfinite(abs_stoch) and abs_stoch > 1e-9
            else np.nan
        )
        inefficient = bool(
            np.isfinite(price_move_pct)
            and np.isfinite(abs_stoch)
            and abs_stoch >= MIN_ABS_STOCH_DELTA
            and abs(price_move_pct) <= INEFFICIENT_ABS_PRICE_PCT
        )
        rows.append(
            {
                "direction": direction,
                "start_i": int(s),
                "end_i": int(e),
                "n_bars": int(nb),
                "start_ts": pd.Timestamp(ts[s]).isoformat(),
                "end_ts": pd.Timestamp(ts[e]).isoformat(),
                "start_available_at": pd.Timestamp(avail[s]).isoformat(),
                "end_available_at": pd.Timestamp(avail[e]).isoformat(),
                "start_price": start_px,
                "end_price": end_px,
                "price_move_pct": float(price_move_pct),
                "signed_price_move_pct": float(signed_move) if np.isfinite(signed_move) else np.nan,
                "wave_high": wave_high,
                "wave_low": wave_low,
                "favorable_move_pct": float(favorable) if np.isfinite(favorable) else np.nan,
                "adverse_move_pct": float(adverse) if np.isfinite(adverse) else np.nan,
                "stoch_k_start": k0,
                "stoch_k_end": k1,
                "stoch_delta": float(stoch_delta) if np.isfinite(stoch_delta) else np.nan,
                "stoch_zone_start": zone[s],
                "stoch_zone_end": zone[e],
                "stoch_state_start": None,
                "stoch_state_end": None,
                "last_turn": direction,
                "rsi_start": rsi0,
                "rsi_end": rsi1,
                "rsi_delta": (rsi1 - rsi0) if np.isfinite(rsi0) and np.isfinite(rsi1) else np.nan,
                "rsi_end_gt_50": bool(rsi1 > 50.0) if np.isfinite(rsi1) else None,
                "rsi_end_lt_50": bool(rsi1 < 50.0) if np.isfinite(rsi1) else None,
                "rsi_gt50_share": rsi_gt50_share,
                "cci_start": cci0,
                "cci_end": cci1,
                "cci_delta": (cci1 - cci0) if np.isfinite(cci0) and np.isfinite(cci1) else np.nan,
                "cci_min": cci_min,
                "cci_max": cci_max,
                "cci_strongest_pos": cci_max,
                "cci_strongest_neg": cci_min,
                "ema9_end": float(ema9[e]) if np.isfinite(ema9[e]) else np.nan,
                "ema20_end": float(ema20[e]) if np.isfinite(ema20[e]) else np.nan,
                "ema100_end": float(ema100[e]) if np.isfinite(ema100[e]) else np.nan,
                "ema400_end": float(ema400[e]) if np.isfinite(ema400[e]) else np.nan,
                "price_vs_ema20_end": pve[e],
                "ema9_vs_ema20_end": e9v[e],
                "directional_efficiency": efficiency,
                "inefficient_flag": inefficient,
            }
        )
    return pd.DataFrame(rows)
