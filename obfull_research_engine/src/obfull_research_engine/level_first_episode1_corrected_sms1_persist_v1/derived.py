"""Derived walls, refills, touches, detections from the corrected Full-OB stream only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..drilldown.aggregation_100ms import (
    _as_dt,
    book_map_sha256,
    last_complete_state,
    reconstruct_book_asof_exclusive,
)
from ..drilldown.refill import detect_refills
from ..timeparse import format_utc_z
from . import DETECTION, EPISODE_ID, FIRST_TOUCH
from .persist import event_available_at, ts_cell
from .walls import analyze_walls_timed


def _stream_at(replay: dict[str, Any], until: datetime) -> dict[str, Any]:
    return reconstruct_book_asof_exclusive(
        initial_bids=replay["initial_bids"],
        initial_asks=replay["initial_asks"],
        level_changes=replay["level_changes"],
        until=until,
        book_resets=replay.get("book_resets"),
        book_snapshots_by_time=replay.get("book_snapshots_by_time"),
        evidence_start=replay["window_start"],
        initial_update_id=replay.get("initial_update_id"),
        initial_sequence_id=replay.get("initial_sequence_id"),
        initial_replay_epoch=replay.get("initial_replay_epoch"),
        initial_checkpoint_id=replay.get("initial_checkpoint_id"),
    )


def _causal_raw(stream: dict[str, Any], replay: dict[str, Any], until: datetime) -> dict[str, Any]:
    last_ts = stream.get("last_applied_event_ts")
    last_type = stream.get("last_event_type")
    source_id = stream.get("checkpoint_or_snapshot_id")
    if last_ts is None:
        return {"causal_event_time": None, "causal_event_type": "initial_book", "causal_source_event_id": None}
    return {
        "causal_event_time": ts_cell(last_ts),
        "causal_event_type": last_type,
        "causal_source_event_id": source_id,
        "asof_exclusive": ts_cell(until),
    }


def build_touch_detection(
    *,
    replay: dict[str, Any],
    states: list[dict[str, Any]],
    first_touch: datetime,
    detection: datetime,
    episode_id: str = EPISODE_ID,
    touch_available_at: datetime | None = None,
    detection_available_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Build TOUCH/DETECTION book snapshots. Times must be pre-derived — not module constants."""
    touch_avail = touch_available_at or first_touch
    det_avail = detection_available_at or detection
    touch = _stream_at(replay, first_touch)
    det = _stream_at(replay, detection)
    last_touch_state = last_complete_state(states, first_touch)
    last_det_state = last_complete_state(states, detection)
    touch_row = {
        "event_id": f"touch:{episode_id}:{format_utc_z(first_touch)}",
        "event_type": "TOUCH",
        "semantic_event_type": "ZONE_FIRST_TOUCH_BOOK_ASOF",
        "event_time": format_utc_z(first_touch),
        "event_available_at": format_utc_z(touch_avail),
        "state_available_at": ts_cell(last_touch_state["available_at"]) if last_touch_state else None,
        "replay_epoch": touch.get("replay_epoch"),
        "side": None,
        "price": None,
        "best_bid": touch.get("best_bid"),
        "best_ask": touch.get("best_ask"),
        "n_bid_levels": len(touch.get("bids") or {}),
        "n_ask_levels": len(touch.get("asks") or {}),
        "book_map_sha256": book_map_sha256(touch.get("bids") or {}, touch.get("asks") or {}),
        "last_update_id": touch.get("last_update_id"),
        "sequence_id": touch.get("sequence_id"),
        "checkpoint_or_snapshot_id": touch.get("checkpoint_or_snapshot_id"),
        "state_source": touch.get("state_source"),
        "book_stream": "reconstruct_book_asof_exclusive",
        "underlying_100ms_cutoff": ts_cell(last_touch_state["available_at"]) if last_touch_state else None,
        "underlying_100ms_bucket_start": ts_cell(last_touch_state["bucket_start"]) if last_touch_state else None,
        **_causal_raw(touch, replay, first_touch),
    }
    det_row = {
        "event_id": f"detection:{episode_id}:{format_utc_z(detection)}",
        "event_type": "DETECTION",
        "event_time": format_utc_z(detection),
        "event_available_at": format_utc_z(det_avail),
        "detected_at": format_utc_z(det_avail),
        "state_available_at": ts_cell(last_det_state["available_at"]) if last_det_state else None,
        "replay_epoch": det.get("replay_epoch"),
        "side": None,
        "price": None,
        "best_bid": det.get("best_bid"),
        "best_ask": det.get("best_ask"),
        "n_bid_levels": len(det.get("bids") or {}),
        "n_ask_levels": len(det.get("asks") or {}),
        "book_map_sha256": book_map_sha256(det.get("bids") or {}, det.get("asks") or {}),
        "last_update_id": det.get("last_update_id"),
        "sequence_id": det.get("sequence_id"),
        "checkpoint_or_snapshot_id": det.get("checkpoint_or_snapshot_id"),
        "state_source": det.get("state_source"),
        "book_stream": "reconstruct_book_asof_exclusive",
        "underlying_100ms_cutoff": ts_cell(last_det_state["available_at"]) if last_det_state else None,
        "underlying_100ms_bucket_start": ts_cell(last_det_state["bucket_start"]) if last_det_state else None,
        **_causal_raw(det, replay, detection),
    }
    return [touch_row], [det_row], touch, det


