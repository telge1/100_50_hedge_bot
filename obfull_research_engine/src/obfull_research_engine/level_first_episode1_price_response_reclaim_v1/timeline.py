"""Causal price-response timeline from handoff + full-L2 book + wall-flow join."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..level_first_episode1_detection_to_wall_flow_integration_v1.handoff import (
    Episode1Handoff,
    HandoffError,
    validate_handoff,
)
from ..timeparse import format_utc_z
from . import TICK_SIZE, TIME_BASIS, WALL_STATE
from .book_stream import iter_state_bbo, last_book_coverage_end, load_book_payload, load_states
from .impact_link import join_wall_flow_impact
from .metrics import compute_point_metrics
from .reclaim_raw import attach_running_cross_fields, build_reclaim_raw_bundle


def load_handoff(path: Path | str) -> Episode1Handoff:
    import json

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_handoff(raw)


def build_price_response_timeline(
    *,
    handoff: Episode1Handoff,
    persist_dir: Path,
    wall_flow_timeline_csv: Path | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    """Build causal FEATURE timeline. Fail-closed on missing handoff / bad generation epoch gate."""
    if handoff is None:
        raise HandoffError("missing handoff")
    persist_dir = Path(persist_dir)
    payload = load_book_payload(persist_dir)
    states = load_states(persist_dir)
    wall_touch = _as_dt(handoff.wall_first_touch_exchange_event_time)
    detection = _as_dt(handoff.detection_exchange_event_time)
    t0 = start_at or wall_touch
    t1 = end_at or detection
    coverage_end = last_book_coverage_end(payload)

    mid_at_touch: float | None = None
    rows: list[dict[str, Any]] = []
    for bbo in iter_state_bbo(payload=payload, states=states, expected_epoch=None):
        # Do not hard-fail entire timeline on epoch change; mark row invalid.
        dt = _as_dt(bbo["decision_time"])
        if dt < t0 or dt > t1:
            continue
        if bbo["best_bid"] is None or bbo["best_ask"] is None:
            row = {
                **bbo,
                "coverage_ok": False,
                "invalid_reason": bbo.get("invalid_reason") or "missing_bbo",
                "look_ahead": False,
            }
            rows.append(row)
            continue

        # Epoch gate vs handoff binding: change after wall touch invalidates row.
        epoch = bbo.get("replay_epoch")
        epoch_mismatch = epoch is not None and int(epoch) != int(handoff.replay_epoch)
        if epoch_mismatch:
            bbo = dict(bbo)
            bbo["coverage_ok"] = False
            bbo["invalid_reason"] = "replay_epoch_change"

        if mid_at_touch is None and bbo.get("coverage_ok"):
            mid_at_touch = 0.5 * (float(bbo["best_bid"]) + float(bbo["best_ask"]))

        if not bbo.get("coverage_ok"):
            rows.append(
                {
                    **bbo,
                    "wall_generation_id": handoff.wall_generation_id,
                    "wall_side": handoff.wall_side,
                    "wall_price": handoff.wall_price,
                    "WALL_STATE": WALL_STATE,
                    "feature_layer": "FEATURE",
                }
            )
            continue

        m = compute_point_metrics(
            best_bid=float(bbo["best_bid"]),
            best_ask=float(bbo["best_ask"]),
            best_bid_size=float(bbo["best_bid_size"]),
            best_ask_size=float(bbo["best_ask_size"]),
            wall_price=handoff.wall_price,
            wall_side=handoff.wall_side,
            zone_low=handoff.zone_low,
            zone_high=handoff.zone_high,
            mid_at_anchor=mid_at_touch,
            tick_size=TICK_SIZE,
        )
        # Causal look-ahead vs this decision time
        look_ahead = bool(_as_dt(bbo["max_input_available_at"]) > dt)
        rows.append(
            {
                **bbo,
                **m,
                "look_ahead": look_ahead,
                "wall_generation_id": handoff.wall_generation_id,
                "wall_id": handoff.wall_id,
                "wall_side": handoff.wall_side,
                "wall_price": handoff.wall_price,
                "zone_id": handoff.zone_id,
                "zone_low": handoff.zone_low,
                "zone_high": handoff.zone_high,
                "WALL_STATE": WALL_STATE,
                "feature_layer": "FEATURE",
                "time_basis": TIME_BASIS,
            }
        )

    rows = attach_running_cross_fields(
        rows, wall_price=handoff.wall_price, wall_side=handoff.wall_side
    )
    if wall_flow_timeline_csv is not None:
        rows = join_wall_flow_impact(rows, Path(wall_flow_timeline_csv))

    reclaim = build_reclaim_raw_bundle(
        rows, wall_price=handoff.wall_price, wall_side=handoff.wall_side
    )
    n_ok = sum(1 for r in rows if r.get("coverage_ok"))
    n_la = sum(1 for r in rows if r.get("look_ahead"))
    return {
        "rows": rows,
        "reclaim_raw": reclaim,
        "meta": {
            "n_rows": len(rows),
            "n_coverage_ok": n_ok,
            "n_look_ahead": n_la,
            "wall_touch_at": format_utc_z(wall_touch),
            "detection_at": format_utc_z(detection),
            "book_coverage_end": format_utc_z(coverage_end) if coverage_end else None,
            "mid_at_wall_touch": mid_at_touch,
            "time_basis": TIME_BASIS,
            "tick_size": TICK_SIZE,
        },
    }


def write_timeline_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # Stable column union
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})
