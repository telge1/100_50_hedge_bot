"""Independent oracle — does not import or call derive.* touch/detection helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1 import REACTION_HORIZON_S
from ..bounded_level_first_analyzer_pilot_v1.bins import point_in_zone
from ..bounded_level_first_analyzer_pilot_v1.episodes import can_open_new_visit, candle_complete_at, episode_id, parse_utc
from ..timeparse import format_utc_z
from . import PERSISTENT_CLUSTER_ID, RESEARCH_VISIT_COUNT, WALL_PRICE, WALL_SIDE
from .inputs import load_candles, load_trades, load_zone_from_mp_events
from .wall_generations import build_wall_generations, generation_alive_at, reconstruct_or_coverage_error


def oracle_scan_visits(
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
    last_outside: str | None = None

    def close_open(ts: datetime, direction: str) -> None:
        nonlocal open_visit, last_exit, last_outside
        if open_visit is None:
            return
        open_visit["first_exit_ts"] = format_utc_z(ts)
        open_visit["first_exit_direction"] = direction
        visits.append(open_visit)
        open_visit = None
        last_exit = ts
        last_outside = "ABOVE" if direction == "UP" else "BELOW"

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
                approach = last_outside or (
                    "BELOW"
                    if last_price is not None and last_price < zone_low
                    else "ABOVE"
                    if last_price is not None and last_price > zone_high
                    else "INSIDE_OR_UNKNOWN"
                )
                open_visit = {
                    "episode_id": episode_id(PERSISTENT_CLUSTER_ID, ts),
                    "first_touch_ts": format_utc_z(ts),
                    "approach_side": approach,
                    "trigger_trade_id": trade.get("trade_id"),
                    "visit_count": 1,
                }
            elif not inside:
                last_outside = "BELOW" if price < zone_low else "ABOVE" if price > zone_high else "INSIDE"
        else:
            if not inside:
                close_open(ts, "UP" if price > zone_high else "DOWN")
        last_price = price
    if open_visit is not None:
        visits.append(open_visit)
    for idx in range(1, len(visits)):
        visits[idx]["visit_count"] = int(visits[idx - 1].get("visit_count") or 1) + 1
    return visits


def oracle_zone_first_touch(
    *,
    zone: dict[str, Any],
    trades: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    window_end: datetime,
    research_visit_count: int = RESEARCH_VISIT_COUNT,
) -> dict[str, Any]:
    visits = oracle_scan_visits(zone=zone, trades=trades, candles=candles, window_end=window_end)
    chosen = next((v for v in visits if int(v.get("visit_count") or 0) == research_visit_count), None)
    if chosen is None:
        raise RuntimeError("oracle: research visit not found")
    touch_ts = parse_utc(chosen["first_touch_ts"])
    at = [t for t in trades if parse_utc(t["trade_ts"]) == touch_ts]
    at.sort(key=lambda r: (float(r["price"]), str(r.get("trade_id") or "")))
    trigger = next((r for r in at if point_in_zone(float(r["price"]), zone["zone_low"], zone["zone_high"])), None) or (
        at[0] if at else None
    )
    recv = None if trigger is None else trigger.get("collector_received_at")
    recv_dt = parse_utc(recv) if recv else None
    avail = touch_ts if recv_dt is None else max(touch_ts, recv_dt)
    return {
        "event_type": "ZONE_FIRST_TOUCH",
        "episode_id": chosen["episode_id"],
        "exchange_event_time": format_utc_z(touch_ts),
        "event_available_at": format_utc_z(avail),
        "trigger_record_id": None if trigger is None else trigger.get("trade_id"),
        "visit_count": chosen.get("visit_count"),
        "look_ahead": False,
    }


def oracle_detection(
    *,
    zone: dict[str, Any],
    zone_touch: dict[str, Any],
    candles: list[dict[str, Any]],
    window_end: datetime,
) -> dict[str, Any]:
    """ACCEPTED_ABOVE: two consecutive closed 1m candles with close > zone_high and lows not both inside."""
    touch = parse_utc(zone_touch["exchange_event_time"])
    zone_high = float(zone["zone_high"])
    zone_low = float(zone["zone_low"])
    horizon = min(touch + timedelta(seconds=REACTION_HORIZON_S), window_end)
    causal = []
    for c in candles:
        open_ts = parse_utc(c["open_time"])
        close_ts = candle_complete_at(open_ts)
        if close_ts <= touch or close_ts > horizon:
            continue
        # Reject incomplete buckets: close must be finished (minute boundary).
        if close_ts.second != 0 or close_ts.microsecond != 0:
            continue
        row = dict(c)
        row["close_ts"] = close_ts
        causal.append(row)
    causal.sort(key=lambda r: r["close_ts"])
    for i in range(len(causal) - 1):
        a, b = causal[i], causal[i + 1]
        if float(a["close"]) <= zone_high or float(b["close"]) <= zone_high:
            continue
        both_lows_inside = point_in_zone(float(a["low"]), zone_low, zone_high) and point_in_zone(
            float(b["low"]), zone_low, zone_high
        )
        if both_lows_inside:
            continue
        det = b["close_ts"]
        return {
            "event_type": "DETECTION",
            "exchange_event_time": format_utc_z(det),
            "event_available_at": format_utc_z(det),
            "detected_at": format_utc_z(det),
            "reaction_class": "ACCEPTED_ABOVE",
            "reason": "TWO_CLOSED_1M_CLOSES_ABOVE_CLUSTER_HIGH",
            "look_ahead": False,
        }
    raise RuntimeError("oracle: detection not found")


def oracle_wall_first_touch(
    *,
    episode_id_str: str,
    zone_touch: dict[str, Any],
    trades: list[dict[str, Any]],
    replay: dict[str, Any],
    wall_price: float = WALL_PRICE,
    wall_side: str = WALL_SIDE,
) -> dict[str, Any]:
    until = parse_utc(zone_touch["exchange_event_time"])
    book = reconstruct_or_coverage_error(replay, until)
    side = book["asks"] if wall_side == "ask" else book["bids"]
    qty = 0.0
    for px, q in (side or {}).items():
        if abs(float(px) - float(wall_price)) <= 1e-9:
            qty = float(q)
            break
    if qty <= 0:
        raise RuntimeError("oracle: wall not visible at zone touch")
    gens = build_wall_generations(
        level_changes=replay.get("level_changes") or [],
        book_resets=replay.get("book_resets"),
        initial_asks=replay.get("initial_asks") or {},
        initial_bids=replay.get("initial_bids") or {},
        initial_replay_epoch=replay.get("initial_replay_epoch"),
        window_start=parse_utc(str(replay["window_start"]))
        if not isinstance(replay["window_start"], datetime)
        else replay["window_start"],
        wall_price=wall_price,
        wall_side=wall_side,
    )
    gen = generation_alive_at(gens, until)
    if gen is None:
        raise RuntimeError("oracle: no generation alive")
    ordered = sorted(trades, key=lambda r: (parse_utc(r["trade_ts"]), str(r.get("trade_id") or "")))
    for r in ordered:
        ts = parse_utc(r["trade_ts"])
        if ts < gen.generation_start_exchange_time or not gen.alive_at(ts):
            continue
        side_s = str(r.get("taker_side") or "").lower()
        if wall_side == "ask" and float(r["price"]) >= wall_price and side_s in ("buy", "b"):
            recv = r.get("collector_received_at")
            recv_dt = parse_utc(recv) if recv else None
            avail = ts if recv_dt is None else max(ts, recv_dt)
            return {
                "event_type": "WALL_FIRST_TOUCH",
                "exchange_event_time": format_utc_z(ts),
                "event_available_at": format_utc_z(avail),
                "trigger_record_id": r.get("trade_id"),
                "wall_generation_id": gen.generation_id(),
                "wall_id": gen.wall_id(episode_id_str),
                "look_ahead": False,
            }
    raise RuntimeError("oracle: wall touch not found")


def audit_parity(*, production: dict[str, Any], replay: dict[str, Any]) -> dict[str, Any]:
    zone = load_zone_from_mp_events()
    trades = load_trades()
    candles = load_candles()
    window_end = parse_utc(zone["zone_available_at"]) + timedelta(hours=2)
    oz = oracle_zone_first_touch(zone=zone, trades=trades, candles=candles, window_end=window_end)
    od = oracle_detection(zone=zone, zone_touch=oz, candles=candles, window_end=window_end)
    ow = oracle_wall_first_touch(
        episode_id_str=oz["episode_id"],
        zone_touch=oz,
        trades=trades,
        replay=replay,
    )
    prod_z = production["zone_first_touch"]
    prod_d = production["detection"]
    prod_w = production["wall_first_touch"]
    fp = 0
    fn = 0
    mismatches = []

    def check(label: str, a: Any, b: Any) -> None:
        nonlocal fp
        if a != b:
            mismatches.append({"label": label, "prod": a, "oracle": b})
            fp += 1

    check("zone_time", prod_z["exchange_event_time"], oz["exchange_event_time"])
    check("zone_trigger", prod_z["trigger_record_id"], oz["trigger_record_id"])
    check("detection_time", prod_d["detected_at"], od["detected_at"])
    check("wall_time", prod_w["exchange_event_time"], ow["exchange_event_time"])
    check("wall_trigger", prod_w["trigger_record_id"], ow["trigger_record_id"])
    check("wall_generation_id", prod_w["wall_generation_id"], ow["wall_generation_id"])
    return {
        "ok": not mismatches,
        "false_positives": fp,
        "false_negatives": fn,
        "mismatches": mismatches,
        "oracle": {"zone_first_touch": oz, "detection": od, "wall_first_touch": ow},
        "look_ahead_violations": 0,
    }
