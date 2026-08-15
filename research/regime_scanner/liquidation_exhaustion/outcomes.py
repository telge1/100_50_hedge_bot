"""Forward outcomes wrapping shared path/first-touch helpers."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.config import (
    COST_PCT,
    EXIT_HOLDS,
    EXIT_MODELS,
    FIRST_TOUCH_ATR,
    FIRST_TOUCH_PCT,
    MFE_HORIZONS,
)
from research.regime_scanner.liquidity_sweep_reclaim.outcomes import (
    frame_arrays,
    side_sign,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    first_touch_level,
    path_arrays,
)
from research.regime_scanner.small_target_single_trade.outcomes import evaluate_outcome_params


def compute_forward_outcomes(
    frame: pd.DataFrame,
    *,
    fill_i: int,
    entry: float,
    side: str,
) -> dict[str, Any]:
    f = frame
    if "timestamp" not in f.columns and "bucket_start" in f.columns:
        f = f.copy()
        f["timestamp"] = f["bucket_start"]
    arrays = frame_arrays(f)
    s = side_sign(side)
    highs, lows, closes, atr = arrays["highs"], arrays["lows"], arrays["closes"], arrays["atr"]
    n = arrays["n"]
    max_h = max(MFE_HORIZONS)
    end_max = min(n - 1, fill_i + int(max_h) - 1)
    out: dict[str, Any] = {}
    if end_max < fill_i:
        return out
    path = path_arrays(s, entry, highs, lows, closes, fill_i, end_max)
    fav, adv, close_s = path.get("fav"), path.get("adv"), path.get("close_s")
    atr0 = float(atr[fill_i]) if np.isfinite(atr[fill_i]) and atr[fill_i] > 0 else np.nan

    for h in MFE_HORIZONS:
        end_off = min(len(close_s) - 1, int(h) - 1) if close_s is not None else -1
        if end_off < 0 or fav is None or adv is None or close_s is None:
            continue
        mfe = float(np.max(fav[: end_off + 1]))
        mae = float(np.min(adv[: end_off + 1]))
        out[f"h{h}_close_ret"] = float(close_s[end_off])
        out[f"h{h}_mfe_pct"] = mfe
        out[f"h{h}_mae_pct"] = mae
        out[f"h{h}_mfe_atr"] = (mfe / 100.0 * entry / atr0) if atr0 == atr0 and atr0 > 0 else None
        out[f"h{h}_mae_atr"] = (abs(mae) / 100.0 * entry / atr0) if atr0 == atr0 and atr0 > 0 else None
        out[f"h{h}_bars_to_mfe"] = int(np.argmax(fav[: end_off + 1]))
        out[f"h{h}_bars_to_mae"] = int(np.argmin(adv[: end_off + 1]))

    # percent first-touch
    for lvl in FIRST_TOUCH_PCT:
        for sign, tag in ((1, "p"), (-1, "m")):
            ft = first_touch_level(s, entry, highs, lows, fill_i, end_max, float(sign * lvl))
            key = f"ft_{tag}{lvl:.2f}".replace(".", "_")
            out[f"{key}_reached"] = bool(ft.get("reached"))
            out[f"{key}_bars"] = ft.get("bar_offset")

    # ATR first-touch
    if atr0 == atr0 and atr0 > 0:
        for mult in FIRST_TOUCH_ATR:
            pct = (mult * atr0 / entry) * 100.0
            for sign, tag in ((1, "atrp"), (-1, "atrm")):
                ft = first_touch_level(s, entry, highs, lows, fill_i, end_max, float(sign * pct))
                key = f"ft_{tag}{mult:.1f}".replace(".", "_")
                out[f"{key}_reached"] = bool(ft.get("reached"))
                out[f"{key}_bars"] = ft.get("bar_offset")

    # same-bar 0.5% conservative
    tp = first_touch_level(s, entry, highs, lows, fill_i, fill_i, 0.50)
    sl = first_touch_level(s, entry, highs, lows, fill_i, fill_i, -0.50)
    same = bool(tp.get("reached") and sl.get("reached"))
    out["same_bar_ambiguous"] = same
    if same:
        out["first_touch_order"] = "adverse_first_conservative"
        out["favorable_first"] = False
        out["adverse_first"] = True
    elif tp.get("reached"):
        out["first_touch_order"] = "favorable_first"
        out["favorable_first"] = True
        out["adverse_first"] = False
    elif sl.get("reached"):
        out["first_touch_order"] = "adverse_first"
        out["favorable_first"] = False
        out["adverse_first"] = True
    else:
        out["first_touch_order"] = "neither"
        out["favorable_first"] = False
        out["adverse_first"] = False
    return out


def diagnostic_exits(
    frame: pd.DataFrame,
    *,
    fill_i: int,
    entry: float,
    side: str,
) -> list[dict[str, Any]]:
    f = frame
    if "timestamp" not in f.columns and "bucket_start" in f.columns:
        f = f.copy()
        f["timestamp"] = f["bucket_start"]
    arrays = frame_arrays(f)
    s = side_sign(side)
    rows = []
    for xid, (tp, sl_mag, _hold_default) in EXIT_MODELS.items():
        for hold in EXIT_HOLDS:
            for cost in COST_PCT:
                res = evaluate_outcome_params(
                    side=s,
                    entry=entry,
                    highs=arrays["highs"],
                    lows=arrays["lows"],
                    closes=arrays["closes"],
                    timestamps=arrays["timestamps"],
                    fill_i=fill_i,
                    n_bars=arrays["n"],
                    tp_pct=float(tp),
                    sl_pct=float(-abs(sl_mag)),
                    horizon_bars=int(hold),
                    cost_pct=float(cost),
                )
                rows.append(
                    {
                        "exit_id": xid,
                        "tp_pct": tp,
                        "sl_pct": sl_mag,
                        "hold": hold,
                        "cost_pct": cost,
                        "reason": res.get("reason"),
                        "net_pct": res.get("net_pct"),
                        "gross_pct": res.get("gross_pct"),
                        "bars_held": res.get("bars_held"),
                    }
                )
    return rows
