"""OB200 compact-array storage rows and causal one-second facts."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .contracts import (
    OB200_CONTRACT_VERSION,
    OB200_KEY_VERSION,
    PROCESSOR_VERSION,
    RESEARCH_CONTRACT_VERSION,
)
from .ob200_parser import FullBookEvent
from .source_file_registry import SourceFile


def snapshot_row(
    *,
    symbol: str,
    event: FullBookEvent,
    source: SourceFile,
    batch_id: str,
    ingested_at: datetime,
) -> tuple[Any, ...]:
    return (
        symbol,
        event.event_time,
        event.receive_time,
        event.exchange_sequence,
        event.update_id,
        event.raw_event_type,
        [p for p, _ in event.bids],
        [q for _, q in event.bids],
        [p for p, _ in event.asks],
        [q for _, q in event.asks],
        len(event.bids),
        len(event.asks),
        1,
        0,
        source.source_file_id,
        source.relative_path,
        event.source_record,
        source.fingerprint,
        "FS_RAW_OB200_V3",
        OB200_CONTRACT_VERSION,
        str(source.manifest.get("parser_version", "ob200_v3")),
        PROCESSOR_VERSION,
        batch_id,
        ingested_at,
        list(event.quality_flags),
        "COMPLETE",
        "FINALIZED",
        event.event_key,
        OB200_KEY_VERSION,
        event.content_fingerprint,
    )


SNAPSHOT_COLUMNS = (
    "symbol",
    "event_time",
    "receive_time",
    "exchange_sequence",
    "update_id",
    "raw_event_type",
    "bid_prices",
    "bid_sizes",
    "ask_prices",
    "ask_sizes",
    "bid_level_count",
    "ask_level_count",
    "is_genuine",
    "is_carried_forward",
    "source_file_id",
    "source_segment",
    "source_record",
    "source_fingerprint",
    "source_id",
    "source_contract_version",
    "parser_version",
    "processor_version",
    "ingestion_batch_id",
    "ingested_at",
    "quality_flags",
    "coverage_status",
    "finalization_status",
    "event_key",
    "key_version",
    "content_fingerprint",
)


def build_orderbook_seconds(
    symbol: str,
    start: datetime,
    end: datetime,
    events: list[FullBookEvent],
    batch_id: str,
    ingested_at: datetime,
) -> list[tuple[Any, ...]]:
    if not events:
        raise ValueError("cannot build OB seconds without events")
    by_second: dict[datetime, FullBookEvent] = {}
    for event in events:
        second = event.event_time.replace(microsecond=0)
        by_second[second] = event

    out: list[tuple[Any, ...]] = []
    current: FullBookEvent | None = None
    for event in events:
        if event.event_time < start:
            current = event
        else:
            break
    bucket = start.replace(microsecond=0)
    while bucket < end:
        genuine = bucket in by_second
        if genuine:
            current = by_second[bucket]
        if current is None:
            raise ValueError(f"no causal book state at {bucket.isoformat()}")
        bids, asks = current.bids, current.asks
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
        bid50, ask50 = bids[:50], asks[:50]
        bid_qty = sum((q for _, q in bid50), Decimal("0"))
        ask_qty = sum((q for _, q in ask50), Decimal("0"))
        denom = bid_qty + ask_qty
        imbalance = float((bid_qty - ask_qty) / denom) if denom else 0.0
        out.append(
            (
                symbol,
                bucket,
                mid,
                best_bid,
                best_ask,
                spread,
                float(spread / mid * Decimal("10000")),
                bid_qty,
                ask_qty,
                imbalance,
                sum((p * q for p, q in bid50), Decimal("0")),
                sum((p * q for p, q in ask50), Decimal("0")),
                len(bids),
                len(asks),
                int(genuine),
                int(not genuine),
                current.event_time,
                current.update_id,
                "CONTIGUOUS_U",
                "FS_RAW_OB200_V3",
                OB200_CONTRACT_VERSION,
                PROCESSOR_VERSION,
                batch_id,
                ingested_at,
                [] if genuine else ["carried_forward"],
                "COMPLETE",
                "FINALIZED",
                f"{symbol}|{bucket.isoformat()}|{RESEARCH_CONTRACT_VERSION}",
            )
        )
        bucket += timedelta(seconds=1)
    return out


ORDERBOOK_SECOND_COLUMNS = (
    "symbol",
    "bucket_time",
    "mid",
    "best_bid",
    "best_ask",
    "spread_abs",
    "spread_bps",
    "bid_qty_l50",
    "ask_qty_l50",
    "imbalance_l50",
    "bid_notional_l50",
    "ask_notional_l50",
    "bid_level_count",
    "ask_level_count",
    "is_genuine",
    "is_carried_forward",
    "source_snapshot_time",
    "last_update_id",
    "sequence_status",
    "source_id",
    "source_contract_version",
    "processor_version",
    "ingestion_batch_id",
    "ingested_at",
    "quality_flags",
    "coverage_status",
    "finalization_status",
    "bucket_key",
)
