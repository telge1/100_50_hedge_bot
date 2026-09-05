"""Adapters: AEF compression episodes → InputEvent; synthetic fixtures."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import (
    wall_side_for_aef_direction,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.models import InputEvent


def input_from_aef_compression(row: dict[str, Any], *, source: str = "aef_compression") -> InputEvent:
    """Adapt an AEF compression_events row. Does NOT invent a pool edge."""
    direction = str(row["direction"]).upper()
    wall = wall_side_for_aef_direction(direction)
    t0 = parse_utc(row["t0"])
    t1 = parse_utc(row["t1"])
    t2 = parse_utc(row["t2"])
    return InputEvent(
        event_id=str(row.get("episode_id") or f"{row.get('symbol')}_{row['t0']}_{direction}"),
        symbol=str(row.get("symbol") or "").upper(),
        direction=direction,
        wall_side=wall,
        edge_price=None,
        edge_source="inferred_direction_only",
        edge_confidence="none",
        flow_start_ts=t0,
        flow_end_ts=t1,
        decision_ts=t2,
        reference_price=None,
        data_quality="OK" if str(row.get("allowed")).lower() in {"true", "1"} else "DEGRADED",
        source=source,
        meta={
            "aef_allowed": row.get("allowed"),
            "aef_reason_code": row.get("reason_code"),
            "aef_semantic_case": row.get("semantic_case"),
            "aef_notional": row.get("notional"),
            "aggressor_side": aggressor_side(direction),
            "note": "wall_side inferred from AEF direction; edge_price absent → Acceptance UNKNOWN_EDGE",
        },
    )


def synthetic_event(
    *,
    event_id: str,
    symbol: str,
    direction: str,
    wall_side: Optional[str],
    edge_price: Optional[float],
    flow_start_ts: datetime,
    flow_end_ts: datetime,
    decision_ts: datetime,
    edge_source: str = "synthetic_fixture",
    edge_confidence: str = "high",
    reference_price: Optional[float] = None,
    meta: Optional[dict[str, Any]] = None,
) -> InputEvent:
    return InputEvent(
        event_id=event_id,
        symbol=symbol.upper(),
        direction=direction.upper(),
        wall_side=wall_side.upper() if wall_side else None,
        edge_price=edge_price,
        edge_source=edge_source,
        edge_confidence=edge_confidence if edge_price is not None else "none",
        flow_start_ts=flow_start_ts,
        flow_end_ts=flow_end_ts,
        decision_ts=decision_ts,
        reference_price=reference_price,
        data_quality="OK",
        source="synthetic_fixture",
        meta=meta or {},
    )
