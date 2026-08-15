"""Forward outcomes from t+1 with side-aware favorable/adverse helpers."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.oi_price_delta_pattern.features import _contiguous
from research.regime_scanner.orderflow_absorption.config import AbsorptionConfig, thr_label


def forward_outcome_at(
    df: pd.DataFrame,
    t: int,
    *,
    horizons: tuple[int, ...],
    thresholds: tuple[float, ...],
) -> dict[str, Any] | None:
    n = len(df)
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    entry = float(closes[t])
    if not (entry > 0 and np.isfinite(entry)):
        return None
    if t + 1 >= n:
        return None

    out: dict[str, Any] = {
        "symbol": str(df["symbol"].iloc[t]),
        "timestamp": str(df["bucket_start"].iloc[t]),
        "anchor_i": int(t),
        "anchor_close": entry,
    }

    for h in horizons:
        end = min(n - 1, t + h)
        if end < t + 1 or not _contiguous(seq, ts, t, end):
            out[f"h{h}_valid"] = False
            continue
        out[f"h{h}_valid"] = True
        sl = slice(t + 1, end + 1)
        hh, ll, cc = highs[sl], lows[sl], closes[sl]
        up_path = (hh / entry - 1.0) * 100.0
        down_path = (ll / entry - 1.0) * 100.0
        mfe = float(np.max(up_path))
        mae = float(np.min(down_path))
        close_ret = float(cc[-1] / entry - 1.0) * 100.0
        out[f"h{h}_mfe_pct"] = mfe
        out[f"h{h}_mae_pct"] = mae
        out[f"h{h}_close_ret_pct"] = close_ret
        out[f"h{h}_edge"] = mfe - abs(mae)
        out[f"h{h}_abs_expansion"] = max(abs(mfe), abs(mae))
        out[f"h{h}_stronger_side"] = "up" if mfe >= abs(mae) else "down"
        # side-aware excursion (positive numbers)
        out[f"h{h}_bull_mfe"] = mfe
        out[f"h{h}_bull_mae"] = abs(mae)
        out[f"h{h}_bull_edge"] = mfe - abs(mae)
        out[f"h{h}_bear_mfe"] = abs(mae)
        out[f"h{h}_bear_mae"] = mfe
        out[f"h{h}_bear_edge"] = abs(mae) - mfe

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
                # raw race left unset; side-aware applied in summary
                up_first = dn_first = False
                order = "same_bar"
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
            # bullish: fav=up, adv=down; same-bar → adverse-first
            if same:
                out[f"{pref}_bull_fav_first"] = False
                out[f"{pref}_bull_adv_first"] = True
                out[f"{pref}_bear_fav_first"] = False
                out[f"{pref}_bear_adv_first"] = True
            else:
                out[f"{pref}_bull_fav_first"] = up_first
                out[f"{pref}_bull_adv_first"] = dn_first
                out[f"{pref}_bear_fav_first"] = dn_first
                out[f"{pref}_bear_adv_first"] = up_first

    return out


def compute_outcomes(
    df: pd.DataFrame,
    feature_rows: list[dict[str, Any]],
    cfg: AbsorptionConfig,
) -> list[dict[str, Any]]:
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
