"""Chronological visits per zone via detect_visits_for_cluster — ALL visits, no visit_count selector."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import (
    detect_visits_for_cluster,
    episode_id,
    parse_utc,
)
from ..timeparse import format_utc_z


def load_jsonl(path: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def trades_as_events(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize trade rows to {ts, price} for detect_visits_for_cluster."""
    out: list[dict[str, Any]] = []
    for t in trades:
        ts = t.get("ts") or t.get("trade_ts") or t.get("exchange_event_time")
        if ts is None or t.get("price") is None:
            continue
        out.append({"ts": ts, "price": float(t["price"]), **{k: v for k, v in t.items() if k not in ("ts",)}})
    return out


def candles_normalized(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in candles:
        row = dict(c)
        if "open_time" not in row and "ts" in row:
            row["open_time"] = row["ts"]
        out.append(row)
    return out


def build_zone_touch_from_visit(
    visit: dict[str, Any],
    *,
    zone: dict[str, Any],
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map a visit episode to a ZONE_FIRST_TOUCH record (no visit_count selection)."""
    touch_ts = parse_utc(visit["first_touch_ts"])
    avail = touch_ts
    trigger_id = None
    trigger_price = None
    if trades:
        # Best-effort: first trade at/after touch with price in zone
        zlo, zhi = float(zone["zone_low"]), float(zone["zone_high"])
        for t in sorted(trades, key=lambda r: parse_utc(r.get("trade_ts") or r.get("ts"))):
            ts = parse_utc(t.get("trade_ts") or t.get("ts"))
            if ts < touch_ts:
                continue
            px = float(t["price"])
            if zlo <= px <= zhi:
                trigger_id = t.get("trade_id")
                trigger_price = px
                recv = t.get("collector_received_at")
                if recv:
                    avail = max(touch_ts, parse_utc(recv))
                break
    return {
        "event_type": "ZONE_FIRST_TOUCH",
        "episode_id": visit["episode_id"],
        "zone_id": zone["zone_id"],
        "level_cluster_id": zone.get("level_cluster_id") or visit.get("level_cluster_id"),
        "persistent_cluster_id": zone.get("persistent_cluster_id"),
        "zone_low": float(zone["zone_low"]),
        "zone_high": float(zone["zone_high"]),
        "zone_available_at": zone["zone_available_at"],
        "exchange_event_time": format_utc_z(touch_ts),
        "event_available_at": format_utc_z(avail),
        "trigger_record_id": trigger_id or f"visit:{visit['episode_id']}",
        "trigger_price": trigger_price,
        "approach_side": visit.get("approach_side"),
        "visit_status": visit.get("status"),
        # visit_count may be present on the visit row for diagnostics only — never a selector.
        "visit_count_diagnostic_only": visit.get("visit_count"),
        "used_research_visit_count_as_selector": False,
    }


def detect_all_visits_for_zones(
    *,
    zones: list[dict[str, Any]],
    trade_events: list[dict[str, Any]],
    candles_1m: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    mid_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Detect ALL chronological visits for each zone. No visit_count filter."""
    mid_events = mid_events or []
    candles = candles_normalized(candles_1m)
    trades = trades_as_events(trade_events)
    all_visits: list[dict[str, Any]] = []
    for zone in zones:
        # Causal: visits only after zone available
        start = max(window_start, parse_utc(zone["zone_available_at"]))
        if start >= window_end:
            continue
        cluster = {
            "cluster_price_low": float(zone["cluster_price_low"]),
            "cluster_price_high": float(zone["cluster_price_high"]),
            "level_cluster_id": zone.get("persistent_cluster_id") or zone["zone_id"],
        }
        visits = detect_visits_for_cluster(
            cluster=cluster,
            trade_events=trades,
            mid_events=mid_events,
            candles_1m=candles,
            window_start=start,
            window_end=window_end,
        )
        for v in visits:
            # Ensure episode_id uses persistent identity when available
            pid = zone.get("persistent_cluster_id") or zone["zone_id"]
            touch = parse_utc(v["first_touch_ts"])
            v = dict(v)
            v["episode_id"] = episode_id(str(pid), touch)
            v["zone_id"] = zone["zone_id"]
            v["persistent_cluster_id"] = pid
            v["zone_available_at"] = zone["zone_available_at"]
            v["zone_low"] = float(zone["zone_low"])
            v["zone_high"] = float(zone["zone_high"])
            v["tpo_type"] = zone.get("tpo_type")
            all_visits.append(v)
    all_visits.sort(key=lambda v: parse_utc(v["first_touch_ts"]))
    return all_visits
