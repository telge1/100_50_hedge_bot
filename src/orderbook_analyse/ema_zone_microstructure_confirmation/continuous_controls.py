"""Descriptive control groups — no parameter feedback."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING


def assign_control_group(c: dict[str, Any]) -> str:
    state = c.get("candidate_state")
    mech = str(c.get("mechanism") or "")
    major = bool(c.get("major_wall_confluence"))
    if state == "block_flat_compression":
        return "B_flat_compression_blocked"
    if state == "wait_microstructure_confirmation":
        return "A_zone_touch_no_micro_confirm"
    if state == "defense_rejection_confirmed" and not major:
        return "C_defense_without_major_wall"
    if state == "defense_rejection_confirmed" and major:
        return "D_defense_with_major_wall"
    if state == "breakout_confirmed" and "ABSORPTION" in mech:
        return "E_breakout_true_absorption"
    if state == "breakout_confirmed" and "LIQUIDITY_PULL" in mech:
        return "F_breakout_liquidity_pull"
    if state == "false_breakout_confirmed" and mech in ("ASK_DEFENSE", "BID_DEFENSE"):
        return "G_false_breakout_confirmed_defense"
    if state == "false_breakout_confirmed" and mech == "UNDETERMINED":
        return "H_false_breakout_undetermined_mechanism"
    return "OTHER"


def build_controls(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for c in candidates:
        rows.append(
            {
                "symbol": c.get("symbol"),
                "episode_id": c.get("episode_id"),
                "control_group": assign_control_group(c),
                "candidate_state": c.get("candidate_state"),
                "mechanism": c.get("mechanism"),
                "regime": c.get("regime"),
                "zone_name": c.get("zone_name"),
                "approach_direction": c.get("approach_direction"),
                "major_wall_confluence": c.get("major_wall_confluence"),
                "candidate_direction": c.get("candidate_direction"),
                "matched": False,
                "match_note": "unmatched_descriptive_only_small_n",
            }
        )
    return rows


def matched_control_summary(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not controls:
        return []
    df = pd.DataFrame(controls)
    out = []
    for (sym, grp), g in df.groupby(["symbol", "control_group"]):
        out.append(
            {
                "symbol": sym,
                "control_group": grp,
                "n": int(len(g)),
                "matched_pairs": 0,
                "matching": "not_applied_small_n_descriptive",
                "note": MISSING,
            }
        )
    return out
