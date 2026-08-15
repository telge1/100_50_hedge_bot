"""Forward outcomes, follow-through, fakeouts, first-touch for breakouts."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidity_sweep_reclaim.outcomes import frame_arrays, side_sign
from research.regime_scanner.oi_compression_breakout.config import (
    COST_PCT,
    EXIT_HOLDS,
    EXIT_MODELS,
    FIRST_TOUCH_ATR,
    FIRST_TOUCH_BOX,
    FIRST_TOUCH_PCT,
    MFE_HORIZONS,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    first_touch_level,
    path_arrays,
)
from research.regime_scanner.small_target_single_trade.outcomes import evaluate_outcome_params


def follow_through_and_fakeout(
    df: pd.DataFrame,
    *,
    breakout_i: int,
    fill_i: int,
    side: str,
    box_high: float,
    box_low: float,
) -> dict[str, Any]:
    closes = df["close"].to_numpy(dtype=float)
    n = len(df)
    out: dict[str, Any] = {}
    for h in (1, 3, 6, 12):
        j = fill_i + h - 1
        if j >= n:
            out[f"outside_close_h{h}"] = None
            continue
        c = closes[j]
        if side == "long":
            out[f"outside_close_h{h}"] = bool(c > box_high)
        else:
            out[f"outside_close_h{h}"] = bool(c < box_low)

    # return to box / opposite break
    back_bars = None
    opp_bars = None
    max_dist = 0.0
    for k in range(fill_i, min(n, fill_i + 96)):
        c = closes[k]
        if side == "long":
            max_dist = max(max_dist, float(c - box_high))
            if c <= box_high and back_bars is None:
                back_bars = k - fill_i
            if c < box_low and opp_bars is None:
                opp_bars = k - fill_i
        else:
            max_dist = max(max_dist, float(box_low - c))
            if c >= box_low and back_bars is None:
                back_bars = k - fill_i
            if c > box_high and opp_bars is None:
                opp_bars = k - fill_i

    out["max_distance_outside"] = max_dist
    out["bars_to_box_return"] = back_bars
    out["bars_to_opposite_break"] = opp_bars
    out["F1_fakeout"] = bool(back_bars is not None and back_bars <= 3)
    out["F2_fakeout"] = bool(back_bars is not None and back_bars <= 6)
    out["F3_fakeout"] = bool(opp_bars is not None and opp_bars <= 12)
    return out


def compute_breakout_outcomes(
    df: pd.DataFrame,
    *,
    fill_i: int,
    entry: float,
    side: str,
    box_width: float,
    box_high: float,
    box_low: float,
    breakout_i: int,
    compute_exits: bool = True,
) -> dict[str, Any]:
    f = df.copy()
    if "timestamp" not in f.columns:
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
        if box_width > 0 and entry > 0:
            out[f"h{h}_mfe_box"] = (mfe / 100.0 * entry) / box_width
            out[f"h{h}_mae_box"] = (abs(mae) / 100.0 * entry) / box_width
        out[f"h{h}_bars_to_mfe"] = int(np.argmax(fav[: end_off + 1]))
        out[f"h{h}_bars_to_mae"] = int(np.argmin(adv[: end_off + 1]))

    for lvl in FIRST_TOUCH_PCT:
        for sign, tag in ((1, "p"), (-1, "m")):
            ft = first_touch_level(s, entry, highs, lows, fill_i, end_max, float(sign * lvl))
            key = f"ft_{tag}{lvl:.2f}".replace(".", "_")
            out[f"{key}_reached"] = bool(ft.get("reached"))
            out[f"{key}_bars"] = ft.get("bar_offset")

    if atr0 == atr0 and atr0 > 0:
        for mult in FIRST_TOUCH_ATR:
            pct = (mult * atr0 / entry) * 100.0
            for sign, tag in ((1, "atrp"), (-1, "atrm")):
                ft = first_touch_level(s, entry, highs, lows, fill_i, end_max, float(sign * pct))
                key = f"ft_{tag}{mult:.1f}".replace(".", "_")
                out[f"{key}_reached"] = bool(ft.get("reached"))
                out[f"{key}_bars"] = ft.get("bar_offset")

    if box_width > 0 and entry > 0:
        for mult in FIRST_TOUCH_BOX:
            pct = (mult * box_width / entry) * 100.0
            for sign, tag in ((1, "boxp"), (-1, "boxm")):
                ft = first_touch_level(s, entry, highs, lows, fill_i, end_max, float(sign * pct))
                key = f"ft_{tag}{mult:.1f}".replace(".", "_")
                out[f"{key}_reached"] = bool(ft.get("reached"))
                out[f"{key}_bars"] = ft.get("bar_offset")

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

    out.update(
        follow_through_and_fakeout(
            df,
            breakout_i=breakout_i,
            fill_i=fill_i,
            side=side,
            box_high=box_high,
            box_low=box_low,
        )
    )

    if compute_exits:
        exits = []
        for xid, (tp_pct, sl_mag, _) in EXIT_MODELS.items():
            for hold in EXIT_HOLDS:
                for cost in COST_PCT:
                    res = evaluate_outcome_params(
                        side=s,
                        entry=entry,
                        highs=highs,
                        lows=lows,
                        closes=closes,
                        timestamps=arrays["timestamps"],
                        fill_i=fill_i,
                        n_bars=n,
                        tp_pct=float(tp_pct),
                        sl_pct=float(-abs(sl_mag)),
                        horizon_bars=int(hold),
                        cost_pct=float(cost),
                    )
                    # API uses net_pnl_pct / gross_pnl_pct / exit_reason
                    net = res.get("net_pnl_pct", res.get("net_pct"))
                    gross = res.get("gross_pnl_pct", res.get("gross_pct"))
                    reason = res.get("exit_reason", res.get("reason"))
                    exits.append(
                        {
                            "exit_id": xid,
                            "tp_pct": tp_pct,
                            "sl_pct": sl_mag,
                            "hold": hold,
                            "cost_pct": cost,
                            "reason": reason,
                            "net_pct": net,
                            "gross_pct": gross,
                        }
                    )
        # flatten primary X1/h12/c0.25 into main row; keep compact exit summary
        for e in exits:
            if e["exit_id"] == "X1" and e["hold"] == 12 and e["cost_pct"] == 0.25:
                out["exit_X1_h12_c025_net"] = e.get("net_pct")
                out["exit_X1_h12_c025_reason"] = e.get("reason")
                break
        out["n_exit_combos"] = len(exits)
        nets = [e["net_pct"] for e in exits if e.get("net_pct") is not None]
        out["exit_best_net"] = float(max(nets)) if nets else None
        out["exit_any_pos_at_025"] = any(
            e.get("net_pct") is not None and e["cost_pct"] == 0.25 and float(e["net_pct"]) > 0
            for e in exits
        )

    return out
