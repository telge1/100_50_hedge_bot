"""Canonical public-trade identity and single-pass deduplication.

canonical_trade_key = symbol + trade_id

Does not invent substitute IDs when trade_id is missing — such rows are rejected.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import BUCKET_MS, SYMBOL
from ..level_first_episode1_corrected_sms1_persist_v1.persist import event_available_at


@dataclass(frozen=True)
class CanonicalTrade:
    canonical_trade_key: str
    symbol: str
    trade_id: str
    exchange_event_time: str
    collector_received_at: str | None
    event_available_at: str
    sequence: int | None
    price: float
    size_base: float
    notional_usdt: float
    taker_side: str
    is_block_trade: bool
    source_file: str
    source_record_id: str
    dedup_status: str  # KEPT | DUPLICATE_DROPPED | REJECTED_MISSING_TRADE_ID

    def exchange_dt(self) -> datetime:
        return _as_dt(self.exchange_event_time)

    def available_dt(self) -> datetime:
        return _as_dt(self.event_available_at)


@dataclass
class TradeDedupReport:
    raw_count: int = 0
    unique_count: int = 0
    duplicate_count: int = 0
    rejected_missing_trade_id: int = 0
    identity_rule: str = "canonical_trade_key = symbol + trade_id"
    receive_time_present_count: int = 0
    receive_time_missing_count: int = 0


def _norm_side(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in ("buy", "b", "bid"):
        return "Buy"
    if s in ("sell", "s", "ask"):
        return "Sell"
    return str(value or "")


def canonical_trade_key(symbol: str, trade_id: str) -> str:
    return f"{symbol}|{trade_id}"


def trades_hash(trade_ids: list[str]) -> str:
    payload = "\n".join(trade_ids).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_canonical_trades(
    rows: list[dict[str, Any]],
    *,
    symbol: str = SYMBOL,
    source_file: str,
) -> tuple[list[CanonicalTrade], TradeDedupReport, list[CanonicalTrade]]:
    """Return (kept unique trades, report, dropped duplicate records)."""
    report = TradeDedupReport(raw_count=len(rows))
    prepared: list[CanonicalTrade] = []
    dropped: list[CanonicalTrade] = []
    seen: set[str] = set()

    indexed = list(enumerate(rows))
    indexed.sort(
        key=lambda iv: (
            _as_dt(iv[1].get("trade_ts") or iv[1].get("exchange_event_time")).timestamp(),
            str(iv[1].get("trade_id") or ""),
            iv[0],
        )
    )

    for idx, row in indexed:
        tid = row.get("trade_id")
        if tid is None or str(tid).strip() == "":
            report.rejected_missing_trade_id += 1
            continue
        trade_id = str(tid)
        key = canonical_trade_key(symbol, trade_id)
        exch = _as_dt(row.get("trade_ts") or row.get("exchange_event_time"))
        recv_raw = row.get("collector_received_at") or row.get("ingest_timestamp")
        recv = None if recv_raw in (None, "") else _as_dt(recv_raw)
        # Feature availability: prefer real receive; else research proxy (flagged upstream).
        if recv is not None:
            avail = recv
            report.receive_time_present_count += 1
        else:
            avail = event_available_at(exch, BUCKET_MS)
            report.receive_time_missing_count += 1
        size = float(row.get("size") or row.get("size_base") or 0.0)
        price = float(row["price"])
        notional = float(row["notional"]) if row.get("notional") is not None else price * size
        seq = row.get("sequence")
        if seq is None:
            seq = row.get("seq")
        ct = CanonicalTrade(
            canonical_trade_key=key,
            symbol=symbol,
            trade_id=trade_id,
            exchange_event_time=format_utc_z(exch),
            collector_received_at=None if recv is None else format_utc_z(recv),
            event_available_at=format_utc_z(avail),
            sequence=None if seq is None else int(seq),
            price=price,
            size_base=size,
            notional_usdt=notional,
            taker_side=_norm_side(row.get("taker_side") or row.get("side")),
            is_block_trade=bool(row.get("is_block_trade") or False),
            source_file=source_file,
            source_record_id=str(row.get("source_record_id") or f"{source_file}:{idx}:{trade_id}"),
            dedup_status="KEPT",
        )
        if key in seen:
            report.duplicate_count += 1
            dropped.append(
                CanonicalTrade(
                    canonical_trade_key=ct.canonical_trade_key,
                    symbol=ct.symbol,
                    trade_id=ct.trade_id,
                    exchange_event_time=ct.exchange_event_time,
                    collector_received_at=ct.collector_received_at,
                    event_available_at=ct.event_available_at,
                    sequence=ct.sequence,
                    price=ct.price,
                    size_base=ct.size_base,
                    notional_usdt=ct.notional_usdt,
                    taker_side=ct.taker_side,
                    is_block_trade=ct.is_block_trade,
                    source_file=ct.source_file,
                    source_record_id=ct.source_record_id,
                    dedup_status="DUPLICATE_DROPPED",
                )
            )
            continue
        seen.add(key)
        prepared.append(ct)

    report.unique_count = len(prepared)
    return prepared, report, dropped
