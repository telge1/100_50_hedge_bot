"""Fixed outcome class contract + path-based classification (percent)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from .params import (
    BIG_MFE_PCT,
    NO_EXPANSION_PCT,
    PRIMARY_CLASS_PRIORITY,
    STOP_PCT,
    TARGET_PCT,
    VERY_BIG_MFE_PCT,
)


def build_outcome_contract() -> dict[str, Any]:
    return {
        "contract_name": "frozen_outcome_class_contract",
        "version": 1,
        "units": "percent",
        "frozen": True,
        "target_pct": TARGET_PCT,
        "stop_pct": STOP_PCT,
        "big_mfe_pct": BIG_MFE_PCT,
        "very_big_mfe_pct": VERY_BIG_MFE_PCT,
        "no_expansion_pct": NO_EXPANSION_PCT,
        "primary_class_priority": list(PRIMARY_CLASS_PRIORITY),
        "note": "Classes frozen before feature comparison; not retuned from results.",
    }


def write_outcome_contract(path: Path) -> dict[str, Any]:
    c = build_outcome_contract()
    payload = json.dumps(c, sort_keys=True, separators=(",", ":"))
    c["contract_hash_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return c


def _first_hit_order(rows: Sequence[dict[str, Any]], *, target: float, stop: float) -> dict[str, Any]:
    """Walk path bars; return who hit first for target/stop MFE/MAE thresholds."""
    t_i = None
    s_i = None
    for i, r in enumerate(rows):
        hit_t = (r.get("running_mfe_pct") or 0) + 1e-15 >= target or (r.get("bar_mfe_pct") or 0) + 1e-15 >= target
        hit_s = (r.get("running_mae_pct") or 0) + 1e-15 >= stop or (r.get("bar_mae_pct") or 0) + 1e-15 >= stop
        if hit_t and hit_s and t_i is None and s_i is None:
            return {
                "order": "AMBIGUOUS",
                "minutes_to_target": i,
                "minutes_to_stop": i,
                "target_reached": True,
                "stop_reached": True,
            }
        if hit_t and t_i is None:
            t_i = i
            if s_i is None:
                return {
                    "order": "TARGET_FIRST",
                    "minutes_to_target": i,
                    "minutes_to_stop": None,
                    "target_reached": True,
                    "stop_reached": False,
                }
        if hit_s and s_i is None:
            s_i = i
            # continue to see if target later
    target_later = None
    if s_i is not None:
        for i, r in enumerate(rows):
            if i <= s_i:
                continue
            if (r.get("running_mfe_pct") or 0) + 1e-15 >= target or (r.get("bar_mfe_pct") or 0) + 1e-15 >= target:
                target_later = i
                break
        if target_later is not None:
            return {
                "order": "STOP_THEN_TARGET",
                "minutes_to_target": target_later,
                "minutes_to_stop": s_i,
                "target_reached": True,
                "stop_reached": True,
            }
        # check if target never after stop — maybe target never
        any_target = any(
            (r.get("running_mfe_pct") or 0) + 1e-15 >= target or (r.get("bar_mfe_pct") or 0) + 1e-15 >= target
            for r in rows
        )
        return {
            "order": "STOP_FIRST",
            "minutes_to_target": None,
            "minutes_to_stop": s_i,
            "target_reached": bool(any_target),
            "stop_reached": True,
        }
    if t_i is not None:
        return {
            "order": "TARGET_FIRST",
            "minutes_to_target": t_i,
            "minutes_to_stop": None,
            "target_reached": True,
            "stop_reached": False,
        }
    return {
        "order": "NEITHER",
        "minutes_to_target": None,
        "minutes_to_stop": None,
        "target_reached": False,
        "stop_reached": False,
    }


def mae_before_target(rows: Sequence[dict[str, Any]], *, target: float = TARGET_PCT) -> float | None:
    t_i = None
    for i, r in enumerate(rows):
        if (r.get("running_mfe_pct") or 0) + 1e-15 >= target or (r.get("bar_mfe_pct") or 0) + 1e-15 >= target:
            t_i = i
            break
    if t_i is None:
        return None
    before = rows[:t_i]
    if not before:
        return 0.0
    return float(max(max(r.get("running_mae_pct") or 0, r.get("bar_mae_pct") or 0) for r in before))


def classify_outcome(
    rows: Sequence[dict[str, Any]],
    *,
    mfe_4h: float,
    mae_4h: float,
    close_4h: float | None = None,
) -> dict[str, Any]:
    hit = _first_hit_order(rows, target=TARGET_PCT, stop=STOP_PCT)
    mae_b = mae_before_target(rows, target=TARGET_PCT)
    order = hit["order"]
    qualified = order == "TARGET_FIRST"
    flags = {
        "is_qualified_move": qualified,
        "is_big_clean_move": bool(mfe_4h + 1e-15 >= BIG_MFE_PCT and qualified),
        "is_very_big_clean_move": bool(mfe_4h + 1e-15 >= VERY_BIG_MFE_PCT and qualified),
        "is_big_dirty_move": bool(
            mfe_4h + 1e-15 >= BIG_MFE_PCT and order in ("STOP_FIRST", "STOP_THEN_TARGET", "AMBIGUOUS")
        ),
        "is_wrong_way": bool(order == "STOP_FIRST" and not hit["target_reached"]),
        "is_stop_then_late_target": bool(order == "STOP_THEN_TARGET"),
        "is_no_expansion": bool(mfe_4h < NO_EXPANSION_PCT and mae_4h < NO_EXPANSION_PCT),
        "is_low_favorable_expansion": bool(mfe_4h < TARGET_PCT),
    }
    # primary by priority
    primary = "NEUTRAL_UNCLASSIFIED"
    mapping = {
        "VERY_BIG_CLEAN_MOVE": flags["is_very_big_clean_move"],
        "BIG_CLEAN_MOVE": flags["is_big_clean_move"],
        "BIG_DIRTY_MOVE": flags["is_big_dirty_move"],
        "STOP_THEN_LATE_TARGET": flags["is_stop_then_late_target"],
        "WRONG_WAY": flags["is_wrong_way"],
        "NO_EXPANSION": flags["is_no_expansion"],
        "LOW_FAVORABLE_EXPANSION": flags["is_low_favorable_expansion"],
        "QUALIFIED_MOVE": flags["is_qualified_move"],
    }
    for name in PRIMARY_CLASS_PRIORITY:
        if name == "NEUTRAL_UNCLASSIFIED":
            continue
        if mapping.get(name):
            primary = name
            break
    # QUALIFIED_MOVE only if nothing stronger — already handled by priority
    return {
        "primary_outcome_class": primary,
        "target_stop_order": order,
        "minutes_to_0_41_pct": hit["minutes_to_target"],
        "minutes_to_0_15_stop": hit["minutes_to_stop"],
        "mae_before_0_41_pct": mae_b,
        "mfe_4h_pct": mfe_4h,
        "mae_4h_pct": mae_4h,
        "close_return_4h_pct": close_4h,
        **flags,
    }
