"""Canonical public-trade transformation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import PROCESSOR_VERSION, TRADE_CONTRACT_VERSION

TRADE_COLUMNS = (
    "symbol",
    "event_time",
    "receive_time",
    "trade_id",
    "price",
    "base_size",
    "quote_notional",
    "taker_side",
    "source_id",
    "source_contract_version",
    "processor_version",
    "ingestion_batch_id",
    "ingested_at",
    "quality_flags",
    "coverage_status",
    "finalization_status",
    "event_key",
)


def transform_trades(
    source_rows: list[tuple],
    *,
    symbol: str,
    batch_id: str,
    ingested_at: datetime,
) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for trade_id, event_time, receive_time, price, size, notional, side, source in source_rows:
        trade_id = str(trade_id)
        if trade_id in seen:
            raise ValueError(f"duplicate logical trade_id: {trade_id}")
        seen.add(trade_id)
        side = str(side)
        if side not in {"Buy", "Sell"}:
            raise ValueError(f"unknown taker side: {side}")
        if price <= 0 or size <= 0 or notional <= 0:
            raise ValueError(f"nonpositive trade values: {trade_id}")
        out.append(
            (
                symbol,
                event_time,
                receive_time,
                trade_id,
                price,
                size,
                notional,
                side,
                f"CH_PUBLIC_TRADES_CANONICAL:{source}",
                TRADE_CONTRACT_VERSION,
                PROCESSOR_VERSION,
                batch_id,
                ingested_at,
                [],
                "COMPLETE",
                "FINALIZED",
                f"{symbol}|{trade_id}",
            )
        )
    return out
