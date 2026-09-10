"""Assign ACTIVE LLD zones to MP clusters. No extra distance. No notional."""

from __future__ import annotations

from typing import Any

from .bins import intervals_overlap


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def active_overlapping_lld(
    *,
    cluster: dict[str, Any],
    zones: list[dict[str, Any]],
    decision_time_z: str,
) -> list[dict[str, Any]]:
    decision = parse_ts(decision_time_z)
    if decision is None:
        return []
    hits: list[dict[str, Any]] = []
    for zone in zones:
        avail = parse_ts(zone.get("available_at"))
        if avail is None or avail > decision:
            continue
        if zone.get("status") != "ACTIVE":
            continue
        if not intervals_overlap(
            cluster["cluster_price_low"],
            cluster["cluster_price_high"],
            zone["price_low"],
            zone["price_high"],
        ):
            continue
        hits.append(zone)
    hits.sort(key=lambda z: z["zone_id"])
    return hits


def lld_confluence_type(zones: list[dict[str, Any]]) -> str:
    types = {str(z["zone_type"]) for z in zones}
    if not types:
        return "MP_ONLY"
    has_res = "LLD_RESISTANCE_ZONE" in types
    has_sup = "LLD_SUPPORT_ZONE" in types
    if has_res and has_sup:
        return "MP_LLD_DIRECTION_CONFLICT"
    if len(zones) > 1:
        return "MP_WITH_MULTIPLE_LLD"
    if has_res:
        return "MP_WITH_LLD_RESISTANCE"
    if has_sup:
        return "MP_WITH_LLD_SUPPORT"
    return "MP_ONLY"


def nearest_active_destinations(
    *,
    cluster: dict[str, Any],
    zones: list[dict[str, Any]],
    decision_time_z: str,
) -> dict[str, Any]:
    decision = parse_ts(decision_time_z)
    mid = (float(cluster["cluster_price_low"]) + float(cluster["cluster_price_high"])) / 2.0
    above = None
    below = None
    above_dist = None
    below_dist = None
    for zone in zones:
        avail = parse_ts(zone.get("available_at"))
        if avail is None or avail > decision:
            continue
        if zone.get("status") != "ACTIVE":
            continue
        if intervals_overlap(
            cluster["cluster_price_low"],
            cluster["cluster_price_high"],
            zone["price_low"],
            zone["price_high"],
        ):
            continue
        if float(zone["price_low"]) > float(cluster["cluster_price_high"]):
            dist = (float(zone["price_low"]) - float(cluster["cluster_price_high"])) / mid * 100.0
            if above_dist is None or dist < above_dist:
                above = zone
                above_dist = dist
        elif float(zone["price_high"]) < float(cluster["cluster_price_low"]):
            dist = (float(cluster["cluster_price_low"]) - float(zone["price_high"])) / mid * 100.0
            if below_dist is None or dist < below_dist:
                below = zone
                below_dist = dist
    return {
        "nearest_active_lld_above_id": None if above is None else above["zone_id"],
        "nearest_active_lld_above_distance_pct": above_dist,
        "nearest_active_lld_below_id": None if below is None else below["zone_id"],
        "nearest_active_lld_below_distance_pct": below_dist,
        "nearest_lld_role": "POTENTIAL_DESTINATION_CONTEXT_ONLY",
        "not_automatic_confluence": True,
        "no_tp_rule": True,
    }


def map_cluster_lld(
    *,
    cluster: dict[str, Any],
    zones: list[dict[str, Any]],
    decision_time_z: str,
) -> dict[str, Any]:
    overlapping = active_overlapping_lld(
        cluster=cluster, zones=zones, decision_time_z=decision_time_z
    )
    hist = [
        z
        for z in zones
        if z.get("status") == "INVALIDATED"
        and parse_ts(z.get("available_at")) is not None
        and parse_ts(z.get("available_at")) <= parse_ts(decision_time_z)
        and intervals_overlap(
            cluster["cluster_price_low"],
            cluster["cluster_price_high"],
            z["price_low"],
            z["price_high"],
        )
    ]
    dest = nearest_active_destinations(
        cluster=cluster, zones=zones, decision_time_z=decision_time_z
    )
    strengths = [z.get("normalized_volume_strength") for z in overlapping]
    return {
        "level_cluster_id": cluster["level_cluster_id"],
        "decision_time": decision_time_z,
        "lld_confluence_type": lld_confluence_type(overlapping),
        "overlapping_active_zone_ids": [z["zone_id"] for z in overlapping],
        "overlapping_active_zone_types": [z["zone_type"] for z in overlapping],
        "normalized_volume_strengths": strengths,
        "strength_is_not_usdt_notional": True,
        "strength_not_used_for_filter_or_score": True,
        "invalidated_overlapping_zone_ids": [z["zone_id"] for z in hist],
        "invalidated_is_history_only_not_active_confluence": True,
        **dest,
    }
