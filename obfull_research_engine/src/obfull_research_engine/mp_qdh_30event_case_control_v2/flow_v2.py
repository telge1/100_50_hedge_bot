"""V2 100ms flow: persistence-wired QDH, fill-cap pull semantics, UNKNOWN, IE."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.drilldown.aggregation_100ms import _as_dt, _floor_bucket
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1 import BUCKET_MS, EPSILON
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.aggressor_flow import (
    AggressorState,
    update_aggressor,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.price_response import (
    PriceResponseState,
    update_price_response,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import (
    QdhState,
    update_qdh,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    WallFlowEvent,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.near_zero import apply_queue_policy
from obfull_research_engine.timeparse import format_utc_z


def _phase(ts: datetime, touch: datetime, trigger: datetime) -> str:
    if ts <= touch:
        return "PRE_TOUCH"
    if ts <= trigger:
        return "TOUCH_TO_TRIGGER"
    return "POST_TRIGGER_FORENSIC"


def recompute_bucket_mass(
    *,
    queue_before: float | None,
    queue_after: float | None,
    raw_fill: float,
    confidence: str,
) -> dict[str, float]:
    """Masterplan pull with fill cap; LOW+no-fill decrease → UNKNOWN not pull."""
    q0 = queue_before
    q1 = queue_after
    if q0 is None or q1 is None:
        return {
            "book_decrease": 0.0,
            "attributed_fill_raw": float(raw_fill),
            "attributed_fill_capped": 0.0,
            "fill_excess": float(raw_fill),
            "residual_pull": 0.0,
            "refill": 0.0,
            "unknown": 0.0,
            "net_depletion": 0.0,
        }
    book_dec = max(float(q0) - float(q1), 0.0)
    raw = max(float(raw_fill), 0.0)
    conf = str(confidence or "MEDIUM").upper()

    if conf == "LOW" and raw <= 1e-15 and book_dec > 1e-15:
        return {
            "book_decrease": book_dec,
            "attributed_fill_raw": 0.0,
            "attributed_fill_capped": 0.0,
            "fill_excess": 0.0,
            "residual_pull": 0.0,
            "refill": 0.0,
            "unknown": book_dec,
            "net_depletion": 0.0,  # unknown not counted as depletion
        }

    fill_capped = min(raw, book_dec)
    fill_excess = max(raw - book_dec, 0.0)
    # Mass with capped fill (Masterplan Pull = max(BookDecrease - Fill, 0))
    net_passive = (float(q1) - float(q0)) + fill_capped
    refill = max(net_passive, 0.0)
    pull = max(-net_passive, 0.0)
    net = fill_capped + pull - refill
    return {
        "book_decrease": book_dec,
        "attributed_fill_raw": raw,
        "attributed_fill_capped": fill_capped,
        "fill_excess": fill_excess,
        "residual_pull": pull,
        "refill": refill,
        "unknown": 0.0,
        "net_depletion": net,
    }


def build_flow_100ms_v2(
    *,
    event_id: str,
    wall_id: str,
    wall_price: float,
    band_low: float,
    band_high: float,
    wall_side: str,
    band_events: list[WallFlowEvent],
    states: list[dict[str, Any]],
    touch_at: datetime,
    trigger_at: datetime,
    series_start: datetime,
    series_end: datetime,
    trade_side: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_bucket: dict[datetime, list[WallFlowEvent]] = {}
    for ev in band_events:
        if ev.attribution_confidence == "INVALID":
            continue
        end = _as_dt(ev.interval_end_exchange_time)
        b = _floor_bucket(end, BUCKET_MS)
        by_bucket.setdefault(b, []).append(ev)

    qdh = QdhState()
    agg = AggressorState()
    # IE direction: attack on ask → buy (+1); bid → sell (-1) for progress in attack direction
    direction = 1 if str(wall_side).lower() == "ask" else -1
    price_st = PriceResponseState(direction=direction)
    rows: list[dict[str, Any]] = []
    cum_fill = cum_pull = cum_refill = cum_net = cum_unk = cum_excess = 0.0
    lookahead = 0
    last_q = None
    queue_at_touch = None
    seeded_price = False
    interarrivals: list[float] = []

    sorted_states = sorted(
        [s for s in states if series_start <= _as_dt(s["bucket_start"]) < series_end],
        key=lambda s: _as_dt(s["bucket_start"]),
    )

    for st in sorted_states:
        bstart = _as_dt(st["bucket_start"])
        bend = _as_dt(st["bucket_end_exclusive"])
        avail = _as_dt(st.get("available_at") or bend)
        if avail <= bstart:
            lookahead += 1
        evs = by_bucket.get(bstart, [])
        dur = max((bend - bstart).total_seconds(), EPSILON)

        # Aggregate raw hits / choose confidence worst
        raw_fill = sum(float(e.attributed_hit_qty) for e in evs)
        raw_notional = sum(float(e.attributed_hit_notional) for e in evs)
        trade_count = sum(int(e.attributed_trade_count or 0) for e in evs)
        confs = [str(e.attribution_confidence or "MEDIUM").upper() for e in evs]
        worst = "HIGH"
        for c in confs:
            if c == "INVALID":
                continue
            if c == "LOW":
                worst = "LOW"
            elif c == "MEDIUM" and worst == "HIGH":
                worst = "MEDIUM"
        q_before = float(evs[0].queue_before) if evs else (last_q if last_q is not None else None)
        q_after = float(evs[-1].queue_after) if evs else q_before
        if q_after is not None:
            last_q = q_after
        if queue_at_touch is None and bstart >= touch_at and q_after is not None:
            queue_at_touch = float(q_after)

        mass = recompute_bucket_mass(
            queue_before=q_before, queue_after=q_after, raw_fill=raw_fill, confidence=worst if evs else "MEDIUM"
        )
        fill = mass["attributed_fill_capped"]
        pull = mass["residual_pull"]
        refill = mass["refill"]
        unk = mass["unknown"]
        net = mass["net_depletion"]
        book_dec = mass["book_decrease"]

        # Aggressor persistence (attack-side hits only; qty already attack-filtered)
        buy_q = fill if str(wall_side).lower() == "ask" else 0.0
        sell_q = fill if str(wall_side).lower() == "bid" else 0.0
        agg = update_aggressor(
            agg,
            hit_qty=fill,
            hit_notional=raw_notional * (fill / raw_fill) if raw_fill > 1e-15 else 0.0,
            hit_trade_count=trade_count if fill > 0 else 0,
            interval_duration_s=dur,
            exchange_time=bend,
            interarrival_ms_samples=interarrivals,
            buy_hit_qty=buy_q,
            sell_hit_qty=sell_q,
        )

        qdh_reason = None
        qdh_valid = False
        qdh_val = None
        toxic = None
        if q_after is not None:
            qdh = update_qdh(
                qdh,
                net_depletion_qty=net,
                interval_duration_s=dur,
                current_queue=float(q_after),
                persistence_ratio=agg.persistence_ratio,
            )
            pol = apply_queue_policy(
                queue_remaining_qty=q_after,
                qdh_base=qdh.qdh_base,
                queue_runway_seconds=qdh.queue_runway_seconds,
            )
            qdh_val = pol.get("canonical_qdh_base")
            qdh_valid = bool(pol.get("qdh_valid"))
            qdh_reason = pol.get("qdh_invalid_reason")
            toxic = qdh.qdh_toxic_base_only if qdh_valid else None
            queue_state = pol.get("queue_state")
        else:
            pol = {"queue_state": "UNKNOWN"}
            queue_state = "UNKNOWN"
            qdh_reason = "QUEUE_UNKNOWN"

        # Impact efficiency via engine price_response (seed at touch).
        # Export IE without EPSILON padding: zero/near-zero notional → NOT_AVAILABLE.
        bb = st.get("best_bid")
        ba = st.get("best_ask")
        mid = st.get("midprice")
        if mid is None and bb is not None and ba is not None:
            mid = 0.5 * (float(bb) + float(ba))
        bid_sz = float(st.get("bid_depth_2bps") or 1.0)
        ask_sz = float(st.get("ask_depth_2bps") or 1.0)
        ie_export: float | None = None
        if bb is not None and ba is not None and avail >= touch_at:
            q_touch = queue_at_touch if queue_at_touch is not None else (float(q_after) if q_after is not None else 1.0)
            price_st = update_price_response(
                price_st,
                best_bid=float(bb),
                bid_size=max(bid_sz, EPSILON),
                best_ask=float(ba),
                ask_size=max(ask_sz, EPSILON),
                cumulative_hit_notional_since_wall_touch=agg.cumulative_hit_notional,
                cumulative_hit_qty=agg.cumulative_hit_qty,
                cumulative_net_refill_qty=cum_refill + refill,
                cumulative_residual_pull_qty=cum_pull + pull,
                current_defended_band_queue=float(q_after) if q_after is not None else q_touch,
                queue_at_wall_touch=float(q_touch) if q_touch else 1.0,
                wall_price=wall_price,
            )
            seeded_price = True
            hit_n = float(agg.cumulative_hit_notional)
            if hit_n > 0.0:
                ie_export = float(price_st.progress_bps) / (hit_n / 1_000_000.0)
            else:
                ie_export = None

        same_key = "ask_depth_2bps" if str(wall_side).lower() == "ask" else "bid_depth_2bps"
        opp_key = "bid_depth_2bps" if str(wall_side).lower() == "ask" else "ask_depth_2bps"
        same_d = st.get(same_key)
        opp_d = st.get(opp_key)

        cum_fill += fill
        cum_pull += pull
        cum_refill += refill
        cum_net += net
        cum_unk += unk
        cum_excess += mass["fill_excess"]

        phase = _phase(avail, touch_at, trigger_at)
        post = phase == "POST_TRIGGER_FORENSIC"
        spread_bps = None
        try:
            if bb is not None and ba is not None and mid and float(mid) > 0:
                spread_bps = 10_000.0 * (float(ba) - float(bb)) / float(mid)
        except (TypeError, ValueError):
            pass

        rows.append(
            {
                "event_id": event_id,
                "bucket_start": format_utc_z(bstart),
                "bucket_end": format_utc_z(bend),
                "relative_seconds_to_touch": (bstart - touch_at).total_seconds(),
                "relative_seconds_to_trigger": (bstart - trigger_at).total_seconds(),
                "wall_id": wall_id,
                "wall_state": queue_state,
                "wall_price": wall_price,
                "band_low": band_low,
                "band_high": band_high,
                "queue_start": q_before,
                "queue_end": q_after,
                "book_decrease": book_dec,
                "attributed_fill_raw": mass["attributed_fill_raw"],
                "attributed_fill": fill,
                "fill_excess_over_decrease": mass["fill_excess"],
                "residual_pull": pull,
                "refill": refill,
                "unknown": unk,
                "net_depletion": net,
                "cumulative_fill": cum_fill,
                "cumulative_pull": cum_pull,
                "cumulative_refill": cum_refill,
                "cumulative_unknown": cum_unk,
                "cumulative_net_depletion": cum_net,
                "persistence_ratio": agg.persistence_ratio,
                "qdh_raw": (net / dur) / (float(q_after) + EPSILON) if q_after is not None else None,
                "qdh_ewma": qdh_val,
                "qdh_valid": qdh_valid,
                "qdh_invalid_reason": qdh_reason,
                "qdh_toxic_base_only": toxic,
                "M_persistence": qdh.m_persistence if q_after is not None else None,
                "queue_exhausted": queue_state == "QUEUE_EXHAUSTED",
                "mid": mid,
                "microprice": price_st.microprice if seeded_price else mid,
                "microprice_source": "PRICE_RESPONSE_ENGINE" if seeded_price else "MID_FALLBACK",
                "microprice_minus_mid": (
                    (price_st.microprice - float(mid))
                    if seeded_price and price_st.microprice is not None and mid is not None
                    else None
                ),
                "spread_bps": spread_bps if spread_bps is not None else price_st.spread_bps,
                "impact_efficiency_bps_per_million": ie_export if seeded_price else None,
                "impact_efficiency_status": (
                    "OK" if (seeded_price and ie_export is not None) else ("NOT_AVAILABLE" if seeded_price else None)
                ),
                "attributed_hit_notional_usdt": agg.cumulative_hit_notional if seeded_price else None,
                "progress_bps": price_st.progress_bps if seeded_price else None,
                "same_side_depth_2bps": float(same_d) if same_d is not None else None,
                "opposite_side_depth_2bps": float(opp_d) if opp_d is not None else None,
                "data_complete": True,
                "phase": phase,
                "post_decision": post,
                "bucket_available_at": format_utc_z(avail),
                "attribution_confidence_bucket": worst if evs else None,
            }
        )

    decision_horizon_s = max((trigger_at - touch_at).total_seconds(), EPSILON)
    meta = {
        "n_rows": len(rows),
        "lookahead_flags": lookahead,
        "final_cum_fill": cum_fill,
        "final_cum_pull": cum_pull,
        "final_cum_refill": cum_refill,
        "final_cum_unknown": cum_unk,
        "final_fill_excess": cum_excess,
        "decision_horizon_s": decision_horizon_s,
        "final_persistence_ratio": agg.persistence_ratio,
        "final_impact_efficiency": (
            (float(price_st.progress_bps) / (agg.cumulative_hit_notional / 1_000_000.0))
            if agg.cumulative_hit_notional > 0.0
            else None
        ),
        "final_hit_notional_usdt": agg.cumulative_hit_notional,
        "final_hit_qty": agg.cumulative_hit_qty,
        "final_hit_trades": agg.cumulative_hit_trades,
        "queue_at_touch": queue_at_touch,
    }
    return rows, meta


def aggregate_1s(rows_100ms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from datetime import timedelta

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows_100ms:
        ts = _as_dt(r["bucket_start"])
        key = ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        groups.setdefault(key, []).append(r)
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        g = groups[key]
        last = g[-1]
        fill = sum(float(x.get("attributed_fill") or 0) for x in g)
        pull = sum(float(x.get("residual_pull") or 0) for x in g)
        refill = sum(float(x.get("refill") or 0) for x in g)
        unk = sum(float(x.get("unknown") or 0) for x in g)
        net = sum(float(x.get("net_depletion") or 0) for x in g)
        out.append(
            {
                "event_id": last.get("event_id"),
                "wall_id": last.get("wall_id"),
                "bucket_start": key,
                "bucket_end": format_utc_z(_as_dt(key) + timedelta(seconds=1)),
                "n_100ms_buckets": len(g),
                "attributed_fill": fill,
                "residual_pull": pull,
                "refill": refill,
                "unknown": unk,
                "net_depletion": net,
                "qdh_ewma": last.get("qdh_ewma"),
                "persistence_ratio": last.get("persistence_ratio"),
                "phase": last.get("phase"),
                "post_decision": last.get("post_decision"),
            }
        )
    return out
