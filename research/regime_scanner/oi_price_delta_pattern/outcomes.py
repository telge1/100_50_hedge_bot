"""Forward outcomes from t+1 (no look-ahead into features)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.oi_price_delta_pattern.config import PatternConfig, thr_label
from research.regime_scanner.oi_price_delta_pattern.features import _contiguous


def forward_outcome_at(
    df: pd.DataFrame,
    t: int,
    *,
    horizons: tuple[int, ...],
    thresholds: tuple[float, ...],
) -> dict[str, Any] | None:
    """Outcomes using bars t+1 .. t+H relative to close[t]."""
    n = len(df)
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    entry = float(closes[t])
    if not (entry > 0 and np.isfinite(entry)):
        return None

    max_h = max(horizons)
    if t + 1 >= n:
        return None
    end_max = min(n - 1, t + max_h)
    if not _contiguous(seq, ts, t, end_max):
        # still allow partial horizons that stay contiguous
        pass

    out: dict[str, Any] = {
        "symbol": str(df["symbol"].iloc[t]),
        "timestamp": str(df["bucket_start"].iloc[t]),
        "anchor_i": int(t),
        "anchor_close": entry,
    }

    for h in horizons:
        end = min(n - 1, t + h)
        if end < t + 1:
            continue
        if not _contiguous(seq, ts, t, end):
            # mark invalid horizon
            out[f"h{h}_valid"] = False
            continue
        out[f"h{h}_valid"] = True
        sl = slice(t + 1, end + 1)
        hh, ll, cc = highs[sl], lows[sl], closes[sl]
        # up/down % from entry
        up_path = (hh / entry - 1.0) * 100.0
        down_path = (ll / entry - 1.0) * 100.0  # negative when down
        mfe = float(np.max(up_path))
        mae = float(np.min(down_path))
        close_ret = float(cc[-1] / entry - 1.0) * 100.0
        out[f"h{h}_mfe_pct"] = mfe
        out[f"h{h}_mae_pct"] = mae
        out[f"h{h}_close_ret_pct"] = close_ret
        out[f"h{h}_edge"] = mfe - abs(mae)
        out[f"h{h}_abs_expansion"] = max(abs(mfe), abs(mae))
        out[f"h{h}_stronger_side"] = "up" if mfe >= abs(mae) else "down"

        for thr in thresholds:
            thr_pct = thr * 100.0
            tag = thr_label(thr)
            up_hit = up_path >= thr_pct
            dn_hit = down_path <= -thr_pct
            up_bars = int(np.argmax(up_hit)) if up_hit.any() else None
            dn_bars = int(np.argmax(dn_hit)) if dn_hit.any() else None
            up_reached = bool(up_hit.any())
            dn_reached = bool(dn_hit.any())
            same = up_reached and dn_reached and up_bars == dn_bars
            if same:
                up_first = False
                dn_first = True  # conservative adverse for "direction race"
                order = "same_bar_conservative_down_first"
            elif up_reached and (not dn_reached or (up_bars is not None and dn_bars is not None and up_bars < dn_bars)):
                up_first, dn_first, order = True, False, "up_first"
            elif dn_reached and (not up_reached or (dn_bars is not None and up_bars is not None and dn_bars < up_bars)):
                up_first, dn_first, order = False, True, "down_first"
            else:
                up_first = dn_first = False
                order = "neither"
            pref = f"h{h}_{tag}"
            out[f"{pref}_up_reached"] = up_reached
            out[f"{pref}_down_reached"] = dn_reached
            out[f"{pref}_up_first"] = up_first
            out[f"{pref}_down_first"] = dn_first
            out[f"{pref}_both"] = bool(up_reached and dn_reached)
            out[f"{pref}_neither"] = bool(not up_reached and not dn_reached)
            out[f"{pref}_same_bar"] = same
            out[f"{pref}_order"] = order
            out[f"{pref}_bars_to_up"] = up_bars
            out[f"{pref}_bars_to_down"] = dn_bars

    return out


def compute_outcomes(
    df: pd.DataFrame,
    feature_rows: list[dict[str, Any]],
    cfg: PatternConfig,
) -> list[dict[str, Any]]:
    """One outcome row per (symbol, timestamp, lookback) anchor from features."""
    # index by anchor_i for this frame
    by_i: dict[int, list[dict[str, Any]]] = {}
    for fr in feature_rows:
        by_i.setdefault(int(fr["anchor_i"]), []).append(fr)

    rows: list[dict[str, Any]] = []
    for t, feats in by_i.items():
        oc = forward_outcome_at(df, t, horizons=cfg.horizons, thresholds=cfg.move_thresholds)
        if oc is None:
            continue
        for fr in feats:
            rows.append({**oc, "lookback": fr["lookback"]})
    return rows
