"""Independent oracle for Episode-1 zone/touch/detection derivation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1 import REACTION_HORIZON_S
from ..bounded_level_first_analyzer_pilot_v1.bins import point_in_zone
from ..bounded_level_first_analyzer_pilot_v1.episodes import can_open_new_visit, candle_complete_at, episode_id, parse_utc
from ..timeparse import format_utc_z
from .independent_derivation import (
    INPUT_FREEZE_DIR,
    TARGET_WALL_PRICE,
    TARGET_WALL_SIDE,
    load_frozen_candles,
    load_frozen_trades,
    load_zone_from_mp_events,
)


def _side_of_zone(price: float, zone_low: float, zone_high: float) -> str:
    if price < float(zone_low):
        return "BELOW"
    if price > float(zone_high):
        return "ABOVE"
    return "INSIDE"


def _completed_candles_after(
    candles: list[dict[str, Any]],
    *,
    touch: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    horizon_end = min(touch + timedelta(seconds=REACTION_HORIZON_S), window_end)
    out = []
    for candle in candles:
        open_ts = parse_utc(candle["open_time"])
        close_ts = candle_complete_at(open_ts)
        if close_ts <= touch or close_ts > horizon_end:
            continue
        row = dict(candle)
        row["close_ts"] = close_ts
        out.append(row)
    out.sort(key=lambda row: row["close_ts"])
    return out


def scan_zone_visits(
    *,
    zone: dict[str, Any],
    trades: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    window_end: datetime,
) -> list[dict[str, Any]]:
    zone_low = float(zone["zone_low"])
    zone_high = float(zone["zone_high"])
    zone_available_at = parse_utc(zone["zone_available_at"])
    marks = []
    for trade in trades:
        ts = parse_utc(trade["trade_ts"])
        if zone_available_at <= ts < window_end:
            marks.append((ts, float(trade["price"]), str(trade.get("trade_id") or ""), trade))
    marks.sort(key=lambda item: (item[0], item[1], item[2]))

    visits: list[dict[str, Any]] = []
    open_visit: dict[str, Any] | None = None
    last_exit: datetime | None = None
    last_price: float | None = None
    last_outside_side: str | None = None

    def close_open(ts: datetime, direction: str) -> None:
        nonlocal open_visit, last_exit, last_outside_side
        if open_visit is None:
            return
        open_visit["first_exit_ts"] = format_utc_z(ts)
        open_visit["first_exit_direction"] = direction
        open_visit["episode_close_ts"] = format_utc_z(ts)
        open_visit["status"] = "LEFT_LEVEL_ZONE_UP" if direction == "UP" else "LEFT_LEVEL_ZONE_DOWN"
        visits.append(open_visit)
        open_visit = None
        last_exit = ts
        last_outside_side = "ABOVE" if direction == "UP" else "BELOW"

    for ts, price, _, trade in marks:
        inside = point_in_zone(price, zone_low, zone_high)
        if open_visit is None:
            if inside and can_open_new_visit(
                last_exit_ts=last_exit,
                candles_1m=candles,
                zone_low=zone_low,
                zone_high=zone_high,
                candidate_ts=ts,
            ):
                approach_side = last_outside_side or (
                    "BELOW" if last_price is not None and last_price < zone_low else
                    "ABOVE" if last_price is not None and last_price > zone_high else
                    "INSIDE_OR_UNKNOWN"
                )
                open_visit = {
                    "episode_id": episode_id(str(zone["persistent_cluster_id"]), ts),
                    "first_touch_ts": format_utc_z(ts),
                    "first_trade_in_zone_ts": format_utc_z(ts),
                    "approach_side": approach_side,
                    "first_exit_ts": None,
                    "first_exit_direction": None,
                    "first_return_ts": None,
                    "episode_close_ts": None,
                    "visit_count": 1,
                    "status": "FIRST_TOUCH",
                    "trigger_trade_id": trade.get("trade_id"),
                }
            elif not inside:
                last_outside_side = _side_of_zone(price, zone_low, zone_high)
        else:
            if inside:
                open_visit["status"] = "INSIDE_LEVEL_ZONE"
            else:
                close_open(ts, "UP" if price > zone_high else "DOWN")
        last_price = price

    if open_visit is not None:
        open_visit["status"] = "INSIDE_LEVEL_ZONE"
        open_visit["quality_status"] = "OPEN_AT_WINDOW_END"
        visits.append(open_visit)

    for idx in range(1, len(visits)):
        visits[idx]["visit_count"] = int(visits[idx - 1].get("visit_count") or 1) + 1
        visits[idx]["first_return_ts"] = visits[idx]["first_touch_ts"]
        visits[idx]["status"] = "RETURNED_TO_LEVEL"
    return visits


def oracle_zone_first_touch(
    *,
    zone: dict[str, Any],
    trades: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    window_end: datetime,
    target_episode_id: str | None = None,
) -> dict[str, Any] | None:
    visits = scan_zone_visits(zone=zone, trades=trades, candles=candles, window_end=window_end)
    if not visits:
        return None
    visit = visits[-1]
    if target_episode_id:
        for candidate in visits:
            if candidate.get("episode_id") == target_episode_id:
                visit = candidate
                break

    touch_ts = parse_utc(visit["first_touch_ts"])
    trigger = None
    at_touch = [trade for trade in trades if parse_utc(trade["trade_ts"]) == touch_ts]
    at_touch.sort(key=lambda row: (float(row["price"]), str(row.get("trade_id") or "")))
    for row in at_touch:
        if point_in_zone(float(row["price"]), float(zone["zone_low"]), float(zone["zone_high"])):
            trigger = row
            break
    if trigger is None and at_touch:
        trigger = at_touch[0]
    recv = None if trigger is None else trigger.get("collector_received_at")
    recv_dt = parse_utc(recv) if recv else None
    event_available_at = touch_ts if recv_dt is None else max(touch_ts, recv_dt)
    return {
        "event_type": "ZONE_FIRST_TOUCH",
        "episode_id": visit["episode_id"],
        "exchange_event_time": format_utc_z(touch_ts),
        "collector_received_at": recv,
        "event_available_at": format_utc_z(event_available_at),
        "trigger_record_id": None if trigger is None else trigger.get("trade_id"),
        "trigger_price": None if trigger is None else trigger.get("price"),
        "trigger_side": None if trigger is None else trigger.get("taker_side"),
        "visit_count": visit.get("visit_count"),
        "approach_side": visit.get("approach_side"),
        "first_exit_direction": visit.get("first_exit_direction"),
        "look_ahead": False,
        "receive_time_present": recv is not None,
    }


def accepted_above_detection_time(
    *,
    candles: list[dict[str, Any]],
    zone_high: float,
    zone_low: float,
    touch: datetime,
    window_end: datetime,
) -> datetime | None:
    causal = _completed_candles_after(candles, touch=touch, window_end=window_end)
    for idx in range(len(causal) - 1):
        first = causal[idx]
        second = causal[idx + 1]
        if float(first["close"]) <= zone_high or float(second["close"]) <= zone_high:
            continue
        both_lows_inside = point_in_zone(float(first["low"]), zone_low, zone_high) and point_in_zone(
            float(second["low"]), zone_low, zone_high
        )
        if not both_lows_inside:
            return second["close_ts"]
    return None


def oracle_detection(
    *,
    zone: dict[str, Any],
    zone_touch: dict[str, Any],
    candles: list[dict[str, Any]],
    window_end: datetime,
) -> dict[str, Any] | None:
    touch = parse_utc(zone_touch["exchange_event_time"])
    detected_at = accepted_above_detection_time(
        candles=candles,
        zone_high=float(zone["zone_high"]),
        zone_low=float(zone["zone_low"]),
        touch=touch,
        window_end=window_end,
    )
    if detected_at is None:
        return None
    return {
        "event_type": "DETECTION",
        "exchange_event_time": format_utc_z(detected_at),
        "event_available_at": format_utc_z(detected_at),
        "reaction_class": "ACCEPTED_ABOVE",
        "look_ahead": False,
    }


def oracle_wall_first_touch(
    *,
    wall_visible_at: str | None,
    trades: list[dict[str, Any]],
    wall_price: float = TARGET_WALL_PRICE,
    wall_side: str = TARGET_WALL_SIDE,
) -> dict[str, Any] | None:
    if not wall_visible_at:
        return None
    visible_dt = parse_utc(wall_visible_at)
    ordered = sorted(trades, key=lambda row: (parse_utc(row["trade_ts"]), str(row.get("trade_id") or "")))
    for row in ordered:
        ts = parse_utc(row["trade_ts"])
        if ts < visible_dt:
            continue
        price = float(row["price"])
        meets_rule = price >= wall_price if wall_side == "ask" else price <= wall_price
        if not meets_rule:
            continue
        recv = row.get("collector_received_at")
        recv_dt = parse_utc(recv) if recv else None
        event_available_at = ts if recv_dt is None else max(ts, recv_dt)
        return {
            "event_type": "WALL_FIRST_TOUCH",
            "exchange_event_time": format_utc_z(ts),
            "event_available_at": format_utc_z(event_available_at),
            "collector_received_at": recv,
            "trigger_record_id": row.get("trade_id"),
            "trigger_price": row.get("price"),
            "trigger_side": row.get("taker_side"),
            "look_ahead": False,
            "receive_time_present": recv is not None,
        }
    return None


def derive_oracle_events(
    *,
    prod: dict[str, Any] | None = None,
) -> dict[str, Any]:
    zone = load_zone_from_mp_events()
    trades_path = Path((prod or {}).get("input_paths", {}).get("trades") or (INPUT_FREEZE_DIR / "public_trades_zone_window.jsonl"))
    candles_path = Path((prod or {}).get("input_paths", {}).get("candles") or (INPUT_FREEZE_DIR / "candles_1m_zone_window.jsonl"))
    trades = load_frozen_trades(trades_path)
    candles = load_frozen_candles(candles_path)
    window_end = parse_utc((prod or {}).get("timing", {}).get("detection")) if (prod or {}).get("timing", {}).get("detection") else parse_utc(zone["zone_available_at"]) + timedelta(hours=2)
    zone_touch = oracle_zone_first_touch(
        zone=zone,
        trades=trades,
        candles=candles,
        window_end=window_end,
        target_episode_id=(prod or {}).get("episode_id"),
    )
    detection = None
    if zone_touch is not None:
        detection = oracle_detection(zone=zone, zone_touch=zone_touch, candles=candles, window_end=window_end)
    wall_obs = (prod or {}).get("wall_observation_at_zone_touch") or {}
    wall_touch = oracle_wall_first_touch(
        wall_visible_at=wall_obs.get("wall_visible_at"),
        trades=trades,
        wall_price=float(wall_obs.get("wall_price") or TARGET_WALL_PRICE),
        wall_side=str(wall_obs.get("wall_side") or TARGET_WALL_SIDE),
    )
    return {
        "zone_available": zone,
        "zone_first_touch": zone_touch,
        "detection": detection,
        "wall_first_touch": wall_touch,
    }


def _event_signature(event: dict[str, Any] | None, keys: list[str]) -> tuple[Any, ...] | None:
    if event is None:
        return None
    return tuple(event.get(key) for key in keys)


def _prod_detection_event(prod: dict[str, Any] | None) -> dict[str, Any] | None:
    if not prod:
        return None
    row = dict(prod)
    if row.get("reaction_class") is None:
        row["reaction_class"] = (row.get("reaction") or {}).get("reaction_class")
    return row


def _compare_event(
    *,
    prod: dict[str, Any] | None,
    oracle: dict[str, Any] | None,
    label: str,
    keys: list[str],
) -> tuple[int, int, list[dict[str, Any]]]:
    prod_sig = _event_signature(prod, keys)
    oracle_sig = _event_signature(oracle, keys)
    if prod_sig is None and oracle_sig is None:
        return 0, 0, []
    if prod_sig is None:
        return 0, 1, [{"event": label, "kind": "missing_in_production", "oracle": oracle}]
    if oracle_sig is None:
        return 1, 0, [{"event": label, "kind": "missing_in_oracle", "production": prod}]
    if prod_sig != oracle_sig:
        return 1, 1, [{"event": label, "kind": "mismatch", "production": prod, "oracle": oracle}]
    return 0, 0, []


def _look_ahead_violations(*events: dict[str, Any] | None) -> list[dict[str, Any]]:
    violations = []
    for event in events:
        if not event:
            continue
        et = event.get("exchange_event_time")
        avail = event.get("event_available_at")
        recv = event.get("collector_received_at")
        if et and avail and parse_utc(avail) < parse_utc(et):
            violations.append({"event_type": event.get("event_type"), "kind": "available_before_event_time"})
        if recv and avail and parse_utc(avail) < parse_utc(recv):
            violations.append({"event_type": event.get("event_type"), "kind": "available_before_receive_time"})
    return violations


def compare_independent_payload(prod: dict[str, Any]) -> dict[str, Any]:
    oracle = derive_oracle_events(prod=prod)
    fp_touch, fn_touch, details_touch = _compare_event(
        prod=prod.get("zone_first_touch"),
        oracle=oracle.get("zone_first_touch"),
        label="zone_first_touch",
        keys=["episode_id", "exchange_event_time", "event_available_at", "trigger_record_id"],
    )
    fp_det, fn_det, details_det = _compare_event(
        prod=_prod_detection_event(prod.get("detection")),
        oracle=oracle.get("detection"),
        label="detection",
        keys=["exchange_event_time", "event_available_at", "reaction_class"],
    )
    fp_wall, fn_wall, details_wall = _compare_event(
        prod=prod.get("wall_first_touch"),
        oracle=oracle.get("wall_first_touch"),
        label="wall_first_touch",
        keys=["exchange_event_time", "event_available_at", "trigger_record_id"],
    )
    look_ahead = _look_ahead_violations(prod.get("zone_first_touch"), prod.get("detection"), prod.get("wall_first_touch"))
    return {
        "derived_false_positives": fp_touch + fp_det + fp_wall,
        "derived_false_negatives": fn_touch + fn_det + fn_wall,
        "look_ahead_violations": len(look_ahead),
        "look_ahead_examples": look_ahead[:10],
        "details": details_touch + details_det + details_wall,
        "oracle": oracle,
    }


def audit_directory(directory: Path) -> dict[str, Any]:
    path = Path(directory) / "independent_derivation.json"
    if not path.is_file():
        return {
            "derived_false_positives": 1,
            "derived_false_negatives": 1,
            "look_ahead_violations": 0,
            "details": [{"kind": "missing_independent_derivation_json", "path": str(path)}],
        }
    prod = json.loads(path.read_text(encoding="utf-8"))
    result = compare_independent_payload(prod)
    result["path"] = str(path)
    return result
