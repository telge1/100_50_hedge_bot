"""Causal 100ms feature timeline from WallFlowEvents + existing states."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt, _floor_bucket
from ..timeparse import format_utc_z
from . import BUCKET_MS, EPSILON, WALL_PRICE, WALL_STATE_NOT_CLASSIFIED
from .aggressor_flow import AggressorState, update_aggressor
from .price_response import PriceResponseState, evidence_flags, price_response_to_dict, update_price_response
from .queue_depletion_hazard import QdhState, qdh_to_dict, update_qdh
from .wall_flow_attribution import WallFlowEvent


def _conf_rank(c: str) -> int:
    return {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INVALID": 0}.get(c, 0)


def _worst_conf(a: str, b: str) -> str:
    return a if _conf_rank(a) <= _conf_rank(b) else b


def build_feature_timeline(
    *,
    states: list[dict[str, Any]],
    exact_events: list[WallFlowEvent],
    band_events: list[WallFlowEvent],
    wall_touch_at: datetime,
    zone_touch_at: datetime,
    detection_at: datetime,
    queue_exact_at_wall_touch: float,
    queue_band_at_wall_touch: float,
    wall_price: float = WALL_PRICE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate attribution onto existing 100ms states (causal, no backfill)."""
    # Index valid events by bucket start of interval_end
    def index_events(events: list[WallFlowEvent]) -> dict[datetime, list[WallFlowEvent]]:
        out: dict[datetime, list[WallFlowEvent]] = {}
        for ev in events:
            if ev.attribution_confidence == "INVALID":
                continue
            end = _as_dt(ev.interval_end_exchange_time)
            bstart = _floor_bucket(end, BUCKET_MS)
            out.setdefault(bstart, []).append(ev)
        return out

    exact_ix = index_events(exact_events)
    band_ix = index_events(band_events)

    agg_exact = AggressorState()
    agg_band = AggressorState()
    qdh_exact = QdhState()
    qdh_band = QdhState()
    price_st = PriceResponseState(direction=1)
    # Seed microprice at wall touch from first state with available_at >= wall_touch
    seeded = False
    cum_refill = 0.0
    cum_pull = 0.0
    last_hit_times: list[datetime] = []
    rows: list[dict[str, Any]] = []
    look_ahead_violations = 0

    sorted_states = sorted(states, key=lambda s: _as_dt(s["bucket_start"]))
    prev_available: datetime | None = None

    for st in sorted_states:
        bstart = _as_dt(st["bucket_start"])
        bend = _as_dt(st["bucket_end_exclusive"])
        state_avail = _as_dt(st.get("available_at") or bend)
        # Hard rule: not available at bucket start
        if state_avail <= bstart:
            look_ahead_violations += 1

        ex_evs = exact_ix.get(bstart, [])
        bd_evs = band_ix.get(bstart, [])

        def summarize(evs: list[WallFlowEvent]) -> dict[str, Any]:
            hit_qty = sum(e.attributed_hit_qty for e in evs)
            hit_notional = sum(e.attributed_hit_notional for e in evs)
            hit_count = sum(e.attributed_trade_count for e in evs)
            refill = sum(e.net_refill_qty for e in evs)
            pull = sum(e.residual_pull_qty for e in evs)
            deplete = sum(e.net_depletion_qty for e in evs)
            conf = "HIGH"
            for e in evs:
                conf = _worst_conf(conf, e.attribution_confidence)
            if not evs:
                conf = "HIGH"
            max_in = state_avail
            for e in evs:
                ea = _as_dt(e.attribution_available_at)
                if ea > max_in:
                    max_in = ea
            q_after = evs[-1].queue_after if evs else None
            return {
                "hit_qty": hit_qty,
                "hit_notional": hit_notional,
                "hit_trade_count": hit_count,
                "net_refill_qty": refill,
                "residual_pull_qty": pull,
                "net_depletion_qty": deplete,
                "confidence": conf,
                "max_input_available_at": max_in,
                "queue_after": q_after,
                "trade_ids": [tid for e in evs for tid in e.attributed_trade_ids],
            }

        sx = summarize(ex_evs)
        sb = summarize(bd_evs)
        dur = (bend - bstart).total_seconds()

        # interarrivals within band hits this bucket (exchange times of attributed trades unknown at row;
        # use interval ends as proxy samples when multiple hits)
        inter: list[float] = []
        for e in bd_evs:
            if e.attributed_trade_count:
                last_hit_times.append(_as_dt(e.interval_end_exchange_time))
        if len(last_hit_times) >= 2:
            # only newest gaps in this bucket
            times = sorted(last_hit_times)[-max(2, sb["hit_trade_count"] + 1) :]
            for a, b in zip(times, times[1:]):
                inter.append((b - a).total_seconds() * 1000.0)

        if bend > wall_touch_at or state_avail >= wall_touch_at:
            agg_band = update_aggressor(
                agg_band,
                hit_qty=sb["hit_qty"],
                hit_notional=sb["hit_notional"],
                hit_trade_count=sb["hit_trade_count"],
                interval_duration_s=dur,
                exchange_time=bend,
                interarrival_ms_samples=inter,
                buy_hit_qty=sb["hit_qty"],
                sell_hit_qty=0.0,
            )
            agg_exact = update_aggressor(
                agg_exact,
                hit_qty=sx["hit_qty"],
                hit_notional=sx["hit_notional"],
                hit_trade_count=sx["hit_trade_count"],
                interval_duration_s=dur,
                exchange_time=bend,
                interarrival_ms_samples=inter,
                buy_hit_qty=sx["hit_qty"],
                sell_hit_qty=0.0,
            )
            cum_refill += sb["net_refill_qty"]
            cum_pull += sb["residual_pull_qty"]

            q_ex = sx["queue_after"]
            q_bd = sb["queue_after"]
            # carry forward last known queues
            if q_ex is None and rows:
                q_ex = rows[-1]["wall_size_exact"]
            if q_bd is None and rows:
                q_bd = rows[-1]["wall_size_band"]
            if q_ex is None:
                q_ex = queue_exact_at_wall_touch
            if q_bd is None:
                q_bd = queue_band_at_wall_touch

            qdh_exact = update_qdh(
                qdh_exact,
                net_depletion_qty=sx["net_depletion_qty"],
                interval_duration_s=dur,
                current_queue=float(q_ex),
                persistence_ratio=agg_exact.persistence_ratio,
            )
            qdh_band = update_qdh(
                qdh_band,
                net_depletion_qty=sb["net_depletion_qty"],
                interval_duration_s=dur,
                current_queue=float(q_bd),
                persistence_ratio=agg_band.persistence_ratio,
            )

            best_bid = float(st["best_bid"]) if st.get("best_bid") is not None else wall_price - 0.1
            best_ask = float(st["best_ask"]) if st.get("best_ask") is not None else wall_price
            # states lack size at best — use depth proxies if present else 1.0
            bid_sz = float(st.get("bid_depth_2bps") or st.get("n_bid_levels") or 1.0)
            ask_sz = float(st.get("ask_depth_2bps") or st.get("n_ask_levels") or 1.0)
            # Prefer unit sizes for microprice stability when depth notional not size
            if st.get("bid_size") is not None:
                bid_sz = float(st["bid_size"])
            if st.get("ask_size") is not None:
                ask_sz = float(st["ask_size"])

            price_st = update_price_response(
                price_st,
                best_bid=best_bid,
                bid_size=max(bid_sz, EPSILON),
                best_ask=best_ask,
                ask_size=max(ask_sz, EPSILON),
                cumulative_hit_notional_since_wall_touch=agg_band.cumulative_hit_notional,
                cumulative_hit_qty=agg_band.cumulative_hit_qty,
                cumulative_net_refill_qty=cum_refill,
                cumulative_residual_pull_qty=cum_pull,
                current_defended_band_queue=float(q_bd),
                queue_at_wall_touch=queue_band_at_wall_touch,
                wall_price=wall_price,
            )
            if not seeded and state_avail >= wall_touch_at:
                seeded = True
        else:
            q_ex = queue_exact_at_wall_touch
            q_bd = queue_band_at_wall_touch

        max_in = max(sx["max_input_available_at"], sb["max_input_available_at"], state_avail)
        feature_available_at = max_in
        # No backfill from later buckets
        if prev_available is not None and feature_available_at < prev_available:
            # availability must be non-decreasing in timeline emission order for causal rows
            feature_available_at = prev_available
        prev_available = feature_available_at

        conf = _worst_conf(sx["confidence"], sb["confidence"])
        flags = evidence_flags(
            hit_qty=sb["hit_qty"],
            queue_survival=price_st.queue_survival_under_attack,
            net_refill=sb["net_refill_qty"],
            residual_pull=sb["residual_pull_qty"],
            qdh_slope=qdh_band.qdh_base_slope,
            persistence_ratio=agg_band.persistence_ratio,
            progress_bps=price_st.progress_bps,
            microprice_change_bps=price_st.microprice_change_bps,
        )

        # look-ahead vs decision: feature claiming known at detection requires avail <= detection
        # we only flag if feature_available_at somehow precedes incomplete inputs (already handled)

        row = {
            "timestamp": format_utc_z(bstart),
            "bucket_start": format_utc_z(bstart),
            "bucket_end": format_utc_z(bend),
            "bucket_available_at": format_utc_z(state_avail),
            "feature_available_at": format_utc_z(feature_available_at),
            "max_input_available_at": format_utc_z(max_in),
            "exchange_feature_time": format_utc_z(bend),
            "wall_size_exact": float(q_ex) if q_ex is not None else None,
            "wall_size_band": float(q_bd) if q_bd is not None else None,
            "hit_qty": sb["hit_qty"],
            "hit_notional": sb["hit_notional"],
            "hit_trade_count": sb["hit_trade_count"],
            "hit_qty_exact": sx["hit_qty"],
            "hit_notional_exact": sx["hit_notional"],
            "hit_trade_count_exact": sx["hit_trade_count"],
            "buy_hit_qty": sb["hit_qty"],
            "sell_hit_qty": 0.0,
            "net_refill_qty": sb["net_refill_qty"],
            "residual_pull_qty": sb["residual_pull_qty"],
            "net_depletion_qty": sb["net_depletion_qty"],
            "hit_rate": agg_band.hit_rate_qty_per_s,
            "trade_rate": agg_band.trade_rate_per_s,
            "persistence_ratio": agg_band.persistence_ratio,
            "qdh_base": qdh_band.qdh_base,
            "qdh_base_exact_price": qdh_exact.qdh_base,
            "qdh_base_defended_band": qdh_band.qdh_base,
            "qdh_base_slope": qdh_band.qdh_base_slope,
            "qdh_base_peak_so_far": qdh_band.qdh_base_peak_so_far,
            "queue_runway_seconds": qdh_band.queue_runway_seconds,
            "M_persistence": qdh_band.m_persistence,
            "M_OI": qdh_band.m_oi,
            "M_LIQ": qdh_band.m_liq,
            "qdh_toxic_base_only": qdh_band.qdh_toxic_base_only,
            "midprice": price_st.midprice,
            "microprice": price_st.microprice,
            "progress_bps": price_st.progress_bps,
            "impact_efficiency": price_st.impact_efficiency_bps_per_million,
            "absorption_pressure_raw": price_st.absorption_pressure_raw,
            "refill_to_hit_ratio": price_st.refill_to_hit_ratio,
            "pull_to_hit_ratio": price_st.pull_to_hit_ratio,
            "absorption_ratio_normalized": None,
            "absorption_ratio_status": "NOT_CALIBRATED",
            "attribution_confidence": conf,
            "coverage_ok": True,
            "replay_epoch": st.get("replay_epoch"),
            "look_ahead": False,
            "WALL_STATE": WALL_STATE_NOT_CLASSIFIED,
            "evidence_flags": flags,
            "attributed_trade_ids_band": sb["trade_ids"],
            "attributed_trade_ids_exact": sx["trade_ids"],
            "zone_touch_at": format_utc_z(zone_touch_at),
            "wall_touch_at": format_utc_z(wall_touch_at),
            "episode_detection_at": format_utc_z(detection_at),
        }
        row.update({f"evidence_{k}": v for k, v in flags.items()})
        rows.append(row)

    meta = {
        "n_rows": len(rows),
        "look_ahead_violations_bucket_contract": look_ahead_violations,
        "aggressor_band": {
            "cumulative_hit_qty": agg_band.cumulative_hit_qty,
            "cumulative_hit_notional": agg_band.cumulative_hit_notional,
            "cumulative_hit_trades": agg_band.cumulative_hit_trades,
            "final_persistence_ratio": agg_band.persistence_ratio,
        },
        "qdh_band": qdh_to_dict(qdh_band),
        "qdh_exact": qdh_to_dict(qdh_exact),
        "price_response": price_response_to_dict(price_st),
        "WALL_STATE": WALL_STATE_NOT_CLASSIFIED,
    }
    return rows, meta
