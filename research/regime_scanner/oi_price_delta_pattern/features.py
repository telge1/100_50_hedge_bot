"""Causal lookback features for price / OI / orderflow delta (exclude bar t)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.features import _true_range, _wilder_atr
from research.regime_scanner.oi_price_delta_pattern.config import ATR_PERIOD, BAR_SECONDS


def enrich_atr(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = _true_range(high, low, prev)
    out["atr_14"] = _wilder_atr(tr, ATR_PERIOD)
    out["true_range"] = tr
    return out


def _contiguous(seq: np.ndarray, ts: np.ndarray, i0: int, i1: int) -> bool:
    if i0 < 0 or i1 >= len(seq) or i0 > i1:
        return False
    s0 = seq[i0]
    for i in range(i0, i1 + 1):
        if seq[i] != s0:
            return False
        if i > i0:
            dt = (pd.Timestamp(ts[i]) - pd.Timestamp(ts[i - 1])).total_seconds()
            if dt != BAR_SECONDS:
                return False
    return True


def _slope_r2(y: np.ndarray) -> tuple[float, float]:
    if len(y) < 2 or not np.isfinite(y).all():
        return float("nan"), float("nan")
    x = np.arange(len(y), dtype=float)
    coef = np.polyfit(x, y, 1)
    pred = np.polyval(coef, x)
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(coef[0]), float(r2)


def _longest_pos_run(diffs: np.ndarray) -> int:
    best = cur = 0
    for d in diffs:
        if d > 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def features_at(df: pd.DataFrame, t: int, lookback: int) -> dict[str, Any] | None:
    """Features for lookback window [t-lookback, t) — bar t excluded."""
    if t < lookback:
        return None
    i0, i1 = t - lookback, t - 1  # inclusive end of past window
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    # require contiguous past window AND anchor bar present in same sequence
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

    o = opens[i0:t]
    h = highs[i0:t]
    l = lows[i0:t]
    c = closes[i0:t]
    oi_w = oi[i0:t]
    buy_w = buy[i0:t]
    sell_w = sell[i0:t]
    tot_w = tot[i0:t]
    d_w = delta[i0:t]

    if not (np.isfinite(o).all() and np.isfinite(h).all() and np.isfinite(l).all() and np.isfinite(c).all()):
        return None

    price_ret = float(c[-1] / o[0] - 1.0) if o[0] > 0 else float("nan")
    rng = float(np.max(h) - np.min(l))
    atr_t = float(atr[t]) if np.isfinite(atr[t]) and atr[t] > 0 else float("nan")
    close_pos = float((c[-1] - np.min(l)) / rng) if rng > 0 else float("nan")
    rets = np.diff(c) / c[:-1] if len(c) > 1 else np.array([])
    rets = rets[np.isfinite(rets)]
    price_slope, _ = _slope_r2(c)
    rv = float(np.std(rets)) if len(rets) else float("nan")

    oi_start = float(oi_w[0])
    oi_end = float(oi_w[-1])
    oi_valid = np.isfinite(oi_start) and oi_start > 0 and np.isfinite(oi_end) and oi_end > 0
    if oi_valid:
        oi_chg_pct = float(oi_end / oi_start - 1.0)
        oi_diffs = np.diff(oi_w)
        oi_diffs = oi_diffs[np.isfinite(oi_diffs)]
        pos_oi = float(np.mean(oi_diffs > 0)) if len(oi_diffs) else float("nan")
        neg_oi = float(np.mean(oi_diffs < 0)) if len(oi_diffs) else float("nan")
        oi_slope, oi_r2 = _slope_r2(oi_w[np.isfinite(oi_w)])
        longest = _longest_pos_run(oi_diffs) if len(oi_diffs) else 0
    else:
        oi_chg_pct = float("nan")
        pos_oi = neg_oi = oi_slope = oi_r2 = float("nan")
        longest = 0

    # Prefer DB delta; recompute if needed
    if not np.isfinite(d_w).all():
        d_w = buy_w - sell_w
    delta_sum = float(np.nansum(d_w))
    delta_mean = float(np.nanmean(d_w))
    vol_sum = float(np.nansum(tot_w))
    delta_ratio = float(delta_sum / vol_sum) if vol_sum > 0 else float("nan")
    d_ok = d_w[np.isfinite(d_w)]
    pos_d = float(np.mean(d_ok > 0)) if len(d_ok) else float("nan")
    neg_d = float(np.mean(d_ok < 0)) if len(d_ok) else float("nan")
    cum_delta = float(np.nansum(d_w))
    d_slope, _ = _slope_r2(d_ok) if len(d_ok) >= 2 else (float("nan"), float("nan"))
    vol_mean = float(np.nanmean(tot_w)) if np.isfinite(tot_w).any() else float("nan")
    vol_last_rel = float(tot_w[-1] / vol_mean) if vol_mean and vol_mean > 0 and np.isfinite(tot_w[-1]) else float("nan")

    return {
        "symbol": str(df["symbol"].iloc[t]),
        "timestamp": str(df["bucket_start"].iloc[t]),
        "lookback": int(lookback),
        "anchor_i": int(t),
        "sequence_id": int(seq[t]) if np.issubdtype(type(seq[t]), np.integer) or isinstance(seq[t], (int, np.integer)) else seq[t],
        "price_return": price_ret,
        "price_return_abs": abs(price_ret) if price_ret == price_ret else float("nan"),
        "range": rng,
        "range_atr": float(rng / atr_t) if atr_t == atr_t and atr_t > 0 else float("nan"),
        "close_pos_in_range": close_pos,
        "price_slope": price_slope,
        "pos_return_share": float(np.mean(rets > 0)) if len(rets) else float("nan"),
        "realized_vol": rv,
        "atr_14": atr_t,
        "oi_start": oi_start if oi_valid else float("nan"),
        "oi_end": oi_end if oi_valid else float("nan"),
        "oi_change_abs": float(oi_end - oi_start) if oi_valid else float("nan"),
        "oi_change_pct": oi_chg_pct,
        "oi_valid": bool(oi_valid),
        "pos_oi_step_share": pos_oi,
        "neg_oi_step_share": neg_oi,
        "longest_pos_oi_run": longest,
        "oi_slope": oi_slope,
        "oi_r2": oi_r2,
        "buy_volume_sum": float(np.nansum(buy_w)),
        "sell_volume_sum": float(np.nansum(sell_w)),
        "total_volume_sum": vol_sum,
        "delta_sum": delta_sum,
        "delta_mean": delta_mean,
        "delta_ratio": delta_ratio,
        "pos_delta_bar_share": pos_d,
        "neg_delta_bar_share": neg_d,
        "cum_delta": cum_delta,
        "delta_slope": d_slope,
        "volume_last_vs_mean": vol_last_rel,
        "anchor_close": float(closes[t]),
    }


def compute_feature_rows(df: pd.DataFrame, lookbacks: tuple[int, ...]) -> list[dict[str, Any]]:
    df = enrich_atr(df)
    rows: list[dict[str, Any]] = []
    n = len(df)
    for lb in lookbacks:
        for t in range(lb, n):
            feat = features_at(df, t, lb)
            if feat is not None:
                rows.append(feat)
    return rows
