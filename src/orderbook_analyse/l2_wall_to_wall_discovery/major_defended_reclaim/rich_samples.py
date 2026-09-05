"""Per-level rich book samples from raw OB200 replay (genuine L2, not aggregate proxy)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.util import (
    band_around,
    in_band,
    median,
    notional,
)
from orderbook_analyse.ob200_v3_raw_discovery.audit import (
    is_replayable_line,
    iter_decompressed_lines,
    line_to_replay_payload,
    load_manifest_light,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef, list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import ZERO, MutableBook


@dataclass
class SideWallSnap:
    price: float
    qty: float
    notional: float
    relative_size: float | None
    median_notional: float | None
    n_levels: int
    band_low: float
    band_high: float
    source: str = "per_level_mutable_book"


@dataclass
class RichSample:
    symbol: str
    ts_ms: int
    mid: float
    best_bid: float
    best_ask: float
    bid_levels: int
    ask_levels: int
    seq_gap: bool
    carried_forward: bool
    warmup: bool
    genuine: bool
    bid_wall: SideWallSnap | None
    ask_wall: SideWallSnap | None
    source_file: str


def _side_snap(
    levels: list[tuple[Decimal, Decimal]],
    *,
    mid: float,
    tick: float,
    side: str,
) -> SideWallSnap | None:
    if not levels or mid <= 0:
        return None
    notionals = [notional(float(p), float(q)) for p, q in levels if q > ZERO]
    if not notionals:
        return None
    med = median(notionals)
    # largest notional level on this side within visible book
    best_i = max(range(len(levels)), key=lambda i: notional(float(levels[i][0]), float(levels[i][1])))
    px = float(levels[best_i][0])
    qty = float(levels[best_i][1])
    wn = notional(px, qty)
    if qty <= 0 or wn <= 0:
        return None
    # wall must be on correct side of mid
    if side == "BID" and px > mid:
        return None
    if side == "ASK" and px < mid:
        return None
    low, high = band_around(px, tick, 2)
    rel = (wn / med) if med and med > 0 else None
    return SideWallSnap(
        price=px,
        qty=qty,
        notional=wn,
        relative_size=rel,
        median_notional=med,
        n_levels=len(levels),
        band_low=low,
        band_high=high,
    )


def _qty_in_band(levels: list[tuple[Decimal, Decimal]], low: float, high: float) -> float:
    total = 0.0
    for p, q in levels:
        fp = float(p)
        if low <= fp <= high:
            total += float(q)
    return total


def wall_notional_in_band(
    levels: list[tuple[Decimal, Decimal]], low: float, high: float
) -> float:
    total = 0.0
    for p, q in levels:
        fp = float(p)
        if low <= fp <= high:
            total += notional(fp, float(q))
    return total


def extract_rich_sample(
    symbol: str,
    ts_ms: int,
    book: MutableBook,
    *,
    source_file: str,
    warmup: bool,
    seq_gap: bool,
    carried_forward: bool,
) -> RichSample | None:
    if not book.is_valid or not book.bids or not book.asks:
        return None
    bids = book.sorted_bids()
    asks = book.sorted_asks()
    bb, _ = bids[0]
    ba, _ = asks[0]
    if bb >= ba:
        return None
    mid = float((bb + ba) / 2)
    tick = tick_size(symbol)
    return RichSample(
        symbol=symbol,
        ts_ms=ts_ms,
        mid=mid,
        best_bid=float(bb),
        best_ask=float(ba),
        bid_levels=len(bids),
        ask_levels=len(asks),
        seq_gap=seq_gap,
        carried_forward=carried_forward,
        warmup=warmup,
        genuine=not carried_forward and not seq_gap and book.is_valid,
        bid_wall=_side_snap(bids, mid=mid, tick=tick, side="BID"),
        ask_wall=_side_snap(asks, mid=mid, tick=tick, side="ASK"),
        source_file=source_file,
    )


def replay_rich_samples(
    raw_root: Path,
    *,
    symbols: tuple[str, ...],
    start: datetime,
    end: datetime,
    sample_ms: int = 250,
    warmup_ms: int = 300_000,
) -> dict[str, list[RichSample]]:
    """Causal per-level samples from closed segments only (no open TMP)."""
    out: dict[str, list[RichSample]] = {s: [] for s in symbols}
    segments = list_closed_segments(
        raw_root, symbols=symbols, start=start, end=end, include_boundary_stubs=False
    )
    for i, ref in enumerate(segments, 1):
        print(f"  rich replay {i}/{len(segments)} {ref.path.name}", flush=True)
        out[ref.symbol].extend(
            list(_replay_one(ref, sample_ms=sample_ms, warmup_ms=warmup_ms))
        )
    for sym in symbols:
        out[sym].sort(key=lambda s: s.ts_ms)
    return out


def _replay_one(
    ref: SegmentRef, *, sample_ms: int, warmup_ms: int
) -> Iterator[RichSample]:
    book = MutableBook()
    last_emit: int | None = None
    sample_first: int | None = None
    gap_latched = False
    for _line, obj in iter_decompressed_lines(ref.path):
        if not is_replayable_line(obj):
            continue
        payload = line_to_replay_payload(obj)
        data = payload.get("data") or {}
        mtype = payload.get("type")
        ts = obj.get("ts")
        if mtype == "snapshot":
            book.apply_snapshot(data)
            gap_latched = False
        elif mtype == "delta":
            warns = book.apply_delta(data)
            if any(w.startswith("seq_gap") for w in warns):
                gap_latched = True
        else:
            continue
        if not isinstance(ts, int) or not book.is_valid:
            continue
        if sample_first is None:
            sample_first = ts
        bucket = (ts // sample_ms) * sample_ms
        if last_emit is not None and bucket <= last_emit:
            continue
        last_emit = bucket
        warmup = (ts - sample_first) < warmup_ms
        # carried_forward: no — we only emit on real archive events advancing the bucket
        row = extract_rich_sample(
            ref.symbol,
            bucket,
            book,
            source_file=str(ref.path),
            warmup=warmup,
            seq_gap=gap_latched,
            carried_forward=False,
        )
        if row is not None:
            yield row


def levels_at_sample_via_book(
    book: MutableBook, side: str
) -> list[tuple[Decimal, Decimal]]:
    if side == "BID":
        return book.sorted_bids()
    return book.sorted_asks()


def segment_quality_note(ref: SegmentRef) -> dict[str, Any]:
    mp = ref.manifest_path
    note: dict[str, Any] = {"path": str(ref.path), "manifest_present": mp.is_file()}
    if mp.is_file():
        m = load_manifest_light(mp)
        note["sequence_gap_count"] = m.get("sequence_gap_count")
        note["event_count"] = m.get("event_count")
    return note
