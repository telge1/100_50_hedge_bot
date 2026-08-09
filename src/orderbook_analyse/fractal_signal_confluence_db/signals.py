"""Build all-TF fade signals from MySQL."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization.annotate import annotate_waves_df
from orderbook_analyse.fractal_parent_lower_tf_quality_db.db_build import build_waves_from_db
from orderbook_analyse.fractal_signal_confluence_db import APT_IS_END, ENV_FILE, SIGNAL_TFS
from orderbook_analyse.fractal_wave_fade_trend_filter.analysis import assign_trend_bucket
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def frozen_eff_edges_all_signal_tfs() -> dict[tuple[str, str, str], dict[float, float]]:
    """APT-IS quartile edges for 15m/30m/1h/4h from MySQL only."""
    load_env_file(ENV_FILE)
    is_end = pd.Timestamp(APT_IS_END)
    edges: dict[tuple[str, str, str], dict[float, float]] = {}
    for tf in SIGNAL_TFS:
        w = build_waves_from_db("APTUSDT", tf)
        w = w[w["end_available_at"] <= is_end]
        for direction, g in w.groupby(w["direction"].astype(str)):
            for col in ("directional_efficiency", "signed_price_move_pct"):
                s = g[col].astype(float).dropna()
                q = s.quantile([0.25, 0.5, 0.75])
                edges[(tf, str(direction), col)] = {
                    0.25: float(q.loc[0.25]),
                    0.5: float(q.loc[0.5]),
                    0.75: float(q.loc[0.75]),
                }
    return edges


def build_symbol_signals(
    symbol: str,
    edges: dict[tuple[str, str, str], dict[float, float]],
) -> pd.DataFrame:
    """All-wave fade events for SIGNAL_TFs with Tier-A / Q4 flags."""
    rows = []
    for tf in SIGNAL_TFS:
        w = build_waves_from_db(symbol, tf)
        if w.empty:
            continue
        ann = annotate_waves_df(w, symbol=symbol, timeframe=tf, quantile_edges=edges)
        ann["trend_bucket"] = assign_trend_bucket(ann)
        ann["is_tier_a"] = (ann["trend_bucket"].astype(str) == "TREND_ALIGNED") & (
            ann["eff_quantile"].astype(str) == "Q4"
        )
        ann["is_q4"] = ann["eff_quantile"].astype(str) == "Q4"
        ann["signal_tf"] = tf
        rows.append(ann)
    if not rows:
        return pd.DataFrame()
    df = pd.concat(rows, ignore_index=True)
    df["confirmation_available_at"] = pd.to_datetime(df["confirmation_available_at"], utc=True)
    df = df.sort_values("confirmation_available_at").reset_index(drop=True)
    df["signal_id"] = np.arange(len(df), dtype=np.int64)
    return df


def resolve_entries(events: pd.DataFrame, open_times: np.ndarray, opens: np.ndarray) -> pd.DataFrame:
    out = events.copy()
    conf = pd.to_datetime(out["confirmation_available_at"], utc=True).to_numpy(dtype="datetime64[ns]")
    idx = np.searchsorted(open_times, conf, side="right").astype(np.int64)
    n = len(open_times)
    valid = (idx >= 0) & (idx < n)
    px = np.full(len(out), np.nan)
    et = np.full(len(out), np.datetime64("NaT"), dtype="datetime64[ns]")
    px[valid] = opens[idx[valid]]
    et[valid] = open_times[idx[valid]]
    out["entry_i"] = np.where(valid, idx, -1)
    out["entry_price"] = px
    out["entry_time"] = pd.to_datetime(et, utc=True)
    out["entry_valid"] = valid & np.isfinite(px) & (px > 0)
    if len(open_times):
        t0, t1 = open_times[0], open_times[-1]
        et2 = out["entry_time"].to_numpy(dtype="datetime64[ns]")
        out.loc[out["entry_valid"] & ((et2 < t0) | (et2 > t1)), "entry_valid"] = False
    return out
