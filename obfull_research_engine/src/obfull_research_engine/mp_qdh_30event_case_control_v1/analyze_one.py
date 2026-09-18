"""Per-event analysis: audit_one + wall movement + vacuum + feature extract."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.mp_qdh_wall_linkage_audit_v1.audit_one import audit_one_event
from obfull_research_engine.mp_wall_flow_qdh_silver_v1.silver_adapters import dt_to_ns
from obfull_research_engine.timeparse import format_utc_z

from .features import extract_flow_features
from .vacuum import extract_vacuum_features
from .wall_movement import normalize_vs_trade_direction, track_wall_movement


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def analyze_one_event(
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
        }

    eid = str(audit.get("event_id") or event["event_id"])
    touch_at = _as_dt(audit.get("touch_at") or event.get("first_touch_ts_ns"))
    # prefer internals timestamps
    internals = audit.pop("_internals", None) or {}
    if internals.get("touch_at"):
        touch_at = internals["touch_at"]
    decision_at = internals.get("trigger_at") or _as_dt(audit.get("trigger_at"))
    if decision_at is None:
        decision_at = _as_dt(
            datetime.fromtimestamp(int(event["trigger_ts_ns"]) / 1e9, tz=timezone.utc)
        )

    base = {
        "ok": bool(audit.get("ok")),
        "event_id": eid,
        "case": case_meta,
        "linkage_status": audit.get("linkage_status"),
        "detail": audit.get("detail"),
        "expected_book_side": audit.get("expected_book_side"),
        "event_role": audit.get("event_role"),
        "label_price_only": audit.get("label_price_only"),
        "trade_side": audit.get("trade_side"),
        "selected": audit.get("selected"),
        "mass_balance_violations": int(audit.get("mass_balance_violations") or 0),
        "causality_violations": int(audit.get("causality_violations") or 0),
        "touch_at": format_utc_z(touch_at),
        "decision_at": format_utc_z(decision_at),
        "db_mutation": False,
    }

    if audit.get("causality_violations", 0) > 0:
        base["ok"] = False
        base["blocked_reason"] = "CAUSALITY_VIOLATION"
        return base

    flow_100 = audit.get("flow_100ms") or []
    flow_1s = audit.get("flow_1s") or []
    funnel = audit.get("funnel")
    recon = audit.get("reconciliation") or {}

    # Attribution summary (aggregated; no millions of rejection rows)
    rej = audit.get("rejections") or []
    rej_counts: dict[str, int] = {}
    rej_qty: dict[str, float] = {}
    for r in rej:
        reason = str(r.get("reason") or r.get("rejection_reason") or "OTHER")
        rej_counts[reason] = rej_counts.get(reason, 0) + 1
        try:
            rej_qty[reason] = rej_qty.get(reason, 0.0) + float(r.get("qty") or r.get("size") or 0)
        except (TypeError, ValueError):
            pass
    # Deterministic sample of up to 20 rejection detail rows
    sample = sorted(rej, key=lambda r: str(r.get("trade_id") or ""))[:20]

    trade_side = str(audit.get("trade_side") or event.get("trade_side") or "")
    flow_feat = extract_flow_features(
        flow_100ms=flow_100,
        funnel=funnel,
        touch_at=touch_at,
        decision_at=decision_at,
        trade_side=trade_side,
    )
    flow_feat["queue_at_touch"] = recon.get("queue_at_touch")
    flow_feat["linkage_present_flag"] = 1.0 if audit.get("linkage_status") == "PRESENT_AT_TOUCH" else 0.0

    wall_summary: dict[str, Any] = {}
    wall_events: list[dict[str, Any]] = []
    vacuum: dict[str, Any] = {"same_side_depth_inside_1bp": "NOT_AVAILABLE"}
    trade_norm: dict[str, Any] = {}

    selected = audit.get("selected")
    if selected and internals.get("level_changes") is not None:
        wall_side = str(selected.get("wall_side") or audit.get("expected_book_side"))
        wall_price = float(selected.get("wall_price_first"))
        # mid series from states
        mids: list[tuple[int, float]] = []
        for st in internals.get("states") or []:
            mid = st.get("midprice")
            if mid is None:
                continue
            bstart = st.get("bucket_start")
            try:
                mids.append((dt_to_ns(_as_dt(bstart)), float(mid)))
            except Exception:  # noqa: BLE001
                continue
        mids.sort()
        pre_start = internals.get("pre_start") or touch_at
        wm = track_wall_movement(
            level_changes=internals["level_changes"],
            wall_side=wall_side,
            wall_price_start=wall_price,
            zone_id=str(event.get("zone_id") or ""),
            start_ns=dt_to_ns(pre_start),
            touch_ns=dt_to_ns(touch_at),
            decision_ns=dt_to_ns(decision_at),
            mids_by_ns=mids,
        )
        wall_summary = wm["summary"]
        wall_events = [{**e, "event_id": eid} for e in wm["events"]]
        trade_norm = normalize_vs_trade_direction(
            wall_side=wall_side,
            movement_state=str(wall_summary.get("movement_state")),
            trade_side=trade_side,
            wall_move_net_ticks=float(wall_summary.get("wall_move_net_ticks") or 0),
        )
        # lag seconds
        lag = wall_summary.get("median_wall_move_lag_ms")
        trade_norm["seconds_wall_leads_price"] = (float(lag) / 1000.0) if lag is not None else None
        trade_norm["seconds_price_leads_wall"] = None
        if (wall_summary.get("wall_moves_after_mid_count") or 0) > (
            wall_summary.get("wall_moves_before_mid_count") or 0
        ):
            trade_norm["seconds_price_leads_wall"] = trade_norm.get("seconds_wall_leads_price")
            trade_norm["seconds_wall_leads_price"] = None

        move_ns = [
            int(e["event_time_ns"])
            for e in wall_events
            if e.get("kind") == "STEP" and int(e["event_time_ns"]) <= dt_to_ns(decision_at)
        ]
        vacuum = extract_vacuum_features(
            states=internals.get("states") or [],
            wall_side=wall_side,
            wall_price=wall_price,
            touch_at=touch_at,
            decision_at=decision_at,
            move_times_ns=move_ns,
        )
    else:
        wall_summary = {
            "movement_state": "WALL_MOVEMENT_AMBIGUOUS",
            "note": "NO_SELECTED_WALL",
        }

    features = {
        **flow_feat,
        **{f"wall_{k}" if not k.startswith("wall_") else k: v for k, v in wall_summary.items()},
        **trade_norm,
        **{f"vac_{k}" if not k.startswith("same_") and not k.startswith("opposite_") and k not in (
            "depth_beyond_wall",
            "baseline_depth_beyond_wall",
            "depth_beyond_wall_fraction",
            "depth_change_behind_wall",
            "empty_price_levels_behind_wall",
            "spread_change_bps",
            "mid_move_after_wall_move",
            "microprice_move_after_wall_move",
            "time_wall_move_to_price_move_ms",
        ) else k: v for k, v in vacuum.items()},
        "distance_to_mp_edge_ticks": (selected or {}).get("distance_to_mp_edge_ticks"),
    }
    # flatten vacuum keys cleanly
    for k, v in vacuum.items():
        features[k] = v
    for k, v in wall_summary.items():
        features[k] = v
    for k, v in trade_norm.items():
        features[k] = v

    micro = {
        "event_id": eid,
        "mid_at_touch": features.get("mid_at_touch"),
        "mid_at_decision": features.get("mid_at_decision"),
        "microprice_at_touch": features.get("microprice_at_touch"),
        "microprice_at_decision": features.get("microprice_at_decision"),
        "microprice_minus_mid_at_touch": features.get("microprice_minus_mid_at_touch"),
        "microprice_minus_mid_at_decision": features.get("microprice_minus_mid_at_decision"),
        "microprice_change": features.get("microprice_change"),
        "mid_change": features.get("mid_change"),
        "mid_change_favorable_signed": features.get("mid_change_favorable_signed"),
        "price_impact_per_attributed_fill": features.get("price_impact_per_attributed_fill"),
        "price_impact_per_net_depletion": features.get("price_impact_per_net_depletion"),
        "adverse_price_response_after_aggression": features.get("adverse_price_response_after_aggression"),
        "favorable_price_response_after_aggression": features.get("favorable_price_response_after_aggression"),
        "microprice_source": "MICROPRICE_PROXY_EQUAL_SIZE_MID",
        "descriptive_only": True,
    }

    qdh_traj = []
    for r in flow_100:
        if r.get("post_decision"):
            continue
        qdh_traj.append(
            {
                "event_id": eid,
                "bucket_start": r.get("bucket_start"),
                "relative_seconds_to_touch": r.get("relative_seconds_to_touch"),
                "queue_end": r.get("queue_end"),
                "qdh_ewma": r.get("qdh_ewma"),
                "queue_exhausted": r.get("queue_exhausted"),
                "phase": r.get("phase"),
            }
        )

    return {
        **base,
        "features": features,
        "flow_100ms": flow_100,
        "flow_1s": flow_1s,
        "wall_movement_summary": wall_summary,
        "wall_movement_events": wall_events,
        "vacuum": vacuum,
        "microprice": micro,
        "qdh_trajectory": qdh_traj,
        "attribution_summary": {
            "event_id": eid,
            "funnel": funnel,
            "rejection_counts": rej_counts,
            "rejection_qty": rej_qty,
            "rejection_sample_n": len(sample),
            "rejection_sample": sample,
            "note": "aggregated_counts_plus_deterministic_sample_no_silent_truncation_of_counts",
        },
        "reconciliation": recon,
        "candidates": audit.get("candidates") or [],
    }
