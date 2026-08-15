"""Causal 5m features for liquidation exhaustion (no centered windows / no FF)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _true_range(high: np.ndarray, low: np.ndarray, prev_close: np.ndarray) -> np.ndarray:
    a = high - low
    b = np.abs(high - prev_close)
    c = np.abs(low - prev_close)
    return np.nanmax(np.vstack([a, b, c]), axis=0)


def _wilder_atr(tr: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(tr)
    out = np.full(n, np.nan)
    if n < period:
        return out
    out[period - 1] = np.nanmean(tr[:period])
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _causal_rolling_median(x: np.ndarray, window: int, valid: np.ndarray) -> np.ndarray:
    """Median of previous `window` valid points (excludes current)."""
    n = len(x)
    out = np.full(n, np.nan)
    buf: list[float] = []
    for i in range(n):
        if len(buf) >= window:
            out[i] = float(np.median(buf[-window:]))
        if valid[i] and np.isfinite(x[i]):
            buf.append(float(x[i]))
        else:
            # sequence/gap break → reset buffer
            buf = []
    return out


def _causal_rolling_mad(x: np.ndarray, window: int, valid: np.ndarray) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan)
    buf: list[float] = []
    for i in range(n):
        if len(buf) >= window:
            med = float(np.median(buf[-window:]))
            out[i] = float(np.median(np.abs(np.asarray(buf[-window:]) - med)))
        if valid[i] and np.isfinite(x[i]):
            buf.append(float(x[i]))
        else:
            buf = []
    return out


def _causal_percentile(x: np.ndarray, window: int, valid: np.ndarray, q: float) -> np.ndarray:
    """Percentile of previous `window` valid values (current excluded)."""
    n = len(x)
    out = np.full(n, np.nan)
    buf: list[float] = []
    for i in range(n):
        if len(buf) >= window:
            out[i] = float(np.percentile(buf[-window:], q))
        if valid[i] and np.isfinite(x[i]):
            buf.append(float(x[i]))
        else:
            buf = []
    return out


def _same_sequence_change(x: np.ndarray, seq: np.ndarray, bars: int) -> np.ndarray:
    """x[i] - x[i-bars] only if all intermediate bars same sequence_id and present."""
    n = len(x)
    out = np.full(n, np.nan)
    for i in range(bars, n):
        if not np.isfinite(x[i]) or not np.isfinite(x[i - bars]):
            continue
        if seq[i] != seq[i - bars]:
            continue
        # require contiguous indices within same sequence (no gap in frame)
        if not np.all(seq[i - bars : i + 1] == seq[i]):
            continue
        # also require no NaN holes in between for price/OI continuity
        ok = True
        for j in range(i - bars + 1, i):
            if not np.isfinite(x[j]):
                ok = False
                break
        if not ok:
            continue
        out[i] = float(x[i] - x[i - bars])
    return out


def enrich_symbol_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add causal features for a single-symbol sorted 5m frame."""
    d = df.sort_values("bucket_start").reset_index(drop=True).copy()
    if "timestamp" not in d.columns:
        d["timestamp"] = d["bucket_start"]
    n = len(d)
    if n == 0:
        return d

    o = d["open"].to_numpy(dtype=float)
    h = d["high"].to_numpy(dtype=float)
    l = d["low"].to_numpy(dtype=float)
    c = d["close"].to_numpy(dtype=float)
    seq = d["sequence_id"].to_numpy(dtype=int)
    oi = d["open_interest"].to_numpy(dtype=float)
    long_liq = d["long_liquidation_usd"].to_numpy(dtype=float)
    short_liq = d["short_liquidation_usd"].to_numpy(dtype=float)
    buy = d["buy_volume"].to_numpy(dtype=float)
    sell = d["sell_volume"].to_numpy(dtype=float)
    total_vol = d["total_volume"].to_numpy(dtype=float)
    delta = d["delta"].to_numpy(dtype=float) if "delta" in d.columns else buy - sell

    # contiguous validity: consecutive 5m steps within same sequence
    valid = np.ones(n, dtype=bool)
    for i in range(1, n):
        dt = (d["bucket_start"].iloc[i] - d["bucket_start"].iloc[i - 1]).total_seconds()
        if dt != 300 or seq[i] != seq[i - 1]:
            valid[i] = False  # marks break AFTER gap for rolling reset at i

    # For rolling buffers we reset when current bar is not a clean continuation
    roll_valid = np.ones(n, dtype=bool)
    roll_valid[0] = True
    for i in range(1, n):
        dt = (d["bucket_start"].iloc[i] - d["bucket_start"].iloc[i - 1]).total_seconds()
        roll_valid[i] = dt == 300 and seq[i] == seq[i - 1]

    prev_c = np.roll(c, 1)
    prev_c[0] = np.nan
    tr = _true_range(h, l, prev_c)
    atr = _wilder_atr(tr, 14)

    ret_5m = (c / prev_c - 1.0) * 100.0
    ret_5m[0] = np.nan

    def ret_bars(bars: int) -> np.ndarray:
        out = np.full(n, np.nan)
        for i in range(bars, n):
            if seq[i] != seq[i - bars]:
                continue
            if not np.all(seq[i - bars : i + 1] == seq[i]):
                continue
            if not np.isfinite(c[i]) or not np.isfinite(c[i - bars]) or c[i - bars] == 0:
                continue
            out[i] = (c[i] / c[i - bars] - 1.0) * 100.0
        return out

    d["ret_5m_pct"] = ret_5m
    d["ret_15m_pct"] = ret_bars(3)
    d["ret_30m_pct"] = ret_bars(6)
    d["ret_1h_pct"] = ret_bars(12)
    d["true_range"] = tr
    d["atr_14"] = atr
    d["range_atr"] = (h - l) / atr
    d["clv"] = np.where((h - l) > 0, ((c - l) - (h - c)) / (h - l), 0.0)
    d["dist_prev_close_pct"] = ret_5m

    # prev 15m / 1h high-low distance (causal completed windows ending previous bar)
    dist_15h = np.full(n, np.nan)
    dist_15l = np.full(n, np.nan)
    dist_1hh = np.full(n, np.nan)
    dist_1hl = np.full(n, np.nan)
    for i in range(n):
        if i >= 3 and np.all(seq[i - 3 : i] == seq[i - 1]):
            ph, pl = np.max(h[i - 3 : i]), np.min(l[i - 3 : i])
            dist_15h[i] = (c[i] / ph - 1.0) * 100.0 if ph > 0 else np.nan
            dist_15l[i] = (c[i] / pl - 1.0) * 100.0 if pl > 0 else np.nan
        if i >= 12 and np.all(seq[i - 12 : i] == seq[i - 1]):
            ph, pl = np.max(h[i - 12 : i]), np.min(l[i - 12 : i])
            dist_1hh[i] = (c[i] / ph - 1.0) * 100.0 if ph > 0 else np.nan
            dist_1hl[i] = (c[i] / pl - 1.0) * 100.0 if pl > 0 else np.nan
    d["dist_prev_15m_high_pct"] = dist_15h
    d["dist_prev_15m_low_pct"] = dist_15l
    d["dist_prev_1h_high_pct"] = dist_1hh
    d["dist_prev_1h_low_pct"] = dist_1hl

    # OI changes — never across sequence
    d["oi_chg_5m"] = _same_sequence_change(oi, seq, 1)
    d["oi_chg_15m"] = _same_sequence_change(oi, seq, 3)
    d["oi_chg_30m"] = _same_sequence_change(oi, seq, 6)
    d["oi_chg_1h"] = _same_sequence_change(oi, seq, 12)
    with np.errstate(divide="ignore", invalid="ignore"):
        d["oi_chg_5m_pct"] = np.where(
            np.roll(oi, 1) != 0, d["oi_chg_5m"].to_numpy() / np.roll(oi, 1) * 100.0, np.nan
        )
    d.loc[0, "oi_chg_5m_pct"] = np.nan

    oi_med = _causal_rolling_median(d["oi_chg_5m"].to_numpy(dtype=float), 288, roll_valid)
    oi_mad = _causal_rolling_mad(d["oi_chg_5m"].to_numpy(dtype=float), 288, roll_valid)
    d["oi_chg_vs_median"] = d["oi_chg_5m"].to_numpy(dtype=float) - oi_med
    with np.errstate(divide="ignore", invalid="ignore"):
        d["oi_chg_z"] = np.where(oi_mad > 0, (d["oi_chg_5m"].to_numpy(dtype=float) - oi_med) / oi_mad, np.nan)
    d["oi_chg_p25"] = _causal_percentile(d["oi_chg_5m"].to_numpy(dtype=float), 288, roll_valid, 25)

    # Liquidations
    total_liq = np.nan_to_num(long_liq, nan=0.0) + np.nan_to_num(short_liq, nan=0.0)
    d["long_liq_usd"] = long_liq
    d["short_liq_usd"] = short_liq
    d["total_liq_usd"] = total_liq
    with np.errstate(divide="ignore", invalid="ignore"):
        d["long_liq_share"] = np.where(total_liq > 0, long_liq / total_liq, np.nan)
        d["short_liq_share"] = np.where(total_liq > 0, short_liq / total_liq, np.nan)
        d["long_liq_intensity"] = np.where(total_vol > 0, long_liq / total_vol, np.nan)
        d["short_liq_intensity"] = np.where(total_vol > 0, short_liq / total_vol, np.nan)

    for side, col in (("long", long_liq), ("short", short_liq)):
        med = _causal_rolling_median(col, 288, roll_valid)
        mad = _causal_rolling_mad(col, 288, roll_valid)
        d[f"{side}_liq_median"] = med
        d[f"{side}_liq_mad"] = mad
        d[f"{side}_liq_p95"] = _causal_percentile(col, 288, roll_valid, 95)
        with np.errstate(divide="ignore", invalid="ignore"):
            d[f"{side}_liq_burst_ratio"] = np.where(med > 0, col / med, np.nan)
        intensity = d[f"{side}_liq_intensity"].to_numpy(dtype=float)
        d[f"{side}_liq_intensity_p95"] = _causal_percentile(intensity, 288, roll_valid, 95)

    # Orderflow
    d["buy_volume_f"] = buy
    d["sell_volume_f"] = sell
    d["delta_f"] = delta
    with np.errstate(divide="ignore", invalid="ignore"):
        d["delta_ratio_f"] = np.where(total_vol > 0, delta / total_vol, np.nan)
    d["delta_15m"] = _same_sequence_change(np.nancumsum(delta), seq, 3)  # rough; better sum
    # proper rolling sum within sequence
    def sum_bars(arr: np.ndarray, bars: int) -> np.ndarray:
        out = np.full(n, np.nan)
        for i in range(bars - 1, n):
            if not np.all(seq[i - bars + 1 : i + 1] == seq[i]):
                continue
            out[i] = float(np.nansum(arr[i - bars + 1 : i + 1]))
        return out

    d["delta_15m"] = sum_bars(delta, 3)
    d["delta_30m"] = sum_bars(delta, 6)
    # cumulative delta within sequence
    cum = np.zeros(n)
    for i in range(n):
        if i == 0 or seq[i] != seq[i - 1] or not roll_valid[i]:
            cum[i] = delta[i] if np.isfinite(delta[i]) else 0.0
        else:
            cum[i] = cum[i - 1] + (delta[i] if np.isfinite(delta[i]) else 0.0)
    d["cum_delta_seq"] = cum

    vol_med = _causal_rolling_median(total_vol, 288, roll_valid)
    with np.errstate(divide="ignore", invalid="ignore"):
        d["volume_burst"] = np.where(vol_med > 0, total_vol / vol_med, np.nan)
    d["spread_mean_f"] = d["spread_mean"].to_numpy(dtype=float) if "spread_mean" in d else np.nan
    d["spread_max_f"] = d["spread_max"].to_numpy(dtype=float) if "spread_max" in d else np.nan
    d["roll_valid"] = roll_valid
    d["warmup_ok"] = np.arange(n) >= 288  # need lookback history in buffer sense — refined in bursts
    return d


def enrich_features(joined: pd.DataFrame) -> pd.DataFrame:
    if joined.empty:
        return joined
    parts = [enrich_symbol_features(g) for _, g in joined.groupby("symbol", sort=True)]
    return pd.concat(parts, ignore_index=True)
