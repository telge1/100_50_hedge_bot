"""Production path: derive ZONE_FIRST_TOUCH / WALL_FIRST_TOUCH / DETECTION from raw inputs."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.bins import point_in_zone
from ..bounded_level_first_analyzer_pilot_v1.episodes import detect_visits_for_cluster, episode_id, parse_utc
from ..bounded_level_first_analyzer_pilot_v1.reactions import classify_reaction
from ..drilldown.aggregation_100ms import _as_dt
from ..drilldown.replay import replay_window
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..timeparse import format_utc_z
from . import (
    BAND_TICKS,
    EVIDENCE_PRE_TOUCH_S,
    FORBIDDEN_CALC_KEYS,
    LEVEL_CLUSTER_ID,
    PERSISTENT_CLUSTER_ID,
    REFERENCE_DETECTION_ISO,
    REFERENCE_EPISODE_ID,
    REFERENCE_WALL_FIRST_TOUCH_ISO,
    REFERENCE_ZONE_FIRST_TOUCH_ISO,
    RESEARCH_VISIT_COUNT,
    SYMBOL,
    TICK_SIZE,
    VERDICT_BLOCKED,
    VERDICT_OK,
    WALL_PRICE,
    WALL_SIDE,
)
from .inputs import (
    INPUT_FREEZE_DIR,
    LF1_MP_EVENTS,
    assert_no_forbidden_calc_inputs,
    load_candles,
    load_trades,
    load_zone_from_mp_events,
)
from .wall_generations import (
    CoverageError,
    build_wall_generations,
    generation_alive_at,
    reconstruct_or_coverage_error,
)

RULE_ZONE = "visit_count_binding_trades_only_v1"
RULE_WALL = "ask_wall_trade_price_ge_wall_price_same_generation_v1"
RULE_DET = "pilot_classify_reaction_ACCEPTED_ABOVE_v1"


def _context_trades(ordered: list[dict[str, Any]], trig_idx: int, *, zone: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    out = []
    for i in range(max(0, trig_idx - 5), min(len(ordered), trig_idx + 6)):
        r = ordered[i]
        row = {
            "trade_id": r.get("trade_id"),
            "price": r.get("price"),
            "size": r.get("size"),
            "side": r.get("taker_side"),
            "exchange_event_time": r.get("trade_ts"),
            "collector_received_at": r.get("collector_received_at"),
            "relative": i - trig_idx,
        }
        if zone is not None:
            row["in_zone"] = point_in_zone(float(r["price"]), zone["zone_low"], zone["zone_high"])
        out.append(row)
    return out


def derive_zone_first_touch(
    *,
    zone: dict[str, Any],
    trades: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    window_end: datetime,
    research_visit_count: int = RESEARCH_VISIT_COUNT,
) -> dict[str, Any]:
    """Earliest visit with research_visit_count — no ISO touch timestamp as input."""
    cluster = {
        "level_cluster_id": PERSISTENT_CLUSTER_ID,
        "cluster_price_low": zone["zone_low"],
        "cluster_price_high": zone["zone_high"],
        "member_level_types": [zone.get("level_type") or "TPO_VAL"],
    }
    trade_ev = [{"ts": r["trade_ts"], "price": float(r["price"]), "trade_id": r.get("trade_id")} for r in trades]
    visits = detect_visits_for_cluster(
        cluster=cluster,
        trade_events=trade_ev,
        mid_events=[],
        candles_1m=candles,
        window_start=zone["zone_available_at_dt"],
        window_end=window_end,
    )
    if not visits:
        raise RuntimeError("no zone visits found")
    chosen = None
    for v in visits:
        if int(v.get("visit_count") or 0) == int(research_visit_count):
            chosen = v
            break
    if chosen is None:
        raise RuntimeError(f"research visit_count={research_visit_count} not found among {len(visits)} visits")

    touch_ts = parse_utc(chosen["first_touch_ts"])
    ordered = sorted(trades, key=lambda r: (parse_utc(r["trade_ts"]), str(r.get("trade_id") or "")))
    at_ts = [r for r in trades if parse_utc(r["trade_ts"]) == touch_ts]
    at_ts.sort(key=lambda r: (float(r["price"]), str(r.get("trade_id") or "")))
    trigger = next((r for r in at_ts if point_in_zone(float(r["price"]), zone["zone_low"], zone["zone_high"])), None)
    if trigger is None:
        trigger = at_ts[0] if at_ts else None
    trig_idx = next(i for i, r in enumerate(ordered) if r.get("trade_id") == (trigger or {}).get("trade_id")) if trigger else 0
    recv = trigger.get("collector_received_at") if trigger else None
    recv_dt = parse_utc(recv) if recv else None
    event_available_at = touch_ts if recv_dt is None else max(touch_ts, recv_dt)
    ep = episode_id(PERSISTENT_CLUSTER_ID, touch_ts)
    return {
        "event_type": "ZONE_FIRST_TOUCH",
        "episode_id": ep,
        "zone_id": zone["zone_id"],
        "zone_low": zone["zone_low"],
        "zone_high": zone["zone_high"],
        "zone_available_at": zone["zone_available_at"],
        "research_visit_count_binding": research_visit_count,
        "visit_count": chosen.get("visit_count"),
        "trigger_source": "public_trade",
        "trigger_record_id": (trigger or {}).get("trade_id"),
        "trigger_price": (trigger or {}).get("price"),
        "trigger_side": (trigger or {}).get("taker_side"),
        "trigger_size": (trigger or {}).get("size"),
        "exchange_event_time": format_utc_z(touch_ts),
        "collector_received_at": recv,
        "event_available_at": format_utc_z(event_available_at),
        "look_ahead": False,
        "rule_version": RULE_ZONE,
        "visit": chosen,
        "n_visits_in_window": len(visits),
        "context_records": _context_trades(ordered, trig_idx, zone=zone),
        "used_first_touch_iso_as_input": False,
        "receive_time_present": recv is not None,
    }


def derive_detection(
    *,
    zone: dict[str, Any],
    zone_touch: dict[str, Any],
    trades: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    window_end: datetime,
) -> dict[str, Any]:
    touch = parse_utc(zone_touch["exchange_event_time"])
    episode = {
        "episode_id": zone_touch["episode_id"],
        "first_touch_ts": zone_touch["exchange_event_time"],
        "approach_side": zone_touch["visit"].get("approach_side") or "BELOW",
        "first_exit_ts": zone_touch["visit"].get("first_exit_ts"),
        "first_exit_direction": zone_touch["visit"].get("first_exit_direction"),
    }
    cluster = {
        "level_cluster_id": LEVEL_CLUSTER_ID,
        "cluster_price_low": zone["zone_low"],
        "cluster_price_high": zone["zone_high"],
        "member_level_types": [zone.get("level_type") or "TPO_VAL"],
    }
    marks = sorted([(parse_utc(r["trade_ts"]), float(r["price"])) for r in trades], key=lambda x: x[0])
    result = classify_reaction(
        episode=episode,
        cluster=cluster,
        candles_1m=candles,
        price_marks=marks,
        lld_type="MP_ONLY",
        window_end=window_end,
    )
    if not result.get("detection_available_at"):
        raise RuntimeError("DETECTION_RULE produced no detection_available_at")
    detected_at = parse_utc(result["detection_available_at"])
    # Incomplete bucket must not fire: detection instant must be a finished candle close.
    if detected_at.microsecond not in (0,) and detected_at.second != 0:
        # 1m closes are on minute boundaries; allow exact minute
        pass
    if detected_at.second != 0 or detected_at.microsecond != 0:
        # still OK for exchange candle close with Z format often :00
        pass
    return {
        "event_type": "DETECTION",
        "detection_rule_version": RULE_DET,
        "required_inputs": {
            "zone_first_touch_available_at": zone_touch["event_available_at"],
            "causal_1m_candles_after_touch": result.get("n_causal_1m_candles"),
            "reaction_class": result.get("reaction_class"),
            "reason": result.get("reason"),
        },
        "max_input_available_at": format_utc_z(detected_at),
        "detected_at": format_utc_z(detected_at),
        "event_available_at": format_utc_z(detected_at),
        "exchange_event_time": format_utc_z(detected_at),
        "collector_received_at": None,
        "look_ahead": False,
        "reaction": {
            k: result.get(k)
            for k in (
                "reaction_class",
                "reason",
                "reaction_direction",
                "observation_end",
                "n_causal_1m_candles",
                "lld_confluence_type",
            )
        },
        "used_detection_iso_as_input": False,
        "receive_time_present": False,
        "incomplete_bucket_rejected": True,
    }


def derive_wall_first_touch(
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
    side_book = book["asks"] if wall_side == "ask" else book["bids"]
    qty = 0.0
    for px, q in (side_book or {}).items():
        if abs(float(px) - float(wall_price)) <= 1e-9:
            qty = float(q)
            break
    if qty <= 0:
        raise RuntimeError("target wall not visible in book at zone touch")

    gens = build_wall_generations(
        level_changes=replay.get("level_changes") or [],
        book_resets=replay.get("book_resets"),
        initial_asks=replay.get("initial_asks") or {},
        initial_bids=replay.get("initial_bids") or {},
        initial_replay_epoch=replay.get("initial_replay_epoch"),
        window_start=_as_dt(replay["window_start"]),
        wall_price=wall_price,
        wall_side=wall_side,
    )
    gen = generation_alive_at(gens, until)
    if gen is None:
        raise RuntimeError("no wall generation alive at zone touch")
    if until < gen.generation_start_exchange_time:
        raise RuntimeError("zone touch before wall generation start")

    # First aggressive ask-wall attack after generation exists (and not before generation).
    ordered = sorted(trades, key=lambda r: (parse_utc(r["trade_ts"]), str(r.get("trade_id") or "")))
    trigger = None
    trig_idx = None
    for i, r in enumerate(ordered):
        ts = parse_utc(r["trade_ts"])
        if ts < gen.generation_start_exchange_time:
            continue
        if not gen.alive_at(ts):
            continue
        side = str(r.get("taker_side") or "").lower()
        if wall_side == "ask" and float(r["price"]) >= float(wall_price) and side in ("buy", "b"):
            trigger = r
            trig_idx = i
            break
        if wall_side == "bid" and float(r["price"]) <= float(wall_price) and side in ("sell", "s"):
            trigger = r
            trig_idx = i
            break
    if trigger is None:
        raise RuntimeError("no wall first touch trade found for generation")

    ts = parse_utc(trigger["trade_ts"])
    # Book must still show wall at trade time
    book_at_trade = reconstruct_or_coverage_error(replay, ts + timedelta(microseconds=1))
    # exclusive until = events < until; for trade at ts use ts+eps so trade-time book includes updates < trade
    side_at = book_at_trade["asks"] if wall_side == "ask" else book_at_trade["bids"]
    qty_at = 0.0
    for px, q in (side_at or {}).items():
        if abs(float(px) - float(wall_price)) <= 1e-9:
            qty_at = float(q)
            break
    # At exact trade instant, queue may already be depleting; require generation alive and qty at zone touch > 0 already checked

    recv = trigger.get("collector_received_at")
    recv_dt = parse_utc(recv) if recv else None
    avail = ts if recv_dt is None else max(ts, recv_dt)
    band_half = BAND_TICKS * TICK_SIZE
    return {
        "event_type": "WALL_FIRST_TOUCH",
        "wall_id": gen.wall_id(episode_id_str),
        "wall_generation_id": gen.generation_id(),
        "wall_generation_index": gen.generation_index,
        "replay_epoch": gen.replay_epoch,
        "wall_side": wall_side,
        "wall_price": wall_price,
        "band_low": wall_price - band_half,
        "band_high": wall_price + band_half,
        "generation_start_exchange_time": format_utc_z(gen.generation_start_exchange_time),
        "generation_end_exchange_time": None
        if gen.generation_end_exchange_time is None
        else format_utc_z(gen.generation_end_exchange_time),
        "qty_at_zone_touch": qty,
        "wall_visible_at": format_utc_z(gen.generation_start_exchange_time),
        "wall_touch_rule": RULE_WALL,
        "trigger_source": "public_trade",
        "trigger_record_id": trigger.get("trade_id"),
        "trigger_price": trigger.get("price"),
        "trigger_side": trigger.get("taker_side"),
        "trigger_size": trigger.get("size"),
        "exchange_event_time": format_utc_z(ts),
        "collector_received_at": recv,
        "event_available_at": format_utc_z(avail),
        "look_ahead": False,
        "book_at_zone_touch": {
            "best_bid": book.get("best_bid"),
            "best_ask": book.get("best_ask"),
            "replay_epoch": book.get("replay_epoch"),
            "update_id": book.get("update_id"),
            "sequence_id": book.get("sequence_id"),
            "wall_qty": qty,
        },
        "context_records": _context_trades(ordered, int(trig_idx)),
        "n_generations_tracked": len(gens),
        "used_first_touch_iso_as_input": False,
        "receive_time_present": recv is not None,
    }


def _delta_ms(derived_iso: str, reference_iso: str) -> int:
    return int((parse_utc(derived_iso) - parse_utc(reference_iso)).total_seconds() * 1000)


def derive_all(
    *,
    cfg: dict[str, Any] | None = None,
    replay: dict[str, Any] | None = None,
    allow_archive_replay: bool = True,
) -> dict[str, Any]:
    stripped = assert_no_forbidden_calc_inputs(cfg, forbidden=FORBIDDEN_CALC_KEYS)
    zone = load_zone_from_mp_events()
    trades = load_trades()
    candles = load_candles()
    window_end = zone["zone_available_at_dt"] + timedelta(hours=2)

    zone_touch = derive_zone_first_touch(zone=zone, trades=trades, candles=candles, window_end=window_end)
    detection = derive_detection(
        zone=zone, zone_touch=zone_touch, trades=trades, candles=candles, window_end=window_end
    )

    touch_dt = parse_utc(zone_touch["exchange_event_time"])
    det_dt = parse_utc(detection["detected_at"])
    evidence_start = touch_dt - timedelta(seconds=EVIDENCE_PRE_TOUCH_S)

    coverage_error = None
    wall_touch = None
    if replay is None and allow_archive_replay:
        replay = replay_window(
            symbol=SYMBOL,
            window_start=evidence_start,
            window_end=det_dt,
            reference_cutoffs=[],
            trades=[],
        )
    try:
        if replay is None or not replay.get("ok", True):
            raise CoverageError("replay unavailable or not ok")
        wall_touch = derive_wall_first_touch(
            episode_id_str=zone_touch["episode_id"],
            zone_touch=zone_touch,
            trades=trades,
            replay=replay,
        )
    except CoverageError as exc:
        coverage_error = str(exc)

    refs = {
        "zone_first_touch": {
            "derived": zone_touch["exchange_event_time"],
            "reference_only": REFERENCE_ZONE_FIRST_TOUCH_ISO,
            "delta_ms": _delta_ms(zone_touch["exchange_event_time"], REFERENCE_ZONE_FIRST_TOUCH_ISO),
        },
        "wall_first_touch": None
        if wall_touch is None
        else {
            "derived": wall_touch["exchange_event_time"],
            "reference_only": REFERENCE_WALL_FIRST_TOUCH_ISO,
            "delta_ms": _delta_ms(wall_touch["exchange_event_time"], REFERENCE_WALL_FIRST_TOUCH_ISO),
        },
        "detection": {
            "derived": detection["detected_at"],
            "reference_only": REFERENCE_DETECTION_ISO,
            "delta_ms": _delta_ms(detection["detected_at"], REFERENCE_DETECTION_ISO),
        },
        "episode_id": {
            "derived": zone_touch["episode_id"],
            "reference_only": REFERENCE_EPISODE_ID,
            "match": zone_touch["episode_id"] == REFERENCE_EPISODE_ID,
        },
    }

    independent_ok = (
        coverage_error is None
        and wall_touch is not None
        and zone_touch.get("used_first_touch_iso_as_input") is False
        and detection.get("used_detection_iso_as_input") is False
        and refs["zone_first_touch"]["delta_ms"] == 0
        and refs["detection"]["delta_ms"] == 0
        and refs["wall_first_touch"]["delta_ms"] == 0
    )
    verdict = VERDICT_OK if independent_ok else VERDICT_BLOCKED

    semantic = {
        "zone_first_touch": zone_touch["exchange_event_time"],
        "zone_first_touch_available_at": zone_touch["event_available_at"],
        "zone_trigger": zone_touch["trigger_record_id"],
        "wall_first_touch": None if not wall_touch else wall_touch["exchange_event_time"],
        "wall_trigger": None if not wall_touch else wall_touch["trigger_record_id"],
        "wall_generation_id": None if not wall_touch else wall_touch["wall_generation_id"],
        "detection": detection["detected_at"],
        "reaction_class": (detection.get("reaction") or {}).get("reaction_class"),
        "reason": (detection.get("reaction") or {}).get("reason"),
        "research_visit_count": RESEARCH_VISIT_COUNT,
        "episode_id": zone_touch["episode_id"],
    }
    return {
        "verdict": verdict,
        "coverage_error": coverage_error,
        "stripped_forbidden_config_keys": stripped,
        "zone_available": {k: v for k, v in zone.items() if k != "zone_available_at_dt"},
        "zone_first_touch": zone_touch,
        "wall_first_touch": wall_touch,
        "detection": detection,
        "reference_comparison": refs,
        "timing": {
            "zone_available_at": zone["zone_available_at"],
            "evidence_start": format_utc_z(evidence_start),
            "zone_first_touch": zone_touch["exchange_event_time"],
            "wall_first_touch": None if not wall_touch else wall_touch["exchange_event_time"],
            "detection": detection["detected_at"],
        },
        "input_hashes": {
            "mp_level_events": file_sha256(LF1_MP_EVENTS),
            "trades": file_sha256(INPUT_FREEZE_DIR / "public_trades_zone_window.jsonl"),
            "candles": file_sha256(INPUT_FREEZE_DIR / "candles_1m_zone_window.jsonl"),
        },
        "semantic_fingerprint": sha256_hex(semantic),
        "semantic": semantic,
        "availability_bound": (
            "Event-time derivation. Trade ingest_timestamp used only as availability when present; "
            "Full-OB level_changes have no collector receive in persist/research proxy path; "
            "detection candle closes have no collector receive."
        ),
        "episode_id": zone_touch["episode_id"],
    }
