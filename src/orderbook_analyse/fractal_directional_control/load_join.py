"""Load existing fractal wave CSVs and causal as-of joins."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_directional_control import (
    CONTEXT_TFS,
    REGIME_TFS,
    TRIGGER_TFS,
)

DEFAULT_WAVE_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/fractal_cycle_wave_analysis_apt"
)

BOOL_COLS = (
    "rsi_end_gt_50",
    "rsi_end_lt_50",
    "inefficient_flag",
)


def load_waves(wave_dir: Path, timeframe: str) -> pd.DataFrame:
    path = wave_dir / f"waves_{timeframe}.csv"
    if not path.is_file():
        # monthly file is waves_1M.csv
        alt = wave_dir / f"waves_{timeframe}.csv"
        path = alt
    df = pd.read_csv(path)
    for c in ("start_available_at", "end_available_at", "start_ts", "end_ts"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    for c in BOOL_COLS:
        if c in df.columns:
            df[c] = df[c].map(_as_bool)
    df = df.sort_values("end_available_at").reset_index(drop=True)
    df["wave_id"] = np.arange(len(df), dtype=np.int64)
    df["timeframe"] = timeframe
    return df


def _as_bool(x) -> bool | None:
    if pd.isna(x):
        return None
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


def load_all_waves(wave_dir: Path = DEFAULT_WAVE_DIR) -> dict[str, pd.DataFrame]:
    tfs = ("1d", "1w", "1M", "4h", "1h", "15m", "5m", "1m")
    out: dict[str, pd.DataFrame] = {}
    for tf in tfs:
        path = wave_dir / f"waves_{tf}.csv"
        if not path.is_file():
            print(f"[warn] missing {path}", flush=True)
            continue
        df = pd.read_csv(path)
        for c in ("start_available_at", "end_available_at", "start_ts", "end_ts"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], utc=True)
        for c in BOOL_COLS:
            if c in df.columns:
                df[c] = df[c].map(_as_bool)
        df = df.sort_values("end_available_at").reset_index(drop=True)
        df["wave_id"] = np.arange(len(df), dtype=np.int64)
        df["timeframe"] = tf
        out[tf] = df
        print(f"[load] {tf}: n={len(df)}", flush=True)
    return out


def asof_last_completed(
    parent: pd.DataFrame,
    decision_times: np.ndarray,
    cols: list[str],
    prefix: str,
) -> pd.DataFrame:
    """For each decision time, take last parent wave with end_available_at <= t."""
    if parent is None or parent.empty:
        return pd.DataFrame({f"{prefix}_{c}": [None] * len(decision_times) for c in cols})

    p = parent.sort_values("end_available_at").reset_index(drop=True)
    p_end = p["end_available_at"].to_numpy(dtype="datetime64[ns]")
    times = np.asarray(decision_times, dtype="datetime64[ns]")
    idx = np.searchsorted(p_end, times, side="right") - 1

    data: dict[str, list] = {}
    for c in cols:
        src = p[c].to_numpy()
        vals = []
        for j in idx:
            if j < 0:
                vals.append(None)
            else:
                vals.append(src[j])
        data[f"{prefix}_{c}"] = vals
    data[f"{prefix}_available"] = [bool(j >= 0) for j in idx]
    return pd.DataFrame(data)


CONTEXT_COLS = [
    "direction",
    "stoch_zone_end",
    "rsi_end",
    "rsi_end_gt_50",
    "rsi_gt50_share",
    "price_vs_ema20_end",
    "ema9_vs_ema20_end",
    "directional_efficiency",
    "signed_price_move_pct",
    "end_available_at",
]

REGIME_COLS = [
    "direction",
    "stoch_zone_end",
    "rsi_end",
    "rsi_end_gt_50",
    "ema9_vs_ema20_end",
    "price_vs_ema20_end",
    "directional_efficiency",
    "signed_price_move_pct",
    "end_available_at",
]


def join_trigger_context(
    trigger: pd.DataFrame,
    waves: dict[str, pd.DataFrame],
    *,
    decision_col: str = "end_available_at",
) -> pd.DataFrame:
    """Attach causal 1d/1w/1M/4h/1h context at trigger decision time."""
    t = trigger.sort_values(decision_col).reset_index(drop=True).copy()
    times = t[decision_col].to_numpy(dtype="datetime64[ns]")

    frames = [t]
    for tf, cols, prefix in (
        ("1d", REGIME_COLS, "d1"),
        ("1w", REGIME_COLS, "w1"),
        ("1M", REGIME_COLS, "m1"),
        ("4h", CONTEXT_COLS, "h4"),
        ("1h", CONTEXT_COLS, "h1"),
    ):
        parent = waves.get(tf)
        if parent is None:
            continue
        # rename stoch_zone_end -> stoch_zone in output prefix mapping via cols as-is
        joined = asof_last_completed(parent, times, cols, prefix)
        frames.append(joined)

    out = pd.concat(frames, axis=1)
    return out


def attach_next_opposite_wave(df: pd.DataFrame) -> pd.DataFrame:
    """Within same TF chronological order, attach next opposite-direction wave metrics."""
    out = df.sort_values("end_available_at").reset_index(drop=True).copy()
    n = len(out)
    nxt_dir = [None] * n
    nxt_signed = [np.nan] * n
    nxt_fav = [np.nan] * n
    nxt_adv = [np.nan] * n
    nxt_eff = [np.nan] * n
    nxt_price = [np.nan] * n
    has_next = [False] * n

    dirs = out["direction"].to_numpy()
    signed = out["signed_price_move_pct"].to_numpy(dtype=float)
    fav = out["favorable_move_pct"].to_numpy(dtype=float)
    adv = out["adverse_move_pct"].to_numpy(dtype=float)
    eff = out["directional_efficiency"].to_numpy(dtype=float)
    price = out["price_move_pct"].to_numpy(dtype=float)

    for i in range(n - 1):
        # next wave chronologically that is opposite direction
        for j in range(i + 1, min(i + 6, n)):  # look ahead a few waves
            if dirs[j] != dirs[i]:
                nxt_dir[i] = dirs[j]
                nxt_signed[i] = signed[j]
                nxt_fav[i] = fav[j]
                nxt_adv[i] = adv[j]
                nxt_eff[i] = eff[j]
                nxt_price[i] = price[j]
                has_next[i] = True
                break

    out["next_opp_direction"] = nxt_dir
    out["next_opp_signed_price_move_pct"] = nxt_signed
    out["next_opp_favorable_move_pct"] = nxt_fav
    out["next_opp_adverse_move_pct"] = nxt_adv
    out["next_opp_directional_efficiency"] = nxt_eff
    out["next_opp_price_move_pct"] = nxt_price
    out["has_next_opp"] = has_next
    return out
