"""Fight cluster sensitivity — merge-gap analysis without threshold freeze."""

from __future__ import annotations

from datetime import datetime
from typing import Any

SENSITIVITY_GAPS = (0, 1, 2, 5, 10, 30, 60)
SENSITIVITY_LABEL = "UNFROZEN_SENSITIVITY_ONLY"


def build_fight_cluster_sensitivity(
    visits: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Group adjacent same-edge visits by inside-gap thresholds."""
    rows: list[dict[str, Any]] = []
    by_gap: dict[str, Any] = {}

    for gap in SENSITIVITY_GAPS:
        clusters = _cluster_visits(visits, episodes, max_inside_gap_seconds=float(gap))
        stats = _cluster_stats(clusters)
        row = {
            "max_inside_gap_seconds": gap,
            "cluster_count": len(clusters),
            "sensitivity_status": SENSITIVITY_LABEL,
            **stats,
        }
        rows.append(row)
        by_gap[str(gap)] = {
            "max_inside_gap_seconds": gap,
            "sensitivity_status": SENSITIVITY_LABEL,
            "cluster_count": len(clusters),
            "clusters": clusters,
        }
    return rows, by_gap


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _inside_gap_seconds(visit_a: dict[str, Any], visit_b: dict[str, Any], episodes: list[dict[str, Any]]) -> float:
    end_a = _parse_ts(visit_a["end_ts"])
    start_b = _parse_ts(visit_b["start_ts"])
    gap = (start_b - end_a).total_seconds()
    if gap < 0:
        return 0.0
    ep_by_id = {e["episode_id"]: e for e in episodes}
    inside_only = True
    idx_a = max(ep.get("episode_index", -1) for ep in episodes if ep["episode_id"] in visit_a.get("raw_episode_ids", []))
    idx_b = min(ep.get("episode_index", 999999) for ep in episodes if ep["episode_id"] in visit_b.get("raw_episode_ids", []))
    for ep in episodes:
        i = ep.get("episode_index", -1)
        if idx_a < i < idx_b and ep.get("state") != "INSIDE_BOTH_PROFILES":
            inside_only = False
            break
    return gap if inside_only else float("inf")


def _cluster_visits(
    visits: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    *,
    max_inside_gap_seconds: float,
) -> list[dict[str, Any]]:
    if not visits:
        return []
    sorted_visits = sorted(visits, key=lambda v: v["start_ts"])
    clusters: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = [sorted_visits[0]]

    for v in sorted_visits[1:]:
        prev = current[-1]
        if v["edge"] != prev["edge"]:
            clusters.append(_merge_cluster(current))
            current = [v]
            continue
        if max_inside_gap_seconds == 0:
            clusters.append(_merge_cluster(current))
            current = [v]
            continue
        gap = _inside_gap_seconds(prev, v, episodes)
        if gap > 0 and gap <= max_inside_gap_seconds:
            current.append(v)
        else:
            clusters.append(_merge_cluster(current))
            current = [v]
    clusters.append(_merge_cluster(current))
    return clusters


def _merge_cluster(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "edge": members[0]["edge"],
        "visit_count": len(members),
        "visit_ids": [m["edge_visit_id"] for m in members],
        "start_ts": members[0]["start_ts"],
        "end_ts": members[-1]["end_ts"],
        "duration_seconds": sum(m.get("duration_seconds") or 0 for m in members),
        "outside_excursions": sum(m.get("outside_excursion_count") or 0 for m in members),
        "reclaims": sum(m.get("reclaim_count") or 0 for m in members),
        "taker_delta_quote": sum(m.get("taker_delta_quote") or 0 for m in members),
        "quote_notional": sum(m.get("quote_notional") or 0 for m in members),
        "min_price": min(m.get("min_price") or 0 for m in members),
        "max_price": max(m.get("max_price") or 0 for m in members),
        "sensitivity_status": SENSITIVITY_LABEL,
    }


def _cluster_stats(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    if not clusters:
        return {
            "visits_per_cluster_mean": 0,
            "total_duration_seconds": 0,
            "active_edge_time_seconds": 0,
            "inside_gap_time_seconds": 0,
            "outside_excursions": 0,
            "reclaims": 0,
            "quote_notional": 0,
            "taker_delta_quote": 0,
            "max_price_span": 0,
        }
    visit_counts = [c.get("visit_count") or 0 for c in clusters]
    durations = [c.get("duration_seconds") or 0 for c in clusters]
    spans = [(c.get("max_price") or 0) - (c.get("min_price") or 0) for c in clusters]
    return {
        "visits_per_cluster_mean": sum(visit_counts) / len(visit_counts),
        "visits_per_cluster_max": max(visit_counts),
        "total_duration_seconds": sum(durations),
        "active_edge_time_seconds": sum(durations),
        "inside_gap_time_seconds": 0,
        "outside_excursions": sum(c.get("outside_excursions") or 0 for c in clusters),
        "reclaims": sum(c.get("reclaims") or 0 for c in clusters),
        "quote_notional": sum(c.get("quote_notional") or 0 for c in clusters),
        "taker_delta_quote": sum(c.get("taker_delta_quote") or 0 for c in clusters),
        "max_price_span": max(spans) if spans else 0,
    }
