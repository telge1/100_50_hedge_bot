"""Independent oracle checks: FP/FN/mismatch/look-ahead; Episode-1 rediscovery assert."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import detect_visits_for_cluster, episode_id, parse_utc
from . import EPISODE1_REDISCOVERY_EPISODE_ID, EPISODE1_REDISCOVERY_PERSISTENT_CLUSTER_ID


def count_lookahead_features(snapshots: list[dict[str, Any]]) -> int:
    return sum(1 for s in snapshots if s.get("look_ahead"))


def oracle_snapshot_causality(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    violations = []
    for s in snapshots:
        if not s.get("available"):
            continue
        dt = s.get("decision_time")
        avail = s.get("event_available_at")
        if dt is None or avail is None:
            continue
        if parse_utc(avail) > parse_utc(dt):
            violations.append(
                {
                    "episode_id": s.get("episode_id"),
                    "anchor_type": s.get("anchor_type"),
                    "offset_s": s.get("offset_s"),
                    "decision_time": dt,
                    "event_available_at": avail,
                }
            )
    return {
        "n_snapshots": len(snapshots),
        "n_lookahead_violations": len(violations),
        "violations": violations[:50],
        "ok": len(violations) == 0,
    }


def rediscover_episode1_visit(
    *,
    zone: dict[str, Any],
    trade_events: list[dict[str, Any]],
    candles_1m: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    mid_events: list[dict[str, Any]] | None = None,
    expected_episode_id: str = EPISODE1_REDISCOVERY_EPISODE_ID,
    expected_persistent_cluster_id: str = EPISODE1_REDISCOVERY_PERSISTENT_CLUSTER_ID,
) -> dict[str, Any]:
    """
    Re-detect visits for the CLOSED 30m VAL zone and assert expected episode_id appears.

    CRITICAL: expected_episode_id is a REGRESSION ASSERT ONLY — it must not be used as
    an input to selection / discovery / wall choice.
    """
    if zone.get("persistent_cluster_id") != expected_persistent_cluster_id and zone.get(
        "zone_id"
    ) != expected_persistent_cluster_id:
        return {
            "ran": False,
            "reason": "zone_not_episode1_target",
            "zone_id": zone.get("zone_id"),
        }
    cluster = {
        "cluster_price_low": float(zone["cluster_price_low"]),
        "cluster_price_high": float(zone["cluster_price_high"]),
        "level_cluster_id": expected_persistent_cluster_id,
    }
    start = max(window_start, parse_utc(zone["zone_available_at"]))
    visits = detect_visits_for_cluster(
        cluster=cluster,
        trade_events=trade_events,
        mid_events=mid_events or [],
        candles_1m=candles_1m,
        window_start=start,
        window_end=window_end,
    )
    found_ids = []
    for v in visits:
        eid = episode_id(expected_persistent_cluster_id, parse_utc(v["first_touch_ts"]))
        found_ids.append(eid)
    hit = expected_episode_id in found_ids
    return {
        "ran": True,
        "used_as_selection_input": False,
        "expected_episode_id": expected_episode_id,
        "found": hit,
        "n_visits": len(visits),
        "found_episode_ids": found_ids,
        "ok": hit,
        "note": (
            "Episode-1 rediscovery is a post-discovery regression assert only; "
            "the expected id was not used to select or prioritize clusters."
        ),
    }


def build_oracle_report(
    *,
    snapshots: list[dict[str, Any]],
    rediscovery: dict[str, Any] | None = None,
    fp: int = 0,
    fn: int = 0,
    mismatch: int = 0,
) -> dict[str, Any]:
    causality = oracle_snapshot_causality(snapshots)
    return {
        "false_positives": fp,
        "false_negatives": fn,
        "mismatches": mismatch,
        "lookahead": causality,
        "episode1_rediscovery": rediscovery,
        "ok": causality["ok"] and (rediscovery is None or rediscovery.get("ok", True) or not rediscovery.get("ran")),
    }
