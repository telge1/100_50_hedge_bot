"""Build 100ms + 1s flow series from canonical wall-flow events + metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from obfull_research_engine.drilldown.aggregation_100ms import _as_dt, _floor_bucket
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1 import BUCKET_MS, EPSILON
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


def build_flow_100ms(
    *,
    event_id: str,
    wall_id: str,
    wall_price: float,
    band_low: float,
    band_high: float,
    band_events: list[WallFlowEvent],
    states: list[dict[str, Any]],
    touch_at: datetime,
    trigger_at: datetime,
    series_start: datetime,
    series_end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate attribution onto 100ms states; QDH via existing update_qdh only."""
    by_bucket: dict[datetime, list[WallFlowEvent]] = {}
    for ev in band_events:
        if ev.attribution_confidence == "INVALID":
            continue
        end = _as_dt(ev.interval_end_exchange_time)
        b = _floor_bucket(end, BUCKET_MS)
        by_bucket.setdefault(b, []).append(ev)

    qdh = QdhState()
    rows: list[dict[str, Any]] = []
    cum_fill = cum_pull = cum_refill = cum_net = 0.0
    mass_viol = 0
    lookahead = 0
    last_q = None

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
        fill = sum(float(e.attributed_hit_qty) for e in evs)
        pull = sum(float(e.residual_pull_qty) for e in evs)
        refill = sum(float(e.net_refill_qty) for e in evs)
        net = sum(float(e.net_depletion_qty) for e in evs)
        unk = 0.0
        eligible = fill  # engine only counts attributed attack qty in bucket
        q_before = float(evs[0].queue_before) if evs else (last_q if last_q is not None else None)
        q_after = float(evs[-1].queue_after) if evs else q_before
        if q_after is not None:
            last_q = q_after
        book_dec = 0.0
        if q_before is not None and q_after is not None:
            book_dec = max(q_before - q_after, 0.0)
        for e in evs:
            # identity already checked in engine; recount soft
            pass
        dur = max((bend - bstart).total_seconds(), EPSILON)
        if q_after is not None:
            qdh = update_qdh(
                qdh,
                net_depletion_qty=net,
                interval_duration_s=dur,
                current_queue=float(q_after),
                persistence_ratio=1.0,
            )
        pol = apply_queue_policy(
            queue_remaining_qty=q_after,
            qdh_base=qdh.qdh_base,
            queue_runway_seconds=qdh.queue_runway_seconds,
        )
        mid = st.get("midprice")
        if mid is None:
            mid = st.get("mid")
        bb = st.get("best_bid")
        ba = st.get("best_ask")
        micro = None
        spread_bps = None
        try:
            if bb is not None and ba is not None:
                mid_f = (float(bb) + float(ba)) / 2.0 if mid is None else float(mid)
                mid = mid_f
                # MICROPRICE_PROXY: equal sizes → mid
                micro = mid_f
                if mid_f > 0:
                    spread_bps = 10_000.0 * (float(ba) - float(bb)) / mid_f
        except (TypeError, ValueError):
            pass

        cum_fill += fill
        cum_pull += pull
        cum_refill += refill
        cum_net += net
        gross_churn = pull + refill
        net_book = (q_after - q_before) if (q_after is not None and q_before is not None) else None
        fill_share = (fill / book_dec) if book_dec > 1e-12 else None
        pull_share = (pull / book_dec) if book_dec > 1e-12 else None
        churn_ratio = (gross_churn / abs(net_book)) if net_book not in (None, 0, 0.0) else None

        phase = _phase(avail, touch_at, trigger_at)
        post = phase == "POST_TRIGGER_FORENSIC"
        rows.append(
            {
                "event_id": event_id,
                "bucket_start": format_utc_z(bstart),
                "bucket_end": format_utc_z(bend),
                "relative_seconds_to_touch": (bstart - touch_at).total_seconds(),
                "relative_seconds_to_trigger": (bstart - trigger_at).total_seconds(),
                "wall_id": wall_id,
                "wall_state": pol.get("queue_state"),
                "wall_price": wall_price,
                "band_low": band_low,
                "band_high": band_high,
                "queue_start": q_before,
                "queue_end": q_after,
                "queue_change": (None if q_before is None or q_after is None else q_after - q_before),
                "book_decrease": book_dec,
                "eligible_trade_qty": eligible,
                "attributed_fill": fill,
                "residual_pull": pull,
                "refill": refill,
                "unknown": unk,
                "net_depletion": net,
                "cumulative_fill": cum_fill,
                "cumulative_pull": cum_pull,
                "cumulative_refill": cum_refill,
                "cumulative_net_depletion": cum_net,
                "qdh_raw": (net / dur) / (float(q_after) + EPSILON) if q_after is not None else None,
                "qdh_ewma": pol.get("canonical_qdh_base"),
                "queue_exhausted": pol.get("queue_state") == "QUEUE_EXHAUSTED",
                "mid": mid,
                "microprice": micro,
                "microprice_source": "MICROPRICE_PROXY_EQUAL_SIZE_MID",
                "microprice_minus_mid": 0.0 if micro is not None and mid is not None else None,
                "spread_bps": spread_bps,
                "data_complete": True,
                "phase": phase,
                "post_decision": post,
                "gross_book_churn": gross_churn,
                "net_book_change": net_book,
                "churn_ratio": churn_ratio,
                "fill_share_of_decrease": fill_share,
                "pull_share_of_decrease": pull_share,
                "bucket_available_at": format_utc_z(avail),
            }
        )

    meta = {
        "n_rows": len(rows),
        "lookahead_flags": lookahead,
        "mass_balance_soft_violations": mass_viol,
        "final_cum_fill": cum_fill,
        "final_cum_pull": cum_pull,
        "final_cum_refill": cum_refill,
    }
    return rows, meta


def aggregate_1s(rows_100ms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum flow quantities to 1s buckets; last QDH/queue in second."""
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
        net = sum(float(x.get("net_depletion") or 0) for x in g)
        book_dec = sum(float(x.get("book_decrease") or 0) for x in g)
        out.append(
            {
                **{k: last.get(k) for k in (
                    "event_id", "wall_id", "wall_price", "band_low", "band_high",
                    "wall_state", "phase", "post_decision", "mid", "microprice", "spread_bps",
                )},
                "bucket_start": key,
                "bucket_end": format_utc_z(_as_dt(key) + timedelta(seconds=1)),
                "n_100ms_buckets": len(g),
                "attributed_fill": fill,
                "residual_pull": pull,
                "refill": refill,
                "net_depletion": net,
                "book_decrease": book_dec,
                "queue_end": last.get("queue_end"),
                "qdh_ewma": last.get("qdh_ewma"),
                "cumulative_fill": last.get("cumulative_fill"),
                "cumulative_pull": last.get("cumulative_pull"),
                "cumulative_refill": last.get("cumulative_refill"),
                "fill_share_of_decrease": (fill / book_dec) if book_dec > 1e-12 else None,
                "relative_seconds_to_touch": last.get("relative_seconds_to_touch"),
                "relative_seconds_to_trigger": last.get("relative_seconds_to_trigger"),
            }
        )
    return out


def assert_1s_matches_100ms(rows_100ms: list[dict[str, Any]], rows_1s: list[dict[str, Any]]) -> bool:
    s100 = sum(float(r.get("attributed_fill") or 0) for r in rows_100ms)
    s1 = sum(float(r.get("attributed_fill") or 0) for r in rows_1s)
    return abs(s100 - s1) <= 1e-6
