"""Liquidation semantics, timestamps, OI state, 5s buckets. No I/O."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from . import (
    CATEGORY,
    EXCHANGE,
    LIQUIDATED_LONG,
    LIQUIDATED_SHORT,
    SOURCE_WS,
)

OI_FIELDS = (
    "open_interest",
    "open_interest_value",
    "single_open_interest",
    "single_open_interest_value",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ms_to_dt(ms: Any) -> datetime | None:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def floor_5s(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % 5), tz=timezone.utc)


def interpret_liquidated_position_side(position_side_raw: object) -> str | None:
    """Bybit allLiquidation `S` is the liquidated position side, not aggressor.

    S=Buy  → LONG was liquidated
    S=Sell → SHORT was liquidated
    """
    if position_side_raw == "Buy":
        return LIQUIDATED_LONG
    if position_side_raw == "Sell":
        return LIQUIDATED_SHORT
    return None


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def liquidation_event_key(
    *,
    symbol: str,
    event_time: datetime,
    side: str,
    size: Decimal,
    price: Decimal,
) -> str:
    ms = int(event_time.timestamp() * 1000)
    return f"{EXCHANGE}|{symbol}|{ms}|{side}|{size}|{price}"


def parse_liquidation_records(
    msg: dict[str, Any],
    *,
    received_at: datetime,
    collector_instance_id: str,
) -> list[dict[str, Any]]:
    data = msg.get("data")
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    topic = str(msg.get("topic") or "")
    system_ts = ms_to_dt(msg.get("ts")) or received_at
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_side = item.get("S")
        interpreted = interpret_liquidated_position_side(raw_side)
        if interpreted is None:
            continue
        symbol = str(item.get("s") or "")
        size = to_decimal(item.get("v"))
        price = to_decimal(item.get("p"))
        if not symbol or size is None or price is None:
            continue
        event_time = ms_to_dt(item.get("T")) or system_ts
        event_key = liquidation_event_key(
            symbol=symbol, event_time=event_time, side=str(raw_side), size=size, price=price
        )
        rows.append(
            {
                "exchange": EXCHANGE,
                "category": CATEGORY,
                "symbol": symbol,
                "event_time": event_time,
                "system_generated_at": system_ts,
                "received_at": received_at,
                "position_side_raw": str(raw_side),
                "liquidated_position_side": interpreted,
                "size": size,
                "bankruptcy_price": price,
                "notional_estimate": size * price,
                "source_topic": topic,
                "event_key": event_key,
                "raw_payload_hash": sha256_hex(canonical_json(item)),
                "collector_instance_id": collector_instance_id,
                "inserted_at": received_at,
            }
        )
    return rows


def extract_ticker_payload(msg: dict[str, Any]) -> dict[str, Any] | None:
    data = msg.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


@dataclass
class OIState:
    symbol: str
    valid: bool = False
    event_time: datetime | None = None
    received_at: datetime | None = None
    cross_sequence: int | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None
    single_open_interest: Decimal | None = None
    single_open_interest_value: Decimal | None = None
    last_price: Decimal | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    funding_rate: Decimal | None = None
    last_message_type: str | None = None
    source_topic: str = ""
    _oi_fingerprint: tuple[Any, ...] | None = None

    def invalidate(self) -> None:
        self.valid = False
        self._oi_fingerprint = None

    def oi_fingerprint(self) -> tuple[Any, ...]:
        return (
            self.open_interest,
            self.open_interest_value,
            self.single_open_interest,
            self.single_open_interest_value,
        )

    def _apply_fields(self, payload: dict[str, Any]) -> None:
        mapping = {
            "openInterest": "open_interest",
            "openInterestValue": "open_interest_value",
            "singleOpenInterest": "single_open_interest",
            "singleOpenInterestValue": "single_open_interest_value",
            "lastPrice": "last_price",
            "markPrice": "mark_price",
            "indexPrice": "index_price",
            "fundingRate": "funding_rate",
        }
        for src, dst in mapping.items():
            if src not in payload:
                continue
            parsed = to_decimal(payload[src])
            if parsed is None and payload[src] in (None, ""):
                continue
            if parsed is not None:
                setattr(self, dst, parsed)
        symbol = payload.get("symbol")
        if isinstance(symbol, str) and symbol:
            self.symbol = symbol

    def apply_ticker(
        self,
        msg: dict[str, Any],
        *,
        received_at: datetime,
    ) -> dict[str, Any]:
        """Return action: ignored_no_snapshot | no_change | change | initialized."""
        payload = extract_ticker_payload(msg)
        if payload is None:
            return {"action": "parse_error", "reason": "missing_data"}
        msg_type = str(msg.get("type") or "")
        event_time = ms_to_dt(msg.get("ts")) or received_at
        cs_raw = msg.get("cs")
        if cs_raw is None:
            cs_raw = payload.get("cs")
        try:
            cs = int(cs_raw) if cs_raw is not None and cs_raw != "" else None
        except (TypeError, ValueError):
            cs = None
        topic = str(msg.get("topic") or "")

        if msg_type == "snapshot":
            self._apply_fields(payload)
            self.event_time = event_time
            self.received_at = received_at
            self.cross_sequence = cs
            self.last_message_type = "snapshot"
            self.source_topic = topic
            self.valid = self.open_interest is not None and self.open_interest_value is not None
            prev = self._oi_fingerprint
            self._oi_fingerprint = self.oi_fingerprint() if self.valid else None
            changed = self.valid and prev != self._oi_fingerprint
            return {
                "action": "initialized" if self.valid else "snapshot_incomplete",
                "changed": changed,
                "msg_type": msg_type,
            }

        if msg_type == "delta":
            if not self.valid:
                return {"action": "ignored_no_snapshot", "changed": False, "msg_type": msg_type}
            self._apply_fields(payload)
            self.event_time = event_time
            self.received_at = received_at
            if cs is not None:
                self.cross_sequence = cs
            self.last_message_type = "delta"
            self.source_topic = topic
            new_fp = self.oi_fingerprint()
            changed = new_fp != self._oi_fingerprint
            self._oi_fingerprint = new_fp
            return {"action": "change" if changed else "no_change", "changed": changed, "msg_type": msg_type}

        return {"action": "ignored_unknown_type", "changed": False, "msg_type": msg_type}

    def change_event_row(self, collector_instance_id: str) -> dict[str, Any] | None:
        if not self.valid or self.event_time is None or self.received_at is None:
            return None
        if self.open_interest is None or self.open_interest_value is None:
            return None
        key = (
            f"{EXCHANGE}|{self.symbol}|{int(self.event_time.timestamp()*1000)}|"
            f"{self.open_interest}|{self.open_interest_value}|{self.cross_sequence}"
        )
        return {
            "exchange": EXCHANGE,
            "category": CATEGORY,
            "symbol": self.symbol,
            "event_time": self.event_time,
            "received_at": self.received_at,
            "cross_sequence": self.cross_sequence,
            "open_interest": self.open_interest,
            "open_interest_value": self.open_interest_value,
            "single_open_interest": self.single_open_interest,
            "single_open_interest_value": self.single_open_interest_value,
            "last_price": self.last_price,
            "mark_price": self.mark_price,
            "index_price": self.index_price,
            "funding_rate": self.funding_rate,
            "message_type": self.last_message_type or "",
            "source_topic": self.source_topic,
            "state_valid": 1,
            "event_key": key,
            "collector_instance_id": collector_instance_id,
            "inserted_at": self.received_at,
        }

    def snapshot_5s_row(
        self, *, bucket_time: datetime, now: datetime, collector_instance_id: str
    ) -> dict[str, Any] | None:
        if not self.valid or self.event_time is None or self.received_at is None:
            return None
        if self.open_interest is None or self.open_interest_value is None:
            return None
        age_ms = int((now - self.received_at).total_seconds() * 1000)
        return {
            "exchange": EXCHANGE,
            "category": CATEGORY,
            "symbol": self.symbol,
            "bucket_time": bucket_time,
            "source_event_time": self.event_time,
            "received_at": now,
            "open_interest": self.open_interest,
            "open_interest_value": self.open_interest_value,
            "single_open_interest": self.single_open_interest,
            "single_open_interest_value": self.single_open_interest_value,
            "last_price": self.last_price,
            "mark_price": self.mark_price,
            "index_price": self.index_price,
            "state_age_ms": age_ms,
            "state_valid": 1,
            "source": SOURCE_WS,
            "collector_instance_id": collector_instance_id,
            "inserted_at": now,
        }


@dataclass
class DedupCache:
    """In-process dedup. Two theoretically identical liquidations can collide."""

    max_size: int = 50_000
    seen: set[str] = field(default_factory=set)
    order: list[str] = field(default_factory=list)

    def check_and_add(self, key: str) -> bool:
        """Return True if this is a new key."""
        if key in self.seen:
            return False
        self.seen.add(key)
        self.order.append(key)
        if len(self.order) > self.max_size:
            old = self.order.pop(0)
            self.seen.discard(old)
        return True
