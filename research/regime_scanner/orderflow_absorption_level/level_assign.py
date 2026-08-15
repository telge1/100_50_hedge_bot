"""Assign nearest causal levels to absorption anchors."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.orderflow_absorption_level.config import (
    LEVEL_PRIORITY,
    LevelAbsorptionConfig,
    distance_bucket,
)
from research.regime_scanner.orderflow_absorption_level.levels_build import active_levels_at


def atr_reference_at(df: pd.DataFrame, t: int) -> float:
    """Causal ATR: atr_14[t-1]."""
    if t < 1 or "atr_14" not in df.columns:
        return float("nan")
    v = float(df["atr_14"].iloc[t - 1])
    return v if np.isfinite(v) and v > 0 else float("nan")


def distance_atr(*, close_t: float, level_price: float, atr_ref: float) -> float:
    if not (np.isfinite(close_t) and np.isfinite(level_price) and np.isfinite(atr_ref) and atr_ref > 0):
        return float("nan")
    return abs(float(close_t) - float(level_price)) / float(atr_ref)


def _side_ok(level: dict[str, Any], close_t: float) -> bool:
    price = float(level["level_price"])
    side = str(level["side"])
    if side == "support":
        return price <= close_t + 1e-12
    if side == "resistance":
        return price >= close_t - 1e-12
    return False


def pick_level_for_anchor(
    candidates: list[dict[str, Any]],
    *,
    close_t: float,
    atr_ref: float,
    wanted_side: str,
    max_distance_atr: float,
    confluence_atr: float,
) -> dict[str, Any]:
    """Priority: protected > external_swing; then nearest. Confluence if other type within radius."""
    scored: list[tuple[int, float, dict[str, Any]]] = []
    for lv in candidates:
        if str(lv["side"]) != wanted_side:
            continue
        if not _side_ok(lv, close_t):
            continue
        d = distance_atr(close_t=close_t, level_price=float(lv["level_price"]), atr_ref=atr_ref)
        if not np.isfinite(d):
            continue
        pri = LEVEL_PRIORITY.get(str(lv["level_type"]), 99)
        scored.append((pri, float(d), lv))

    if not scored or not np.isfinite(atr_ref):
        return {
            "level_id": None,
            "level_type": None,
            "side": wanted_side,
            "level_price": None,
            "distance_atr": None,
            "distance_bucket": "no_level",
            "confluent": False,
            "no_level": True,
            "far_from_level": False,
            "assignment_reason": "no_visible_level" if np.isfinite(atr_ref) else "atr_invalid",
        }

    in_radius = [s for s in scored if s[1] <= max_distance_atr]
    if not in_radius:
        nearest = min(scored, key=lambda x: (x[0], x[1]))
        return {
            "level_id": nearest[2]["level_id"],
            "level_type": nearest[2]["level_type"],
            "side": wanted_side,
            "level_price": float(nearest[2]["level_price"]),
            "distance_atr": nearest[1],
            "distance_bucket": distance_bucket(nearest[1], max_distance_atr=max_distance_atr),
            "confluent": False,
            "no_level": False,
            "far_from_level": True,
            "assignment_reason": "nearest_beyond_radius",
        }

    best = min(in_radius, key=lambda x: (x[0], x[1]))
    best_type = str(best[2]["level_type"])
    confluent = False
    for pri, d, lv in in_radius:
        if str(lv["level_type"]) != best_type and d <= confluence_atr:
            confluent = True
            break
        # also: other type within confluence of best distance neighborhood
        if str(lv["level_id"]) != str(best[2]["level_id"]) and str(lv["level_type"]) != best_type:
            if abs(d - best[1]) <= confluence_atr or d <= confluence_atr:
                confluent = True
                break

    return {
        "level_id": best[2]["level_id"],
        "level_type": best_type,
        "side": wanted_side,
        "level_price": float(best[2]["level_price"]),
        "distance_atr": best[1],
        "distance_bucket": distance_bucket(best[1], max_distance_atr=max_distance_atr),
        "confluent": confluent,
        "no_level": False,
        "far_from_level": False,
        "assignment_reason": "nearest_in_radius_by_priority",
    }


def pattern_wanted_side(pattern: str) -> str | None:
    if pattern == "A4":
        return "support"
    if pattern in ("A2", "A1"):
        return "resistance"
    if pattern == "C2":
        return "support"
    if pattern == "C1":
        return "resistance"
    return None


def assign_levels_to_anchors(
    df: pd.DataFrame,
    assignments: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    """Attach level assignment to each absorption/control pattern row."""
    closes = df["close"].to_numpy(dtype=float)
    seqs = df["sequence_id"].to_numpy() if "sequence_id" in df.columns else np.zeros(len(df))
    out: list[dict[str, Any]] = []
    for a in assignments:
        t = int(a["anchor_i"])
        if t < 0 or t >= len(df):
            continue
        pattern = str(a["pattern"])
        wanted = pattern_wanted_side(pattern)
        close_t = float(closes[t])
        atr_ref = atr_reference_at(df, t)
        seq = seqs[t]
        base = {
            "symbol": a["symbol"],
            "timestamp": a["timestamp"],
            "anchor_index": t,
            "pattern": pattern,
            "flow_rule": a.get("flow_rule"),
            "lookback": a.get("lookback"),
            "anchor_price": close_t,
            "atr_reference": atr_ref if np.isfinite(atr_ref) else None,
            "expected_side": a.get("expected_side"),
            "price_reaction": a.get("price_reaction"),
            "delta_ratio": a.get("delta_ratio"),
        }
        if wanted is None:
            out.append(
                {
                    **base,
                    "level_id": None,
                    "level_type": None,
                    "side": None,
                    "level_price": None,
                    "distance_atr": None,
                    "distance_bucket": "no_level",
                    "confluent": False,
                    "no_level": True,
                    "far_from_level": False,
                    "assignment_reason": "pattern_no_level_side",
                }
            )
            continue
        visible = active_levels_at(inventory, t, sequence_id=seq)
        picked = pick_level_for_anchor(
            visible,
            close_t=close_t,
            atr_ref=atr_ref,
            wanted_side=wanted,
            max_distance_atr=cfg.max_distance_atr,
            confluence_atr=cfg.confluence_atr,
        )
        out.append({**base, **picked})
    return out
