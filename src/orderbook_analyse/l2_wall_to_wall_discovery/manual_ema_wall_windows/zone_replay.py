"""Causal per-level samples with walls inside EMA bands (closed segments only)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.rich_samples import (
    SideWallSnap,
    _side_snap,
)
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.util import median, notional
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    PERCENTILE_MIN,
    REL_SIZE_MIN,
    WALL_LOOKBACK_SAMPLES,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    last_closed_bar_at,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import make_zone
from orderbook_analyse.ob200_v3_raw_discovery.audit import (
    is_replayable_line,
    iter_decompressed_lines,
    line_to_replay_payload,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import ZERO, MutableBook


@dataclass
class ZoneWallSnap:
    side: str
    zone_name: str
    price: float
    qty: float
    notional: float
    relative_size: float | None
    median_book_notional: float | None
    causal_percentile: float | None
    in_zone: bool


@dataclass
class AnalysisSample:
    ts_ms: int
    mid: float
    best_bid: float
    best_ask: float
    bid_levels: int
    ask_levels: int
    genuine: bool
    seq_gap: bool
    carried_forward: bool
    warmup: bool
    ema20: float | None
    ema59: float | None
    atr: float | None
    bid_wall: SideWallSnap | None
    ask_wall: SideWallSnap | None
    ask_in_ema20: ZoneWallSnap | None
    bid_in_ema20: ZoneWallSnap | None
    ask_in_ema59: ZoneWallSnap | None
    bid_in_ema59: ZoneWallSnap | None
    source_file: str
    candle_low: float | None = None
    candle_high: float | None = None


def _strongest_in_band(
    levels: list[tuple[Decimal, Decimal]],
    low: float,
    high: float,
    *,
    mid: float,
    side: str,
) -> tuple[float, float, float] | None:
    best = None
    best_n = -1.0
    for p, q in levels:
        fp = float(p)
        fq = float(q)
        if fq <= 0 or not (low <= fp <= high):
            continue
        if side == "BID" and fp > mid:
            continue
        if side == "ASK" and fp < mid:
            continue
        n = notional(fp, fq)
        if n > best_n:
            best_n = n
            best = (fp, fq, n)
    return best


def _book_median_notional(levels: list[tuple[Decimal, Decimal]]) -> float | None:
    vals = [notional(float(p), float(q)) for p, q in levels if q > ZERO]
    return median(vals) if vals else None


class CausalWallThreshold:
    """Rolling causal notional history for percentile (no future window leakage)."""

    def __init__(self, lookback: int = WALL_LOOKBACK_SAMPLES) -> None:
        self.lookback = lookback
        self._bid: deque[float] = deque(maxlen=lookback)
        self._ask: deque[float] = deque(maxlen=lookback)
        self._sorted_bid: list[float] = []
        self._sorted_ask: list[float] = []
        self._rebuild_every = 500
        self._n = 0

    def observe(self, bid_n: float | None, ask_n: float | None) -> None:
        if bid_n is not None and bid_n > 0:
            self._bid.append(bid_n)
        if ask_n is not None and ask_n > 0:
            self._ask.append(ask_n)
        self._n += 1
        if self._n % self._rebuild_every == 0:
            self._sorted_bid = sorted(self._bid)
            self._sorted_ask = sorted(self._ask)

    def percentile(self, side: str, value: float) -> float | None:
        hist = self._sorted_bid if side == "BID" else self._sorted_ask
        raw = self._bid if side == "BID" else self._ask
        if len(raw) < 50:
            return None
        if not hist or len(hist) != len(raw):
            hist = sorted(raw)
            if side == "BID":
                self._sorted_bid = hist
            else:
                self._sorted_ask = hist
        # fraction of lookback values <= value
        idx = int(np.searchsorted(hist, value, side="right"))
        return idx / len(hist)


def replay_analysis_samples(
    raw_root: Path,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    bars_5m: pd.DataFrame,
    sample_ms: int = 250,
    warmup_ms: int = 300_000,
) -> list[AnalysisSample]:
    segments = list_closed_segments(
        raw_root, symbols=(symbol,), start=start, end=end, include_boundary_stubs=False
    )
    out: list[AnalysisSample] = []
    thr = CausalWallThreshold()
    tick = tick_size(symbol)
    for i, ref in enumerate(segments, 1):
        print(f"  zone replay {i}/{len(segments)} {ref.path.name}", flush=True)
        for row in _replay_one(
            ref,
            bars_5m=bars_5m,
            thr=thr,
            tick=tick,
            sample_ms=sample_ms,
            warmup_ms=warmup_ms,
        ):
            out.append(row)
    out.sort(key=lambda s: s.ts_ms)
    return out


def _ms_to_dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def _zone_wall(
    levels: list[tuple[Decimal, Decimal]],
    *,
    side: str,
    zone_name: str,
    low: float,
    high: float,
    mid: float,
    thr: CausalWallThreshold,
) -> ZoneWallSnap | None:
    hit = _strongest_in_band(levels, low, high, mid=mid, side=side)
    if hit is None:
        return None
    px, qty, wn = hit
    med = _book_median_notional(levels)
    rel = (wn / med) if med and med > 0 else None
    pct = thr.percentile(side, wn)
    return ZoneWallSnap(
        side=side,
        zone_name=zone_name,
        price=px,
        qty=qty,
        notional=wn,
        relative_size=rel,
        median_book_notional=med,
        causal_percentile=pct,
        in_zone=True,
    )


def _replay_one(
    ref,
    *,
    bars_5m: pd.DataFrame,
    thr: CausalWallThreshold,
    tick: float,
    sample_ms: int,
    warmup_ms: int,
) -> Iterator[AnalysisSample]:
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
        if not book.bids or not book.asks:
            continue
        bids = book.sorted_bids()
        asks = book.sorted_asks()
        bb, _ = bids[0]
        ba, _ = asks[0]
        if bb >= ba:
            continue
        mid = float((bb + ba) / 2)
        bid_wall = _side_snap(bids, mid=mid, tick=tick, side="BID")
        ask_wall = _side_snap(asks, mid=mid, tick=tick, side="ASK")
        # observe full-book strongest walls for causal percentile BEFORE using percentile
        thr.observe(
            bid_wall.notional if bid_wall else None,
            ask_wall.notional if ask_wall else None,
        )
        asof = _ms_to_dt(bucket)
        ind = last_closed_bar_at(bars_5m, asof)
        ema20 = float(ind["ema20"]) if ind is not None else None
        ema59 = float(ind["ema59"]) if ind is not None else None
        atr = float(ind["atr"]) if ind is not None else None
        ask20 = bid20 = ask59 = bid59 = None
        if ema20 is not None and atr is not None and atr > 0:
            z20 = make_zone("EMA20", ema20, atr)
            # slightly expand search to near-zone (±1 half-width)
            expand = z20.half_width
            ask20 = _zone_wall(
                asks,
                side="ASK",
                zone_name="EMA20",
                low=z20.low - expand,
                high=z20.high + expand,
                mid=mid,
                thr=thr,
            )
            bid20 = _zone_wall(
                bids,
                side="BID",
                zone_name="EMA20",
                low=z20.low - expand,
                high=z20.high + expand,
                mid=mid,
                thr=thr,
            )
        if ema59 is not None and atr is not None and atr > 0:
            z59 = make_zone("EMA59", ema59, atr)
            expand = z59.half_width
            ask59 = _zone_wall(
                asks,
                side="ASK",
                zone_name="EMA59",
                low=z59.low - expand,
                high=z59.high + expand,
                mid=mid,
                thr=thr,
            )
            bid59 = _zone_wall(
                bids,
                side="BID",
                zone_name="EMA59",
                low=z59.low - expand,
                high=z59.high + expand,
                mid=mid,
                thr=thr,
            )
        yield AnalysisSample(
            ts_ms=bucket,
            mid=mid,
            best_bid=float(bb),
            best_ask=float(ba),
            bid_levels=len(bids),
            ask_levels=len(asks),
            genuine=not gap_latched and book.is_valid,
            seq_gap=gap_latched,
            carried_forward=False,
            warmup=warmup,
            ema20=ema20,
            ema59=ema59,
            atr=atr,
            bid_wall=bid_wall,
            ask_wall=ask_wall,
            ask_in_ema20=ask20,
            bid_in_ema20=bid20,
            ask_in_ema59=ask59,
            bid_in_ema59=bid59,
            source_file=str(ref.path),
        )


def is_majorish(zw: ZoneWallSnap | None) -> bool:
    if zw is None:
        return False
    rel_ok = zw.relative_size is not None and zw.relative_size >= REL_SIZE_MIN
    pct_ok = zw.causal_percentile is not None and zw.causal_percentile >= PERCENTILE_MIN
    return bool(rel_ok and pct_ok)
