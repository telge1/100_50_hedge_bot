"""Path outcomes AFTER the detection blind hash. Reuses aligned_path metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..aligned_path_analysis_v1.metrics import analyze_path
from ..aligned_path_analysis_v1.prices import choose_reference, path_arrays_from_trades
from ..outcomes.public_trade_index import PublicTradeIndex
from . import FIRST_MOVE_FLOOR_PCT, HORIZONS_S, MARK_PCTS
from .episodes import parse_utc


def measure_detection_outcomes(
    *,
    detection: datetime,
    direction: str | None,
    trades: PublicTradeIndex,
    mid_index: Any,
    outcome_end: datetime,
) -> dict[str, Any]:
    det = pd.Timestamp(parse_utc(detection))
    if direction is None:
        return {
            "directional_hit_evaluated": False,
            "reason": "NO_REACTION_DIRECTION",
            "horizons": [],
            "threshold_first_touch": [],
        }
    ref = choose_reference(detection=det, mid_index=mid_index, trades=trades)
    if not ref.get("ok"):
        return {
            "directional_hit_evaluated": False,
            "reason": "NO_CAUSAL_REFERENCE_PRICE",
            "horizons": [],
            "threshold_first_touch": [],
            "reference": ref,
        }
    rows = []
    marks = []
    max_h = min(
        max(HORIZONS_S),
        int((parse_utc(outcome_end) - parse_utc(detection)).total_seconds()),
    )
    for horizon in HORIZONS_S:
        if horizon > max_h:
            rows.append(
                {
                    "horizon_s": horizon,
                    "path_complete": False,
                    "reason": "HORIZON_BEYOND_OUTCOME_END",
                }
            )
            continue
        ts, px = path_arrays_from_trades(trades, detection=det, horizon_s=horizon)
        path = analyze_path(
            ts=ts,
            prices=px,
            detection=det,
            direction=direction,
            reference_price=float(ref["reference_price"]),
            reference_price_ts=pd.Timestamp(ref["reference_price_ts"]),
            price_source=str(ref["price_source"]),
            path_resolution_ms=1,
            horizon_s=horizon,
            first_move_floor_pct=FIRST_MOVE_FLOOR_PCT,
        )
        path["horizon_s"] = horizon
        rows.append(path)
        if horizon == max(h for h in HORIZONS_S if h <= max_h):
            for mark in MARK_PCTS:
                key = f"{mark:.2f}".replace(".", "_")
                hit = path.get(f"time_to_positive_{key}_ms")
                if hit is None and "mae_before_positive_0_05_pct" in path:
                    pass
                marks.append(
                    {
                        "threshold_pct": mark,
                        "horizon_s": horizon,
                        "hit": path.get(f"time_to_positive_{key.replace('.', '_')}_ms")
                        if False
                        else None,
                        "descriptive_only": True,
                        "not_tp_sl": True,
                    }
                )
    # Use existing first-index helpers already inside analyze_path mark fields.
    primary = next((r for r in rows if r.get("horizon_s") == 1800 or r.get("path_complete")), rows[-1] if rows else {})
    threshold_rows = []
    for mark in MARK_PCTS:
        label = f"{mark:.2f}".replace(".", "_")
        threshold_rows.append(
            {
                "threshold_pct": mark,
                "mae_before_mark_pct": primary.get(f"mae_before_positive_{label}_pct"),
                "descriptive_only": True,
                "not_used_for_tp_sl_selection": True,
            }
        )
    return {
        "directional_hit_evaluated": True,
        "direction": direction,
        "reference_price": ref["reference_price"],
        "reference_price_ts": str(ref["reference_price_ts"]),
        "price_source": ref["price_source"],
        "horizons": rows,
        "threshold_first_touch": threshold_rows,
        "primary": primary,
    }
