"""Causal wall-movement tracking across reconstructed orderbook snapshots.

Read-only research module. Does not write to ClickHouse or touch the live recorder.

Confidence score (diagnostic, not ML) for a multi-step sequence::

    confidence = clip01(
        0.25 * min(n_confirmed_shifts / 3, 1)
      + 0.20 * min(avg_wall_multiple / 5, 1)
      + 0.15 * persistence_ratio          # fraction of samples with a wall present
      + 0.15 * price_alignment            # 1 if mid moves with floor/ceiling, else 0
      + 0.10 * oi_alignment               # 1 if OI change sign agrees with move, else 0.5 neutral
      + 0.10 * (1 - contradiction_ratio)  # opposing shifts in window
      + 0.05 * (1 - avg_old_remaining)    # old liquidity largely gone favors replacement/chase
    )

Classifications are diagnostic labels only.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import (
    PROJECT_ROOT,
    BucketStat,
    ReadOnlyClickHouse,
    WallDetectorParams,
    analyze_resolution,
    choose_bucket_size,
    connect_readonly,
    find_bootstrap_snapshot,
    infer_tick_size,
    load_events,
    load_liquidation_context,
    load_oi_context,
    load_trade_context,
    parse_utc,
    reconstruct_with_samples,
    utc_now,
    write_csv,
)
from orderbook_analyse.orderbook_replay import OrderBookState, ReplayError

logger = logging.getLogger(__name__)

# Classifications
RISING_BID_FLOOR = "RISING_BID_FLOOR"
FALLING_BID_FLOOR = "FALLING_BID_FLOOR"
RISING_ASK_CEILING = "RISING_ASK_CEILING"
FALLING_ASK_CEILING = "FALLING_ASK_CEILING"
WALL_REPLACED_HIGHER = "WALL_REPLACED_HIGHER"
WALL_REPLACED_LOWER = "WALL_REPLACED_LOWER"
WALL_STABLE = "WALL_STABLE"
WALL_PULLED = "WALL_PULLED"
WALL_CONSUMED = "WALL_CONSUMED"
WALL_CHASING_PRICE = "WALL_CHASING_PRICE"
LIQUIDITY_COMPRESSION = "LIQUIDITY_COMPRESSION"
LIQUIDITY_EXPANSION = "LIQUIDITY_EXPANSION"


@dataclass
class MovementParams:
    sample_seconds: int = 30
    target_bps: float = 10.0
    distance_max_pct: float = 3.0
    match_max_buckets: int = 1
    stable_price_tol_buckets: float = 0.0
    stable_notional_tol_pct: float = 20.0
    pull_drop_pct: float = 50.0
    consume_trade_coverage: float = 0.35
    replace_old_remaining_max: float = 0.35
    chase_distance_tol_pct: float = 0.25
    chase_min_shifts: int = 3
    sequence_min_snapshots: int = 3
    sequence_min_shifts: int = 2
    sequence_window_samples: int = 8
    near_min_distance_pct: float = 0.10
    near_max_distance_pct: float = 1.50
    near_top_n: int = 3
    near_max_buckets: int = 15
    wall_params: WallDetectorParams = field(default_factory=WallDetectorParams)


@dataclass
class WallView:
    side: str
    price: Decimal
    notional: Decimal
    wall_multiple: float
    distance_pct: float
    is_wall: bool

    def to_short(self) -> dict[str, Any]:
        return {
            "price": format(self.price, "f"),
            "notional": format(self.notional, "f"),
            "wall_multiple": round(self.wall_multiple, 4),
            "distance_pct": round(self.distance_pct, 4),
            "is_wall": self.is_wall,
        }


@dataclass
class SnapshotRecord:
    timestamp: datetime
    mid_price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    bucket_size: Decimal
    strongest_bid: WallView | None
    strongest_ask: WallView | None
    top_bid_walls: list[WallView]
    top_ask_walls: list[WallView]
    all_bid_buckets: dict[Decimal, Decimal]  # price -> notional (within distance)
    all_ask_buckets: dict[Decimal, Decimal]
    buy_notional_since_prev: Decimal
    sell_notional_since_prev: Decimal
    trade_delta_notional: Decimal
    open_interest: Decimal | None
    oi_change_since_prev: Decimal | None
    # trade notional near levels for pull/consume diagnostics (buy hits asks, sell hits bids)
    aggressive_sell_near_bid: Decimal = Decimal("0")
    aggressive_buy_near_ask: Decimal = Decimal("0")
    # near vs dominant (optional; filled by near-liquidity pass)
    nearest_bid: WallView | None = None
    nearest_ask: WallView | None = None
    dominant_bid: WallView | None = None
    dominant_ask: WallView | None = None
    near_bids: list[WallView] = field(default_factory=list)
    near_asks: list[WallView] = field(default_factory=list)
    total_near_bid_notional: Decimal = Decimal("0")
    total_near_ask_notional: Decimal = Decimal("0")
    near_book_imbalance: float = 0.0
    near_bid_weighted_price: Decimal | None = None
    near_ask_weighted_price: Decimal | None = None
    nearest_bid_ask_gap: Decimal | None = None
    mid_position_between_near_walls: float | None = None
    weighted_liquidity_gap: Decimal | None = None
    weighted_liquidity_midpoint: Decimal | None = None
    all_bid_candidates: list[WallView] = field(default_factory=list)
    all_ask_candidates: list[WallView] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        sb, sa = self.strongest_bid, self.strongest_ask
        # Compatibility: strongest_* remains dominant-by-notional among walls
        db = self.dominant_bid or sb
        da = self.dominant_ask or sa
        nb, na = self.nearest_bid, self.nearest_ask
        row = {
            "timestamp": self.timestamp.isoformat(),
            "mid_price": format(self.mid_price, "f"),
            "best_bid": None if self.best_bid is None else format(self.best_bid, "f"),
            "best_ask": None if self.best_ask is None else format(self.best_ask, "f"),
            "bucket_size": format(self.bucket_size, "f"),
            "strongest_bid_wall_price": None if sb is None else format(sb.price, "f"),
            "strongest_bid_wall_notional": None if sb is None else format(sb.notional, "f"),
            "strongest_bid_wall_multiple": None if sb is None else round(sb.wall_multiple, 4),
            "strongest_bid_wall_distance_pct": None if sb is None else round(sb.distance_pct, 4),
            "strongest_ask_wall_price": None if sa is None else format(sa.price, "f"),
            "strongest_ask_wall_notional": None if sa is None else format(sa.notional, "f"),
            "strongest_ask_wall_multiple": None if sa is None else round(sa.wall_multiple, 4),
            "strongest_ask_wall_distance_pct": None if sa is None else round(sa.distance_pct, 4),
            "dominant_bid_wall_price": None if db is None else format(db.price, "f"),
            "dominant_bid_wall_notional": None if db is None else format(db.notional, "f"),
            "dominant_ask_wall_price": None if da is None else format(da.price, "f"),
            "dominant_ask_wall_notional": None if da is None else format(da.notional, "f"),
            "nearest_bid_wall_price": None if nb is None else format(nb.price, "f"),
            "nearest_bid_wall_notional": None if nb is None else format(nb.notional, "f"),
            "nearest_bid_wall_multiple": None if nb is None else round(nb.wall_multiple, 4),
            "nearest_bid_wall_distance_pct": None if nb is None else round(nb.distance_pct, 4),
            "nearest_ask_wall_price": None if na is None else format(na.price, "f"),
            "nearest_ask_wall_notional": None if na is None else format(na.notional, "f"),
            "nearest_ask_wall_multiple": None if na is None else round(na.wall_multiple, 4),
            "nearest_ask_wall_distance_pct": None if na is None else round(na.distance_pct, 4),
            "top3_bid_walls": _encode_walls(self.top_bid_walls),
            "top3_ask_walls": _encode_walls(self.top_ask_walls),
            "buy_notional_since_previous_snapshot": format(self.buy_notional_since_prev, "f"),
            "sell_notional_since_previous_snapshot": format(self.sell_notional_since_prev, "f"),
            "trade_delta_notional": format(self.trade_delta_notional, "f"),
            "open_interest": None if self.open_interest is None else format(self.open_interest, "f"),
            "oi_change_since_previous_snapshot": None
            if self.oi_change_since_prev is None
            else format(self.oi_change_since_prev, "f"),
        }
        return row


@dataclass
class TransitionRecord:
    previous_timestamp: datetime
    current_timestamp: datetime
    side: str
    previous_wall_price: Decimal
    current_wall_price: Decimal
    shift_buckets: float
    shift_pct: float
    previous_notional: Decimal
    current_notional: Decimal
    notional_change: Decimal
    old_wall_remaining_notional: Decimal
    old_wall_remaining_ratio: float
    mid_price_change_pct: float
    trade_delta_notional: Decimal
    oi_change: Decimal | None
    classification: str
    match_score: float = 0.0

    def to_row(self) -> dict[str, Any]:
        return {
            "previous_timestamp": self.previous_timestamp.isoformat(),
            "current_timestamp": self.current_timestamp.isoformat(),
            "side": self.side,
            "previous_wall_price": format(self.previous_wall_price, "f"),
            "current_wall_price": format(self.current_wall_price, "f"),
            "shift_buckets": round(self.shift_buckets, 4),
            "shift_pct": round(self.shift_pct, 6),
            "previous_notional": format(self.previous_notional, "f"),
            "current_notional": format(self.current_notional, "f"),
            "notional_change": format(self.notional_change, "f"),
            "old_wall_remaining_notional": format(self.old_wall_remaining_notional, "f"),
            "old_wall_remaining_ratio": round(self.old_wall_remaining_ratio, 6),
            "mid_price_change_pct": round(self.mid_price_change_pct, 6),
            "trade_delta_notional": format(self.trade_delta_notional, "f"),
            "oi_change": None if self.oi_change is None else format(self.oi_change, "f"),
            "classification": self.classification,
            "match_score": round(self.match_score, 4),
        }


@dataclass
class SequenceRecord:
    side: str
    classification: str
    sequence_start: datetime
    sequence_end: datetime
    number_of_shifts: int
    total_shift_buckets: float
    total_shift_pct: float
    start_wall_price: Decimal
    end_wall_price: Decimal
    start_mid: Decimal
    end_mid: Decimal
    wall_mid_beta: float | None
    average_distance_pct: float
    old_wall_average_remaining_ratio: float
    confidence_score: float
    notes: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "classification": self.classification,
            "sequence_start": self.sequence_start.isoformat(),
            "sequence_end": self.sequence_end.isoformat(),
            "number_of_shifts": self.number_of_shifts,
            "total_shift_buckets": round(self.total_shift_buckets, 4),
            "total_shift_pct": round(self.total_shift_pct, 6),
            "start_wall_price": format(self.start_wall_price, "f"),
            "end_wall_price": format(self.end_wall_price, "f"),
            "start_mid": format(self.start_mid, "f"),
            "end_mid": format(self.end_mid, "f"),
            "wall_mid_beta": None if self.wall_mid_beta is None else round(self.wall_mid_beta, 4),
            "average_distance_pct": round(self.average_distance_pct, 4),
            "old_wall_average_remaining_ratio": round(self.old_wall_average_remaining_ratio, 4),
            "confidence_score": round(self.confidence_score, 4),
            "notes": self.notes,
        }


def _encode_walls(walls: Sequence[WallView]) -> str:
    parts = [
        f"{format(w.price, 'f')}@{format(w.notional, 'f')}(m={w.wall_multiple:.2f})"
        for w in walls
    ]
    return "; ".join(parts)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def wall_view_from_stat(stat: BucketStat) -> WallView:
    return WallView(
        side=stat.side,
        price=stat.bucket_price,
        notional=stat.notional,
        wall_multiple=stat.wall_multiple,
        distance_pct=stat.distance_pct,
        is_wall=stat.is_wall,
    )


def extract_walls_from_book(
    book: OrderBookState,
    *,
    bucket_size: Decimal,
    params: WallDetectorParams,
) -> tuple[
    list[WallView],
    list[WallView],
    dict[Decimal, Decimal],
    dict[Decimal, Decimal],
    Decimal,
    list[WallView],
    list[WallView],
]:
    mid = book.mid_price()
    if mid is None:
        raise ReplayError("book has no mid_price")
    analysis = analyze_resolution(
        book,
        bucket_size=bucket_size,
        resolution="movement",
        mid=mid,
        params=params,
    )
    all_bids = [
        wall_view_from_stat(c) for c in analysis["candidates"] if c.side == "bid"
    ]
    all_asks = [
        wall_view_from_stat(c) for c in analysis["candidates"] if c.side == "ask"
    ]
    bid_walls = sorted(
        [w for w in all_bids if w.is_wall], key=lambda c: c.notional, reverse=True
    )
    ask_walls = sorted(
        [w for w in all_asks if w.is_wall], key=lambda c: c.notional, reverse=True
    )
    # If no wall passed thresholds, still track strongest bucket as soft dominant zone
    if not bid_walls and all_bids:
        top = max(all_bids, key=lambda c: c.notional)
        soft = WallView(
            side=top.side,
            price=top.price,
            notional=top.notional,
            wall_multiple=top.wall_multiple,
            distance_pct=top.distance_pct,
            is_wall=False,
        )
        bid_walls = [soft]
    if not ask_walls and all_asks:
        top = max(all_asks, key=lambda c: c.notional)
        soft = WallView(
            side=top.side,
            price=top.price,
            notional=top.notional,
            wall_multiple=top.wall_multiple,
            distance_pct=top.distance_pct,
            is_wall=False,
        )
        ask_walls = [soft]

    bid_map = {c.price: c.notional for c in all_bids}
    ask_map = {c.price: c.notional for c in all_asks}
    return bid_walls, ask_walls, bid_map, ask_map, mid, all_bids, all_asks


def match_walls(
    previous: Sequence[WallView],
    current: Sequence[WallView],
    *,
    bucket_size: Decimal,
    max_buckets: int,
) -> list[tuple[WallView, WallView, float]]:
    """Greedy unique matching by price proximity, notional as tie-break."""
    if not previous or not current:
        return []
    candidates: list[tuple[float, int, int, float]] = []
    for i, prev in enumerate(previous):
        for j, cur in enumerate(current):
            if prev.side != cur.side:
                continue
            gap = abs(cur.price - prev.price) / bucket_size
            if gap > Decimal(max_buckets):
                continue
            # Primary: smaller gap; secondary: notional similarity
            prev_n = float(prev.notional) or 1.0
            sim = 1.0 - min(abs(float(cur.notional) - prev_n) / prev_n, 1.0)
            score = float(1.0 / (1.0 + float(gap))) * 0.75 + sim * 0.25
            candidates.append((score, i, j, float(gap)))
    candidates.sort(reverse=True)
    used_prev: set[int] = set()
    used_cur: set[int] = set()
    matches: list[tuple[WallView, WallView, float]] = []
    for score, i, j, _gap in candidates:
        if i in used_prev or j in used_cur:
            continue
        used_prev.add(i)
        used_cur.add(j)
        matches.append((previous[i], current[j], score))
    return matches


def classify_transition(
    *,
    side: str,
    prev: WallView,
    cur: WallView,
    prev_snap: SnapshotRecord,
    cur_snap: SnapshotRecord,
    old_remaining: Decimal,
    params: MovementParams,
) -> str:
    bucket = float(cur_snap.bucket_size)
    shift_buckets = float((cur.price - prev.price) / cur_snap.bucket_size)
    mid_change = float(
        (cur_snap.mid_price - prev_snap.mid_price) / prev_snap.mid_price * Decimal(100)
    )
    notional_drop_pct = 0.0
    if prev.notional > 0:
        notional_drop_pct = float((prev.notional - cur.notional) / prev.notional * 100)
    remaining_ratio = float(old_remaining / prev.notional) if prev.notional > 0 else 0.0

    # Stable?
    if abs(shift_buckets) <= params.stable_price_tol_buckets + 1e-9 and abs(
        float(cur.notional - prev.notional) / (float(prev.notional) or 1.0) * 100
    ) <= params.stable_notional_tol_pct:
        return WALL_STABLE

    # Pull / consume on strong notional drop at similar price
    if abs(shift_buckets) <= 1.0 + 1e-9 and notional_drop_pct >= params.pull_drop_pct:
        if side == "bid":
            coverage = float(cur_snap.aggressive_sell_near_bid / prev.notional) if prev.notional else 0.0
        else:
            coverage = float(cur_snap.aggressive_buy_near_ask / prev.notional) if prev.notional else 0.0
        if coverage >= params.consume_trade_coverage:
            return WALL_CONSUMED
        return WALL_PULLED

    # Replacement: large price jump with old level drained
    if abs(shift_buckets) >= 1.0 - 1e-9 and remaining_ratio <= params.replace_old_remaining_max:
        if side == "bid":
            return WALL_REPLACED_HIGHER if shift_buckets > 0 else WALL_REPLACED_LOWER
        return WALL_REPLACED_HIGHER if shift_buckets > 0 else WALL_REPLACED_LOWER

    # Directional floor/ceiling (single-step labels; multi-step confirmed later)
    if side == "bid":
        if shift_buckets >= 1.0 - 1e-9 and mid_change >= -0.05 and cur.is_wall:
            return RISING_BID_FLOOR
        if shift_buckets <= -1.0 + 1e-9:
            return FALLING_BID_FLOOR
    else:
        if shift_buckets >= 1.0 - 1e-9:
            return RISING_ASK_CEILING
        if shift_buckets <= -1.0 + 1e-9 and mid_change <= 0.05 and cur.is_wall:
            return FALLING_ASK_CEILING

    if abs(shift_buckets) < 1.0:
        return WALL_STABLE
    return WALL_REPLACED_HIGHER if shift_buckets > 0 else WALL_REPLACED_LOWER


def build_transition(
    *,
    side: str,
    prev: WallView,
    cur: WallView,
    prev_snap: SnapshotRecord,
    cur_snap: SnapshotRecord,
    match_score: float,
    params: MovementParams,
) -> TransitionRecord:
    bucket_map = cur_snap.all_bid_buckets if side == "bid" else cur_snap.all_ask_buckets
    old_remaining = bucket_map.get(prev.price, Decimal("0"))
    remaining_ratio = float(old_remaining / prev.notional) if prev.notional > 0 else 0.0
    shift_buckets = float((cur.price - prev.price) / cur_snap.bucket_size)
    shift_pct = float((cur.price - prev.price) / prev.price * 100) if prev.price else 0.0
    mid_change = float(
        (cur_snap.mid_price - prev_snap.mid_price) / prev_snap.mid_price * 100
    )
    classification = classify_transition(
        side=side,
        prev=prev,
        cur=cur,
        prev_snap=prev_snap,
        cur_snap=cur_snap,
        old_remaining=old_remaining,
        params=params,
    )
    return TransitionRecord(
        previous_timestamp=prev_snap.timestamp,
        current_timestamp=cur_snap.timestamp,
        side=side,
        previous_wall_price=prev.price,
        current_wall_price=cur.price,
        shift_buckets=shift_buckets,
        shift_pct=shift_pct,
        previous_notional=prev.notional,
        current_notional=cur.notional,
        notional_change=cur.notional - prev.notional,
        old_wall_remaining_notional=old_remaining,
        old_wall_remaining_ratio=remaining_ratio,
        mid_price_change_pct=mid_change,
        trade_delta_notional=cur_snap.trade_delta_notional,
        oi_change=cur_snap.oi_change_since_prev,
        classification=classification,
        match_score=match_score,
    )


def detect_liquidity_regime(
    snapshots: Sequence[SnapshotRecord],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prev, cur in zip(snapshots, snapshots[1:]):
        if not prev.strongest_bid or not prev.strongest_ask or not cur.strongest_bid or not cur.strongest_ask:
            continue
        prev_gap = float(prev.strongest_ask.price - prev.strongest_bid.price)
        cur_gap = float(cur.strongest_ask.price - cur.strongest_bid.price)
        bid_up = cur.strongest_bid.price > prev.strongest_bid.price
        ask_down = cur.strongest_ask.price < prev.strongest_ask.price
        bid_down = cur.strongest_bid.price < prev.strongest_bid.price
        ask_up = cur.strongest_ask.price > prev.strongest_ask.price
        label = None
        if bid_up and ask_down and cur_gap < prev_gap:
            label = LIQUIDITY_COMPRESSION
        elif bid_down and ask_up and cur_gap > prev_gap:
            label = LIQUIDITY_EXPANSION
        if label:
            rows.append(
                {
                    "previous_timestamp": prev.timestamp.isoformat(),
                    "current_timestamp": cur.timestamp.isoformat(),
                    "classification": label,
                    "previous_gap": prev_gap,
                    "current_gap": cur_gap,
                    "gap_change": cur_gap - prev_gap,
                }
            )
    return rows


def confidence_for_sequence(
    *,
    n_shifts: int,
    avg_multiple: float,
    persistence: float,
    price_aligned: bool,
    oi_aligned: float,
    contradiction_ratio: float,
    avg_old_remaining: float,
) -> float:
    return _clip01(
        0.25 * min(n_shifts / 3.0, 1.0)
        + 0.20 * min(avg_multiple / 5.0, 1.0)
        + 0.15 * persistence
        + 0.15 * (1.0 if price_aligned else 0.0)
        + 0.10 * oi_aligned
        + 0.10 * (1.0 - contradiction_ratio)
        + 0.05 * (1.0 - _clip01(avg_old_remaining))
    )


def build_sequences(
    snapshots: Sequence[SnapshotRecord],
    transitions: Sequence[TransitionRecord],
    params: MovementParams,
) -> list[SequenceRecord]:
    sequences: list[SequenceRecord] = []
    # Rising bid floor sequences from consecutive upward bid shifts
    sequences.extend(
        _extract_directional_sequences(
            snapshots,
            transitions,
            side="bid",
            want_shift_sign=1,
            single_labels={RISING_BID_FLOOR, WALL_REPLACED_HIGHER},
            sequence_label=RISING_BID_FLOOR,
            params=params,
        )
    )
    sequences.extend(
        _extract_directional_sequences(
            snapshots,
            transitions,
            side="bid",
            want_shift_sign=-1,
            single_labels={FALLING_BID_FLOOR, WALL_REPLACED_LOWER},
            sequence_label=FALLING_BID_FLOOR,
            params=params,
        )
    )
    sequences.extend(
        _extract_directional_sequences(
            snapshots,
            transitions,
            side="ask",
            want_shift_sign=1,
            single_labels={RISING_ASK_CEILING, WALL_REPLACED_HIGHER},
            sequence_label=RISING_ASK_CEILING,
            params=params,
        )
    )
    sequences.extend(
        _extract_directional_sequences(
            snapshots,
            transitions,
            side="ask",
            want_shift_sign=-1,
            single_labels={FALLING_ASK_CEILING, WALL_REPLACED_LOWER},
            sequence_label=FALLING_ASK_CEILING,
            params=params,
        )
    )
    chase = detect_wall_chasing(snapshots, params)
    sequences.extend(chase)
    return sequences


def _extract_directional_sequences(
    snapshots: Sequence[SnapshotRecord],
    transitions: Sequence[TransitionRecord],
    *,
    side: str,
    want_shift_sign: int,
    single_labels: set[str],
    sequence_label: str,
    params: MovementParams,
) -> list[SequenceRecord]:
    side_tx = [t for t in transitions if t.side == side and t.classification in single_labels]
    side_tx = [
        t
        for t in side_tx
        if (t.shift_buckets > 0 and want_shift_sign > 0)
        or (t.shift_buckets < 0 and want_shift_sign < 0)
    ]
    if not side_tx:
        return []

    # Group into contiguous runs (by timestamp order)
    side_tx = sorted(side_tx, key=lambda t: t.current_timestamp)
    runs: list[list[TransitionRecord]] = []
    current_run: list[TransitionRecord] = [side_tx[0]]
    for tx in side_tx[1:]:
        gap = (tx.previous_timestamp - current_run[-1].current_timestamp).total_seconds()
        # allow small gaps up to sequence_window_samples * sample spacing estimate
        if gap <= params.sequence_window_samples * params.sample_seconds and (
            (want_shift_sign > 0 and tx.current_wall_price >= current_run[-1].current_wall_price)
            or (want_shift_sign < 0 and tx.current_wall_price <= current_run[-1].current_wall_price)
        ):
            current_run.append(tx)
        else:
            runs.append(current_run)
            current_run = [tx]
    runs.append(current_run)

    out: list[SequenceRecord] = []
    snap_by_ts = {s.timestamp: s for s in snapshots}
    for run in runs:
        # Need >=2 confirmed shifts OR covering >=3 snapshots
        n_shifts = len(run)
        start_ts = run[0].previous_timestamp
        end_ts = run[-1].current_timestamp
        spanned = [
            s
            for s in snapshots
            if start_ts <= s.timestamp <= end_ts
        ]
        if n_shifts < params.sequence_min_shifts and len(spanned) < params.sequence_min_snapshots:
            continue
        if n_shifts < params.sequence_min_shifts:
            # single shift alone is not enough even if window long
            continue

        start_wall = run[0].previous_wall_price
        end_wall = run[-1].current_wall_price
        start_mid = snap_by_ts[start_ts].mid_price if start_ts in snap_by_ts else run[0].previous_wall_price
        end_mid = snap_by_ts[end_ts].mid_price if end_ts in snap_by_ts else run[-1].current_wall_price
        wall_chg = float((end_wall - start_wall) / start_wall * 100) if start_wall else 0.0
        mid_chg = float((end_mid - start_mid) / start_mid * 100) if start_mid else 0.0
        beta = None if abs(mid_chg) < 1e-9 else wall_chg / mid_chg
        dists: list[float] = []
        for t in run:
            snap = snap_by_ts.get(t.current_timestamp)
            if not snap:
                continue
            wall = snap.strongest_bid if side == "bid" else snap.strongest_ask
            if wall:
                dists.append(wall.distance_pct)
        avg_dist = sum(dists) / len(dists) if dists else 0.0
        avg_remaining = sum(t.old_wall_remaining_ratio for t in run) / len(run)
        multiples: list[float] = []
        for t in run:
            snap = snap_by_ts.get(t.current_timestamp)
            if not snap:
                continue
            wall = snap.strongest_bid if side == "bid" else snap.strongest_ask
            if wall:
                multiples.append(wall.wall_multiple)
        avg_mult = sum(multiples) / len(multiples) if multiples else 0.0
        price_aligned = (want_shift_sign > 0 and mid_chg >= 0) or (want_shift_sign < 0 and mid_chg <= 0)
        oi_changes = [float(t.oi_change) for t in run if t.oi_change is not None]
        if not oi_changes:
            oi_aligned = 0.5
        else:
            mean_oi = sum(oi_changes) / len(oi_changes)
            oi_aligned = 1.0 if (want_shift_sign > 0 and mean_oi >= 0) or (want_shift_sign < 0 and mean_oi <= 0) else 0.2
        # contradictions: opposite-direction transitions of same side inside window
        opposite = [
            t
            for t in transitions
            if t.side == side
            and start_ts <= t.current_timestamp <= end_ts
            and ((want_shift_sign > 0 and t.shift_buckets < 0) or (want_shift_sign < 0 and t.shift_buckets > 0))
        ]
        contradiction = len(opposite) / max(len(run) + len(opposite), 1)
        persistence = min(len(spanned) / max(params.sequence_min_snapshots, 1), 1.0)
        conf = confidence_for_sequence(
            n_shifts=n_shifts,
            avg_multiple=avg_mult,
            persistence=persistence,
            price_aligned=price_aligned,
            oi_aligned=oi_aligned,
            contradiction_ratio=contradiction,
            avg_old_remaining=avg_remaining,
        )
        out.append(
            SequenceRecord(
                side=side,
                classification=sequence_label,
                sequence_start=start_ts,
                sequence_end=end_ts,
                number_of_shifts=n_shifts,
                total_shift_buckets=sum(t.shift_buckets for t in run),
                total_shift_pct=wall_chg,
                start_wall_price=start_wall,
                end_wall_price=end_wall,
                start_mid=start_mid,
                end_mid=end_mid,
                wall_mid_beta=beta,
                average_distance_pct=avg_dist,
                old_wall_average_remaining_ratio=avg_remaining,
                confidence_score=conf,
                notes=f"run_len={n_shifts}; mid_chg_pct={mid_chg:.4f}",
            )
        )
    return out


def detect_wall_chasing(
    snapshots: Sequence[SnapshotRecord],
    params: MovementParams,
) -> list[SequenceRecord]:
    """Bid wall repeatedly moves up with nearly constant distance under rising mid."""
    if len(snapshots) < params.chase_min_shifts + 1:
        return []
    records: list[SequenceRecord] = []
    # Scan for runs where distance stays stable and wall rises with mid
    i = 0
    while i < len(snapshots) - 1:
        run = [snapshots[i]]
        j = i + 1
        while j < len(snapshots):
            prev, cur = run[-1], snapshots[j]
            if not prev.strongest_bid or not cur.strongest_bid:
                break
            if cur.mid_price <= prev.mid_price:
                break
            if cur.strongest_bid.price <= prev.strongest_bid.price:
                break
            dist_delta = abs(cur.strongest_bid.distance_pct - prev.strongest_bid.distance_pct)
            if dist_delta > params.chase_distance_tol_pct:
                break
            # old wall should not remain strongly
            rem = float(
                cur.all_bid_buckets.get(prev.strongest_bid.price, Decimal("0"))
                / prev.strongest_bid.notional
            ) if prev.strongest_bid.notional > 0 else 0.0
            if rem > params.replace_old_remaining_max:
                break
            run.append(cur)
            j += 1
        if len(run) >= params.chase_min_shifts + 1:
            start, end = run[0], run[-1]
            assert start.strongest_bid and end.strongest_bid
            wall_chg = float(
                (end.strongest_bid.price - start.strongest_bid.price)
                / start.strongest_bid.price
                * 100
            )
            mid_chg = float((end.mid_price - start.mid_price) / start.mid_price * 100)
            beta = None if abs(mid_chg) < 1e-9 else wall_chg / mid_chg
            avg_dist = sum(s.strongest_bid.distance_pct for s in run if s.strongest_bid) / len(run)
            conf = confidence_for_sequence(
                n_shifts=len(run) - 1,
                avg_multiple=sum(s.strongest_bid.wall_multiple for s in run if s.strongest_bid) / len(run),
                persistence=1.0,
                price_aligned=True,
                oi_aligned=0.5,
                contradiction_ratio=0.0,
                avg_old_remaining=0.1,
            )
            records.append(
                SequenceRecord(
                    side="bid",
                    classification=WALL_CHASING_PRICE,
                    sequence_start=start.timestamp,
                    sequence_end=end.timestamp,
                    number_of_shifts=len(run) - 1,
                    total_shift_buckets=float(
                        (end.strongest_bid.price - start.strongest_bid.price) / end.bucket_size
                    ),
                    total_shift_pct=wall_chg,
                    start_wall_price=start.strongest_bid.price,
                    end_wall_price=end.strongest_bid.price,
                    start_mid=start.mid_price,
                    end_mid=end.mid_price,
                    wall_mid_beta=beta,
                    average_distance_pct=avg_dist,
                    old_wall_average_remaining_ratio=0.1,
                    confidence_score=conf,
                    notes="diagnostic: tactical liquidity / spoofing risk — not proof",
                )
            )
            i = j
        else:
            i += 1
    return records


def load_trades_between(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return buy_notional, sell_notional, sell_near_bid_proxy, buy_near_ask_proxy.

    Near-level aggressive proxies use all sells/buys in the interval (diagnostic);
    finer level filtering would need trade price vs wall price joins per snapshot.
    """
    row = db.query(
        """
        SELECT
            sumIf(notional, side = 'Buy') AS buy_n,
            sumIf(notional, side = 'Sell') AS sell_n
        FROM public_trades
        WHERE symbol = %(symbol)s
          AND trade_ts > %(start)s
          AND trade_ts <= %(end)s
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).first_item
    buy_n = _dec(row["buy_n"])
    sell_n = _dec(row["sell_n"])
    return buy_n, sell_n, sell_n, buy_n


def load_oi_at(
    db: ReadOnlyClickHouse, *, symbol: str, as_of: datetime
) -> Decimal | None:
    rows = db.query(
        """
        SELECT open_interest
        FROM ticker_samples
        WHERE symbol = %(symbol)s
          AND exchange_ts <= %(as_of)s
          AND open_interest IS NOT NULL
        ORDER BY exchange_ts DESC
        LIMIT 1
        """,
        parameters={"symbol": symbol, "as_of": as_of},
    ).result_rows
    if not rows:
        return None
    return _dec(rows[0][0])


def build_snapshots_from_books(
    timed_books: dict[datetime, OrderBookState],
    sample_times: Sequence[datetime],
    *,
    bucket_size: Decimal,
    params: MovementParams,
    trade_intervals: dict[datetime, tuple[Decimal, Decimal, Decimal, Decimal]],
    oi_at: dict[datetime, Decimal | None],
) -> list[SnapshotRecord]:
    from orderbook_analyse.near_liquidity import NearParams, select_near_and_dominant

    near_params = NearParams(
        near_min_distance_pct=params.near_min_distance_pct,
        near_max_distance_pct=params.near_max_distance_pct,
        near_top_n=params.near_top_n,
        near_max_buckets=params.near_max_buckets,
        match_max_buckets=params.match_max_buckets,
        pull_drop_pct=params.pull_drop_pct,
        consume_trade_coverage=params.consume_trade_coverage,
        sequence_min_shifts=params.sequence_min_shifts,
        sequence_min_snapshots=params.sequence_min_snapshots,
        sample_seconds=params.sample_seconds,
    )
    snaps: list[SnapshotRecord] = []
    prev_oi: Decimal | None = None
    for ts in sample_times:
        book = timed_books.get(ts)
        if book is None or book.mid_price() is None:
            continue
        wall_params = params.wall_params
        wall_params.distance_max_pct = params.distance_max_pct
        bid_walls, ask_walls, bid_map, ask_map, mid, all_bids, all_asks = extract_walls_from_book(
            book, bucket_size=bucket_size, params=wall_params
        )
        near_view = select_near_and_dominant(
            bid_candidates=all_bids,
            ask_candidates=all_asks,
            mid=mid,
            best_bid=book.best_bid(),
            best_ask=book.best_ask(),
            bucket_size=bucket_size,
            near=near_params,
        )
        buy_n, sell_n, sell_near, buy_near = trade_intervals.get(
            ts, (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
        )
        oi = oi_at.get(ts)
        oi_chg = None if oi is None or prev_oi is None else oi - prev_oi
        # strongest_* remains largest qualified wall (compat); dominant_* explicit alias
        strongest_bid = bid_walls[0] if bid_walls else None
        strongest_ask = ask_walls[0] if ask_walls else None
        snaps.append(
            SnapshotRecord(
                timestamp=ts,
                mid_price=mid,
                best_bid=book.best_bid(),
                best_ask=book.best_ask(),
                bucket_size=bucket_size,
                strongest_bid=strongest_bid,
                strongest_ask=strongest_ask,
                top_bid_walls=bid_walls[:3],
                top_ask_walls=ask_walls[:3],
                all_bid_buckets=bid_map,
                all_ask_buckets=ask_map,
                buy_notional_since_prev=buy_n,
                sell_notional_since_prev=sell_n,
                trade_delta_notional=buy_n - sell_n,
                open_interest=oi,
                oi_change_since_prev=oi_chg,
                aggressive_sell_near_bid=sell_near,
                aggressive_buy_near_ask=buy_near,
                nearest_bid=near_view.nearest_bid,
                nearest_ask=near_view.nearest_ask,
                dominant_bid=near_view.dominant_bid or strongest_bid,
                dominant_ask=near_view.dominant_ask or strongest_ask,
                near_bids=list(near_view.near_bids),
                near_asks=list(near_view.near_asks),
                total_near_bid_notional=near_view.total_near_bid_notional,
                total_near_ask_notional=near_view.total_near_ask_notional,
                near_book_imbalance=near_view.near_book_imbalance,
                near_bid_weighted_price=near_view.near_bid_weighted_price,
                near_ask_weighted_price=near_view.near_ask_weighted_price,
                nearest_bid_ask_gap=near_view.nearest_bid_ask_gap,
                mid_position_between_near_walls=near_view.mid_position_between_near_walls,
                weighted_liquidity_gap=near_view.weighted_liquidity_gap,
                weighted_liquidity_midpoint=near_view.weighted_liquidity_midpoint,
                all_bid_candidates=all_bids,
                all_ask_candidates=all_asks,
            )
        )
        if oi is not None:
            prev_oi = oi
    return snaps


def build_transitions(
    snapshots: Sequence[SnapshotRecord], params: MovementParams
) -> list[TransitionRecord]:
    out: list[TransitionRecord] = []
    for prev, cur in zip(snapshots, snapshots[1:]):
        for side, prev_list, cur_list in (
            ("bid", prev.top_bid_walls, cur.top_bid_walls),
            ("ask", prev.top_ask_walls, cur.top_ask_walls),
        ):
            # Prefer matching strongest walls; also match top lists
            matches = match_walls(
                prev_list,
                cur_list,
                bucket_size=cur.bucket_size,
                max_buckets=params.match_max_buckets,
            )
            # Always include strongest-to-strongest if both exist even if unmatched by proximity
            strongest_prev = prev.strongest_bid if side == "bid" else prev.strongest_ask
            strongest_cur = cur.strongest_bid if side == "bid" else cur.strongest_ask
            have_strongest = False
            if strongest_prev and strongest_cur:
                for a, b, _ in matches:
                    if a.price == strongest_prev.price and b.price == strongest_cur.price:
                        have_strongest = True
                        break
                if not have_strongest:
                    gap = abs(strongest_cur.price - strongest_prev.price) / cur.bucket_size
                    # allow larger jump for replacement detection on strongest only
                    if gap <= Decimal(max(params.match_max_buckets, 8)):
                        matches.append((strongest_prev, strongest_cur, 0.5))

            seen_pairs: set[tuple[Decimal, Decimal]] = set()
            for prev_w, cur_w, score in matches:
                key = (prev_w.price, cur_w.price)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                out.append(
                    build_transition(
                        side=side,
                        prev=prev_w,
                        cur=cur_w,
                        prev_snap=prev,
                        cur_snap=cur,
                        match_score=score,
                        params=params,
                    )
                )
    return out


def render_movement_report(
    summary: dict[str, Any],
    sequences: Sequence[SequenceRecord],
    regimes: Sequence[dict[str, Any]],
    near_summary: dict[str, Any] | None = None,
    ladder_seqs: Sequence[Any] | None = None,
) -> str:
    rising = [s for s in sequences if s.classification == RISING_BID_FLOOR]
    chasing = [s for s in sequences if s.classification == WALL_CHASING_PRICE]
    ask_fall = [s for s in sequences if s.classification == FALLING_ASK_CEILING]
    ask_rise = [s for s in sequences if s.classification == RISING_ASK_CEILING]
    near_summary = near_summary or {}
    ladder_seqs = list(ladder_seqs or [])
    lines = [
        "# Wall Movement Tracker Report",
        "",
        f"- Symbol: `{summary['symbol']}`",
        f"- Window: `{summary['start']}` → `{summary['end']}`",
        f"- Sample seconds: {summary['sample_seconds']}",
        f"- Tick size: `{summary['tick_size']}`",
        f"- Bucket size (target {summary['target_bps']} bps): `{summary['bucket_size']}`",
        f"- Snapshots: {summary['snapshot_count']}",
        f"- Decision: **{summary['decision']}**",
        f"- Near-liquidity decision: **{summary.get('near_decision', 'n/a')}**",
        "",
        "## Near Ask vs Dominant Ask",
        "",
        f"- Nearest significant ask (short-term resistance): "
        f"`{near_summary.get('nearest_resistance')}`",
        f"- Dominant ask (may be farther): `{near_summary.get('dominant_resistance')}`",
        f"- Start→end nearest ask: `{near_summary.get('start_nearest_ask')}` → "
        f"`{near_summary.get('end_nearest_ask')}`",
        f"- Start→end dominant ask: `{near_summary.get('start_dominant_ask')}` → "
        f"`{near_summary.get('end_dominant_ask')}`",
        f"- Near ask notional: `{near_summary.get('start_total_near_ask_notional')}` → "
        f"`{near_summary.get('end_total_near_ask_notional')}` "
        f"({near_summary.get('near_ask_strength_change')})",
        f"- Weighted near ask: `{near_summary.get('start_near_ask_weighted')}` → "
        f"`{near_summary.get('end_near_ask_weighted')}`",
        "",
        "## Near Bid / Ask directions",
        "",
        f"- near_bid_direction: `{near_summary.get('near_bid_direction')}`",
        f"- near_ask_direction: `{near_summary.get('near_ask_direction')}`",
        f"- near_bid_strength_change: `{near_summary.get('near_bid_strength_change')}`",
        f"- near_ask_strength_change: `{near_summary.get('near_ask_strength_change')}`",
        f"- auction_direction: `{near_summary.get('auction_direction')}`",
        f"- short_term_bias (diagnostic): `{near_summary.get('short_term_bias')}`",
        f"- nearest_support / resistance: `{near_summary.get('nearest_support')}` / "
        f"`{near_summary.get('nearest_resistance')}`",
        f"- dominant_support / resistance: `{near_summary.get('dominant_support')}` / "
        f"`{near_summary.get('dominant_resistance')}`",
        "",
        "## Ask ladder sequences",
        "",
    ]
    if not ladder_seqs:
        lines.append("- none")
    else:
        for s in ladder_seqs[:12]:
            row = s.to_row() if hasattr(s, "to_row") else s
            lines.append(
                f"- `{row['classification']}` {row.get('start_level')}→{row.get('end_level')} "
                f"shifts={row.get('number_of_shifts')} conf={row.get('confidence_score')} "
                f"[{row.get('sequence_start')} → {row.get('sequence_end')}]"
            )

    earliest = summary.get("earliest_near_directional_change")
    lines += [
        "",
        "## Earliest near directional change",
        "",
        f"- {earliest if earliest else 'none'}",
        "",
        "## Bid floor movement",
        "",
    ]
    if rising:
        first = min(rising, key=lambda s: s.sequence_start)
        lines.append(
            f"- **Yes — RISING_BID_FLOOR detected.** Earliest causal confirmation: "
            f"`{first.sequence_start.isoformat()}` → `{first.sequence_end.isoformat()}` "
            f"({format(first.start_wall_price, 'f')} → {format(first.end_wall_price, 'f')}, "
            f"shifts={first.number_of_shifts}, confidence={first.confidence_score:.2f})."
        )
    else:
        lines.append("- No multi-step RISING_BID_FLOOR sequence confirmed under default thresholds.")

    lines += ["", "## Ask ceiling movement (dominant/strongest track)", ""]
    if ask_fall:
        lines.append(f"- FALLING_ASK_CEILING sequences: {len(ask_fall)}")
    if ask_rise:
        lines.append(f"- RISING_ASK_CEILING sequences: {len(ask_rise)}")
    if not ask_fall and not ask_rise:
        lines.append("- No multi-step ask-ceiling sequences confirmed.")

    lines += ["", "## Chasing vs stable migration", ""]
    if chasing:
        lines.append(
            f"- WALL_CHASING_PRICE present ({len(chasing)} run(s)) — diagnostic risk label only."
        )
    else:
        lines.append("- No WALL_CHASING_PRICE run detected under distance-stability rules.")

    lines += ["", "## Bid/Ask zone gap regime", ""]
    comp = sum(1 for r in regimes if r["classification"] == LIQUIDITY_COMPRESSION)
    exp = sum(1 for r in regimes if r["classification"] == LIQUIDITY_EXPANSION)
    lines.append(f"- LIQUIDITY_COMPRESSION steps: {comp}")
    lines.append(f"- LIQUIDITY_EXPANSION steps: {exp}")

    lines += ["", "## Strongest classic sequences", ""]
    ranked = sorted(sequences, key=lambda s: s.confidence_score, reverse=True)
    if not ranked:
        lines.append("- none")
    for s in ranked[:6]:
        lines.append(
            f"- `{s.classification}` {s.side} {format(s.start_wall_price, 'f')}→"
            f"{format(s.end_wall_price, 'f')} shifts={s.number_of_shifts} "
            f"conf={s.confidence_score:.2f} "
            f"[{s.sequence_start.isoformat()} → {s.sequence_end.isoformat()}]"
        )

    lines += [
        "",
        "## Market context",
        "",
        f"- Trades: {summary.get('trade_context')}",
        f"- OI: {summary.get('oi_context')}",
        f"- Liquidations: {summary.get('liquidation_context')}",
        "",
        "## Methodological limits",
        "",
    ]
    for lim in summary.get("limitations", []):
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def run_tracker(args: argparse.Namespace) -> dict[str, Any]:
    from orderbook_analyse.near_liquidity import (
        NearParams,
        NearSnapshotView,
        build_near_ask_transitions,
        detect_ask_ladder_sequences,
        earliest_directional_change,
        summarize_near_regime,
    )

    symbol = args.symbol
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    params = MovementParams(
        sample_seconds=int(args.sample_seconds),
        target_bps=float(args.target_bps),
        distance_max_pct=float(args.distance_max_pct),
        match_max_buckets=int(args.match_max_buckets),
        sequence_min_snapshots=int(args.sequence_min_snapshots),
        sequence_min_shifts=int(args.sequence_min_shifts),
        near_min_distance_pct=float(args.near_min_distance_pct),
        near_max_distance_pct=float(args.near_max_distance_pct),
        near_top_n=int(args.near_top_n),
        near_max_buckets=int(args.near_max_buckets),
        wall_params=WallDetectorParams(
            wall_multiple_min=float(args.wall_multiple_min),
            percentile_min=float(args.percentile_min),
            depth_share_min=float(args.depth_share_min),
            distance_max_pct=float(args.distance_max_pct),
        ),
    )
    out_dir = Path(args.output_dir) if args.output_dir else (
        PROJECT_ROOT / "results" / f"wall_movement_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    db = connect_readonly()
    try:
        snap_ts, snap_u, snap_seq = find_bootstrap_snapshot(
            db, symbol=symbol, start=start, end=end
        )
        events = load_events(
            db,
            symbol=symbol,
            snapshot_ts=snap_ts,
            snapshot_u=snap_u,
            snapshot_seq=snap_seq,
            end=end,
        )
        if not events:
            raise ReplayError("no events loaded")

        sample_times: list[datetime] = []
        t = start
        while t <= end:
            sample_times.append(t)
            t += timedelta(seconds=params.sample_seconds)
        if not sample_times or sample_times[-1] < end:
            sample_times.append(end)

        final_book, timed_books = reconstruct_with_samples(
            events, sample_times=sample_times, end=end
        )
        if end not in timed_books:
            timed_books[end] = final_book

        prices: list[Decimal] = []
        for book in timed_books.values():
            prices.extend(book.bids)
            prices.extend(book.asks)
        tick = infer_tick_size(prices) if prices else Decimal("0.0001")
        mid_end = final_book.mid_price()
        if mid_end is None:
            raise ReplayError("final book has no mid")
        bucket_size = choose_bucket_size(mid_end, tick, params.target_bps)

        trade_intervals: dict[datetime, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        oi_at: dict[datetime, Decimal | None] = {}
        prev_ts = start
        for ts in sample_times:
            if ts == sample_times[0]:
                trade_intervals[ts] = (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
            else:
                trade_intervals[ts] = load_trades_between(
                    db, symbol=symbol, start=prev_ts, end=ts
                )
            oi_at[ts] = load_oi_at(db, symbol=symbol, as_of=ts)
            prev_ts = ts

        snapshots = build_snapshots_from_books(
            timed_books,
            sample_times,
            bucket_size=bucket_size,
            params=params,
            trade_intervals=trade_intervals,
            oi_at=oi_at,
        )
        transitions = build_transitions(snapshots, params)
        sequences = build_sequences(snapshots, transitions, params)
        regimes = detect_liquidity_regime(snapshots)

        near_params = NearParams(
            near_min_distance_pct=params.near_min_distance_pct,
            near_max_distance_pct=params.near_max_distance_pct,
            near_top_n=params.near_top_n,
            near_max_buckets=params.near_max_buckets,
            match_max_buckets=params.match_max_buckets,
            pull_drop_pct=params.pull_drop_pct,
            consume_trade_coverage=params.consume_trade_coverage,
            sequence_min_shifts=params.sequence_min_shifts,
            sequence_min_snapshots=params.sequence_min_snapshots,
            sample_seconds=params.sample_seconds,
        )
        near_views: list[NearSnapshotView] = []
        for snap in snapshots:
            near_views.append(
                NearSnapshotView(
                    nearest_bid=snap.nearest_bid,
                    nearest_ask=snap.nearest_ask,
                    dominant_bid=snap.dominant_bid,
                    dominant_ask=snap.dominant_ask,
                    near_bids=snap.near_bids,
                    near_asks=snap.near_asks,
                    total_near_bid_notional=snap.total_near_bid_notional,
                    total_near_ask_notional=snap.total_near_ask_notional,
                    near_book_imbalance=snap.near_book_imbalance,
                    nearest_bid_ask_gap=snap.nearest_bid_ask_gap,
                    mid_position_between_near_walls=snap.mid_position_between_near_walls,
                    near_bid_weighted_price=snap.near_bid_weighted_price,
                    near_ask_weighted_price=snap.near_ask_weighted_price,
                    weighted_liquidity_gap=snap.weighted_liquidity_gap,
                    weighted_liquidity_midpoint=snap.weighted_liquidity_midpoint,
                )
            )
        near_ask_tx = build_near_ask_transitions(snapshots, near_views, near_params)
        ladder_seqs = detect_ask_ladder_sequences(snapshots, near_views, near_params)
        near_summary = summarize_near_regime(snapshots, near_views, near_ask_tx, ladder_seqs)
        earliest_dir = earliest_directional_change(ladder_seqs, near_ask_tx)

        trade_ctx = load_trade_context(db, symbol=symbol, start=start, end=end)
        oi_ctx = load_oi_context(db, symbol=symbol, start=start, end=end)
        liq_ctx = load_liquidation_context(db, symbol=symbol, start=start, end=end)

        rising = [s for s in sequences if s.classification == RISING_BID_FLOOR]
        chasing = [s for s in sequences if s.classification == WALL_CHASING_PRICE]
        earliest_rising = (
            min(rising, key=lambda s: s.sequence_start).sequence_start.isoformat()
            if rising
            else None
        )

        path_hit = False
        if rising:
            for seq in rising:
                if seq.start_wall_price <= Decimal("0.616") and seq.end_wall_price >= Decimal(
                    "0.619"
                ):
                    path_hit = True
                    break

        # Near-liquidity decision
        near_decision = "NEAR_LIQUIDITY_INCONCLUSIVE"
        if snapshots and near_views and near_views[-1].nearest_ask is not None:
            dom = near_views[-1].dominant_ask
            nearest = near_views[-1].nearest_ask
            separated = (
                dom is not None
                and nearest is not None
                and abs(dom.price - nearest.price) / bucket_size >= Decimal("2")
            )
            has_ladder = any(
                s.classification.startswith("ASK_LADDER") or s.classification.startswith("NEAR_ASK")
                for s in ladder_seqs
            )
            if separated or has_ladder or near_summary.get("short_term_bias") not in {
                "INCONCLUSIVE",
                None,
            }:
                near_decision = "NEAR_LIQUIDITY_PROMISING"
        elif not snapshots:
            near_decision = "NEAR_LIQUIDITY_FAILED"

        if rising:
            decision = "WALL_MOVEMENT_PROMISING"
        elif snapshots:
            decision = "WALL_MOVEMENT_INCONCLUSIVE"
        else:
            decision = "WALL_MOVEMENT_FAILED"

        limitations = [
            "Classifications are diagnostic heuristics, not trading signals.",
            "WALL_PULLED / WALL_CONSUMED / NEAR_ASK_PULLED / NEAR_ASK_CONSUMED use interval-level trade proxies.",
            "WALL_CHASING_PRICE flags tactical-liquidity risk; it is not proof of spoofing.",
            "Multi-step sequences require >=2 confirmed shifts.",
            "Nearest walls use near-distance band; dominant walls may sit farther away.",
            "short_term_bias is diagnostic only — not an order signal.",
            "Read-only ClickHouse access; live recorder untouched.",
        ]

        summary = {
            "symbol": symbol,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sample_seconds": params.sample_seconds,
            "target_bps": params.target_bps,
            "tick_size": format(tick, "f"),
            "bucket_size": format(bucket_size, "f"),
            "near_window": {
                "near_min_distance_pct": params.near_min_distance_pct,
                "near_max_distance_pct": params.near_max_distance_pct,
                "near_top_n": params.near_top_n,
                "near_max_buckets": params.near_max_buckets,
            },
            "bootstrap_snapshot": {
                "exchange_ts": snap_ts.isoformat(),
                "update_id": snap_u,
                "cross_sequence": snap_seq,
            },
            "events_loaded": len(events),
            "snapshot_count": len(snapshots),
            "transition_count": len(transitions),
            "sequence_count": len(sequences),
            "rising_bid_floor_count": len(rising),
            "earliest_rising_bid_floor": earliest_rising,
            "wall_chasing_price_present": bool(chasing),
            "approx_path_0_614_to_0_621_hit": path_hit,
            "sequences": [s.to_row() for s in sequences],
            "liquidity_regime_events": regimes,
            "near_liquidity": near_summary,
            "ask_ladder_sequences": [s.to_row() for s in ladder_seqs],
            "earliest_near_directional_change": earliest_dir,
            "near_decision": near_decision,
            "trade_context": trade_ctx,
            "oi_context": oi_ctx,
            "liquidation_context": liq_ctx,
            "decision": decision,
            "limitations": limitations,
            "confidence_formula": (
                "clip01(0.25*min(n_shifts/3,1)+0.20*min(avg_multiple/5,1)+"
                "0.15*persistence+0.15*price_alignment+0.10*oi_alignment+"
                "0.10*(1-contradiction_ratio)+0.05*(1-avg_old_remaining))"
            ),
            "output_dir": str(out_dir),
        }

        write_csv(out_dir / "wall_snapshots.csv", [s.to_row() for s in snapshots])
        write_csv(out_dir / "wall_transitions.csv", [t.to_row() for t in transitions])
        write_csv(out_dir / "wall_movement_sequences.csv", [s.to_row() for s in sequences])

        near_snap_rows = []
        for snap, nv in zip(snapshots, near_views):
            row = {"timestamp": snap.timestamp.isoformat(), "mid_price": format(snap.mid_price, "f")}
            row.update(nv.to_row())
            near_snap_rows.append(row)
        write_csv(out_dir / "near_wall_snapshots.csv", near_snap_rows)
        write_csv(out_dir / "near_wall_transitions.csv", [t.to_row() for t in near_ask_tx])
        write_csv(out_dir / "ask_ladder_sequences.csv", [s.to_row() for s in ladder_seqs])
        (out_dir / "near_liquidity_summary.json").write_bytes(
            orjson.dumps(
                {
                    **near_summary,
                    "near_decision": near_decision,
                    "earliest_near_directional_change": earliest_dir,
                    "ask_ladder_sequences": [s.to_row() for s in ladder_seqs],
                },
                option=orjson.OPT_INDENT_2,
            )
        )
        (out_dir / "wall_movement_summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "REPORT.md").write_text(
            render_movement_report(summary, sequences, regimes, near_summary, ladder_seqs),
            encoding="utf-8",
        )
        return summary
    finally:
        db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Causal wall movement tracker (read-only)")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--sample-seconds", type=int, default=30)
    p.add_argument("--target-bps", type=float, default=10.0)
    p.add_argument("--distance-max-pct", type=float, default=3.0)
    p.add_argument("--match-max-buckets", type=int, default=1)
    p.add_argument("--sequence-min-snapshots", type=int, default=3)
    p.add_argument("--sequence-min-shifts", type=int, default=2)
    p.add_argument("--wall-multiple-min", type=float, default=3.0)
    p.add_argument("--percentile-min", type=float, default=90.0)
    p.add_argument("--depth-share-min", type=float, default=0.01)
    p.add_argument("--near-min-distance-pct", type=float, default=0.10)
    p.add_argument("--near-max-distance-pct", type=float, default=1.50)
    p.add_argument("--near-top-n", type=int, default=3)
    p.add_argument("--near-max-buckets", type=int, default=15)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        summary = run_tracker(args)
        sys.stdout.buffer.write(orjson.dumps(summary, option=orjson.OPT_INDENT_2))
        sys.stdout.buffer.write(b"\n")
        return 0 if summary["decision"] != "WALL_MOVEMENT_FAILED" else 1
    except Exception as exc:  # noqa: BLE001
        logger.error("ERROR: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
