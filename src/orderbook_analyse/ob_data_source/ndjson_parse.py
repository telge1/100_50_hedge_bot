"""Parse Bybit orderbook.200 NDJSON messages into BookLevelEvent rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import orjson

from orderbook_analyse.orderbook_replay import BookLevelEvent


class Ob200ParseError(ValueError):
    """Invalid OB200 NDJSON message or line."""


def ms_to_utc(ms: Any) -> datetime:
    try:
        value = int(ms)
    except (TypeError, ValueError) as exc:
        raise Ob200ParseError(f"invalid millisecond timestamp: {ms!r}") from exc
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def _as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise Ob200ParseError(f"invalid decimal: {value!r}") from exc


@dataclass(frozen=True)
class Ob200Message:
    """One WS orderbook message plus source provenance (cts kept for audit)."""

    symbol: str
    message_type: str
    exchange_ts: datetime
    matching_engine_ts: datetime | None
    update_id: int
    cross_sequence: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    source_file: str
    source_line: int
    raw_ts_ms: int
    raw_cts_ms: int | None

    def dedupe_key(self) -> tuple[Any, ...]:
        return (
            self.symbol,
            self.message_type,
            self.raw_ts_ms,
            self.raw_cts_ms,
            self.update_id,
            self.cross_sequence,
        )

    def to_book_level_events(self) -> list[BookLevelEvent]:
        events: list[BookLevelEvent] = []
        for idx, (price, qty) in enumerate(self.bids):
            events.append(
                BookLevelEvent(
                    exchange_ts=self.exchange_ts,
                    side="bid",
                    price=price,
                    quantity=qty,
                    message_type=self.message_type,
                    update_id=self.update_id,
                    cross_sequence=self.cross_sequence,
                    level_index=idx,
                )
            )
        for idx, (price, qty) in enumerate(self.asks):
            events.append(
                BookLevelEvent(
                    exchange_ts=self.exchange_ts,
                    side="ask",
                    price=price,
                    quantity=qty,
                    message_type=self.message_type,
                    update_id=self.update_id,
                    cross_sequence=self.cross_sequence,
                    level_index=idx,
                )
            )
        return events


def _parse_side_levels(levels: Any, *, side: str) -> tuple[tuple[Decimal, Decimal], ...]:
    if levels is None:
        return ()
    if not isinstance(levels, list):
        raise Ob200ParseError(f"{side} levels must be a list, got {type(levels).__name__}")
    out: list[tuple[Decimal, Decimal]] = []
    for item in levels:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise Ob200ParseError(f"invalid {side} level: {item!r}")
        out.append((_as_decimal(item[0]), _as_decimal(item[1])))
    return tuple(out)


def parse_ob200_obj(
    obj: Any,
    *,
    expected_symbol: str | None = None,
    source_file: str = "",
    source_line: int = 0,
) -> Ob200Message:
    if not isinstance(obj, dict):
        raise Ob200ParseError(f"message must be object, got {type(obj).__name__}")
    msg_type = obj.get("type")
    if msg_type not in ("snapshot", "delta"):
        raise Ob200ParseError(f"unsupported type={msg_type!r}")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise Ob200ParseError("data must be an object")
    symbol = str(data.get("s") or "")
    if not symbol:
        raise Ob200ParseError("missing data.s symbol")
    if expected_symbol is not None and symbol != expected_symbol:
        raise Ob200ParseError(
            f"symbol mismatch: expected {expected_symbol}, got {symbol} "
            f"({source_file}:{source_line})"
        )
    try:
        update_id = int(data["u"])
        seq = int(data["seq"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Ob200ParseError(f"invalid u/seq: {exc}") from exc
    if "ts" not in obj:
        raise Ob200ParseError("missing ts")
    raw_ts = int(obj["ts"])
    exchange_ts = ms_to_utc(raw_ts)
    raw_cts: int | None
    matching: datetime | None
    if "cts" in obj and obj["cts"] is not None:
        raw_cts = int(obj["cts"])
        matching = ms_to_utc(raw_cts)
    else:
        raw_cts = None
        matching = None
    return Ob200Message(
        symbol=symbol,
        message_type=str(msg_type),
        exchange_ts=exchange_ts,
        matching_engine_ts=matching,
        update_id=update_id,
        cross_sequence=seq,
        bids=_parse_side_levels(data.get("b"), side="bid"),
        asks=_parse_side_levels(data.get("a"), side="ask"),
        source_file=source_file,
        source_line=source_line,
        raw_ts_ms=raw_ts,
        raw_cts_ms=raw_cts,
    )


def parse_ob200_line(
    line: bytes | str,
    *,
    expected_symbol: str | None = None,
    source_file: str = "",
    source_line: int = 0,
) -> Ob200Message:
    if isinstance(line, str):
        raw = line.strip()
        if not raw:
            raise Ob200ParseError("empty line")
        payload: bytes = raw.encode("utf-8")
    else:
        payload = line.strip()
        if not payload:
            raise Ob200ParseError("empty line")
    try:
        obj = orjson.loads(payload)
    except orjson.JSONDecodeError as exc:
        raise Ob200ParseError(
            f"invalid JSON at {source_file}:{source_line}: {exc}"
        ) from exc
    return parse_ob200_obj(
        obj,
        expected_symbol=expected_symbol,
        source_file=source_file,
        source_line=source_line,
    )
