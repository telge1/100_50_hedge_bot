"""Thin wrapper around existing replay / 100ms / walls / refill engines."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..drilldown.aggregation_100ms import build_states_100ms
from ..drilldown.engine import _book_at
from ..drilldown.refill import detect_refills
from ..drilldown.replay import replay_window
from ..drilldown.validation import causality_check
from ..drilldown.walls import analyze_walls
from ..timeparse import format_utc_z


def replay_evidence_window(
    *,
    symbol: str,
    evidence_start: datetime,
    evidence_end: datetime,
    first_touch: datetime,
    detection: datetime,
    cfg: dict[str, Any],
    trades: list[Any] | None = None,
) -> dict[str, Any]:
    if evidence_end > detection:
        raise RuntimeError("evidence_end after detection")
    replay = replay_window(
        symbol=symbol,
        window_start=evidence_start,
        window_end=evidence_end,
        trades=trades,
    )
    if not replay.get("ok"):
        return {"ok": False, "replay": replay, "states_100ms": [], "walls": [], "refills": []}
    states = build_states_100ms(
        window_start=replay["window_start"],
        window_end=replay["window_end"],
        timeline=replay["timeline"],
        level_changes=replay["level_changes"],
        trades=replay["trades"],
        book_snapshots_by_time=replay.get("book_snapshots_by_time"),
        initial_bids=replay["initial_bids"],
        initial_asks=replay["initial_asks"],
        bucket_ms=int(cfg["aggregation_bucket_ms"]),
        ordering_confidence=replay.get("ordering_confidence") or "ORDERING_DETERMINISTIC_CONTRACT",
        book_resets=replay.get("book_resets"),
        evidence_start=replay.get("window_start"),
        initial_update_id=replay.get("initial_update_id"),
        initial_sequence_id=replay.get("initial_sequence_id"),
        initial_replay_epoch=replay.get("initial_replay_epoch"),
        initial_checkpoint_id=replay.get("initial_checkpoint_id"),
    )
    causal = causality_check(replay["timeline"], pd.Timestamp(detection))
    if not causal["ok"]:
        return {
            "ok": False,
            "error": "POST_DETECTION_EVENTS",
            "replay": replay,
            "states_100ms": states,
            "n_future_events": causal["n_future_events"],
        }
    bids_touch, asks_touch = _book_at(
        replay["initial_bids"],
        replay["initial_asks"],
        replay["level_changes"],
        pd.Timestamp(first_touch),
    )
    mid = None
    if bids_touch and asks_touch:
        mid = (max(bids_touch) + min(asks_touch)) / 2.0
    walls = analyze_walls(
        level_changes=replay["level_changes"],
        bids_at_trigger=bids_touch,
        asks_at_trigger=asks_touch,
        mid_at_trigger=mid or 0.0,
        causal_end=pd.Timestamp(detection),
        large_notional=float(cfg["wall_large_notional_usdt"]),
        migration_max_bps=float(cfg["wall_migration_max_bps"]),
        migration_max_ms=int(cfg["wall_migration_max_ms"]),
        partial_ratio=float(cfg["wall_partial_consume_min_ratio"]),
        removed_ratio=float(cfg["wall_removed_min_ratio"]),
    )
    refills = detect_refills(
        replay["level_changes"],
        refill_window_ms=int(cfg["refill_window_ms"]),
        nearby_max_bps=float(cfg["nearby_refill_max_bps"]),
        causal_end=pd.Timestamp(detection),
        mid_price_hint=mid,
    )
    return {
        "ok": True,
        "replay": replay,
        "states_100ms": states,
        "walls": walls,
        "refills": refills,
        "bids_at_touch": bids_touch,
        "asks_at_touch": asks_touch,
        "mid_at_touch": mid,
        "ordering_confidence": replay.get("ordering_confidence"),
        "sequence_gaps": int(replay.get("sequence_gaps") or 0),
        "replay_epoch": replay.get("replay_epoch"),
        "window_start": format_utc_z(replay["window_start"]),
        "window_end": format_utc_z(replay["window_end"]),
        "engines": {
            "replay": "drilldown.replay.replay_window",
            "states": "drilldown.aggregation_100ms.build_states_100ms",
            "walls": "drilldown.walls.analyze_walls",
            "refills": "drilldown.refill.detect_refills",
            "book_at": "drilldown.engine._book_at",
            "book_at_note": "walls/refills still use snapshot-blind _book_at; 100ms uses book_resets",
        },
    }


def drilldown_cfg_values(drilldown: dict[str, Any]) -> dict[str, Any]:
    return {
        "aggregation_bucket_ms": int(drilldown["aggregation_bucket_ms"]),
        "wall_large_notional_usdt": float(drilldown["wall_large_notional_usdt"]),
        "wall_migration_max_bps": float(drilldown["wall_migration_max_bps"]),
        "wall_migration_max_ms": int(drilldown["wall_migration_max_ms"]),
        "wall_partial_consume_min_ratio": float(drilldown["wall_partial_consume_min_ratio"]),
        "wall_removed_min_ratio": float(drilldown["wall_removed_min_ratio"]),
        "refill_window_ms": int(drilldown["refill_window_ms"]),
        "nearby_refill_max_bps": float(drilldown["nearby_refill_max_bps"]),
    }
