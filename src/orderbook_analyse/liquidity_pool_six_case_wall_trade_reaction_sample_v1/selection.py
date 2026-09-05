"""Deterministic six-case selection from V2 market clusters (start-time features only for ranking)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    KNOWN_ASK_START_TS,
    MAX_POST_START_S,
    PRE_START_S,
)


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def component_key(row: dict[str, Any]) -> tuple[str, float, float]:
    return (
        str(row["side"]),
        round(float(row["component_lower_edge"]), 1),
        round(float(row["component_upper_edge"]), 1),
    )


def primary_pool_ids(row: dict[str, Any]) -> frozenset[str]:
    return frozenset(p for p in str(row.get("member_pool_ids") or "").split("|") if p)


def causal_window(row: dict[str, Any]) -> dict[str, Any]:
    """Derive analysis window. cluster_end used only for window/non-overlap, not ranking."""
    start = _utc(row["cluster_start_ts"])
    end_raw = _utc(row["cluster_end_ts"])
    cap = start + timedelta(seconds=MAX_POST_START_S)
    censored = end_raw > cap
    causal_end = min(end_raw, cap)
    load_start = start - timedelta(seconds=PRE_START_S)
    return {
        "load_start_ts": _iso(load_start),
        "cluster_start_ts": _iso(start),
        "causal_window_end_ts": _iso(causal_end),
        "cluster_end_ts_raw": _iso(end_raw),
        "window_censored_active": censored,
        "load_start": load_start,
        "start": start,
        "causal_end": causal_end,
    }


def windows_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    a0, a1 = a["load_start"], a["causal_end"]
    b0, b1 = b["load_start"], b["causal_end"]
    return a0 <= b1 and b0 <= a1


def _start_rank_key(row: dict[str, Any]) -> tuple:
    """Stable sort using only start-time-known fields."""
    major = str(row.get("any_major_wall_at_cluster_start")).lower() == "true"
    rank = int(float(row.get("strongest_cluster_wall_full_side_rank_at_start") or 999))
    width = abs(float(row["component_upper_edge"]) - float(row["component_lower_edge"]))
    members = int(float(row.get("member_pool_count") or 0))
    # Prefer MAJOR at start, then tighter rank, then earlier time, then id (deterministic).
    return (
        0 if major else 1,
        rank,
        _utc(row["cluster_start_ts"]),
        round(width, 4),
        -members,  # prefer multi-pool slightly for diversity of structure, still start-known
        str(row["market_arrival_cluster_id"]),
    )


def select_six_cases(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    """Select 3 ASK/FROM_BELOW + 3 BID/FROM_ABOVE without outcome leakage in ranking."""
    exclusions: list[dict[str, Any]] = []
    known = [
        r
        for r in clusters
        if r["cluster_start_ts"] == KNOWN_ASK_START_TS and r["side"] == "ASK"
    ]
    if len(known) != 1:
        raise RuntimeError(f"expected exactly one known ASK at {KNOWN_ASK_START_TS}, got {len(known)}")
    known_row = known[0]

    def pool_bucket(side: str, approach: str) -> list[dict[str, Any]]:
        return [
            r
            for r in clusters
            if r["side"] == side and r["approach_direction"] == approach
        ]

    def greedy(side: str, approach: str, seed: dict[str, Any] | None, need: int) -> list[dict[str, Any]]:
        picked: list[dict[str, Any]] = []
        used_components: set[tuple] = set()
        used_pools: set[str] = set()
        windows: list[dict[str, Any]] = []

        def try_add(row: dict[str, Any], reason: str) -> bool:
            if any(p["market_arrival_cluster_id"] == row["market_arrival_cluster_id"] for p in picked):
                exclusions.append({"id": row["market_arrival_cluster_id"], "reason": "duplicate_cluster_id"})
                return False
            ck = component_key(row)
            pools = primary_pool_ids(row)
            w = causal_window(row)
            if ck in used_components:
                exclusions.append(
                    {
                        "id": row["market_arrival_cluster_id"],
                        "reason": "same_pool_component_edges",
                        "component_key": list(ck),
                    }
                )
                return False
            if pools & used_pools:
                exclusions.append(
                    {
                        "id": row["market_arrival_cluster_id"],
                        "reason": "shared_member_pool_reentry",
                        "shared": sorted(pools & used_pools)[:3],
                    }
                )
                return False
            for pw in windows:
                if windows_overlap(w, pw):
                    exclusions.append(
                        {
                            "id": row["market_arrival_cluster_id"],
                            "reason": "overlapping_analysis_window",
                            "with": pw.get("cluster_id"),
                        }
                    )
                    return False
            # Immediate re-entry: same side + start within 5m of an already picked start
            for p in picked:
                dt = abs((_utc(row["cluster_start_ts"]) - _utc(p["cluster_start_ts"])).total_seconds())
                if dt < 300:
                    exclusions.append(
                        {
                            "id": row["market_arrival_cluster_id"],
                            "reason": "too_close_in_time_to_selected_same_side",
                            "dt_s": dt,
                        }
                    )
                    return False
            meta = {**row, "_window": w, "_select_reason": reason}
            w["cluster_id"] = row["market_arrival_cluster_id"]
            picked.append(meta)
            used_components.add(ck)
            used_pools.update(pools)
            windows.append(w)
            return True

        if seed is not None:
            if not try_add(seed, "forced_known_ask_022736"):
                raise RuntimeError("known ASK case failed selection constraints")

        seed_id = seed["market_arrival_cluster_id"] if seed is not None else None
        cands = sorted(
            [
                r
                for r in pool_bucket(side, approach)
                if seed_id is None or r["market_arrival_cluster_id"] != seed_id
            ],
            key=_start_rank_key,
        )
        for row in cands:
            if len(picked) >= need:
                break
            if seed is not None and row["market_arrival_cluster_id"] == seed["market_arrival_cluster_id"]:
                continue
            try_add(row, "deterministic_start_features_sort")

        if len(picked) < need:
            raise RuntimeError(f"could not select {need} {side}/{approach} cases, got {len(picked)}")
        return picked

    ask = greedy("ASK", "FROM_BELOW", known_row, 3)
    bid = greedy("BID", "FROM_ABOVE", None, 3)
    selected = ask + bid
    # Assign stable case_ids by chronological start then side
    selected_sorted = sorted(
        selected,
        key=lambda r: (_utc(r["cluster_start_ts"]), r["side"], r["market_arrival_cluster_id"]),
    )
    cases = []
    for i, r in enumerate(selected_sorted, start=1):
        w = r["_window"]
        cases.append(
            {
                "case_id": f"CASE_{i:02d}",
                "market_arrival_cluster_id": r["market_arrival_cluster_id"],
                "side": r["side"],
                "approach_direction": r["approach_direction"],
                "cluster_start_ts": r["cluster_start_ts"],
                "cluster_end_ts_raw": w["cluster_end_ts_raw"],
                "causal_window_end_ts": w["causal_window_end_ts"],
                "load_start_ts": w["load_start_ts"],
                "window_censored_active": w["window_censored_active"],
                "component_lower_edge": float(r["component_lower_edge"]),
                "component_upper_edge": float(r["component_upper_edge"]),
                "member_pool_count": int(float(r["member_pool_count"])),
                "member_pool_ids": r["member_pool_ids"],
                "strongest_cluster_wall_price_at_start": float(
                    r["strongest_cluster_wall_price_at_start"] or 0
                )
                if r.get("strongest_cluster_wall_price_at_start")
                else None,
                "strongest_cluster_wall_notional_at_start": float(
                    r["strongest_cluster_wall_notional_at_start"] or 0
                )
                if r.get("strongest_cluster_wall_notional_at_start")
                else None,
                "strongest_cluster_wall_full_side_rank_at_start": int(
                    float(r["strongest_cluster_wall_full_side_rank_at_start"])
                )
                if r.get("strongest_cluster_wall_full_side_rank_at_start")
                else None,
                "any_major_wall_at_cluster_start": str(r.get("any_major_wall_at_cluster_start")).lower()
                == "true",
                "select_reason": r["_select_reason"],
                "component_key": list(component_key(r)),
            }
        )

    return {
        "selection_rule": {
            "n_ask_from_below": 3,
            "n_bid_from_above": 3,
            "forced_known_ask_start_ts": KNOWN_ASK_START_TS,
            "ranking_fields_at_cluster_start_only": [
                "side",
                "approach_direction",
                "cluster_start_ts",
                "component_lower_edge",
                "component_upper_edge",
                "member_pool_count",
                "member_pool_ids",
                "any_major_wall_at_cluster_start",
                "strongest_cluster_wall_full_side_rank_at_start",
                "market_arrival_cluster_id",
            ],
            "forbidden_for_ranking": [
                "cluster_end_ts",
                "end_reason",
                "later_price",
                "later_walls",
                "trades_after_arrival",
                "reaction_class",
                "mfe_mae",
                "return_pnl",
            ],
            "constraints": [
                "distinct_market_arrival_cluster_id",
                "non_overlapping_analysis_windows",
                "distinct_component_edge_keys",
                "no_shared_member_pool_ids",
                "no_same_side_starts_within_300s",
            ],
            "sort_key": "major_first, wall_rank_asc, start_ts, width, -member_count, cluster_id",
            "window": {"pre_s": PRE_START_S, "max_post_s": MAX_POST_START_S},
        },
        "cases": cases,
        "exclusions_sample": exclusions[:80],
        "n_exclusions_recorded": len(exclusions),
    }
