"""Causal market 1s/1m aggregation with explicit source freshness."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .contracts import (
    FUNDING_STATUS,
    PROCESSOR_VERSION,
    RESEARCH_CONTRACT_VERSION,
)

ZERO = Decimal("0")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


MARKET_SECOND_COLUMNS = (
    "symbol", "bucket_time", "last_trade_price", "mid",
    "taker_buy_base", "taker_sell_base", "taker_buy_quote", "taker_sell_quote",
    "trade_count", "taker_delta_base", "open_interest", "oi_delta",
    "oi_freshness_ms", "oi_status", "long_liquidation_base",
    "short_liquidation_base", "forced_buy_base", "forced_sell_base",
    "spread_bps", "imbalance_l50", "ob_is_genuine", "ob_is_carried_forward",
    "funding_status", "source_coverage_mask", "source_id",
    "source_contract_version", "processor_version", "ingestion_batch_id",
    "ingested_at", "quality_flags", "coverage_status", "finalization_status",
    "bucket_key",
)

MARKET_MINUTE_COLUMNS = (
    "symbol", "bucket_time", "open", "high", "low", "close", "volume_base",
    "volume_quote", "taker_buy_base", "taker_sell_base", "taker_delta_base",
    "trade_count", "oi_open", "oi_close", "oi_delta", "long_liquidation_base",
    "short_liquidation_base", "forced_buy_base", "forced_sell_base", "mid_open",
    "mid_close", "spread_bps_mean", "imbalance_l50_mean", "genuine_seconds",
    "carried_forward_seconds", "funding_status", "source_id",
    "source_contract_version", "processor_version", "ingestion_batch_id",
    "ingested_at", "quality_flags", "coverage_status", "finalization_status",
    "bucket_key",
)


def build_market_seconds(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    trades: list[tuple],
    liquidations: list[tuple],
    oi_rows: list[tuple],
    orderbook_rows: list[tuple],
    batch_id: str,
    ingested_at: datetime,
) -> list[tuple[Any, ...]]:
    trade_buckets: dict[datetime, list[tuple]] = defaultdict(list)
    for row in trades:
        trade_buckets[_aware(row[1]).replace(microsecond=0)].append(row)
    liq_buckets: dict[datetime, list[tuple]] = defaultdict(list)
    for row in liquidations:
        liq_buckets[_aware(row[1]).replace(microsecond=0)].append(row)
    ob_by_time = {_aware(row[1]): row for row in orderbook_rows}
    oi_by_time = {
        _aware(row[0]).replace(microsecond=0): row for row in oi_rows
    }

    out: list[tuple[Any, ...]] = []
    last_oi: tuple | None = None
    previous_oi: Decimal | None = None
    bucket = start.replace(microsecond=0)
    while bucket < end:
        if bucket in oi_by_time:
            last_oi = oi_by_time[bucket]
        trows = trade_buckets.get(bucket, [])
        lrows = liq_buckets.get(bucket, [])
        ob = ob_by_time.get(bucket)
        if ob is None:
            raise ValueError(f"missing orderbook second: {bucket.isoformat()}")
        buys = [r for r in trows if str(r[6]) == "Buy"]
        sells = [r for r in trows if str(r[6]) == "Sell"]
        buy_base = sum((Decimal(str(r[4])) for r in buys), ZERO)
        sell_base = sum((Decimal(str(r[4])) for r in sells), ZERO)
        buy_quote = sum((Decimal(str(r[5])) for r in buys), ZERO)
        sell_quote = sum((Decimal(str(r[5])) for r in sells), ZERO)
        last_price = Decimal(str(trows[-1][3])) if trows else None

        oi_value: Decimal | None = None
        oi_delta: Decimal | None = None
        oi_freshness: int | None = None
        oi_status = "MISSING"
        if last_oi is not None:
            source_bucket = _aware(last_oi[0])
            oi_freshness = int((bucket - source_bucket).total_seconds() * 1000)
            if oi_freshness <= 10_000 and int(last_oi[4]) == 1:
                oi_value = Decimal(str(last_oi[1]))
                oi_delta = (
                    oi_value - previous_oi if previous_oi is not None else None
                )
                previous_oi = oi_value
                oi_status = "FRESH"

        long_base = sum(
            (Decimal(str(r[6])) for r in lrows if str(r[4]) == "LIQUIDATED_LONG"),
            ZERO,
        )
        short_base = sum(
            (Decimal(str(r[6])) for r in lrows if str(r[4]) == "LIQUIDATED_SHORT"),
            ZERO,
        )
        mask = 1 | 2 | (4 if oi_status == "FRESH" else 0) | (8 if lrows else 0)
        flags = [] if oi_status == "FRESH" else ["OI_MISSING_OR_STALE"]
        out.append(
            (
                symbol, bucket, last_price, ob[2], buy_base, sell_base,
                buy_quote, sell_quote, len(trows), buy_base - sell_base,
                oi_value, oi_delta, oi_freshness, oi_status, long_base,
                short_base, short_base, long_base, ob[6], ob[9], ob[14], ob[15],
                FUNDING_STATUS, mask, "RESEARCH_CANONICAL_SOURCES",
                RESEARCH_CONTRACT_VERSION, PROCESSOR_VERSION, batch_id,
                ingested_at, flags, "COMPLETE", "FINALIZED",
                f"{symbol}|{bucket.isoformat()}|{RESEARCH_CONTRACT_VERSION}",
            )
        )
        bucket += timedelta(seconds=1)
    return out


def build_market_minutes(
    seconds: list[tuple],
    *,
    symbol: str,
    batch_id: str,
    ingested_at: datetime,
) -> list[tuple[Any, ...]]:
    grouped: dict[datetime, list[tuple]] = defaultdict(list)
    for row in seconds:
        minute = row[1].replace(second=0, microsecond=0)
        grouped[minute].append(row)
    out: list[tuple[Any, ...]] = []
    for minute, rows in sorted(grouped.items()):
        prices = [r[2] for r in rows if r[2] is not None]
        oi_values = [r[10] for r in rows if r[10] is not None]
        spreads = [r[18] for r in rows if r[18] is not None]
        imbalances = [r[19] for r in rows if r[19] is not None]
        buy_base = sum((Decimal(str(r[4])) for r in rows), ZERO)
        sell_base = sum((Decimal(str(r[5])) for r in rows), ZERO)
        buy_quote = sum((Decimal(str(r[6])) for r in rows), ZERO)
        sell_quote = sum((Decimal(str(r[7])) for r in rows), ZERO)
        out.append(
            (
                symbol, minute,
                prices[0] if prices else None,
                max(prices) if prices else None,
                min(prices) if prices else None,
                prices[-1] if prices else None,
                buy_base + sell_base, buy_quote + sell_quote,
                buy_base, sell_base, buy_base - sell_base,
                sum(int(r[8]) for r in rows),
                oi_values[0] if oi_values else None,
                oi_values[-1] if oi_values else None,
                (oi_values[-1] - oi_values[0]) if len(oi_values) >= 2 else None,
                sum((Decimal(str(r[14])) for r in rows), ZERO),
                sum((Decimal(str(r[15])) for r in rows), ZERO),
                sum((Decimal(str(r[16])) for r in rows), ZERO),
                sum((Decimal(str(r[17])) for r in rows), ZERO),
                rows[0][3], rows[-1][3],
                sum(float(v) for v in spreads) / len(spreads) if spreads else None,
                sum(float(v) for v in imbalances) / len(imbalances) if imbalances else None,
                sum(int(r[20] or 0) for r in rows),
                sum(int(r[21] or 0) for r in rows),
                FUNDING_STATUS, "RESEARCH_MARKET_1S", RESEARCH_CONTRACT_VERSION,
                PROCESSOR_VERSION, batch_id, ingested_at, [],
                "COMPLETE", "FINALIZED",
                f"{symbol}|{minute.isoformat()}|{RESEARCH_CONTRACT_VERSION}",
            )
        )
    return out
