"""Forward returns and path excursions on the 5m grid (label-only future)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from orderbook_analyse.fractal_direction_and_entry import HORIZONS_MIN, ROUNDTRIP_FEE_PCT


def bars_for_horizon(minutes: int, *, bar_minutes: int = 5) -> int:
    return max(1, int(minutes // bar_minutes))


def _path_excursions(
    close: np.ndarray, high: np.ndarray, low: np.ndarray, hb: int
) -> tuple[np.ndarray, np.ndarray]:
    """MFE/MAE over bars i+1 .. i+hb relative to close[i]."""
    n = len(close)
    fav = np.full(n, np.nan, dtype=np.float64)
    adv = np.full(n, np.nan, dtype=np.float64)
    if n <= hb + 1:
        return fav, adv
    wh = sliding_window_view(high, hb)  # (n-hb+1, hb)
    wl = sliding_window_view(low, hb)
    idx = np.arange(0, n - hb, dtype=np.int64)
    w_idx = idx + 1  # window starts at next bar
    # valid when w_idx + hb - 1 < n  => w_idx <= n-hb
    valid = w_idx <= (n - hb)
    idx = idx[valid]
    w_idx = w_idx[valid]
    hmax = wh[w_idx].max(axis=1)
    lmin = wl[w_idx].min(axis=1)
    c0 = close[idx]
    fav[idx] = (hmax / c0 - 1.0) * 100.0
    adv[idx] = (lmin / c0 - 1.0) * 100.0
    return fav, adv


def attach_forward_outcomes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Causal entry = close at decision bar.
    Forward path uses subsequent bars only (i+1 ...).
    Directional sign applied later by state/side.
    """
    out = df.copy()
    close = out["close"].astype(float).to_numpy()
    high = out["high"].astype(float).to_numpy()
    low = out["low"].astype(float).to_numpy()
    n = len(out)

    for h in HORIZONS_MIN:
        hb = bars_for_horizon(h)
        ret = np.full(n, np.nan, dtype=np.float64)
        if hb < n:
            ret[: n - hb] = (close[hb:] / close[: n - hb] - 1.0) * 100.0
        fav, adv = _path_excursions(close, high, low, hb)
        out[f"raw_ret_{h}m"] = ret
        out[f"raw_mfe_{h}m"] = fav
        out[f"raw_mae_{h}m"] = adv
        out[f"raw_ret_{h}m_net_fee"] = ret - ROUNDTRIP_FEE_PCT
    return out


def signed_direction_metrics(sub: pd.DataFrame, *, bullish: bool) -> dict:
    """For bullish states, raw+ is correct; for bearish, raw- is correct."""
    sign = 1.0 if bullish else -1.0
    out: dict = {"n": int(len(sub))}
    if sub.empty:
        return out
    for h in HORIZONS_MIN:
        r = sub[f"raw_ret_{h}m"].astype(float) * sign
        mfe_raw = sub[f"raw_mfe_{h}m"].astype(float)
        mae_raw = sub[f"raw_mae_{h}m"].astype(float)
        if bullish:
            fav = mfe_raw
            adv = mae_raw
        else:
            fav = -mae_raw
            adv = -mfe_raw
        out[f"hit_rate_{h}m"] = float((r > 0).mean()) if r.notna().any() else None
        out[f"median_dir_ret_{h}m"] = float(r.median()) if r.notna().any() else None
        out[f"mean_dir_ret_{h}m"] = float(r.mean()) if r.notna().any() else None
        out[f"median_fav_{h}m"] = float(fav.median()) if fav.notna().any() else None
        out[f"median_adv_{h}m"] = float(adv.median()) if adv.notna().any() else None
        rn = r - ROUNDTRIP_FEE_PCT
        out[f"median_dir_ret_{h}m_net_fee"] = float(rn.median()) if rn.notna().any() else None
        out[f"hit_rate_{h}m_net_fee"] = float((rn > 0).mean()) if rn.notna().any() else None
    return out
