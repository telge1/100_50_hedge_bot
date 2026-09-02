"""Frozen liquidation_flow_facts_v1 transformation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from research.btc_ob_fight.liquidation_flow_contract import (
    EVENT_KEY_VERSION,
    LIQUIDATION_FLOW_CONTRACT,
    LIQUIDATION_FLOW_CONTRACT_FROZEN,
    map_bybit_position_side,
)

from .contracts import PROCESSOR_VERSION

if LIQUIDATION_FLOW_CONTRACT != "liquidation_flow_facts_v1":
    raise RuntimeError("unexpected liquidation contract")
if LIQUIDATION_FLOW_CONTRACT_FROZEN is not True:
    raise RuntimeError("liquidation contract must be frozen")

LIQUIDATION_COLUMNS = (
    "symbol",
    "event_time",
    "receive_time",
    "position_side_raw",
    "liquidated_position_side",
    "forced_flow",
    "executed_base_size",
    "bankruptcy_price",
    "bankruptcy_reference_quote",
    "execution_price",
    "execution_notional",
    "event_key",
    "event_key_version",
    "source_id",
    "source_contract_version",
    "processor_version",
    "ingestion_batch_id",
    "ingested_at",
    "quality_flags",
    "coverage_status",
    "finalization_status",
)


def transform_liquidations(
    source_rows: list[tuple],
    *,
    symbol: str,
    batch_id: str,
    ingested_at: datetime,
) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for (
        event_key,
        event_time,
        receive_time,
        raw_side,
        source_liquidated_side,
        size,
        bankruptcy_price,
        _source_inserted_at,
    ) in source_rows:
        event_key = str(event_key)
        if event_key in seen:
            raise ValueError(f"duplicate logical liquidation key: {event_key}")
        seen.add(event_key)
        mapping = map_bybit_position_side(str(raw_side))
        if mapping["liquidated_position_side"] != str(source_liquidated_side):
            raise ValueError(f"liquidation side conflict: {event_key}")
        size_d = Decimal(str(size))
        price_d = Decimal(str(bankruptcy_price))
        if size_d <= 0 or price_d <= 0:
            raise ValueError(f"invalid liquidation values: {event_key}")
        out.append(
            (
                symbol,
                event_time,
                receive_time,
                mapping["position_side_raw"],
                mapping["liquidated_position_side"],
                mapping["forced_trade_direction"],
                size_d,
                price_d,
                size_d * price_d,
                None,
                None,
                event_key,
                EVENT_KEY_VERSION,
                "CH_ALL_LIQUIDATIONS",
                LIQUIDATION_FLOW_CONTRACT,
                PROCESSOR_VERSION,
                batch_id,
                ingested_at,
                [],
                "COMPLETE",
                "FINALIZED",
            )
        )
    return out