def build_walls(
    *,
    replay: dict[str, Any],
    touch: dict[str, Any],
    detection_book: dict[str, Any],
    cfg: dict[str, Any],
    config_hash: str,
    first_touch: datetime,
    detection: datetime,
    episode_id: str = EPISODE_ID,
) -> list[dict[str, Any]]:
    bids = touch["bids"]
    asks = touch["asks"]
    mid = None
    if touch.get("best_bid") is not None and touch.get("best_ask") is not None:
        mid = (float(touch["best_bid"]) + float(touch["best_ask"])) / 2.0
    return analyze_walls_timed(
        level_changes=replay["level_changes"],
        bids_at_trigger=bids,
        asks_at_trigger=asks,
        mid_at_trigger=mid or 0.0,
        detection=detection,
        cfg=cfg,
        first_touch=first_touch,
        evidence_start=replay["window_start"],
        episode_id=episode_id,
        zone_low=float(cfg["zone_low"]),
        zone_high=float(cfg["zone_high"]),
        direction=cfg["reaction_direction"],
        reaction_class=cfg["reaction_class"],
        bids_at_detection=detection_book["bids"],
        asks_at_detection=detection_book["asks"],
        config_hash=config_hash,
        replay_epoch_at_touch=touch.get("replay_epoch"),
    )


