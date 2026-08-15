"""Causal absorption features: lookback [t-L, t), exclude bar t."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.features import (
    _causal_percentile,
    _causal_rolling_mad,
    _causal_rolling_median,
    _true_range,
    _wilder_atr,
)
from research.regime_scanner.oi_price_delta_pattern.features import _contiguous, _slope_r2
from research.regime_scanner.orderflow_absorption.config import ATR_PERIOD, BAR_SECONDS, AbsorptionConfig


def enrich_frame(df: pd.DataFrame, cfg: AbsorptionConfig) -> pd.DataFrame:
    """Add ATR and causal rolling refs for delta_ratio / volume (current bar excluded from refs)."""
    out = df.copy()
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = _true_range(high, low, prev)
    out["atr_14"] = _wilder_atr(tr, ATR_PERIOD)
    out["true_range"] = tr

    n = len(out)
    seq = out["sequence_id"].to_numpy()
    ts = out["bucket_start"].to_numpy()
    valid = np.ones(n, dtype=bool)
    for i in range(1, n):
        if seq[i] != seq[i - 1]:
            valid[i] = False
            continue
        dt = (pd.Timestamp(ts[i]) - pd.Timestamp(ts[i - 1])).total_seconds()
        if dt != BAR_SECONDS:
            valid[i] = False

    # Per-bar delta_ratio for F3 / diagnostics
    tot = out["total_volume"].to_numpy(dtype=float)
    delta = out["delta"].to_numpy(dtype=float)
    if not np.isfinite(delta).all():
        buy = out["buy_volume"].to_numpy(dtype=float)
        sell = out["sell_volume"].to_numpy(dtype=float)
        delta = buy - sell
        out["delta"] = delta
    bar_dr = np.where(tot > 0, delta / tot, np.nan)
    out["bar_delta_ratio"] = bar_dr
    abs_dr = np.abs(bar_dr)

    w = int(cfg.rolling_ref_bars)
    out["abs_delta_ratio_p90_prior"] = _causal_percentile(abs_dr, w, valid, cfg.f3_percentile)
    # z-score of bar delta_ratio vs prior window
    med = _causal_rolling_median(bar_dr, w, valid)
    mad = _causal_rolling_mad(bar_dr, w, valid)
    denom = 1.4826 * mad
    out["delta_ratio_z"] = np.divide(
        bar_dr - med,
        denom,
        out=np.full_like(bar_dr, np.nan, dtype=float),
        where=np.isfinite(mad) & (mad > 0) & np.isfinite(med) & np.isfinite(bar_dr) & np.isfinite(denom),
    )
    out["volume_median_prior"] = _causal_rolling_median(tot, w, valid)
    return out


def features_at(df: pd.DataFrame, t: int, lookback: int) -> dict[str, Any] | None:
    if t < lookback:
        return None
    i0 = t - lookback
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    if not _contiguous(seq, ts, i0, t):
        return None

    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    oi = df["open_interest"].to_numpy(dtype=float)
    buy = df["buy_volume"].to_numpy(dtype=float)
    sell = df["sell_volume"].to_numpy(dtype=float)
    tot = df["total_volume"].to_numpy(dtype=float)
    delta = df["delta"].to_numpy(dtype=float)
    atr = df["atr_14"].to_numpy(dtype=float)
    bar_dr = df["bar_delta_ratio"].to_numpy(dtype=float)

    o = opens[i0:t]
    h = highs[i0:t]
    l = lows[i0:t]
    c = closes[i0:t]
    oi_w = oi[i0:t]
    buy_w = buy[i0:t]
    sell_w = sell[i0:t]
    tot_w = tot[i0:t]
    d_w = delta[i0:t]
    dr_w = bar_dr[i0:t]

    if not (np.isfinite(o).all() and np.isfinite(h).all() and np.isfinite(l).all() and np.isfinite(c).all()):
        return None

    open_start = float(o[0])
    close_end = float(c[-1])
    price_ret = float(close_end / open_start - 1.0) if open_start > 0 else float("nan")
    rng = float(np.max(h) - np.min(l))
    atr_t = float(atr[t - 1]) if np.isfinite(atr[t - 1]) and atr[t - 1] > 0 else float("nan")
    # ATR at last feature bar (t-1), not future bar t
    close_loc = float((close_end - np.min(l)) / rng) if rng > 0 else float("nan")
    # last feature bar close location within that bar
    last_bar_rng = float(h[-1] - l[-1])
    last_close_loc = float((c[-1] - l[-1]) / last_bar_rng) if last_bar_rng > 0 else float("nan")

    rets = np.diff(c) / c[:-1] if len(c) > 1 else np.array([])
    rets = rets[np.isfinite(rets)]
    price_slope, _ = _slope_r2(c)
    rv = float(np.std(rets)) if len(rets) else float("nan")
    abs_path = float(np.sum(np.abs(rets))) if len(rets) else float("nan")
    efficiency = (
        float(abs(close_end - open_start) / (open_start * abs_path))
        if open_start > 0 and abs_path == abs_path and abs_path > 0
        else float("nan")
    )
    # diagnostic MFE/MAE inside feature window vs open_start
    if open_start > 0:
        feat_up = float(np.max(h) / open_start - 1.0)
        feat_dn = float(np.min(l) / open_start - 1.0)
    else:
        feat_up = feat_dn = float("nan")

    if not np.isfinite(d_w).all():
        d_w = buy_w - sell_w
    delta_sum = float(np.nansum(d_w))
    delta_mean = float(np.nanmean(d_w))
    vol_sum = float(np.nansum(tot_w))
    delta_ratio = float(delta_sum / vol_sum) if vol_sum > 0 else float("nan")
    d_ok = d_w[np.isfinite(d_w)]
    pos_d = float(np.mean(d_ok > 0)) if len(d_ok) else float("nan")
    neg_d = float(np.mean(d_ok < 0)) if len(d_ok) else float("nan")
    d_slope, _ = _slope_r2(d_ok) if len(d_ok) >= 2 else (float("nan"), float("nan"))
    last_delta = float(d_w[-1]) if np.isfinite(d_w[-1]) else float("nan")
    last_dr = float(dr_w[-1]) if np.isfinite(dr_w[-1]) else float("nan")

    # F3 / relative refs evaluated at last feature bar index (t-1)
    last_i = t - 1
    p90 = float(df["abs_delta_ratio_p90_prior"].iloc[last_i])
    dr_z = float(df["delta_ratio_z"].iloc[last_i])
    vol_med = float(df["volume_median_prior"].iloc[last_i])
    rel_vol = float(vol_sum / (vol_med * lookback)) if vol_med == vol_med and vol_med > 0 else float("nan")

    oi_start = float(oi_w[0])
    oi_end = float(oi_w[-1])
    oi_valid = np.isfinite(oi_start) and oi_start > 0 and np.isfinite(oi_end) and oi_end > 0
    if oi_valid:
        oi_chg = float(oi_end / oi_start - 1.0)
        oi_diffs = np.diff(oi_w)
        oi_diffs = oi_diffs[np.isfinite(oi_diffs)]
        pos_oi = float(np.mean(oi_diffs > 0)) if len(oi_diffs) else float("nan")
    else:
        oi_chg = pos_oi = float("nan")

    return {
        "symbol": str(df["symbol"].iloc[t]),
        "timestamp": str(df["bucket_start"].iloc[t]),
        "lookback": int(lookback),
        "anchor_i": int(t),
        "sequence_id": int(seq[t]) if isinstance(seq[t], (int, np.integer)) else seq[t],
        "open_start": open_start,
        "close_end": close_end,
        "price_return": price_ret,
        "price_return_abs": abs(price_ret) if price_ret == price_ret else float("nan"),
        "range": rng,
        "range_atr": float(rng / atr_t) if atr_t == atr_t and atr_t > 0 else float("nan"),
        "price_slope": price_slope,
        "realized_vol": rv,
        "pos_return_share": float(np.mean(rets > 0)) if len(rets) else float("nan"),
        "close_location": close_loc,
        "last_bar_close_location": last_close_loc,
        "feature_window_mfe": feat_up,
        "feature_window_mae": feat_dn,
        "price_efficiency": efficiency,
        "atr_14": atr_t,
        "buy_volume_sum": float(np.nansum(buy_w)),
        "sell_volume_sum": float(np.nansum(sell_w)),
        "total_volume_sum": vol_sum,
        "delta_sum": delta_sum,
        "delta_mean": delta_mean,
        "delta_ratio": delta_ratio,
        "pos_delta_bar_share": pos_d,
        "neg_delta_bar_share": neg_d,
        "delta_slope": d_slope,
        "cum_delta": delta_sum,
        "last_bar_delta": last_delta,
        "last_bar_delta_ratio": last_dr,
        "delta_ratio_z": dr_z,
        "abs_delta_ratio_p90_prior": p90,
        "rel_volume_vs_prior_median": rel_vol,
        "oi_change_pct": oi_chg,
        "oi_valid": bool(oi_valid),
        "pos_oi_step_share": pos_oi,
        "anchor_close": float(closes[t]),
    }


def compute_feature_rows(df: pd.DataFrame, cfg: AbsorptionConfig) -> list[dict[str, Any]]:
    df = enrich_frame(df, cfg)
    rows: list[dict[str, Any]] = []
    n = len(df)
    for lb in cfg.lookbacks:
        for t in range(lb, n):
            feat = features_at(df, t, lb)
            if feat is not None:
                rows.append(feat)
    return rows
