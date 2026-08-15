"""Forward outcomes and exit benchmarks for liquidity_sweep_reclaim_v1."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidity_sweep_reclaim.config import (
    COST_STRESS_PCT,
    EXIT_BENCHMARKS,
    FIRST_TOUCH_ADVERSE,
    FIRST_TOUCH_FAVORABLE,
    MFE_HORIZONS,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    first_touch_level,
    path_arrays,
    signed_return_pct,
)
from research.regime_scanner.small_target_single_trade.outcomes import evaluate_outcome_params


def side_sign(side: str) -> int:
    return 1 if str(side).lower() == "long" else -1


def frame_arrays(frame: pd.DataFrame) -> dict[str, Any]:
    atr_col = "atr_14" if "atr_14" in frame.columns else "atr"
    return {
        "highs": frame["high"].to_numpy(dtype=float),
        "lows": frame["low"].to_numpy(dtype=float),
        "closes": frame["close"].to_numpy(dtype=float),
        "atr": frame[atr_col].to_numpy(dtype=float)
        if atr_col in frame.columns
        else np.full(len(frame), np.nan),
        "timestamps": list(pd.to_datetime(frame["timestamp"], utc=True)),
        "n": len(frame),
    }


def forward_outcomes_fast(
    arrays: dict[str, Any],
    *,
    fill_i: int,
    entry: float,
    side: str,
) -> dict[str, Any]:
    """One max-horizon path pass; slice metrics per requested horizon."""
    s = side_sign(side)
    highs, lows, closes, atr = arrays["highs"], arrays["lows"], arrays["closes"], arrays["atr"]
    n = arrays["n"]
    max_h = max(MFE_HORIZONS)
    end_max = min(n - 1, fill_i + int(max_h) - 1)
    if end_max < fill_i:
        return {}
    path = path_arrays(s, entry, highs, lows, closes, fill_i, end_max)
    fav = path.get("fav")
    adv = path.get("adv")
    close_s = path.get("close_s")
    atr0 = float(atr[fill_i]) if np.isfinite(atr[fill_i]) and atr[fill_i] > 0 else np.nan
    out: dict[str, Any] = {}
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
        out[f"h{h}_mfe_mae_ratio"] = (mfe / abs(mae)) if abs(mae) > 1e-12 else None
        out[f"h{h}_bars_to_mfe"] = int(np.argmax(fav[: end_off + 1]))
        out[f"h{h}_bars_to_mae"] = int(np.argmin(adv[: end_off + 1]))

    for lvl in FIRST_TOUCH_FAVORABLE + FIRST_TOUCH_ADVERSE:
        key = f"ft_{'p' if lvl > 0 else 'm'}{abs(lvl):.2f}".replace(".", "_")
        ft = first_touch_level(s, entry, highs, lows, fill_i, end_max, float(lvl))
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
    return out


def forward_outcomes(
    frame: pd.DataFrame,
    *,
    fill_i: int,
    entry: float,
    side: str,
) -> dict[str, Any]:
    return forward_outcomes_fast(frame_arrays(frame), fill_i=fill_i, entry=entry, side=side)


def exit_benchmark_outcome_arrays(
    arrays: dict[str, Any],
    *,
    fill_i: int,
    entry: float,
    side: str,
    exit_id: str,
    cost_pct: float | None = None,
) -> dict[str, Any]:
    tp, sl_mag, horizon, cost = EXIT_BENCHMARKS[exit_id]
    if cost_pct is not None:
        cost = float(cost_pct)
    return evaluate_outcome_params(
        side=side_sign(side),
        entry=entry,
        highs=arrays["highs"],
        lows=arrays["lows"],
        closes=arrays["closes"],
        timestamps=arrays["timestamps"],
        fill_i=fill_i,
        n_bars=arrays["n"],
        tp_pct=float(tp),
        sl_pct=float(-abs(sl_mag)),
        horizon_bars=int(horizon),
        cost_pct=float(cost),
    )


def exit_benchmark_outcome(
    frame: pd.DataFrame,
    *,
    fill_i: int,
    entry: float,
    side: str,
    exit_id: str,
    cost_pct: float | None = None,
) -> dict[str, Any]:
    return exit_benchmark_outcome_arrays(
        frame_arrays(frame),
        fill_i=fill_i,
        entry=entry,
        side=side,
        exit_id=exit_id,
        cost_pct=cost_pct,
    )


def cost_stress_from_gross(gross_pnl_pct: float | None, cost_pct: float = COST_STRESS_PCT) -> float | None:
    if gross_pnl_pct is None or not np.isfinite(gross_pnl_pct):
        return None
    return float(gross_pnl_pct) - float(cost_pct)
