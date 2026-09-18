"""Per-event wall-linkage + trade-funnel audit (read-only CH)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    band_bounds,
    build_band_nodes,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.loaders import (
    assess_chunks,
    chunks_overlapping,
    load_level_changes,
    load_metrics_enriched,
    load_public_trades,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import build_zone_bands
from obfull_research_engine.mp_qdh_canonical_integration_v1 import BAND_TICKS, TICK_SIZE
from obfull_research_engine.mp_wall_flow_qdh_silver_v1.silver_adapters import (
    dt_to_ns,
    level_changes_to_qdh_rows,
    metrics_to_qdh_states,
    ns_to_dt,
    seed_initial_asks_from_level_changes,
    xray_trades_to_raw_rows,
)
from obfull_research_engine.drilldown.aggregation_100ms import _as_dt
from obfull_research_engine.timeparse import format_utc_z

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    FORENSIC_TAIL_S,
    PRE_TOUCH_S,
    SILVER_DATABASE,
    SYMBOL,
    WARMUP_S,
)
from .flow_series import aggregate_1s, assert_1s_matches_100ms, build_flow_100ms
from .trade_funnel import build_trade_funnel
from .wall_track import track_pre_touch_walls


def _assert_ro() -> None:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")


def audit_one_event(
    *,
    event: dict[str, Any],
    window: dict[str, Any] | None,
    case_meta: dict[str, Any],
    database: str = SILVER_DATABASE,
    client: Any | None = None,
    dry_run: bool = False,
    return_internals: bool = False,
) -> dict[str, Any]:
    _assert_ro()
    eid = str(event["event_id"])
    touch_ns = int(event["first_touch_ts_ns"])
    touch_at = ns_to_dt(touch_ns)
    trig_raw = event.get("trigger_ts_ns")
    if trig_raw in (None, "", "None"):
        return {
            "ok": False,
            "event_id": eid,
            "linkage_status": "DATA_INCOMPLETE",
            "blocker_reason": "MISSING_TRIGGER_TS",
            "case": case_meta,
        }
    trigger_at = ns_to_dt(int(trig_raw))
    role = str(event["event_role"])
    bands = build_zone_bands(
        role=role, low=float(event["confluence_low"]), high=float(event["confluence_high"])
    )
    # TRUE_BREAK must not flip side — defense from role only
    expected_side = bands.defense_side
    pre_start = touch_at - timedelta(seconds=PRE_TOUCH_S)
    forensic_end = trigger_at + timedelta(seconds=FORENSIC_TAIL_S)
    profile_avail = event.get("profile_available_ts_ns")
    if profile_avail not in (None, "", "None"):
        zone_available = ns_to_dt(int(profile_avail))
    else:
        zone_available = touch_at - timedelta(seconds=60)
    coverage_start = min(zone_available, pre_start) - timedelta(seconds=float(WARMUP_S))

    chain = None
    if window:
        chain = window.get("chain_version") or None
    chain = chain or event.get("chain_version") or None

    own = client is None
    client = client or get_clickhouse_client(role="mp_qdh_wall_linkage_audit")
    try:
        start_ns = dt_to_ns(coverage_start)
        end_ns = dt_to_ns(forensic_end)
        try:
            assessment = assess_chunks(
                client,
                database=database,
                symbol=str(event.get("symbol") or SYMBOL),
                start=coverage_start,
                end=forensic_end,
            )
            chunk_keys = list(assessment.get("chunk_keys") or [])
        except RuntimeError as exc:
            assessment = {"status": "FALLBACK", "error": str(exc)}
            chunk_keys = []
        chain = chain or str(assessment.get("chain_version") or "")
        if not chunk_keys and chain:
            chunk_keys = chunks_overlapping(
                client,
                database=database,
                chain_version=chain,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            assessment = {**assessment, "chain_version": chain, "chunk_keys": chunk_keys}
        if not chunk_keys:
            return {
                "ok": False,
                "event_id": eid,
                "linkage_status": "DATA_INCOMPLETE",
                "blocker_reason": "NO_SILVER_CHUNKS",
                "case": case_meta,
                "assessment": assessment,
            }

        lo, hi = float(bands.low), float(bands.high)
        pad = 50.0
        lcs = load_level_changes(
            client,
            database=database,
            symbol=str(event.get("symbol") or SYMBOL),
            start_ns=start_ns,
            end_ns=end_ns,
            chunk_keys=chunk_keys,
            price_min=min(lo, hi) - pad,
            price_max=max(lo, hi) + pad,
            side=None,
        )
        touch_price = event.get("touch_price")
        tp = float(touch_price) if touch_price not in (None, "", "None") else None

        linkage = track_pre_touch_walls(
            level_changes=lcs,
            event_role=role,
            confluence_low=lo,
            confluence_high=hi,
            zone_id=str(event.get("zone_id") or ""),
            touch_price=tp,
            zone_touch_ns=touch_ns,
            pre_touch_start_ns=dt_to_ns(pre_start),
        )

        dry = {
            "ok": True,
            "dry_run": True,
            "event_id": eid,
            "event_role": role,
            "mp_edge_side": role,
            "expected_book_side": expected_side,
            "label_price_only": event.get("label_price_only"),
            "trade_side": event.get("trade_side"),
            "linkage_status": linkage.status,
            "detail": linkage.detail,
            "selected": linkage.selected,
            "n_candidates": len(linkage.candidates),
            "zone": {"lo": linkage.zone_lo, "hi": linkage.zone_hi, "qmin": linkage.qmin, "qmax": linkage.qmax},
            "windows": {
                "pre_touch": [format_utc_z(pre_start), format_utc_z(touch_at)],
                "decision": [format_utc_z(touch_at), format_utc_z(trigger_at)],
                "forensic": [format_utc_z(trigger_at), format_utc_z(forensic_end)],
            },
            "db_mutation": False,
            "case": case_meta,
        }
        if dry_run:
            return dry

        # Attribution path when we have a wall price (present or historical selected)
        selected = linkage.selected
        if selected is None:
            return {
                **dry,
                "dry_run": False,
                "ok": linkage.status not in ("DATA_INCOMPLETE",),
                "funnel": None,
                "flow_100ms": [],
                "flow_1s": [],
                "reconciliation": {
                    "event_id": eid,
                    "linkage_status": linkage.status,
                    "attributed_fill_qty": 0.0,
                    "residual_pull_qty": 0.0,
                    "refill_qty": 0.0,
                    "note": "NO_WALL_FOR_ATTRIBUTION",
                },
                "candidates": linkage.candidates,
                "mass_balance_violations": 0,
                "causality_violations": 0,
            }

        wall_price = float(selected["wall_price_first"])
        wall_side = str(selected["wall_side"])
        wall_id = str(selected["candidate_wall_id"])
        band_low, band_high = band_bounds(wall_price, tick_size=TICK_SIZE, band_ticks=BAND_TICKS)

        trades_xray = load_public_trades(
            client,
            symbol=str(event.get("symbol") or SYMBOL),
            start_ns=start_ns,
            end_ns=end_ns,
        )
        raw_rows = xray_trades_to_raw_rows(trades_xray, symbol=str(event.get("symbol") or SYMBOL))

        metrics = load_metrics_enriched(
            client,
            database=database,
            symbol=str(event.get("symbol") or SYMBOL),
            start_ns=dt_to_ns(pre_start),
            end_ns=end_ns - 1,
            chunk_keys=chunk_keys,
        )
        states = metrics_to_qdh_states(metrics)
        lc_rows_all = level_changes_to_qdh_rows(lcs)
        initial = seed_initial_asks_from_level_changes(
            lc_rows_all,
            band_low=band_low,
            band_high=band_high,
            wall_side=wall_side,
            coverage_start=coverage_start,
        )
        lc_rows = [r for r in lc_rows_all if _as_dt(r["event_time"]) >= coverage_start]
        init_avail = states[0]["available_at"] if states else coverage_start
        band_nodes, band_meta = build_band_nodes(
            initial_asks=initial,
            initial_event_time=coverage_start,
            initial_available_at=_as_dt(init_avail) if not isinstance(init_avail, datetime) else init_avail,
            initial_replay_epoch=None,
            level_changes=lc_rows,
            band_low=band_low,
            band_high=band_high,
            wall_side=wall_side,
        )

        # Analysis window for attribution: pre_touch through forensic (audit);
        # wall selection already locked to ≤ touch.
        funnel_pack = build_trade_funnel(
            raw_trade_rows=raw_rows,
            nodes=band_nodes,
            wall_side=wall_side,
            wall_price=wall_price,
            band_ticks=BAND_TICKS,
            tick_size=TICK_SIZE,
            wall_visible_at=pre_start,  # allow pre-touch attribution for historical explanation
            analysis_start=pre_start,
            analysis_end_exclusive=forensic_end,
            wall_id=wall_id,
            zone_id=str(event.get("zone_id") or ""),
            episode_id=f"audit:{eid}",
            event_id=eid,
        )

        flow_100, flow_meta = build_flow_100ms(
            event_id=eid,
            wall_id=wall_id,
            wall_price=wall_price,
            band_low=band_low,
            band_high=band_high,
            band_events=funnel_pack["band_events_raw"],
            states=states,
            touch_at=touch_at,
            trigger_at=trigger_at,
            series_start=pre_start,
            series_end=forensic_end,
        )
        flow_1s = aggregate_1s(flow_100)
        agg_ok = assert_1s_matches_100ms(flow_100, flow_1s)

        # Phase sums
        def _sum_phase(phase: str, key: str) -> float:
            return sum(float(r.get(key) or 0) for r in flow_100 if r.get("phase") == phase)

        # Decision-window only for signal-ish features
        dec_fill = _sum_phase("TOUCH_TO_TRIGGER", "attributed_fill")
        dec_pull = _sum_phase("TOUCH_TO_TRIGGER", "residual_pull")
        dec_refill = _sum_phase("TOUCH_TO_TRIGGER", "refill")
        pre_fill = _sum_phase("PRE_TOUCH", "attributed_fill")
        pre_pull = _sum_phase("PRE_TOUCH", "residual_pull")

        book_dec_all = sum(float(r.get("book_decrease") or 0) for r in flow_100 if not r.get("post_decision"))
        fill_all = sum(float(r.get("attributed_fill") or 0) for r in flow_100 if not r.get("post_decision"))
        pull_all = sum(float(r.get("residual_pull") or 0) for r in flow_100 if not r.get("post_decision"))
        refill_all = sum(float(r.get("refill") or 0) for r in flow_100 if not r.get("post_decision"))

        # QDH at touch / trigger from flow
        def _qdh_at(ts: datetime) -> float | None:
            pre = [r for r in flow_100 if _as_dt(r["bucket_available_at"]) <= ts]
            if not pre:
                return None
            return pre[-1].get("qdh_ewma")

        recon = {
            "event_id": eid,
            "linkage_status": linkage.status,
            "wall_id": wall_id,
            "wall_side": wall_side,
            "wall_price": wall_price,
            "band_low": band_low,
            "band_high": band_high,
            "queue_at_touch": selected.get("queue_at_touch"),
            "max_queue_pre_touch": selected.get("max_queue"),
            "pre_touch_fill": pre_fill,
            "pre_touch_pull": pre_pull,
            "decision_fill": dec_fill,
            "decision_pull": dec_pull,
            "decision_refill": dec_refill,
            "pre_trigger_fill": fill_all,
            "pre_trigger_pull": pull_all,
            "pre_trigger_refill": refill_all,
            "pre_trigger_book_decrease": book_dec_all,
            "fill_share_of_decrease": (fill_all / book_dec_all) if book_dec_all > 1e-12 else None,
            "pull_share_of_decrease": (pull_all / book_dec_all) if book_dec_all > 1e-12 else None,
            "gross_book_churn": pull_all + refill_all,
            "qdh_at_touch": _qdh_at(touch_at),
            "qdh_at_trigger": _qdh_at(trigger_at),
            "agg_1s_matches_100ms": agg_ok,
            "funnel_attributed_fill_qty": funnel_pack["funnel"]["attributed_fill_qty"],
            "funnel_in_band_qty": funnel_pack["funnel"]["in_band_trade_qty"],
            "funnel_correct_aggressor_qty": funnel_pack["funnel"]["correct_aggressor_trade_qty"],
            "funnel_unique_trades": funnel_pack["funnel"]["total_unique_trade_count"],
            "mass_balance_violations": int(funnel_pack["stats"].get("mass_balance_violations") or 0),
            "causality_violations": int(flow_meta.get("lookahead_flags") or 0),
        }

        out: dict[str, Any] = {
            "ok": True,
            "dry_run": False,
            "event_id": eid,
            "case": case_meta,
            "event_role": role,
            "mp_edge_side": role,
            "expected_book_side": expected_side,
            "label_price_only": event.get("label_price_only"),
            "trade_side": event.get("trade_side"),
            "linkage_status": linkage.status,
            "detail": linkage.detail,
            "selected": selected,
            "candidates": linkage.candidates,
            "funnel": funnel_pack["funnel"],
            "rejections": funnel_pack["rejections"],
            "flow_100ms": flow_100,
            "flow_1s": flow_1s,
            "reconciliation": recon,
            "mass_balance_violations": recon["mass_balance_violations"],
            "causality_violations": recon["causality_violations"],
            "n_lc": len(lcs),
            "n_trades_raw": len(raw_rows),
            "assessment": {k: assessment.get(k) for k in ("status", "chain_version", "error") if k in assessment},
            "touch_at": format_utc_z(touch_at),
            "trigger_at": format_utc_z(trigger_at),
            "pre_start": format_utc_z(pre_start),
            "forensic_end": format_utc_z(forensic_end),
        }
        if return_internals:
            # Optional artifacts for downstream case-control (not persisted by audit runner).
            out["_internals"] = {
                "level_changes": lcs,
                "states": states,
                "touch_at": touch_at,
                "trigger_at": trigger_at,
                "pre_start": pre_start,
                "forensic_end": forensic_end,
                "defense_side": expected_side,
                "zone_lo": lo,
                "zone_hi": hi,
                "band_events": funnel_pack.get("band_events_raw") or [],
                "attribution_stats": funnel_pack.get("stats") or {},
                "band_low": band_low,
                "band_high": band_high,
                "wall_id": wall_id,
                "wall_price": wall_price,
                "wall_side": wall_side,
            }
        return out
    finally:
        if own:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
