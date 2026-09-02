"""Join audit: structural edge visits vs sensitivity clusters."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .fight_cluster_sensitivity import SENSITIVITY_GAPS, _inside_gap_seconds, _parse_ts


def build_edge_visit_cluster_join_audit(
    visits: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    clusters_by_gap: dict[str, Any],
) -> list[dict[str, Any]]:
    """One row per visit with gap/cluster assignment for each sensitivity threshold."""
    sorted_visits = sorted(visits, key=lambda v: v["start_ts"])
    visit_to_cluster: dict[str, dict[str, str]] = {v["edge_visit_id"]: {} for v in sorted_visits}

    for gap in SENSITIVITY_GAPS:
        gap_key = str(gap)
        clusters = (clusters_by_gap.get(gap_key) or {}).get("clusters") or []
        for ci, cluster in enumerate(clusters):
            cid = f"cluster_{gap_key}_{ci:03d}"
            for vid in cluster.get("visit_ids") or []:
                if vid in visit_to_cluster:
                    visit_to_cluster[vid][gap_key] = cid

    rows: list[dict[str, Any]] = []
    for i, v in enumerate(sorted_visits):
        next_v = sorted_visits[i + 1] if i + 1 < len(sorted_visits) else None
        gap_sec = None
        same_edge = None
        merge_gap0 = "NO_NEXT_VISIT"
        if next_v is not None:
            gap_sec = (_parse_ts(next_v["start_ts"]) - _parse_ts(v["end_ts"])).total_seconds()
            same_edge = v["edge"] == next_v["edge"]
            inside_gap = _inside_gap_seconds(v, next_v, episodes)
            merge_gap0 = "KEEP_SEPARATE"

        row = {
            "edge_visit_id": v["edge_visit_id"],
            "edge": v["edge"],
            "start_ts": v["start_ts"],
            "end_ts": v["end_ts"],
            "closed": v.get("closed"),
            "end_reason": v.get("end_reason"),
            "raw_episode_count": v.get("raw_episode_count"),
            "next_visit_id": next_v["edge_visit_id"] if next_v else None,
            "gap_to_next_visit_seconds": gap_sec,
            "same_edge_as_next": same_edge,
            "merge_decision_at_gap_0": merge_gap0,
        }
        for gap in SENSITIVITY_GAPS:
            row[f"cluster_id_gap_{gap}"] = visit_to_cluster.get(v["edge_visit_id"], {}).get(str(gap))
        rows.append(row)
    return rows
