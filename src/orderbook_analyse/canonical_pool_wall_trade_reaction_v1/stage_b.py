"""Stage B: reuse six-case OB tracker on CLEAR_POOL_SELECTION_RULE_V1 candidates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
    strongest_inside,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    MAX_POST_START_S,
    PRE_START_S,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    audit_cluster_case,
    iter_ob_1s,
)
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.selection_rule_v1 import (
    RULE_ID,
    STAGE_B_LABELS,
)

# Evidence → Stage-B zone labels (selection rule V1)
EVIDENCE_TO_ZONE = {
    "POOL_REJECTION_WITH_ABSORPTION_EVIDENCE": "ZONE_HELD",
    "POOL_REJECTION_MIXED_WALL_REACTION": "ZONE_HELD",
    "POOL_BREAKOUT_WITH_ACCEPTANCE": "ZONE_EATEN",
    "WALL_CANCEL_OR_MOVE_DOMINANT": "ZONE_PULLED",
    "WALL_NOT_MEANINGFULLY_ATTACKED": "ZONE_UNKNOWN",
    "WINDOW_CENSORED_ACTIVE": "ZONE_UNKNOWN",
    "INSUFFICIENT_DATA": "ZONE_UNKNOWN",
}


def _utc(ts: str | datetime | pd.Timestamp) -> datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_strongest_wall_in_zone(
    *,
    raw_root: Path,
    side: str,
    lower: float,
    upper: float,
    at: datetime,
) -> dict[str, Any] | None:
    """Strongest resting level inside [lower, upper] near `at` (raw OB200)."""
    at = _utc(at)
    rows = list(iter_ob_1s(raw_root, at - timedelta(seconds=2), at + timedelta(seconds=2)))
    if not rows:
        return None
    # prefer exact second, else nearest
    target_ms = int(at.timestamp() * 1000)
    target_ms = (target_ms // 1000) * 1000
    best = None
    best_dist = 10**18
    for bucket, genuine, _bb, _ba, _mid, bids, asks in rows:
        if not genuine:
            continue
        dist = abs(bucket - target_ms)
        if dist < best_dist:
            best_dist = dist
            best = (bids, asks)
    if best is None:
        return None
    bids, asks = best
    levels = asks if side == "ASK" else bids
    ranked = side_levels_ranked_full(levels)
    return strongest_inside(ranked, lower, upper)


def candidate_to_case(row: dict[str, Any], *, case_id: str, raw_root: Path) -> dict[str, Any]:
    """Map Stage-A candidate (A7 already passed) → six-case auditor case dict.

    Wall anchor = strongest resting level **inside** the A7-confirmed zone at touch.
    Never invents a wall from the 1s proxy.
    """
    side = str(row["side"]).upper()
    lo = float(row["lower"])
    hi = float(row["upper"])
    touch = _utc(row["first_touch_ts"])
    causal_end = touch + timedelta(seconds=MAX_POST_START_S)
    load_start = touch - timedelta(seconds=PRE_START_S)

    # Prefer A7 fields already measured by Stage A; re-discover only if missing.
    wall_price = row.get("a7_strongest_in_zone_price")
    wall_notional = row.get("a7_strongest_in_zone_notional")
    wall_rank = row.get("a7_strongest_in_zone_full_side_rank")
    if wall_price is None or (isinstance(wall_price, float) and pd.isna(wall_price)):
        wall = discover_strongest_wall_in_zone(
            raw_root=raw_root, side=side, lower=lo, upper=hi, at=touch
        )
        wall_price = float(wall["price"]) if wall else None
        wall_notional = float(wall["notional"]) if wall else None
        wall_rank = int(wall["full_side_rank"]) if wall else None
        zone_wall_ok = wall is not None
    else:
        zone_wall_ok = True

    approach = "FROM_BELOW" if side == "ASK" else "FROM_ABOVE"
    return {
        "case_id": case_id,
        "market_arrival_cluster_id": f"stage_a:{row['pool_id']}",
        "side": side,
        "approach_direction": approach,
        "cluster_start_ts": _iso(touch),
        "cluster_end_ts_raw": _iso(causal_end),
        "causal_window_end_ts": _iso(causal_end),
        "load_start_ts": _iso(load_start),
        "window_censored_active": False,
        "component_lower_edge": lo,
        "component_upper_edge": hi,
        "member_pool_count": int(row.get("maximum_P") or 0),
        "member_pool_ids": str(row["pool_id"]),
        "strongest_cluster_wall_price_at_start": wall_price,
        "strongest_cluster_wall_notional_at_start": wall_notional,
        "strongest_cluster_wall_full_side_rank_at_start": wall_rank,
        "any_major_wall_at_cluster_start": False,  # zone-fill gate is A7, not MAJOR class
        "select_reason": RULE_ID,
        "pool_id": row["pool_id"],
        "timeframe": row.get("timeframe"),
        "reaction_1s_prior": row.get("reaction"),
        "wall_in_pool_1s": row.get("wall_in_pool_1s_proxy") or row.get("wall_in_pool"),
        "zone_wall_discovered": zone_wall_ok,
        "a7_zone_level_count": row.get("a7_zone_level_count"),
        "a7_zone_notional": row.get("a7_zone_notional"),
    }


def map_zone_label(evidence_class: str, summary: dict[str, Any]) -> str:
    """Map six-case evidence to CLEAR_POOL_SELECTION_RULE_V1 Stage-B labels."""
    base = EVIDENCE_TO_ZONE.get(evidence_class, "ZONE_UNKNOWN")
    # Refine: breakout + trade depletion → EATEN; cancel dominant already PULLED
    if evidence_class == "POOL_BREAKOUT_WITH_ACCEPTANCE":
        if summary.get("cancel_or_move_dominant") and not summary.get("trade_depletion_dominant"):
            return "ZONE_PULLED"
        return "ZONE_EATEN"
    if evidence_class in (
        "POOL_REJECTION_WITH_ABSORPTION_EVIDENCE",
        "POOL_REJECTION_MIXED_WALL_REACTION",
    ):
        if summary.get("cancel_or_move_dominant") and not summary.get("trade_depletion_dominant"):
            # rejected at pool level but wall mostly cancelled → still HELD on price, note pull
            return "ZONE_HELD"
        return "ZONE_HELD"
    if base not in STAGE_B_LABELS:
        return "ZONE_UNKNOWN"
    return base


def run_stage_b_on_candidates(
    candidates: pd.DataFrame,
    *,
    raw_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    prefixes: list[dict[str, Any]] = []
    empty_arrivals: dict[str, list] = {}

    for i, row in enumerate(candidates.to_dict(orient="records"), start=1):
        case_id = f"SA_{i:03d}"
        print(f"stage_b {case_id} {row['pool_id']} {row['first_touch_ts']}…", flush=True)
        case = candidate_to_case(row, case_id=case_id, raw_root=raw_root)
        if case["strongest_cluster_wall_price_at_start"] is None:
            # No resting level in zone at touch → Stage A7 fail at decision time
            summaries.append(
                {
                    "case_id": case_id,
                    "pool_id": row["pool_id"],
                    "timeframe": row.get("timeframe"),
                    "side": case["side"],
                    "cluster_start_ts": case["cluster_start_ts"],
                    "component_lower_edge": case["component_lower_edge"],
                    "component_upper_edge": case["component_upper_edge"],
                    "evidence_class": "INSUFFICIENT_DATA",
                    "zone_label": "ZONE_UNKNOWN",
                    "zone_wall_discovered": False,
                    "select_reason": RULE_ID,
                    "note": "no_resting_level_inside_zone_at_touch",
                    "reaction_1s_prior": row.get("reaction"),
                }
            )
            continue
        res = audit_cluster_case(
            case=case,
            raw_root=raw_root,
            arrivals_by_cluster=empty_arrivals,
        )
        summary = dict(res["summary"])
        summary["pool_id"] = row["pool_id"]
        summary["timeframe"] = row.get("timeframe")
        summary["zone_wall_discovered"] = True
        summary["zone_label"] = map_zone_label(summary["evidence_class"], summary)
        summary["reaction_1s_prior"] = row.get("reaction")
        summary["wall_in_pool_1s"] = row.get("wall_in_pool")
        summary["select_reason"] = RULE_ID
        summaries.append(summary)
        timelines.extend(res["timeline"])
        prefixes.append(res["prefix"])
    return summaries, timelines, prefixes
