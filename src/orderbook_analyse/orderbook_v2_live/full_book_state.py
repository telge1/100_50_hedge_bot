"""In-memory Bybit full-depth orderbook (RAM only, no persistence)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome, classify_live_delta


FULL_DEPTH = 0  # sentinel depth for on-demand / socket contract
MAX_UI_BARS_PER_SIDE = 600
DEFAULT_CLIP_PCT = 50.0  # drop fantasy extremes for display span; raw counts kept

# RPI is excluded from Bybit Full-OB REST and WS (documented coverage limit).
RPI_INCLUDED_IN_FULL_OB = False


@dataclass
class ConsistentBookSnapshot:
    """Atomic copy of one book version. Safe to use without `_book_lock`."""

    symbol: str
    bids: dict[float, float]
    asks: dict[float, float]
    update_id: int | None
    seq: int | None
    event_ts_ms: int | None
    cts_ms: int | None
    receive_time_ns: int | None
    book_ready: bool
    last_event_at: datetime | None = None

    def mid(self) -> float | None:
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def full_levels(self) -> tuple[list[list[float]], list[list[float]]]:
        """All levels, no 1000-cap. Bids desc, asks asc."""
        bids = [[p, q] for p, q in sorted(self.bids.items(), key=lambda x: x[0], reverse=True)]
        asks = [[p, q] for p, q in sorted(self.asks.items(), key=lambda x: x[0])]
        return bids, asks


@dataclass
class FullBookState:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    update_id: int | None = None
    seq: int | None = None
    event_ts_ms: int | None = None
    cts_ms: int | None = None
    last_event_at: datetime | None = None
    last_receive_time_ns: int | None = None
    snapshot_loaded: bool = False
    book_ready: bool = False  # True only after aligned REST+buffer sync

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.update_id = None
        self.seq = None
        self.event_ts_ms = None
        self.cts_ms = None
        self.last_receive_time_ns = None
        self.snapshot_loaded = False
        self.book_ready = False

    def apply_snapshot(
        self,
        *,
        bids: list,
        asks: list,
        u: int | None,
        seq: int | None,
        ts_ms: int | None,
        cts_ms: int | None = None,
        receive_time_ns: int | None = None,
        mark_ready: bool = True,
    ) -> None:
        self.bids = {_f(p): _f(q) for p, q in _iter_levels(bids) if _f(q) > 0}
        self.asks = {_f(p): _f(q) for p, q in _iter_levels(asks) if _f(q) > 0}
        self.update_id = int(u) if u is not None else None
        self.seq = int(seq) if seq is not None else None
        self.event_ts_ms = int(ts_ms) if ts_ms is not None else None
        self.cts_ms = int(cts_ms) if cts_ms is not None else None
        self.last_event_at = datetime.now(timezone.utc)
        if receive_time_ns is not None:
            self.last_receive_time_ns = int(receive_time_ns)
        self.snapshot_loaded = True
        self.book_ready = bool(mark_ready)

    def apply_delta(
        self,
        *,
        bids: list,
        asks: list,
        u: int | None,
        seq: int | None,
        ts_ms: int | None,
        cts_ms: int | None = None,
        receive_time_ns: int | None = None,
        enforce_continuity: bool = True,
    ) -> DeltaOutcome:
        """Apply delta per Bybit Full-OB live rules. Never invent levels."""
        if not self.snapshot_loaded or self.update_id is None:
            return DeltaOutcome.NOT_READY
        if u is None:
            return DeltaOutcome.NOT_READY
        event_u = int(u)
        event_seq = int(seq) if seq is not None else None
        if enforce_continuity:
            outcome = classify_live_delta(
                local_u=self.update_id,
                event_u=event_u,
                event_seq=event_seq,
                local_seq=self.seq,
            )
            if outcome is not DeltaOutcome.APPLIED:
                return outcome
        for p, q in _iter_levels(bids):
            price, qty = _f(p), _f(q)
            if qty <= 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        for p, q in _iter_levels(asks):
            price, qty = _f(p), _f(q)
            if qty <= 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
        self.update_id = event_u
        if event_seq is not None:
            self.seq = event_seq
        if ts_ms is not None:
            self.event_ts_ms = int(ts_ms)
        if cts_ms is not None:
            self.cts_ms = int(cts_ms)
        self.last_event_at = datetime.now(timezone.utc)
        if receive_time_ns is not None:
            self.last_receive_time_ns = int(receive_time_ns)
        self.book_ready = True
        return DeltaOutcome.APPLIED

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    def copy_consistent_snapshot(self) -> ConsistentBookSnapshot:
        """Shallow-copy maps + metadata from the current version. Call under lock."""
        return ConsistentBookSnapshot(
            symbol=self.symbol,
            bids=dict(self.bids),
            asks=dict(self.asks),
            update_id=self.update_id,
            seq=self.seq,
            event_ts_ms=self.event_ts_ms,
            cts_ms=self.cts_ms,
            receive_time_ns=self.last_receive_time_ns,
            book_ready=self.book_ready,
            last_event_at=self.last_event_at,
        )


def _f(v: Any) -> float:
    return float(v)


def _iter_levels(levels: list) -> list[tuple[Any, Any]]:
    out: list[tuple[Any, Any]] = []
    for row in levels or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append((row[0], row[1]))
        elif isinstance(row, dict):
            out.append((row.get("price"), row.get("size") or row.get("qty")))
    return out


def aggregate_full_book(
    state: FullBookState | ConsistentBookSnapshot,
    *,
    max_bars_per_side: int = MAX_UI_BARS_PER_SIDE,
    clip_pct: float = DEFAULT_CLIP_PCT,
) -> dict[str, Any]:
    """Bucket full book for UI. Keeps raw counts; clips extremes for bucket span.

    Does not cap at 1000 levels. Chart bars are a display aggregation only.
    """
    mid = state.mid()
    raw_bids = sorted(state.bids.items(), key=lambda x: x[0], reverse=True)
    raw_asks = sorted(state.asks.items(), key=lambda x: x[0])
    raw_bid_count = len(raw_bids)
    raw_ask_count = len(raw_asks)
    if mid is None or mid <= 0 or not raw_bids or not raw_asks:
        return {
            "symbol": state.symbol,
            "depth": FULL_DEPTH,
            "book_mode": "full",
            "aggregated": True,
            "raw_bid_count": raw_bid_count,
            "raw_ask_count": raw_ask_count,
            "mid": mid,
            "best_bid": state.best_bid(),
            "best_ask": state.best_ask(),
            "bids": [],
            "asks": [],
            "bucket_size": None,
            "clip_pct": clip_pct,
            "coverage_bid_low": raw_bids[-1][0] if raw_bids else None,
            "coverage_ask_high": raw_asks[-1][0] if raw_asks else None,
        }

    lo = mid * (1.0 - clip_pct / 100.0)
    hi = mid * (1.0 + clip_pct / 100.0)
    bids = [(p, q) for p, q in raw_bids if p >= lo]
    asks = [(p, q) for p, q in raw_asks if p <= hi]
    if not bids:
        bids = raw_bids[: max_bars_per_side]
    if not asks:
        asks = raw_asks[: max_bars_per_side]

    span_lo = bids[-1][0]
    span_hi = asks[-1][0]
    span = max(span_hi - span_lo, mid * 1e-6)
    bucket = span / max(2 * max_bars_per_side, 1)

    return {
        "symbol": state.symbol,
        "depth": FULL_DEPTH,
        "book_mode": "full",
        "aggregated": True,
        "raw_bid_count": raw_bid_count,
        "raw_ask_count": raw_ask_count,
        "mid": mid,
        "best_bid": state.best_bid(),
        "best_ask": state.best_ask(),
        "bids": _bucket_side(bids, bucket, side="bid", max_bars=max_bars_per_side),
        "asks": _bucket_side(asks, bucket, side="ask", max_bars=max_bars_per_side),
        "bucket_size": bucket,
        "clip_pct": clip_pct,
        "coverage_bid_low": raw_bids[-1][0],
        "coverage_ask_high": raw_asks[-1][0],
        "display_bid_low": span_lo,
        "display_ask_high": span_hi,
        "update_id": state.update_id,
        "seq": state.seq,
    }


def _bucket_side(
    levels: list[tuple[float, float]],
    bucket: float,
    *,
    side: str,
    max_bars: int,
) -> list[dict[str, Any]]:
    if bucket <= 0 or not levels:
        return []
    buckets: dict[float, list[float]] = {}
    for price, qty in levels:
        if side == "bid":
            key = (price // bucket) * bucket
        else:
            key = ((price + bucket * 0.999999) // bucket) * bucket
        slot = buckets.setdefault(key, [0.0, 0.0, 0])  # size, notional, count
        slot[0] += qty
        slot[1] += price * qty
        slot[2] += 1
    items = []
    for key, (size, notional, count) in buckets.items():
        if size <= 0:
            continue
        vwap = notional / size
        items.append(
            {
                "price": vwap,
                "size": size,
                "side": side,
                "bucket_low": key,
                "bucket_high": key + bucket,
                "raw_level_count": int(count),
            }
        )
    items.sort(key=lambda x: x["price"], reverse=(side == "bid"))
    return items[:max_bars]


def full_orderbook_topic(symbol: str) -> str:
    return f"orderbook.full.{str(symbol).strip().upper()}"


def parse_full_orderbook_topic(topic: str) -> str | None:
    parts = str(topic or "").split(".")
    if len(parts) != 3 or parts[0] != "orderbook" or parts[1] != "full":
        return None
    sym = parts[2].strip().upper()
    return sym or None
