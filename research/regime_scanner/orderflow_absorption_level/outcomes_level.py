"""Forward outcomes from entry_eligible_index + 1 with level hold/break metrics."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.oi_price_delta_pattern.features import _contiguous
from research.regime_scanner.orderflow_absorption.outcomes import forward_outcome_at
from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig, thr_label


def level_path_metrics(
    df: pd.DataFrame,
    entry_i: int,
    *,
    level_price: float | None,
    level_side: str | None,
    direction: str,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    """Level hold/break/penetration/reclaim after entry_i (path starts entry_i+1)."""
    out: dict[str, Any] = {}
    if level_price is None or level_side is None:
        for h in horizons:
            out[f"h{h}_level_hold"] = None
            out[f"h{h}_level_break"] = None
            out[f"h{h}_max_penetration_atr"] = None
            out[f"h{h}_reclaim_1"] = None
            out[f"h{h}_reclaim_2"] = None
            out[f"h{h}_reclaim_3"] = None
            out[f"h{h}_mae_before_mfe"] = None
        return out

    n = len(df)
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    atrs = df["atr_14"].to_numpy(dtype=float)
    lp = float(level_price)

    for h in horizons:
        end = min(n - 1, entry_i + h)
        if end < entry_i + 1 or not _contiguous(seq, ts, entry_i, end):
            out[f"h{h}_level_hold"] = None
            out[f"h{h}_level_break"] = None
            out[f"h{h}_max_penetration_atr"] = None
            out[f"h{h}_reclaim_1"] = None
            out[f"h{h}_reclaim_2"] = None
            out[f"h{h}_reclaim_3"] = None
            out[f"h{h}_mae_before_mfe"] = None
            continue

        broke = False
        max_pen = 0.0
        break_i: int | None = None
        for j in range(entry_i + 1, end + 1):
            atr = float(atrs[j - 1]) if j >= 1 else float(atrs[j])
            atr = atr if np.isfinite(atr) and atr > 0 else 1e-12
            c = float(closes[j])
            hi = float(highs[j])
            lo = float(lows[j])
            if level_side == "support":
                if c < lp:
                    broke = True
                    if break_i is None:
                        break_i = j
                pen = max(0.0, lp - lo) / atr
            else:
                if c > lp:
                    broke = True
                    if break_i is None:
                        break_i = j
                pen = max(0.0, hi - lp) / atr
            max_pen = max(max_pen, pen)

        reclaim = {1: False, 2: False, 3: False}
        if break_i is not None:
            for k in (1, 2, 3):
                j = break_i + k
                if j > end or not _contiguous(seq, ts, break_i, j):
                    continue
                c = float(closes[j])
                if level_side == "support" and c >= lp:
                    reclaim[k] = True
                if level_side == "resistance" and c <= lp:
                    reclaim[k] = True

        # MAE before MFE (side-aware) using path from entry+1
        entry = float(closes[entry_i])
        sl = slice(entry_i + 1, end + 1)
        up = highs[sl] / entry - 1.0
        dn = lows[sl] / entry - 1.0
        if direction == "bullish":
            mfe_path = up
            mae_path = -dn
        else:
            mfe_path = -dn
            mae_path = up
        mfe_i = int(np.argmax(mfe_path)) if len(mfe_path) else 0
        mae_i = int(np.argmax(mae_path)) if len(mae_path) else 0
        mae_before_mfe = bool(mae_i < mfe_i)

        out[f"h{h}_level_hold"] = not broke
        out[f"h{h}_level_break"] = broke
        out[f"h{h}_max_penetration_atr"] = float(max_pen)
        out[f"h{h}_reclaim_1"] = reclaim[1]
        out[f"h{h}_reclaim_2"] = reclaim[2]
        out[f"h{h}_reclaim_3"] = reclaim[3]
        out[f"h{h}_mae_before_mfe"] = mae_before_mfe
    return out


def side_aware_from_forward(
    oc: dict[str, Any],
    *,
    direction: str,
    horizons: tuple[int, ...],
    thresholds: tuple[float, ...],
) -> dict[str, Any]:
    """Map bull/bear fav/adv fields onto unified favorable/adverse names."""
    out: dict[str, Any] = {}
    bull = direction == "bullish"
    for h in horizons:
        if not oc.get(f"h{h}_valid"):
            continue
        pref_mfe = f"h{h}_bull_mfe" if bull else f"h{h}_bear_mfe"
        pref_mae = f"h{h}_bull_mae" if bull else f"h{h}_bear_mae"
        pref_edge = f"h{h}_bull_edge" if bull else f"h{h}_bear_edge"
        out[f"h{h}_mfe"] = oc.get(pref_mfe)
        out[f"h{h}_mae"] = oc.get(pref_mae)
        out[f"h{h}_edge"] = oc.get(pref_edge)
        out[f"h{h}_close_ret"] = oc.get(f"h{h}_close_ret_pct")
        # signed close ret for direction
        cr = oc.get(f"h{h}_close_ret_pct")
        if cr is not None:
            out[f"h{h}_close_ret_side"] = float(cr) if bull else -float(cr)
        for thr in thresholds:
            tag = thr_label(thr)
            p = f"h{h}_{tag}"
            if bull:
                fav_first = oc.get(f"{p}_bull_fav_first")
                adv_first = oc.get(f"{p}_bull_adv_first")
                bars_fav = oc.get(f"{p}_bars_to_up")
                bars_adv = oc.get(f"{p}_bars_to_down")
                fav_reached = oc.get(f"{p}_up_reached")
                adv_reached = oc.get(f"{p}_down_reached")
            else:
                fav_first = oc.get(f"{p}_bear_fav_first")
                adv_first = oc.get(f"{p}_bear_adv_first")
                bars_fav = oc.get(f"{p}_bars_to_down")
                bars_adv = oc.get(f"{p}_bars_to_up")
                fav_reached = oc.get(f"{p}_down_reached")
                adv_reached = oc.get(f"{p}_up_reached")
            out[f"{p}_favorable_reached"] = fav_reached
            out[f"{p}_adverse_reached"] = adv_reached
            out[f"{p}_favorable_first"] = fav_first
            out[f"{p}_adverse_first"] = adv_first
            out[f"{p}_same_bar"] = oc.get(f"{p}_same_bar")
            out[f"{p}_bars_to_favorable"] = bars_fav
            out[f"{p}_bars_to_adverse"] = bars_adv
    return out


def compute_event_outcomes(
    df: pd.DataFrame,
    confirmation_events: list[dict[str, Any]],
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in confirmation_events:
        entry = int(ev["entry_eligible_index"])
        oc = forward_outcome_at(
            df,
            entry,
            horizons=cfg.horizons,
            thresholds=cfg.move_thresholds,
        )
        if oc is None:
            continue
        # Require at least one valid horizon
        if not any(oc.get(f"h{h}_valid") for h in cfg.horizons):
            continue
        direction = str(ev.get("direction") or ("bullish" if ev.get("pattern") == "A4" else "bearish"))
        side_fields = side_aware_from_forward(
            oc,
            direction=direction,
            horizons=cfg.horizons,
            thresholds=cfg.move_thresholds,
        )
        level_fields = level_path_metrics(
            df,
            entry,
            level_price=ev.get("level_price"),
            level_side=ev.get("level_side"),
            direction=direction,
            horizons=cfg.horizons,
        )
        rows.append(
            {
                "event_id": ev.get("event_id"),
                "confirmation_id": ev.get("confirmation_id") or f"{ev.get('event_id')}|{ev.get('confirmation_type')}",
                "confirmation_type": ev.get("confirmation_type"),
                "symbol": ev.get("symbol"),
                "pattern": ev.get("pattern"),
                "direction": direction,
                "flow_rule": ev.get("flow_rule"),
                "lookback": ev.get("lookback"),
                "level_id": ev.get("level_id"),
                "level_type": ev.get("level_type"),
                "level_side": ev.get("level_side"),
                "level_price": ev.get("level_price"),
                "entry_eligible_index": entry,
                "entry_eligible_timestamp": ev.get("entry_eligible_timestamp"),
                "no_level": ev.get("no_level"),
                "far_from_level": ev.get("far_from_level"),
                "distance_bucket_at_entry": ev.get("distance_bucket_at_entry"),
                "confluent": ev.get("confluent"),
                **{k: oc[k] for k in oc if k.startswith("h")},
                **side_fields,
                **level_fields,
            }
        )
    return rows
