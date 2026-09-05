"""Reuse AEF dual-impact efficiency — no reimplementation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import side_vwap
from orderbook_analyse.aggressor_efficiency_flip.compression import (
    compression_notional,
    evaluate_compression,
)
from orderbook_analyse.aggressor_efficiency_flip.contracts import AEFConfig, aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.impact import DualImpact, measure_dual_impact
from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket, Trade
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.models import InputEvent


def measure_event_efficiency(
    *,
    event: InputEvent,
    buckets: dict[datetime, SecondBucket],
    trades: list[Trade],
    cfg: AEFConfig,
    past_notionals: Optional[list[float]] = None,
    past_shares: Optional[list[float]] = None,
    past_contemp: Optional[list[float]] = None,
    past_posts: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Map AEF DualImpact + CompressionDecision onto stage-1 efficiency fields."""
    side = aggressor_side(event.direction) if event.direction in {"LONG", "SHORT"} else None
    if side is None:
        return {"efficiency_status": "UNKNOWN_DIRECTION", "compression_flag": None}

    t0, t1 = event.flow_start_ts, event.flow_end_ts
    post_s = cfg.post_flow_seconds
    t2 = t1 + timedelta(seconds=post_s)
    # Prefer event.decision_ts if it matches post close; else use t2
    if event.decision_ts and event.decision_ts >= t2:
        t2_use = t2
    else:
        t2_use = t2

    try:
        dual: DualImpact = measure_dual_impact(
            buckets,
            t0=t0,
            t1=t1,
            t2=t2_use,
            side=side,
            reclaim_bps=cfg.reclaim_bps,
            strong_post_bps=cfg.strong_post_followthrough_bps,
        )
    except ValueError as e:
        return {
            "efficiency_status": "UNKNOWN_DATA",
            "efficiency_error": str(e),
            "compression_flag": None,
            "flow_t0": t0.isoformat().replace("+00:00", "Z"),
            "flow_t1": t1.isoformat().replace("+00:00", "Z"),
            "post_t2": t2_use.isoformat().replace("+00:00", "Z"),
        }

    dec = evaluate_compression(
        dual,
        direction=event.direction,
        cfg=cfg,
        past_notionals=past_notionals or [],
        past_shares=past_shares or [],
        past_contemp_impacts=past_contemp or [],
        past_post_follows=past_posts or [],
    )
    # If upstream AEF already confirmed compression, preserve that gate result
    # (rank history is episode-global; empty past must not silently un-confirm).
    upstream_allowed = event.meta.get("aef_allowed")
    if upstream_allowed is not None:
        upstream_bool = str(upstream_allowed).lower() in {"true", "1", "yes"}
        compression_flag = upstream_bool
        compression_reason = str(event.meta.get("aef_reason_code") or dec.reason_code)
        semantic = str(event.meta.get("aef_semantic_case") or dec.semantic_case)
    else:
        compression_flag = bool(dec.allowed)
        compression_reason = dec.reason_code
        semantic = dec.semantic_case

    notion = compression_notional(dual, side)
    vwap = side_vwap(trades, t0, t1, side)
    trade_count = dual.flow.buy_count if side == "Buy" else dual.flow.sell_count
    impact_per_100k = None
    if notion and notion > 0 and dual.same_side_contemporaneous_bps == dual.same_side_contemporaneous_bps:
        impact_per_100k = dual.same_side_contemporaneous_bps / (notion / 100_000.0)

    return {
        "efficiency_status": "OK",
        "aggressor_side": side,
        "flow_t0": t0.isoformat().replace("+00:00", "Z"),
        "flow_t1": t1.isoformat().replace("+00:00", "Z"),
        "post_t2": t2_use.isoformat().replace("+00:00", "Z"),
        # A. FLOW / CONTEMPORANEOUS
        "aggressor_notional": notion,
        "aggressor_trade_count": trade_count,
        "aggressor_vwap": vwap,
        "signed_price_impact_bps": dual.contemporaneous_return_bps,
        "favorable_progress_bps": dual.same_side_contemporaneous_bps,
        "adverse_progress_bps": (
            dual.contemporaneous_up_bps if side == "Sell" else dual.contemporaneous_down_bps
        ),
        "impact_per_100k_notional": impact_per_100k,
        "compression_flag": compression_flag,
        "compression_reason_code": compression_reason,
        "semantic_case": semantic,
        "strong_same_side_impact_veto": dec.strong_same_side_impact_veto,
        "delayed_continuation_veto": dec.delayed_continuation_veto,
        "ordinal_compression_score": dec.ordinal_compression_score,
        "dominant_share": dual.flow.dominant_share(),
        "aef_reeval_allowed": bool(dec.allowed),
        "aef_reeval_reason": dec.reason_code,
        # B. POST-FLOW
        "post_signed_return_bps": dual.post_flow_return_bps,
        "post_max_favorable_bps": dual.post_same_side_followthrough_bps,
        "post_max_adverse_bps": dual.post_counter_move_bps,
        "post_continuation_flag": dual.delayed_initiative_flag,
        "post_reversal_flag": dual.reclaim_flag,
        "post_empty": dual.post_empty,
        # C. counter placeholders (filled by caller if available)
        "counter_flow_found": None,
        "counter_flow_start_ts": None,
        "counter_flow_notional": None,
        "counter_flow_efficiency": None,
        "counter_attach_reason": "not_evaluated_in_stage1_default",
    }
