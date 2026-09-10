"""Group consecutive same-zone visits into attack clusters (gap <= contract default)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..timeparse import format_utc_z
from ..wall_defense_outcome_contract_v1 import ATTACK_CLUSTER_GAP_MS_DEFAULT
from .hashing import attack_cluster_id


def group_attack_clusters(
    visits: list[dict[str, Any]],
    *,
    gap_ms: int = ATTACK_CLUSTER_GAP_MS_DEFAULT,
) -> list[dict[str, Any]]:
    """
    Consecutive visits on the same zone_id with inter-touch gap <= gap_ms form a cluster.
    Clusters ordered by cluster_start ascending.
    """
    if not visits:
        return []
    ordered = sorted(
        visits,
        key=lambda v: (str(v.get("zone_id") or ""), parse_utc(v["first_touch_ts"])),
    )
    gap = timedelta(milliseconds=int(gap_ms))
    clusters: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    cur_zone: str | None = None
    last_ts: datetime | None = None

    def _flush() -> None:
        nonlocal cur, cur_zone, last_ts
        if not cur:
            return
        start = parse_utc(cur[0]["first_touch_ts"])
        end = parse_utc(cur[-1].get("episode_close_ts") or cur[-1]["first_touch_ts"])
        zid = str(cur[0]["zone_id"])
        start_iso = format_utc_z(start)
        acid = attack_cluster_id(zone_id=zid, cluster_start_iso=start_iso)
        clusters.append(
            {
                "attack_cluster_id": acid,
                "zone_id": zid,
                "persistent_cluster_id": cur[0].get("persistent_cluster_id"),
                "cluster_start": start_iso,
                "cluster_end": format_utc_z(end),
                "n_visits": len(cur),
                "episode_ids": [v["episode_id"] for v in cur],
                "visits": cur,
                "zone_low": cur[0].get("zone_low"),
                "zone_high": cur[0].get("zone_high"),
                "zone_available_at": cur[0].get("zone_available_at"),
                "primary_visit": cur[0],  # chronological first visit = zone_touch seed
                "gap_ms": gap_ms,
            }
        )
        cur = []
        cur_zone = None
        last_ts = None

    for v in ordered:
        zid = str(v.get("zone_id") or "")
        ts = parse_utc(v["first_touch_ts"])
        if cur and (zid != cur_zone or last_ts is None or (ts - last_ts) > gap):
            _flush()
        if not cur:
            cur_zone = zid
        cur.append(v)
        last_ts = ts
    _flush()
    clusters.sort(key=lambda c: parse_utc(c["cluster_start"]))
    return clusters
