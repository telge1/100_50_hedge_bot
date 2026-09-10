"""Wall lifecycle timestamps. Copied locally so this package does not import the smoke module."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import pandas as pd

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..drilldown.walls import analyze_walls
from ..level_first_window_native_full_ob_direction_v1.quality import spatial_coverage
from ..timeparse import format_utc_z
from .persist import event_available_at, ts_cell


def _ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return parse_utc(value)


def wall_id_for(episode_id: str, side: str, price: float) -> str:
    raw = f"{episode_id}|{side}|{price:.10f}"
    return "w_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def enrich_walls(
    *,
    walls: list[dict[str, Any]],
    level_changes: list[dict[str, Any]],
    episode_id: str,
    first_touch: datetime,
    detection: datetime,
    evidence_start: datetime,
    zone_low: float,
    zone_high: float,
    direction: str,
    reaction_class: str,
    mid_at_touch: float | None,
    bids_at_touch: dict[float, float],
    asks_at_touch: dict[float, float],
    bids_at_detection: dict[float, float],
    asks_at_detection: dict[float, float],
    config_hash: str,
    adjacent_bps: float,
    migration_max_bps: float,
    migration_max_ms: int,
    replay_epoch_at_touch: Any,
) -> list[dict[str, Any]]:
    det = pd.Timestamp(detection)
    changes = [e for e in level_changes if pd.to_datetime(e["event_time"], utc=True) < det]
    spatial_touch = spatial_coverage(
        reaction_class=reaction_class,
        reaction_direction=direction,
        zone_low=zone_low,
        zone_high=zone_high,
        adjacent_bps=adjacent_bps,
        bids=bids_at_touch,
        asks=asks_at_touch,
        mid_hint=mid_at_touch,
    )
    out = []
    for w in walls:
        side = str(w["side"])
        px = float(w["price"])
        wid = wall_id_for(episode_id, side, px)
        evs = [
            e for e in changes if str(e["side"]) == side and abs(float(e["price"]) - px) <= 1e-9
        ]
        rem = [e for e in evs if float(e.get("size_delta") or 0) < 0]
        add = [e for e in evs if float(e.get("size_delta") or 0) > 0]
        first_seen = first_touch
        last_change = first_touch
        if evs:
            times = [_ts(e["event_time"]) for e in evs]
            times = [t for t in times if t]
            if times:
                last_change = max(times)
                before = [t for t in times if t < first_touch]
                if before:
                    first_seen = min(before)
        first_rem = min((_ts(e["event_time"]) for e in rem), default=None)
        last_rem = max((_ts(e["event_time"]) for e in rem), default=None)
        book_det = bids_at_detection if side == "bid" else asks_at_detection
        present_det = float(book_det.get(px) or 0.0) > 0
        mig_ts = None
        mig_px = None
        if w.get("migration_proxy") and last_rem is not None:
            win = pd.Timedelta(milliseconds=int(migration_max_ms))
            mid = mid_at_touch or px
            for e in changes:
                at = pd.to_datetime(e["event_time"], utc=True)
                if at < pd.Timestamp(last_rem) or at - pd.Timestamp(last_rem) > win:
                    continue
                if str(e["side"]) != side or float(e.get("size_delta") or 0) <= 0:
                    continue
                apx = float(e["price"])
                dist = abs(apx - px) / mid * 1e4 if mid else 0.0
                if 1e-9 < dist <= float(migration_max_bps):
                    mig_ts = _ts(e["event_time"])
                    mig_px = apx
                    break
        inside = zone_low <= px <= zone_high
        relation = "INSIDE_LEVEL_ZONE" if inside else "OUTSIDE_LEVEL_ZONE"
        processed = event_available_at(first_touch)
        # event_time = wall statement time (touch as-of). Not a 100ms bucket end.
        # event_available_at = earliest causal availability of that as-of book.
        # Do not overload available_at: omitted on WALL rows.
        out.append(
            {
                "wall_id": wid,
                "event_type": "WALL",
                "side": side,
                "price": px,
                "size": None,
                "notional": w.get("initial_notional"),
                "initial_notional": w.get("initial_notional"),
                "removed_notional": w.get("removed_notional"),
                "refilled_same_level_notional": w.get("refilled_same_level_notional"),
                "event_time": format_utc_z(first_touch),
                "event_available_at": format_utc_z(first_touch),
                "state_available_at": None,
                "replay_epoch": replay_epoch_at_touch,
                "first_seen_ts": format_utc_z(first_seen),
                "last_seen_ts": format_utc_z(last_change),
                "last_change_ts": format_utc_z(last_change),
                "first_removal_ts": format_utc_z(first_rem) if first_rem else None,
                "last_removal_ts": format_utc_z(last_rem) if last_rem else None,
                "removed_ts": format_utc_z(last_rem) if last_rem else None,
                "removal_ratio": w.get("removed_notional", 0.0) / w["initial_notional"]
                if w.get("initial_notional")
                else 0.0,
                "migration_ts": format_utc_z(mig_ts) if mig_ts else None,
                "migration_price": mig_px,
                "migration_proxy": bool(w.get("migration_proxy")),
                "persisted": bool(w.get("wall_status") == "WALL_PERSISTED" or present_det),
                "removed": bool(w.get("wall_status") in {"WALL_REMOVED_LIKELY", "WALL_PARTIALLY_CONSUMED"}),
                "refilled": bool(w.get("wall_status") == "WALL_REFILLED"),
                "still_present_at_detection": present_det,
                "wall_status": w.get("wall_status"),
                "level_zone_relation": relation,
                "inside_level_zone": inside,
                "spatial_at_touch": spatial_touch.get("spatial_coverage_quality"),
                "source_thresholds_config_hash": config_hash,
                "evidence_start": format_utc_z(evidence_start),
                "n_change_events": len(evs),
                "n_add_events": len(add),
                "underlying_100ms_available_at": ts_cell(processed),
                "state_source": "checkpoint_capable_event_stream",
                "book_stream": "reconstruct_book_asof_exclusive",
            }
        )
    return out


def analyze_walls_timed(
    *,
    level_changes: list[dict[str, Any]],
    bids_at_trigger: dict[float, float],
    asks_at_trigger: dict[float, float],
    mid_at_trigger: float,
    detection: datetime,
    cfg: dict[str, Any],
    replay_epoch_at_touch: Any,
    **enrich_kw: Any,
) -> list[dict[str, Any]]:
    walls = analyze_walls(
        level_changes=level_changes,
        bids_at_trigger=bids_at_trigger,
        asks_at_trigger=asks_at_trigger,
        mid_at_trigger=mid_at_trigger,
        causal_end=pd.Timestamp(detection),
        large_notional=float(cfg["wall_large_notional_usdt"]),
        migration_max_bps=float(cfg["wall_migration_max_bps"]),
        migration_max_ms=int(cfg["wall_migration_max_ms"]),
        partial_ratio=float(cfg["wall_partial_consume_min_ratio"]),
        removed_ratio=float(cfg["wall_removed_min_ratio"]),
    )
    return enrich_walls(
        walls=walls,
        level_changes=level_changes,
        first_touch=enrich_kw["first_touch"],
        detection=detection,
        evidence_start=enrich_kw["evidence_start"],
        episode_id=enrich_kw["episode_id"],
        zone_low=enrich_kw["zone_low"],
        zone_high=enrich_kw["zone_high"],
        direction=enrich_kw["direction"],
        reaction_class=enrich_kw["reaction_class"],
        mid_at_touch=mid_at_trigger,
        bids_at_touch=bids_at_trigger,
        asks_at_touch=asks_at_trigger,
        bids_at_detection=enrich_kw["bids_at_detection"],
        asks_at_detection=enrich_kw["asks_at_detection"],
        config_hash=enrich_kw["config_hash"],
        adjacent_bps=float(cfg["nearby_refill_max_bps"]),
        migration_max_bps=float(cfg["wall_migration_max_bps"]),
        migration_max_ms=int(cfg["wall_migration_max_ms"]),
        replay_epoch_at_touch=replay_epoch_at_touch,
    )
