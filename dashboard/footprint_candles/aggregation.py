"""Pure footprint aggregation and imbalance math (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .contracts import (
    IMBALANCE_RATIO,
    MIN_COMPARE_SIZE,
    STACKED_MIN_LEVELS,
    FootprintCandle,
    FootprintLevel,
    bucket_bounds,
    bucket_index_for_price,
)


@dataclass
class RawLevelAgg:
    bucket_index: int
    bid_size: float = 0.0
    ask_size: float = 0.0
    bid_notional: float = 0.0
    ask_notional: float = 0.0
    trade_count: int = 0


def accumulate_trade(
    levels: dict[int, RawLevelAgg],
    *,
    price: Decimal | float | str,
    side: str,
    size: float,
    notional: float,
) -> None:
    """Fold one deduplicated trade into per-bucket aggs."""
    idx = bucket_index_for_price(price)
    row = levels.get(idx)
    if row is None:
        row = RawLevelAgg(bucket_index=idx)
        levels[idx] = row
    side_n = str(side).strip()
    if side_n == "Buy":
        row.ask_size += float(size)
        row.ask_notional += float(notional)
    elif side_n == "Sell":
        row.bid_size += float(size)
        row.bid_notional += float(notional)
    else:
        raise ValueError(f"unknown side: {side!r}")
    row.trade_count += 1


def compute_imbalances(
    sorted_indices: list[int],
    by_idx: dict[int, RawLevelAgg],
    *,
    ratio: float = IMBALANCE_RATIO,
    min_size: float = MIN_COMPARE_SIZE,
) -> tuple[dict[int, bool], dict[int, bool]]:
    """Return (ask_imbalance, bid_imbalance) maps.

    Ask-imbalance at i compares ask_vol[i] to bid_vol[i-1] (lower neighbour).
    Bid-imbalance at i compares bid_vol[i] to ask_vol[i+1] (higher neighbour).
    Missing neighbour or zero compare volume → False (no div-by-zero).
    """
    ask_imb: dict[int, bool] = {i: False for i in sorted_indices}
    bid_imb: dict[int, bool] = {i: False for i in sorted_indices}
    index_set = set(sorted_indices)

    for i in sorted_indices:
        cur = by_idx[i]
        lower = i - 1
        if lower in index_set:
            bid_lower = by_idx[lower].bid_size
            if cur.ask_size >= min_size and bid_lower > 0:
                if cur.ask_size / bid_lower >= ratio:
                    ask_imb[i] = True

        higher = i + 1
        if higher in index_set:
            ask_higher = by_idx[higher].ask_size
            if cur.bid_size >= min_size and ask_higher > 0:
                if cur.bid_size / ask_higher >= ratio:
                    bid_imb[i] = True

    return ask_imb, bid_imb


def mark_stacked(
    sorted_indices: list[int],
    flags: dict[int, bool],
    *,
    min_run: int = STACKED_MIN_LEVELS,
) -> dict[int, bool]:
    """Mark levels that participate in a contiguous run of length >= min_run."""
    stacked = {i: False for i in sorted_indices}
    n = len(sorted_indices)
    i = 0
    while i < n:
        if not flags.get(sorted_indices[i], False):
            i += 1
            continue
        j = i
        while j < n and flags.get(sorted_indices[j], False):
            # Contiguous in bucket_index space, not just list adjacency with gaps.
            if j > i and sorted_indices[j] != sorted_indices[j - 1] + 1:
                break
            j += 1
        run_len = j - i
        # Also require no index gaps inside the run (already checked).
        if run_len >= min_run:
            for k in range(i, j):
                stacked[sorted_indices[k]] = True
        i = j if j > i else i + 1
    return stacked


def pick_vpoc_bucket(by_idx: dict[int, RawLevelAgg]) -> int | None:
    """Max total_size; tie → lower bucket_index."""
    if not by_idx:
        return None
    best_idx: int | None = None
    best_vol = -1.0
    for idx in sorted(by_idx.keys()):
        total = by_idx[idx].ask_size + by_idx[idx].bid_size
        if total > best_vol or (total == best_vol and (best_idx is None or idx < best_idx)):
            best_vol = total
            best_idx = idx
    return best_idx


def build_levels_from_raw(
    by_idx: dict[int, RawLevelAgg],
    *,
    highlight_imbalances: bool,
) -> tuple[list[FootprintLevel], float, float, float | None, int | None]:
    """Build sorted levels + candle deltas + vPOC."""
    if not by_idx:
        return [], 0.0, 0.0, None, None

    indices = sorted(by_idx.keys())
    ask_imb, bid_imb = compute_imbalances(indices, by_idx)
    stacked_ask = mark_stacked(indices, ask_imb)
    stacked_bid = mark_stacked(indices, bid_imb)
    vpoc_idx = pick_vpoc_bucket(by_idx)

    if not highlight_imbalances:
        ask_imb = {i: False for i in indices}
        bid_imb = {i: False for i in indices}
        stacked_ask = {i: False for i in indices}
        stacked_bid = {i: False for i in indices}

    levels: list[FootprintLevel] = []
    candle_delta_size = 0.0
    candle_delta_notional = 0.0
    for idx in indices:
        raw = by_idx[idx]
        lo, hi = bucket_bounds(idx)
        delta_s = raw.ask_size - raw.bid_size
        delta_n = raw.ask_notional - raw.bid_notional
        candle_delta_size += delta_s
        candle_delta_notional += delta_n
        levels.append(
            FootprintLevel(
                bucket_index=idx,
                price_low=lo,
                price_high=hi,
                bid_size=raw.bid_size,
                ask_size=raw.ask_size,
                bid_notional=raw.bid_notional,
                ask_notional=raw.ask_notional,
                total_size=raw.ask_size + raw.bid_size,
                total_notional=raw.ask_notional + raw.bid_notional,
                delta_size=delta_s,
                ask_imbalance=bool(ask_imb.get(idx, False)),
                bid_imbalance=bool(bid_imb.get(idx, False)),
                stacked_ask=bool(stacked_ask.get(idx, False)),
                stacked_bid=bool(stacked_bid.get(idx, False)),
                is_vpoc=(idx == vpoc_idx),
            )
        )

    vpoc_price = None
    if vpoc_idx is not None:
        lo, hi = bucket_bounds(vpoc_idx)
        vpoc_price = (lo + hi) / 2.0
    return levels, candle_delta_size, candle_delta_notional, vpoc_price, vpoc_idx


def levels_from_ch_rows(
    rows: Iterable[tuple],
) -> dict[int, RawLevelAgg]:
    """Map CH grouped rows (bucket_index, ask_size, bid_size, ask_n, bid_n, trades) → RawLevelAgg."""
    by_idx: dict[int, RawLevelAgg] = {}
    for row in rows:
        idx = int(row[0])
        ask_s = float(row[1] or 0.0)
        bid_s = float(row[2] or 0.0)
        ask_n = float(row[3] or 0.0)
        bid_n = float(row[4] or 0.0)
        trades = int(row[5] or 0)
        by_idx[idx] = RawLevelAgg(
            bucket_index=idx,
            ask_size=ask_s,
            bid_size=bid_s,
            ask_notional=ask_n,
            bid_notional=bid_n,
            trade_count=trades,
        )
    return by_idx


def build_candle(
    *,
    time: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    by_idx: dict[int, RawLevelAgg],
    coverage: str,
    sources: list[str] | None = None,
) -> FootprintCandle:
    from .coverage import allows_imbalance_highlight

    highlight = allows_imbalance_highlight(coverage)
    levels, d_s, d_n, vpoc_px, vpoc_idx = build_levels_from_raw(
        by_idx, highlight_imbalances=highlight
    )
    trade_count = sum(r.trade_count for r in by_idx.values())
    incomplete = coverage != "COMPLETE"
    return FootprintCandle(
        time=int(time),
        open=float(open_),
        high=float(high),
        low=float(low),
        close=float(close),
        candle_delta_size=d_s,
        candle_delta_notional=d_n,
        vpoc_price=vpoc_px,
        vpoc_bucket_index=vpoc_idx,
        coverage=coverage,
        incomplete=incomplete,
        levels=levels,
        trade_count=trade_count,
        sources=list(sources or []),
    )
