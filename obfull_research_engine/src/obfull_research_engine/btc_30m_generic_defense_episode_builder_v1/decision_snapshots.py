"""Causal decision snapshots for all anchors/offsets; headroom placeholders; no look-ahead."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..timeparse import format_utc_z
from . import ANCHORS, SNAPSHOT_OFFSETS_S
from .exclusions import LOOKAHEAD_FEATURE_REFUSED, exclusion_row
from .headroom_schema import empty_headroom_placeholders


def _anchor_times(
    *,
    zone_touch: dict[str, Any],
    wall_touch: dict[str, Any] | None,
    detection: dict[str, Any] | None,
    first_joint_breach: str | None,
) -> dict[str, datetime | None]:
    times: dict[str, datetime | None] = {
        "ZONE_FIRST_TOUCH": parse_utc(zone_touch["exchange_event_time"]),
        "WALL_FIRST_TOUCH": None,
        "FIRST_JOINT_BREACH": None,
        "DETECTION": None,
    }
    if wall_touch and wall_touch.get("exchange_event_time"):
        times["WALL_FIRST_TOUCH"] = parse_utc(wall_touch["exchange_event_time"])
    if first_joint_breach:
        times["FIRST_JOINT_BREACH"] = parse_utc(first_joint_breach)
    if detection and detection.get("available") and detection.get("exchange_event_time"):
        times["DETECTION"] = parse_utc(detection["exchange_event_time"])
    return times


def _row_at_or_before(rows: list[dict[str, Any]], decision_time: datetime) -> dict[str, Any] | None:
    best = None
    best_t = None
    for r in rows:
        t_raw = r.get("decision_time") or r.get("available_at") or r.get("event_time") or r.get("timestamp")
        if t_raw is None:
            continue
        t = parse_utc(t_raw)
        avail = r.get("event_available_at") or r.get("available_at") or r.get("max_input_available_at")
        if avail is not None and parse_utc(avail) > decision_time:
            continue
        if t > decision_time:
            continue
        if best_t is None or t >= best_t:
            best_t = t
            best = r
    return best


def build_decision_snapshots(
    *,
    episode_id: str,
    attack_cluster_id: str,
    zone_touch: dict[str, Any],
    wall_touch: dict[str, Any] | None,
    detection: dict[str, Any] | None,
    timeline_rows: list[dict[str, Any]] | None,
    wall_side: str | None,
    replay_epoch: int | None,
    source_manifest_hash: str | None,
    first_joint_breach: str | None = None,
    defense_chain_id: str | None = None,
    book_checkpoint_id: str | None = None,
    wall_candidates_reference: str | None = None,
) -> list[dict[str, Any]]:
    """
    Build causal feature snapshots for ANCHORS × SNAPSHOT_OFFSETS_S.
    Enforce event_available_at <= decision_time. DETECTION skipped if unavailable.
    """
    anchors = _anchor_times(
        zone_touch=zone_touch,
        wall_touch=wall_touch,
        detection=detection,
        first_joint_breach=first_joint_breach,
    )
    rows = timeline_rows or []
    out: list[dict[str, Any]] = []
    for anchor in ANCHORS:
        base = anchors.get(anchor)
        if base is None:
            out.append(
                {
                    "episode_id": episode_id,
                    "attack_cluster_id": attack_cluster_id,
                    "anchor_type": anchor,
                    "available": False,
                    "reason": "anchor_unavailable",
                }
            )
            continue
        for offset_s in SNAPSHOT_OFFSETS_S:
            decision_time = base + timedelta(seconds=int(offset_s))
            src = _row_at_or_before(rows, decision_time) if rows else None
            # Causality gate on feature fields
            lookahead = False
            if src is not None:
                avail = src.get("event_available_at") or src.get("available_at") or src.get(
                    "max_input_available_at"
                )
                if avail is not None and parse_utc(avail) > decision_time:
                    lookahead = True
                    src = None
            headroom = empty_headroom_placeholders(
                wall_side=wall_side,
                decision_time=format_utc_z(decision_time),
                replay_epoch=replay_epoch,
                source_manifest_hash=source_manifest_hash,
                wall_candidates_reference=wall_candidates_reference,
                defense_chain_id=defense_chain_id,
                book_checkpoint_id=book_checkpoint_id,
                executable_best_bid=(None if src is None else src.get("best_bid")),
                executable_best_ask=(None if src is None else src.get("best_ask")),
            )
            snap: dict[str, Any] = {
                "episode_id": episode_id,
                "attack_cluster_id": attack_cluster_id,
                "anchor_type": anchor,
                "offset_s": int(offset_s),
                "decision_time": format_utc_z(decision_time),
                "anchor_time": format_utc_z(base),
                "available": True,
                "feature_row_present": src is not None,
                "look_ahead": False,
                "midprice": None if src is None else src.get("midprice"),
                "microprice": None if src is None else src.get("microprice"),
                "best_bid": None if src is None else src.get("best_bid"),
                "best_ask": None if src is None else src.get("best_ask"),
                "event_available_at": None
                if src is None
                else (
                    src.get("event_available_at")
                    or src.get("available_at")
                    or src.get("max_input_available_at")
                ),
                "headroom": headroom,
            }
            if lookahead:
                snap["look_ahead"] = True
                snap["exclusion"] = exclusion_row(
                    reason=LOOKAHEAD_FEATURE_REFUSED,
                    subject_id=episode_id,
                    detail="event_available_at > decision_time",
                    stage="decision_snapshots",
                )
            # Enforce available_at <= decision_time on emitted features
            if snap.get("event_available_at") is not None:
                if parse_utc(snap["event_available_at"]) > decision_time:
                    snap["midprice"] = None
                    snap["microprice"] = None
                    snap["best_bid"] = None
                    snap["best_ask"] = None
                    snap["look_ahead"] = True
                    snap["feature_row_present"] = False
            out.append(snap)
    return out
