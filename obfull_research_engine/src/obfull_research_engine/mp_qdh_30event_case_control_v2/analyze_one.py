"""Per-event v2 analysis on top of audit_one internals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.mp_qdh_30event_case_control_v1.wall_movement import (
    normalize_vs_trade_direction,
    track_wall_movement,
)
from obfull_research_engine.mp_qdh_wall_linkage_audit_v1.audit_one import audit_one_event
from obfull_research_engine.mp_wall_flow_qdh_silver_v1.silver_adapters import dt_to_ns
from obfull_research_engine.timeparse import format_utc_z

from .coverage import evaluate_coverage_gate
from .features_v2 import extract_features_v2
from .flow_v2 import aggregate_1s, build_flow_100ms_v2


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    if x is None or str(x).strip() in ("", "None", "null"):
        raise ValueError("missing datetime")
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _dt_from_ns(ns: Any) -> datetime | None:
    if ns is None or str(ns).strip() in ("", "None", "null"):
        return None
    try:
        return datetime.fromtimestamp(int(float(ns)) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def analyze_one_event_v2(
    *,
    event: dict[str, Any],
    window: dict[str, Any] | None,
    case_meta: dict[str, Any],
    client: Any | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    audit = audit_one_event(
        event=event,
        window=window,
        case_meta=case_meta,
        client=client,
        dry_run=dry_run,
        return_internals=not dry_run,
    )
    if dry_run:
        return {
            "ok": bool(audit.get("ok")),
            "dry_run": True,
            "event_id": audit.get("event_id"),
            "linkage_status": audit.get("linkage_status"),
            "case": case_meta,
            "db_mutation": False,
            "schema_version": "mp_qdh_30event_case_control_v2",
        }

    eid = str(audit.get("event_id") or event["event_id"])
    internals = audit.pop("_internals", None) or {}
    touch_at = internals.get("touch_at")
    decision_at = internals.get("trigger_at")
    if touch_at is None:
        raw = audit.get("touch_at")
        try:
            touch_at = _as_dt(raw) if raw not in (None, "", "None") else None
        except ValueError:
            touch_at = None
    if decision_at is None:
        raw = audit.get("trigger_at")
        try:
            decision_at = _as_dt(raw) if raw not in (None, "", "None") else None
        except ValueError:
            decision_at = None
    if touch_at is None:
        touch_at = _dt_from_ns(event.get("first_touch_ts_ns") or event.get("touch_ts_ns"))
    if decision_at is None:
        decision_at = _dt_from_ns(event.get("trigger_ts_ns"))
    if decision_at is None and event.get("trigger_ts_ns") not in (None, "", "None"):
        decision_at = datetime.fromtimestamp(int(event["trigger_ts_ns"]) / 1e9, tz=timezone.utc)
    if touch_at is None or decision_at is None:
        return {
            "ok": False,
            "event_id": eid,
            "case": case_meta,
            "blocked_reason": "MISSING_TOUCH_OR_DECISION_TIME",
            "db_mutation": False,
            "schema_version": "mp_qdh_30event_case_control_v2",
            "status": "BLOCKED",
        }

    coverage = evaluate_coverage_gate(
        n_trades_raw=int(audit.get("n_trades_raw") or 0),
        attribution_stats=internals.get("attribution_stats") or {},
        linkage_status=str(audit.get("linkage_status") or ""),
        has_wall=bool(audit.get("selected")),
        n_lc=audit.get("n_lc"),
    )

    base = {
        "ok": bool(audit.get("ok")) and coverage["pass"],
        "event_id": eid,
        "case": case_meta,
        "linkage_status": audit.get("linkage_status"),
        "detail": audit.get("detail"),
        "expected_book_side": audit.get("expected_book_side"),
        "event_role": audit.get("event_role"),
        "label_price_only": audit.get("label_price_only"),
        "trade_side": audit.get("trade_side"),
        "selected": audit.get("selected"),
        "touch_at": format_utc_z(touch_at),
        "decision_at": format_utc_z(decision_at),
        "coverage": coverage,
        "db_mutation": False,
        "schema_version": "mp_qdh_30event_case_control_v2",
    }

    if int(audit.get("causality_violations") or 0) > 0:
        base["ok"] = False
        base["blocked_reason"] = "CAUSALITY_VIOLATION"
        return base
    if not coverage["pass"]:
        base["ok"] = False
        base["blocked_reason"] = "COVERAGE_GATE:" + "|".join(coverage["blockers"])
        # still attach minimal features for diagnostics
        base["features"] = {
            "coverage_pass": False,
            "coverage_blockers": "|".join(coverage["blockers"]),
            "qdh_at_decision": None,
            "qdh_at_decision_na_reason": "COVERAGE_BLOCKED",
        }
        return base

    selected = audit.get("selected")
    if not selected or not internals.get("band_events"):
        base["ok"] = False
        base["blocked_reason"] = "NO_WALL_OR_BAND_EVENTS"
        base["features"] = {
            "coverage_pass": True,
            "qdh_at_decision": None,
            "qdh_at_decision_na_reason": "NO_WALL",
        }
        return base

    wall_side = str(internals.get("wall_side") or selected.get("wall_side"))
    wall_price = float(internals.get("wall_price") or selected.get("wall_price_first"))
    wall_id = str(internals.get("wall_id") or selected.get("candidate_wall_id"))
    band_low = float(internals["band_low"])
    band_high = float(internals["band_high"])
    pre_start = internals.get("pre_start") or touch_at
    forensic_end = internals.get("forensic_end") or decision_at

    flow_100, flow_meta = build_flow_100ms_v2(
        event_id=eid,
        wall_id=wall_id,
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        wall_side=wall_side,
        band_events=internals["band_events"],
        states=internals.get("states") or [],
        touch_at=touch_at,
        trigger_at=decision_at,
        series_start=pre_start,
        series_end=forensic_end,
        trade_side=str(audit.get("trade_side") or event.get("trade_side") or ""),
    )
    flow_1s = aggregate_1s(flow_100)

    feats = extract_features_v2(
        flow_100ms=flow_100,
        funnel=audit.get("funnel"),
        touch_at=touch_at,
        decision_at=decision_at,
        trade_side=str(audit.get("trade_side") or ""),
        flow_meta=flow_meta,
        coverage=coverage,
    )
    feats["linkage_present_flag"] = 1.0 if audit.get("linkage_status") == "PRESENT_AT_TOUCH" else 0.0
    feats["linkage_status"] = audit.get("linkage_status")

    # Wall movement (reuse v1 tracker; causal ≤ decision)
    mids: list[tuple[int, float]] = []
    for st in internals.get("states") or []:
        mid = st.get("midprice")
        if mid is None:
            continue
        try:
            mids.append((dt_to_ns(_as_dt(st["bucket_start"])), float(mid)))
        except Exception:  # noqa: BLE001
            continue
    mids.sort()
    wm = track_wall_movement(
        level_changes=internals.get("level_changes") or [],
        wall_side=wall_side,
        wall_price_start=wall_price,
        zone_id=str(event.get("zone_id") or ""),
        start_ns=dt_to_ns(pre_start),
        touch_ns=dt_to_ns(touch_at),
        decision_ns=dt_to_ns(decision_at),
        mids_by_ns=mids,
    )
    wall_summary = wm["summary"]
    trade_norm = normalize_vs_trade_direction(
        wall_side=wall_side,
        movement_state=str(wall_summary.get("movement_state")),
        trade_side=str(audit.get("trade_side") or ""),
        wall_move_net_ticks=float(wall_summary.get("wall_move_net_ticks") or 0),
    )
    for k, v in wall_summary.items():
        feats[k] = v
    for k, v in trade_norm.items():
        feats[k] = v

    # Typed timeline + footprint cluster are views over the same attribution
    # (no second trade query; footprint does not add to QDH hits).
    from obfull_research_engine.mp_qdh_first_touch_study_v1.footprint_cluster import (
        build_footprint_cluster_event,
        deterministic_trade_ids_hash,
        collect_qdh_attributed_trade_ids,
    )
    from obfull_research_engine.mp_qdh_first_touch_study_v1.typed_timeline import (
        materialize_typed_timeline,
        typed_timeline_hash,
    )

    band_events = internals["band_events"]
    progress_bps = None
    hit_notional = None
    for row in reversed(flow_100):
        if row.get("post_decision"):
            continue
        if row.get("progress_bps") is not None:
            progress_bps = row.get("progress_bps")
        if row.get("attributed_hit_notional_usdt") is not None:
            hit_notional = row.get("attributed_hit_notional_usdt")
        if progress_bps is not None and hit_notional is not None:
            break

    qdh_tids = collect_qdh_attributed_trade_ids(band_events, decision_at=decision_at)
    footprint = build_footprint_cluster_event(
        event_id=eid,
        episode_id=str(event.get("episode_id") or ""),
        wall_id=wall_id,
        wall_side=wall_side,
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        band_events=band_events,
        decision_at=decision_at,
        touch_at=touch_at,
        coverage_ok=bool(coverage.get("pass")),
        flow_attribution_confidence=None,
        availability_confidence=None,
        progress_bps=float(progress_bps) if progress_bps is not None else None,
        attributed_hit_notional_override=float(hit_notional) if hit_notional is not None else None,
    )
    typed_rows = materialize_typed_timeline(
        event_id=eid,
        episode_id=str(event.get("episode_id") or ""),
        symbol=str(event.get("symbol") or "BTCUSDT"),
        zone_id=str(event.get("zone_id") or ""),
        wall_id=wall_id,
        wall_side=wall_side,
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        touch_at=touch_at,
        decision_at=decision_at,
        zone_available_at=internals.get("profile_available_at") or internals.get("pre_start"),
        wall_visible_at=internals.get("wall_visible_at"),
        band_events=band_events,
        flow_100ms=flow_100,
        wall_move_events=[{**e, "event_id": eid} for e in wm["events"]],
        coverage_ok=bool(coverage.get("pass")),
        flow_attribution_confidence=None,
        availability_confidence=None,
        receive_time_is_proxy=True,
        include_post_decision=False,
    )
    feats["footprint_trade_ids_hash"] = footprint.trade_ids_hash
    feats["qdh_hit_trade_ids_hash"] = deterministic_trade_ids_hash(qdh_tids)
    feats["footprint_adds_to_qdh_hits"] = False
    feats["typed_timeline_hash"] = typed_timeline_hash(typed_rows)
    feats["absorption_ratio_status"] = footprint.absorption_ratio_status
    feats["vacuum_score_status"] = footprint.vacuum_score_status
    feats["normalized_impact_efficiency_status"] = footprint.normalized_impact_efficiency_status

    return {
        **base,
        "features": feats,
        "flow_100ms": flow_100,
        "flow_1s": flow_1s,
        "wall_movement_summary": wall_summary,
        "wall_movement_events": [{**e, "event_id": eid} for e in wm["events"]],
        "typed_wall_flow_timeline": typed_rows,
        "footprint_cluster": footprint.to_dict(),
        "flow_meta": flow_meta,
        "mass_balance_violations": 0,
        "causality_violations": int(flow_meta.get("lookahead_flags") or 0),
        "attribution_summary": {
            "event_id": eid,
            "funnel": audit.get("funnel"),
            "attribution_stats": internals.get("attribution_stats"),
            "fill_capped_total": flow_meta.get("final_cum_fill"),
            "unknown_total": flow_meta.get("final_cum_unknown"),
            "fill_excess_total": flow_meta.get("final_fill_excess"),
            "qdh_hit_trade_ids_hash": deterministic_trade_ids_hash(qdh_tids),
            "footprint_trade_ids_hash": footprint.trade_ids_hash,
        },
    }
