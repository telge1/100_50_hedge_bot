"""Causal book reconstruction and 1s L2 samples from raw segments."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterator

from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import MutableBook
from orderbook_analyse.orderbook_v2.book import BookState

ZERO = Decimal("0")
BPS_BANDS = (1, 2, 5, 10, 20, 50)
LEVEL_BANDS = (5, 10, 25, 50, 100, 200)


def _depth_within_bps(levels: list[tuple[Decimal, Decimal]], mid: Decimal, bps: int, *, bid: bool) -> Decimal:
    if mid <= ZERO:
        return ZERO
    width = mid * Decimal(bps) / Decimal(10000)
    total = ZERO
    for price, qty in levels:
        if bid:
            if mid - price <= width:
                total += qty
        else:
            if price - mid <= width:
                total += qty
    return total


def _top_n_qty(levels: list[tuple[Decimal, Decimal]], n: int) -> Decimal:
    return sum((q for _, q in levels[:n]), ZERO)


@dataclass
class SampleRow:
    symbol: str
    ts_ms: int
    best_bid: float
    best_ask: float
    mid: float
    spread: float
    spread_bps: float
    microprice: float
    bid_levels: int
    ask_levels: int
    bid_qty_l10: float
    ask_qty_l10: float
    imbalance_l10: float
    bid_qty_bps10: float
    ask_qty_bps10: float
    imbalance_bps10: float
    bid_wall_price: float | None
    bid_wall_qty: float | None
    ask_wall_price: float | None
    ask_wall_qty: float | None
    source_file: str
    warmup: bool
    # Nearest large wall beyond near-touch noise (for wall-to-wall targets)
    bid_far_wall_price: float | None = None
    bid_far_wall_qty: float | None = None
    ask_far_wall_price: float | None = None
    ask_far_wall_qty: float | None = None


def _dominant_wall(levels: list[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal] | None:
    if not levels:
        return None
    window = levels[:25]
    return max(window, key=lambda x: x[1])


def _dominant_wall_beyond_bps(
    levels: list[tuple[Decimal, Decimal]],
    mid: Decimal,
    *,
    bid: bool,
    min_bps: float = 5.0,
    max_levels: int = 200,
) -> tuple[Decimal, Decimal] | None:
    """Largest wall among levels at least min_bps away from mid (up to max_levels)."""
    if mid <= ZERO or not levels:
        return None
    width = mid * Decimal(str(min_bps)) / Decimal(10000)
    cands: list[tuple[Decimal, Decimal]] = []
    for price, qty in levels[:max_levels]:
        if qty <= ZERO:
            continue
        if bid:
            if mid - price >= width:
                cands.append((price, qty))
        else:
            if price - mid >= width:
                cands.append((price, qty))
    if not cands:
        return None
    return max(cands, key=lambda x: x[1])


def _dominant_wall_skip_near(
    levels: list[tuple[Decimal, Decimal]],
    *,
    skip: int = 5,
    max_levels: int = 200,
) -> tuple[Decimal, Decimal] | None:
    """Largest wall deeper in the book (skip near-touch top levels)."""
    window = [x for x in levels[skip:max_levels] if x[1] > ZERO]
    if not window:
        return None
    return max(window, key=lambda x: x[1])


def sample_from_mutable_book(
    symbol: str,
    ts_ms: int,
    book: MutableBook,
    *,
    source_file: str,
    warmup: bool,
) -> SampleRow | None:
    if not book.is_valid or not book.bids or not book.asks:
        return None
    bids = book.sorted_bids()
    asks = book.sorted_asks()
    bb, bq = bids[0]
    ba, aq = asks[0]
    if bb >= ba:
        return None
    mid = (bb + ba) / 2
    spread = ba - bb
    spread_bps = float(spread / mid * 10000) if mid > ZERO else 0.0
    denom = bq + aq
    micro = float((ba * bq + bb * aq) / denom) if denom > ZERO else float(mid)
    bid_l10 = float(_top_n_qty(bids, 10))
    ask_l10 = float(_top_n_qty(asks, 10))
    imb_l10 = (bid_l10 - ask_l10) / (bid_l10 + ask_l10) if (bid_l10 + ask_l10) > 0 else 0.0
    bid_bps10 = float(_depth_within_bps(bids, mid, 10, bid=True))
    ask_bps10 = float(_depth_within_bps(asks, mid, 10, bid=False))
    imb_bps10 = (
        (bid_bps10 - ask_bps10) / (bid_bps10 + ask_bps10) if (bid_bps10 + ask_bps10) > 0 else 0.0
    )
    bw = _dominant_wall(bids)
    aw = _dominant_wall(asks)
    # Prefer walls >=5bps away; if the 200-level book is too tight (BTC), use deeper skip-near.
    bfw = _dominant_wall_beyond_bps(bids, mid, bid=True, min_bps=5.0) or _dominant_wall_skip_near(
        bids, skip=5
    )
    afw = _dominant_wall_beyond_bps(asks, mid, bid=False, min_bps=5.0) or _dominant_wall_skip_near(
        asks, skip=5
    )
    return SampleRow(
        symbol=symbol,
        ts_ms=ts_ms,
        best_bid=float(bb),
        best_ask=float(ba),
        mid=float(mid),
        spread=float(spread),
        spread_bps=spread_bps,
        microprice=micro,
        bid_levels=len(bids),
        ask_levels=len(asks),
        bid_qty_l10=bid_l10,
        ask_qty_l10=ask_l10,
        imbalance_l10=imb_l10,
        bid_qty_bps10=bid_bps10,
        ask_qty_bps10=ask_bps10,
        imbalance_bps10=imb_bps10,
        bid_wall_price=None if bw is None else float(bw[0]),
        bid_wall_qty=None if bw is None else float(bw[1]),
        ask_wall_price=None if aw is None else float(aw[0]),
        ask_wall_qty=None if aw is None else float(aw[1]),
        source_file=source_file,
        warmup=warmup,
        bid_far_wall_price=None if bfw is None else float(bfw[0]),
        bid_far_wall_qty=None if bfw is None else float(bfw[1]),
        ask_far_wall_price=None if afw is None else float(afw[0]),
        ask_far_wall_qty=None if afw is None else float(afw[1]),
    )


def sample_from_book(
    symbol: str,
    ts_ms: int,
    book: BookState,
    *,
    source_file: str,
    warmup: bool,
) -> SampleRow | None:
    """Compat wrapper for immutable BookState used in unit tests."""
    mb = MutableBook()
    mb.bids = dict(book.bids)
    mb.asks = dict(book.asks)
    mb.last_u = book.last_u
    mb.last_seq = book.last_seq
    mb.is_valid = book.is_valid
    return sample_from_mutable_book(symbol, ts_ms, mb, source_file=source_file, warmup=warmup)


def iter_causal_samples(
    ref: SegmentRef,
    *,
    sample_ms: int = 1000,
    warmup_ms: int = 60_000,
) -> Iterator[SampleRow]:
    from orderbook_analyse.ob200_v3_raw_discovery.audit import process_segment

    _, samples = process_segment(
        ref, collect_samples=True, sample_ms=sample_ms, warmup_ms=warmup_ms
    )
    yield from samples


def sample_row_to_dict(row: SampleRow) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "ts_ms": row.ts_ms,
        "best_bid": row.best_bid,
        "best_ask": row.best_ask,
        "mid": row.mid,
        "spread": row.spread,
        "spread_bps": row.spread_bps,
        "microprice": row.microprice,
        "bid_levels": row.bid_levels,
        "ask_levels": row.ask_levels,
        "bid_qty_l10": row.bid_qty_l10,
        "ask_qty_l10": row.ask_qty_l10,
        "imbalance_l10": row.imbalance_l10,
        "bid_qty_bps10": row.bid_qty_bps10,
        "ask_qty_bps10": row.ask_qty_bps10,
        "imbalance_bps10": row.imbalance_bps10,
        "bid_wall_price": row.bid_wall_price,
        "bid_wall_qty": row.bid_wall_qty,
        "ask_wall_price": row.ask_wall_price,
        "ask_wall_qty": row.ask_wall_qty,
        "source_file": row.source_file,
        "warmup": row.warmup,
    }