def build_refills(
    *,
    replay: dict[str, Any],
    walls: list[dict[str, Any]],
    mid_at_touch: float | None,
    cfg: dict[str, Any],
    states: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wall_by_key = {(w["side"], float(w["price"])): w["wall_id"] for w in walls}
    epoch_by_avail: list[tuple[datetime, Any]] = [
        (_as_dt(s["available_at"]), s.get("replay_epoch")) for s in states
    ]
    refills = detect_refills(
        replay["level_changes"],
        refill_window_ms=int(cfg["refill_window_ms"]),
        nearby_max_bps=float(cfg["nearby_refill_max_bps"]),
        causal_end=pd.Timestamp(DETECTION),
        mid_price_hint=mid_at_touch,
    )
    rows: list[dict[str, Any]] = []
    n = 0
    for e in replay["level_changes"]:
        et = _as_dt(e["event_time"])
        if et >= DETECTION:
            continue
        side = str(e.get("side") or "")
        px = e.get("price")
        if px is None:
            continue
        wid = wall_by_key.get((side, float(px)))
        if wid is None:
            continue
        n += 1
        sd = float(e.get("size_delta") or 0)
        avail = event_available_at(et)
        epoch = e.get("replay_epoch", e.get("epoch"))
        rows.append(
            {
                "event_id": e.get("source_event_id") or f"lc:{n}",
                "event_type": "WALL_LEVEL_CHANGE",
                "event_time": ts_cell(et),
                "available_at": ts_cell(avail),
                "replay_epoch": epoch,
                "side": side,
                "price": float(px),
                "previous_size": e.get("old_size"),
                "new_size": e.get("new_size"),
                "change": sd,
                "size": e.get("new_size"),
                "refill_type": "REMOVAL" if sd < 0 else ("ADD" if sd > 0 else "UNCHANGED"),
                "exact_or_nearby": None,
                "related_wall_id": wid,
                "source": "level_change",
                "causal_source_event_id": e.get("source_event_id"),
                "underlying_100ms_cutoff": ts_cell(avail),
                "state_source": "checkpoint_capable_event_stream",
            }
        )
    for i, r in enumerate(refills):
        et = _as_dt(r.get("event_time"))
        if et >= DETECTION:
            continue
        rtype = str(r.get("refill_type") or "")
        if rtype not in {"EXACT_REFILL", "NEARBY_REFILL", "NO_REFILL_OBSERVED"}:
            continue
        side = str(r.get("side") or "")
        orig = r.get("original_price")
        wid = wall_by_key.get((side, float(orig))) if orig is not None else None
        avail = event_available_at(et)
        epoch = None
        for a, ep in epoch_by_avail:
            if a <= avail:
                epoch = ep
            else:
                break
        rows.append(
            {
                "event_id": r.get("removal_event_id") or f"refill:{i}",
                "event_type": "WALL_REFILL",
                "event_time": ts_cell(et),
                "available_at": ts_cell(avail),
                "replay_epoch": epoch,
                "side": side,
                "price": r.get("refill_price") if r.get("refill_price") is not None else orig,
                "previous_size": None,
                "new_size": None,
                "change": r.get("refilled_notional"),
                "size": r.get("refilled_notional"),
                "refill_type": rtype,
                "exact_or_nearby": "EXACT"
                if rtype == "EXACT_REFILL"
                else ("NEARBY" if rtype == "NEARBY_REFILL" else None),
                "related_wall_id": wid,
                "original_price": orig,
                "removed_notional": r.get("removed_notional"),
                "refilled_notional": r.get("refilled_notional"),
                "source": "detect_refills",
                "causal_source_event_id": r.get("removal_event_id"),
                "add_event_id": r.get("add_event_id"),
                "underlying_100ms_cutoff": ts_cell(avail),
                "state_source": "checkpoint_capable_event_stream",
            }
        )
    return rows


def _row_event_available_at(rec: dict[str, Any]) -> Any:
    return rec.get("event_available_at") or rec.get("available_at")


def available_at_violations(
    *,
    states: list[dict[str, Any]],
    walls: list[dict[str, Any]],
    refills: list[dict[str, Any]],
    touches: list[dict[str, Any]],
    detections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """event_available_at may not precede any required input's availability."""
    problems: list[dict[str, Any]] = []
    for row in states:
        if _as_dt(row["available_at"]) != _as_dt(row["bucket_end_exclusive"]):
            problems.append({"kind": "state_available_at_ne_bucket_end", "row": row.get("bucket_start")})
        if _as_dt(row["available_at"]) <= _as_dt(row["bucket_start"]):
            problems.append({"kind": "state_available_at_le_start", "row": row.get("bucket_start")})
        if row.get("available_at") is None:
            problems.append({"kind": "state_missing_available_at", "row": row.get("bucket_start")})
    for kind, rows in (
        ("WALL", walls),
        ("REFILL", refills),
        ("TOUCH", touches),
        ("DETECTION", detections),
    ):
        for rec in rows:
            avail = _row_event_available_at(rec)
            et = rec.get("event_time")
            if avail is None or et is None:
                problems.append(
                    {
                        "kind": "missing_event_available_at",
                        "event_type": kind,
                        "event_id": rec.get("event_id") or rec.get("wall_id"),
                    }
                )
                continue
            if rec.get("event_type") in {
                "WALL_LEVEL_CHANGE",
                "WALL_REFILL",
                "REFILL",
                "LEVEL_REMOVE",
                "LEVEL_DECREASE",
                "LEVEL_ADD",
                "LEVEL_INCREASE",
            }:
                if _as_dt(avail) < _as_dt(et):
                    problems.append(
                        {
                            "kind": "event_before_available_at",
                            "event_type": rec.get("event_type"),
                            "event_id": rec.get("event_id"),
                            "event_time": et,
                            "event_available_at": avail,
                        }
                    )
            if rec.get("event_type") in {"TOUCH", "DETECTION", "WALL"}:
                if rec.get("book_stream") and rec.get("book_stream") != "reconstruct_book_asof_exclusive":
                    in_progress = None
                    for st in states:
                        if _as_dt(st["bucket_start"]) <= _as_dt(et) < _as_dt(st["bucket_end_exclusive"]):
                            in_progress = st
                            break
                    if in_progress is not None and _as_dt(in_progress["available_at"]) > _as_dt(et):
                        problems.append(
                            {
                                "kind": "used_100ms_state_before_available_at",
                                "event_type": rec.get("event_type"),
                                "event_id": rec.get("event_id") or rec.get("wall_id"),
                                "event_time": et,
                                "state_available_at": ts_cell(in_progress["available_at"]),
                            }
                        )
            if rec.get("event_type") == "DETECTION":
                inputs: list[Any] = []
                for touch in touches:
                    inputs.append(_row_event_available_at(touch))
                for wall in walls:
                    inputs.append(_row_event_available_at(wall))
                last = last_complete_state(states, _as_dt(et))
                if last is not None:
                    inputs.append(last["available_at"])
                present = [x for x in inputs if x is not None]
                if present and _as_dt(avail) < max(_as_dt(x) for x in present):
                    problems.append(
                        {
                            "kind": "detection_before_input_availability",
                            "event_id": rec.get("event_id"),
                            "event_available_at": avail,
                            "max_input_available_at": ts_cell(max(_as_dt(x) for x in present)),
                        }
                    )
    return problems
