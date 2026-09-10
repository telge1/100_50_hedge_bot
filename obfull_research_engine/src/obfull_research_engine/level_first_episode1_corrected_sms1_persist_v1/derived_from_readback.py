"""Production derived events from closed, re-opened sms1 tables only."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt, book_map_sha256
from . import EPISODE_ID
from .classify_levels import classify_level_kind, is_size_reduction
from .derived import available_at_violations, build_touch_detection, build_walls
from .persist import event_available_at, ts_cell
from .reader import assert_persisted_sms1_tables, load_table, replay_payload_from_tables
from .refill_confirm import classify_refills
from .time_contract import last_state_available_at

TARGET_WALL = {
    "wall_id": "w_781b6ed696e777e1",
    "price": 79780.0,
    "reported_available_at": "2026-09-06T20:19:02.229Z",
}


def _replay_from_disk(directory: Path, *, fallback_start: datetime) -> dict[str, Any]:
    payload = replay_payload_from_tables(directory)
    payload["window_start"] = _as_dt(payload["window_start"]) if payload.get("window_start") else fallback_start
    payload["evidence_start"] = payload["window_start"]
    return payload


def derive_from_persisted_sms1(
    directory: Path,
    *,
    cfg: dict[str, Any],
    config_hash: str,
    first_touch: datetime,
    detection: datetime,
    episode_id: str = EPISODE_ID,
    touch_available_at: datetime | None = None,
    detection_available_at: datetime | None = None,
) -> dict[str, Any]:
    """Must be called after sms1 files are closed. Opens them again via the reader.

    first_touch / detection must be independently derived — never module constants.
    """
    if first_touch is None or detection is None:
        raise ValueError("first_touch and detection must be independently derived inputs")
    assert_persisted_sms1_tables(directory)
    states = load_table(directory, "states_100ms", require=True)
    replay = _replay_from_disk(directory, fallback_start=first_touch)
    touches, detections, touch_book, det_book = build_touch_detection(
        replay=replay,
        states=states,
        first_touch=first_touch,
        detection=detection,
        episode_id=episode_id,
        touch_available_at=touch_available_at,
        detection_available_at=detection_available_at,
    )
    walls = build_walls(
        replay=replay,
        touch=touch_book,
        detection_book=det_book,
        cfg=cfg,
        config_hash=config_hash,
        first_touch=first_touch,
        detection=detection,
        episode_id=episode_id,
    )
    state_avail_touch = last_state_available_at(states, first_touch)
    for wall in walls:
        wall["state_available_at"] = state_avail_touch
        wall["qty"] = None
        book = touch_book["asks"] if wall["side"] == "ask" else touch_book["bids"]
        qty = float((book or {}).get(float(wall["price"])) or 0.0)
        wall["qty"] = qty
        wall["size"] = qty

    mid = None
    if touch_book.get("best_bid") is not None and touch_book.get("best_ask") is not None:
        mid = (float(touch_book["best_bid"]) + float(touch_book["best_ask"])) / 2.0

    kind_counts: dict[str, int] = {}
    classified: list[dict[str, Any]] = []
    for row in replay.get("level_changes") or []:
        rec = dict(row)
        rec["level_kind"] = classify_level_kind(rec.get("old_size"), rec.get("new_size"))
        kind_counts[rec["level_kind"]] = kind_counts.get(rec["level_kind"], 0) + 1
        classified.append(rec)

    level_removals = []
    for rec in classified:
        if not is_size_reduction(rec["level_kind"]):
            continue
        et = _as_dt(rec["event_time"])
        avail = event_available_at(et)
        level_removals.append(
            {
                **rec,
                "event_type": rec["level_kind"],
                "event_time": ts_cell(et),
                "event_available_at": ts_cell(avail),
                "state_available_at": ts_cell(avail),
            }
        )

    refill = classify_refills(
        level_changes=classified,
        book_resets=replay.get("book_resets") or [],
        refill_window_ms=int(cfg["refill_window_ms"]),
        nearby_max_bps=float(cfg["nearby_refill_max_bps"]),
        partial_ratio=float(cfg["wall_partial_consume_min_ratio"]),
        causal_end=detection,
        mid_hint=mid,
    )

    same_stream = (
        touch_book.get("state_source") == "checkpoint_capable_event_stream"
        and det_book.get("state_source") == "checkpoint_capable_event_stream"
        and touches[0].get("book_stream") == detections[0].get("book_stream") == "reconstruct_book_asof_exclusive"
    )
    violations = available_at_violations(
        states=states,
        walls=walls,
        refills=refill["confirmed"] + refill["candidates"],
        touches=touches,
        detections=detections,
    )

    wall_audit = audit_target_wall(walls, touch_book, states, first_touch=first_touch)
    return {
        "derived_input_mode": "persisted_sms1_readback",
        "states": states,
        "replay": replay,
        "walls": walls,
        "level_changes_classified": classified,
        "level_kind_counts": kind_counts,
        "n_level_changes": len(classified),
        "level_removals": level_removals,
        "refill": refill,
        "touches": touches,
        "detections": detections,
        "touch_book": {
            "best_bid": touch_book.get("best_bid"),
            "best_ask": touch_book.get("best_ask"),
            "replay_epoch": touch_book.get("replay_epoch"),
            "n_bid_levels": len(touch_book.get("bids") or {}),
            "n_ask_levels": len(touch_book.get("asks") or {}),
            "book_map_sha256": book_map_sha256(touch_book.get("bids") or {}, touch_book.get("asks") or {}),
            "state_source": touch_book.get("state_source"),
        },
        "detection_book": {
            "best_bid": det_book.get("best_bid"),
            "best_ask": det_book.get("best_ask"),
            "replay_epoch": det_book.get("replay_epoch"),
            "n_bid_levels": len(det_book.get("bids") or {}),
            "n_ask_levels": len(det_book.get("asks") or {}),
            "book_map_sha256": book_map_sha256(det_book.get("bids") or {}, det_book.get("asks") or {}),
            "state_source": det_book.get("state_source"),
        },
        "same_stream": same_stream,
        "available_at_violations": violations,
        "wall_audit_w_781b6ed696e777e1": wall_audit,
        "episode_id": episode_id,
        "first_touch": first_touch,
        "detection": detection,
    }


def audit_target_wall(
    walls: list[dict[str, Any]],
    touch_book: dict[str, Any],
    states: list[dict[str, Any]],
    *,
    first_touch: datetime,
) -> dict[str, Any]:
    target = None
    for wall in walls:
        if wall.get("wall_id") == TARGET_WALL["wall_id"] or (
            abs(float(wall.get("price") or 0.0) - TARGET_WALL["price"]) <= 1e-9 and wall.get("side") == "ask"
        ):
            target = wall
            break
    qty = float((touch_book.get("asks") or {}).get(TARGET_WALL["price"]) or 0.0)
    notional = qty * TARGET_WALL["price"]
    state_avail = last_state_available_at(states, first_touch)
    return {
        "wall_id": TARGET_WALL["wall_id"],
        "price": TARGET_WALL["price"],
        "found": target is not None,
        "side": (target or {}).get("side"),
        "event_time": (target or {}).get("event_time"),
        "event_available_at": (target or {}).get("event_available_at"),
        "state_available_at": (target or {}).get("state_available_at") or state_avail,
        "reported_available_at_was": TARGET_WALL["reported_available_at"],
        "why_not_100ms_grid": (
            "event_time is the zone-first-touch as-of instant for the wall observation book; "
            "not a bucket_end_exclusive. "
            f"Last finished 100ms state is state_available_at {state_avail}."
        ),
        "field_meaning": "event_available_at = earliest causal availability of the touch as-of book",
        "persisted_source": "sms1 walls.jsonl.zst after persisted_sms1_readback; book from persist reconstruct at first_touch",
        "qty_at_touch": qty,
        "notional_at_touch": notional,
        "book_exists_at_touch": qty > 0,
        "replay_epoch": (target or {}).get("replay_epoch") or touch_book.get("replay_epoch"),
        "complete_100ms_state_at_this_instant": False,
        "available_at_not_used_on_wall_rows": True,
    }
