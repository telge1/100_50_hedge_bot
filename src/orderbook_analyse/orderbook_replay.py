"""Stateful orderbook reconstruction from ClickHouse orderbook_deltas rows.

Read-only: this module never writes to ClickHouse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping, Sequence


class ReplayError(RuntimeError):
    """Raised when reconstruction cannot proceed safely."""


@dataclass(frozen=True)
class BookLevelEvent:
    exchange_ts: datetime
    side: str  # bid | ask
    price: Decimal
    quantity: Decimal
    message_type: str  # snapshot | delta
    update_id: int
    cross_sequence: int
    level_index: int


@dataclass
class OrderBookState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_update_id: int | None = None
    last_seq: int | None = None
    has_snapshot: bool = False
    last_exchange_ts: datetime | None = None

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.last_seq = None
        self.has_snapshot = False
        self.last_exchange_ts = None

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def mid_price(self) -> Decimal | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / Decimal("2")

    def spread(self) -> Decimal | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return ba - bb

    def active_level_count(self) -> int:
        return len(self.bids) + len(self.asks)

    def summary(self) -> dict[str, Any]:
        mid = self.mid_price()
        return {
            "best_bid": _dec_str(self.best_bid()),
            "best_ask": _dec_str(self.best_ask()),
            "mid_price": _dec_str(mid),
            "spread": _dec_str(self.spread()),
            "active_bid_levels": len(self.bids),
            "active_ask_levels": len(self.asks),
            "active_levels": self.active_level_count(),
            "last_update_id": self.last_update_id,
            "last_seq": self.last_seq,
            "last_exchange_ts": self.last_exchange_ts.isoformat()
            if self.last_exchange_ts
            else None,
        }


def _dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def event_from_row(row: Mapping[str, Any] | Sequence[Any]) -> BookLevelEvent:
    if isinstance(row, Mapping):
        return BookLevelEvent(
            exchange_ts=_ensure_utc(row["exchange_ts"]),
            side=str(row["side"]),
            price=_as_decimal(row["price"]),
            quantity=_as_decimal(row["quantity"]),
            message_type=str(row["message_type"]),
            update_id=int(row["update_id"]),
            cross_sequence=int(row["cross_sequence"]),
            level_index=int(row["level_index"]),
        )
    (
        exchange_ts,
        side,
        price,
        quantity,
        message_type,
        update_id,
        cross_sequence,
        level_index,
    ) = row
    return BookLevelEvent(
        exchange_ts=_ensure_utc(exchange_ts),
        side=str(side),
        price=_as_decimal(price),
        quantity=_as_decimal(quantity),
        message_type=str(message_type),
        update_id=int(update_id),
        cross_sequence=int(cross_sequence),
        level_index=int(level_index),
    )


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def sort_key(event: BookLevelEvent) -> tuple:
    return (
        event.exchange_ts,
        event.cross_sequence,
        event.update_id,
        0 if event.side == "bid" else 1,
        event.level_index,
    )


def group_messages(
    events: Iterable[BookLevelEvent],
) -> Iterator[tuple[str, int, int, datetime, list[BookLevelEvent]]]:
    """Group level rows that belong to the same Bybit orderbook message."""
    current_key: tuple[str, int, int, datetime] | None = None
    bucket: list[BookLevelEvent] = []
    for event in sorted(events, key=sort_key):
        key = (event.message_type, event.update_id, event.cross_sequence, event.exchange_ts)
        if current_key is None:
            current_key = key
            bucket = [event]
            continue
        if key == current_key:
            bucket.append(event)
            continue
        yield (*current_key, bucket)
        current_key = key
        bucket = [event]
    if current_key is not None and bucket:
        yield (*current_key, bucket)


class OrderBookReplayer:
    """Apply snapshot/delta messages with Bybit-style sequence checks."""

    def __init__(self) -> None:
        self.book = OrderBookState()

    def apply_message(
        self,
        message_type: str,
        update_id: int,
        cross_sequence: int,
        exchange_ts: datetime,
        levels: Sequence[BookLevelEvent],
    ) -> None:
        if message_type == "snapshot":
            self.book.bids.clear()
            self.book.asks.clear()
            self.book.last_update_id = update_id
            self.book.last_seq = cross_sequence
            self.book.has_snapshot = True
        elif message_type == "delta":
            if not self.book.has_snapshot or self.book.last_update_id is None:
                raise ReplayError("delta before snapshot")
            expected = self.book.last_update_id + 1
            if update_id != expected:
                raise ReplayError(
                    f"update_id gap: expected {expected}, got {update_id}"
                )
            if self.book.last_seq is not None and cross_sequence < self.book.last_seq:
                raise ReplayError(
                    f"cross_sequence moved backwards: {self.book.last_seq} -> {cross_sequence}"
                )
            self.book.last_update_id = update_id
            self.book.last_seq = cross_sequence
        else:
            raise ReplayError(f"unsupported message_type={message_type!r}")

        for event in levels:
            self._apply_level(event)
        self.book.last_exchange_ts = exchange_ts

    def _apply_level(self, event: BookLevelEvent) -> None:
        book_side = self.book.bids if event.side == "bid" else self.book.asks
        if event.side not in {"bid", "ask"}:
            raise ReplayError(f"invalid side={event.side!r}")
        if event.quantity == 0:
            book_side.pop(event.price, None)
        else:
            book_side[event.price] = event.quantity

    def replay(self, events: Iterable[BookLevelEvent]) -> OrderBookState:
        for message_type, update_id, seq, ts, levels in group_messages(events):
            self.apply_message(message_type, update_id, seq, ts, levels)
        if not self.book.has_snapshot:
            raise ReplayError("no snapshot applied during replay")
        return self.book


def replay_until(
    events: Iterable[BookLevelEvent],
    *,
    as_of: datetime | None = None,
) -> OrderBookState:
    """Replay events, ignoring anything strictly after ``as_of`` if set."""
    replayer = OrderBookReplayer()
    cutoff = _ensure_utc(as_of) if as_of is not None else None
    filtered: list[BookLevelEvent] = []
    for event in events:
        if cutoff is not None and event.exchange_ts > cutoff:
            continue
        filtered.append(event)
    return replayer.replay(filtered)


def clone_book(book: OrderBookState) -> OrderBookState:
    return OrderBookState(
        bids=dict(book.bids),
        asks=dict(book.asks),
        last_update_id=book.last_update_id,
        last_seq=book.last_seq,
        has_snapshot=book.has_snapshot,
        last_exchange_ts=book.last_exchange_ts,
    )
