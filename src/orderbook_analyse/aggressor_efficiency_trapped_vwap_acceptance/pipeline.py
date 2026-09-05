"""Per-event pipeline: efficiency (AEF) + trap + acceptance + combined."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket, Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second, iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.combined_state import (
    build_decision_ladder,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance import (
    evaluate_edge_acceptance,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.efficiency import (
    measure_event_efficiency,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import strip_trades
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.models import InputEvent
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.outcomes import (
    compute_forward_outcomes,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.trapped_vwap import (
    compute_aggressor_vwap_block,
    evaluate_trap_checkpoints,
)


def process_event(
    event: InputEvent,
    *,
    buckets: dict[datetime, SecondBucket],
    trades: list[Trade],
    cfg: TrapAcceptConfig,
    as_of: Optional[datetime] = None,
    data_end: Optional[datetime] = None,
    past_ranks: Optional[dict[str, list[float]]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (features_decisions, forward_outcomes). Outcomes never feed features."""
    aef = cfg.aef_config()
    past = past_ranks or {}
    eff = measure_event_efficiency(
        event=event,
        buckets=buckets,
        trades=trades,
        cfg=aef,
        past_notionals=past.get("notionals"),
        past_shares=past.get("shares"),
        past_contemp=past.get("contemp"),
        past_posts=past.get("posts"),
    )

    side = eff.get("aggressor_side") or (
        aggressor_side(event.direction) if event.direction in {"LONG", "SHORT"} else None
    )
    vwap_block = (
        compute_aggressor_vwap_block(
            trades,
            flow_start=event.flow_start_ts,
            flow_end=event.flow_end_ts,
            side=side,
        )
        if side
        else {
            "aggressor_vwap_valid": False,
            "aggressor_trades": [],
            "duplicate_trade_count": 0,
            "missing_trade_id_count": 0,
        }
    )

    trap = (
        evaluate_trap_checkpoints(
            buckets=buckets,
            aggressor_trades=vwap_block.get("aggressor_trades") or [],
            side=side,
            vwap=vwap_block.get("aggressor_vwap"),
            decision_ts=event.decision_ts,
            cfg=cfg,
            as_of=as_of,
        )
        if side and vwap_block.get("aggressor_vwap_valid")
        else {
            "trap_status": "UNKNOWN_DATA",
            "checkpoints": {},
            "final_trap_label": "UNKNOWN_DATA",
        }
    )

    # Edge confidence gate: inferred_direction_only → UNKNOWN_EDGE for acceptance
    edge_conf = event.edge_confidence
    edge_price = event.edge_price
    if event.edge_source in {"inferred_direction_only", "none"} or edge_price is None:
        edge_price = None
        edge_conf = "none"

    acceptance = evaluate_edge_acceptance(
        buckets=buckets,
        trades=trades,
        symbol=event.symbol,
        wall_side=event.wall_side if edge_price is not None else None,
        edge_price=edge_price,
        edge_confidence=edge_conf,
        decision_ts=event.decision_ts,
        aggressor_side=side or "Buy",
        cfg=cfg,
        as_of=as_of,
    )

    ladder = build_decision_ladder(eff, trap, acceptance)

    # diagnostic earliest entry: first closed 1s after decision (research only)
    entry_close = floor_second(event.decision_ts) + timedelta(seconds=1)
    entry_bucket = buckets.get(entry_close - timedelta(seconds=1))
    entry_px = entry_bucket.last_price if entry_bucket else None

    join_meta = (event.meta or {}).get("edge_join") or {}

    feat: dict[str, Any] = {
        "event_id": event.event_id,
        "symbol": event.symbol,
        "direction": event.direction,
        "wall_side": event.wall_side,
        "edge_price": event.edge_price,
        "edge_source": event.edge_source,
        "edge_confidence": event.edge_confidence,
        "edge_join_status": join_meta.get("edge_join_status"),
        "matched_edge_id": join_meta.get("matched_edge_id"),
        "matched_edge_price": join_meta.get("matched_edge_price"),
        "matched_edge_source": join_meta.get("matched_edge_source"),
        "matched_edge_available_ts": join_meta.get("matched_edge_available_ts"),
        "matched_edge_distance_bps": join_meta.get("matched_edge_distance_bps"),
        "matched_edge_age_seconds": join_meta.get("matched_edge_age_seconds"),
        "matched_edge_persistence_seconds": join_meta.get("matched_edge_persistence_seconds"),
        "matched_edge_relative_size": join_meta.get("matched_edge_relative_size"),
        "edge_match_explanation_codes": join_meta.get("edge_match_explanation_codes"),
        "edge_match_confidence_class": join_meta.get("edge_match_confidence_class"),
        "edge_match_candidate_count": join_meta.get("edge_match_candidate_count"),
        "flow_start_ts": iso_z(event.flow_start_ts),
        "flow_end_ts": iso_z(event.flow_end_ts),
        "decision_ts": iso_z(event.decision_ts),
        "source": event.source,
        "data_quality": event.data_quality,
        **{k: v for k, v in eff.items() if k != "aggressor_trades"},
        "vwap_block_valid": vwap_block.get("aggressor_vwap_valid"),
        "vwap_data_coverage": vwap_block.get("vwap_data_coverage"),
        "duplicate_trade_count": vwap_block.get("duplicate_trade_count"),
        "missing_trade_id_count": vwap_block.get("missing_trade_id_count"),
        "trap_vwap": vwap_block.get("aggressor_vwap"),
        "trap_status": trap.get("trap_status"),
        "final_trap_label": trap.get("final_trap_label"),
        "trap_checkpoints": trap.get("checkpoints"),
        "acceptance_status": acceptance.get("acceptance_status"),
        "final_acceptance_state": acceptance.get("final_acceptance_state"),
        "acceptance_checkpoints": acceptance.get("checkpoints"),
        "acceptance_state_at_5s": acceptance.get("acceptance_state_at_5s"),
        "acceptance_state_at_10s": acceptance.get("acceptance_state_at_10s"),
        "acceptance_state_at_30s": acceptance.get("acceptance_state_at_30s"),
        "acceptance_state_at_60s": acceptance.get("acceptance_state_at_60s"),
        **ladder,
        "diagnostic_earliest_entry_ts": iso_z(entry_close),
        "diagnostic_earliest_entry_price": entry_px,
        "feature_version": cfg.feature_version,
        "causal_contract_version": cfg.causal_contract_version,
        "as_of": iso_z(as_of) if as_of else None,
    }

    feat = strip_trades(feat)

    outcomes = compute_forward_outcomes(
        event_id=event.event_id,
        symbol=event.symbol,
        direction=event.direction if event.direction in {"LONG", "SHORT"} else "LONG",
        entry_ts=entry_close,
        entry_price=entry_px,
        buckets=buckets,
        data_end=data_end or as_of or (event.decision_ts + timedelta(hours=1)),
    )
    return feat, outcomes
