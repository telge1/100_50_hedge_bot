"""Near-book liquidity analysis: nearest vs dominant walls and ask/bid ladders.

Complements wall_movement_tracker with price-proximal (short-term) liquidity views.
Read-only; no ClickHouse writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, Sequence


class _WallLike(Protocol):
    side: str
    price: Decimal
    notional: Decimal
    wall_multiple: float
    distance_pct: float
    is_wall: bool


class _SnapLike(Protocol):
    timestamp: datetime
    mid_price: Decimal
    bucket_size: Decimal
    aggressive_buy_near_ask: Decimal

# Near-ask / near-bid classifications
NEAR_ASK_STABLE = "NEAR_ASK_STABLE"
NEAR_ASK_MOVING_HIGHER = "NEAR_ASK_MOVING_HIGHER"
NEAR_ASK_MOVING_LOWER = "NEAR_ASK_MOVING_LOWER"
NEAR_ASK_BUILDING = "NEAR_ASK_BUILDING"
NEAR_ASK_THINNING = "NEAR_ASK_THINNING"
NEAR_ASK_PULLED = "NEAR_ASK_PULLED"
NEAR_ASK_CONSUMED = "NEAR_ASK_CONSUMED"
ASK_LADDER_MOVING_HIGHER = "ASK_LADDER_MOVING_HIGHER"
ASK_LADDER_MOVING_LOWER = "ASK_LADDER_MOVING_LOWER"
ASK_LADDER_COMPRESSION = "ASK_LADDER_COMPRESSION"
ASK_LADDER_EXPANSION = "ASK_LADDER_EXPANSION"

NEAR_BID_STABLE = "NEAR_BID_STABLE"
NEAR_BID_MOVING_HIGHER = "NEAR_BID_MOVING_HIGHER"
NEAR_BID_MOVING_LOWER = "NEAR_BID_MOVING_LOWER"

BULLISH_LIQUIDITY_SHIFT = "BULLISH_LIQUIDITY_SHIFT"
BEARISH_LIQUIDITY_SHIFT = "BEARISH_LIQUIDITY_SHIFT"
COMPRESSION = "COMPRESSION"
EXPANSION = "EXPANSION"
MIXED = "MIXED"
INCONCLUSIVE = "INCONCLUSIVE"

EPS = Decimal("1e-12")


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class NearParams:
    near_min_distance_pct: float = 0.10
    near_max_distance_pct: float = 1.50
    near_top_n: int = 3
    near_max_buckets: int = 15
    match_max_buckets: int = 1
    build_notional_pct: float = 25.0
    thin_notional_pct: float = 25.0
    pull_drop_pct: float = 50.0
    consume_trade_coverage: float = 0.35
    sequence_min_shifts: int = 2
    sequence_min_snapshots: int = 3
    sample_seconds: int = 30


@dataclass
class NearSnapshotView:
    nearest_bid: _WallLike | None = None
    nearest_ask: _WallLike | None = None
    dominant_bid: _WallLike | None = None
    dominant_ask: _WallLike | None = None
    near_bids: list[Any] = field(default_factory=list)
    near_asks: list[Any] = field(default_factory=list)
    total_near_bid_notional: Decimal = Decimal("0")
    total_near_ask_notional: Decimal = Decimal("0")
    near_book_imbalance: float = 0.0
    nearest_bid_ask_gap: Decimal | None = None
    mid_position_between_near_walls: float | None = None
    near_bid_weighted_price: Decimal | None = None
    near_ask_weighted_price: Decimal | None = None
    weighted_liquidity_gap: Decimal | None = None
    weighted_liquidity_midpoint: Decimal | None = None

    def to_row(self) -> dict[str, Any]:
        def _slot(walls: list[Any], i: int, key: str) -> Any:
            if i >= len(walls):
                return None
            w = walls[i]
            if key == "price":
                return format(w.price, "f")
            if key == "notional":
                return format(w.notional, "f")
            return round(w.wall_multiple, 4)

        nb, na = self.nearest_bid, self.nearest_ask
        db, da = self.dominant_bid, self.dominant_ask
        row: dict[str, Any] = {
            "nearest_bid_wall_price": None if nb is None else format(nb.price, "f"),
            "nearest_bid_wall_notional": None if nb is None else format(nb.notional, "f"),
            "nearest_bid_wall_multiple": None if nb is None else round(nb.wall_multiple, 4),
            "nearest_bid_wall_distance_pct": None if nb is None else round(nb.distance_pct, 4),
            "nearest_ask_wall_price": None if na is None else format(na.price, "f"),
            "nearest_ask_wall_notional": None if na is None else format(na.notional, "f"),
            "nearest_ask_wall_multiple": None if na is None else round(na.wall_multiple, 4),
            "nearest_ask_wall_distance_pct": None if na is None else round(na.distance_pct, 4),
            "dominant_bid_wall_price": None if db is None else format(db.price, "f"),
            "dominant_bid_wall_notional": None if db is None else format(db.notional, "f"),
            "dominant_ask_wall_price": None if da is None else format(da.price, "f"),
            "dominant_ask_wall_notional": None if da is None else format(da.notional, "f"),
            "total_near_bid_notional": format(self.total_near_bid_notional, "f"),
            "total_near_ask_notional": format(self.total_near_ask_notional, "f"),
            "near_book_imbalance": round(self.near_book_imbalance, 6),
            "nearest_bid_ask_gap": None
            if self.nearest_bid_ask_gap is None
            else format(self.nearest_bid_ask_gap, "f"),
            "mid_position_between_near_walls": self.mid_position_between_near_walls,
            "near_bid_weighted_price": None
            if self.near_bid_weighted_price is None
            else format(self.near_bid_weighted_price, "f"),
            "near_ask_weighted_price": None
            if self.near_ask_weighted_price is None
            else format(self.near_ask_weighted_price, "f"),
            "weighted_liquidity_gap": None
            if self.weighted_liquidity_gap is None
            else format(self.weighted_liquidity_gap, "f"),
            "weighted_liquidity_midpoint": None
            if self.weighted_liquidity_midpoint is None
            else format(self.weighted_liquidity_midpoint, "f"),
        }
        for i in range(3):
            row[f"near_bid_{i+1}_price"] = _slot(self.near_bids, i, "price")
            row[f"near_bid_{i+1}_notional"] = _slot(self.near_bids, i, "notional")
            row[f"near_bid_{i+1}_multiple"] = _slot(self.near_bids, i, "multiple")
            row[f"near_ask_{i+1}_price"] = _slot(self.near_asks, i, "price")
            row[f"near_ask_{i+1}_notional"] = _slot(self.near_asks, i, "notional")
            row[f"near_ask_{i+1}_multiple"] = _slot(self.near_asks, i, "multiple")
        return row


@dataclass
class NearAskTransition:
    previous_timestamp: datetime
    current_timestamp: datetime
    previous_nearest_ask_price: Decimal | None
    current_nearest_ask_price: Decimal | None
    shift_buckets: float | None
    previous_nearest_ask_notional: Decimal | None
    current_nearest_ask_notional: Decimal | None
    notional_change_pct: float | None
    previous_total_near_ask_notional: Decimal
    current_total_near_ask_notional: Decimal
    aggressive_buy_notional: Decimal
    mid_price_change_pct: float
    classification: str
    confidence: float

    def to_row(self) -> dict[str, Any]:
        return {
            "previous_timestamp": self.previous_timestamp.isoformat(),
            "current_timestamp": self.current_timestamp.isoformat(),
            "previous_nearest_ask_price": None
            if self.previous_nearest_ask_price is None
            else format(self.previous_nearest_ask_price, "f"),
            "current_nearest_ask_price": None
            if self.current_nearest_ask_price is None
            else format(self.current_nearest_ask_price, "f"),
            "shift_buckets": None if self.shift_buckets is None else round(self.shift_buckets, 4),
            "previous_nearest_ask_notional": None
            if self.previous_nearest_ask_notional is None
            else format(self.previous_nearest_ask_notional, "f"),
            "current_nearest_ask_notional": None
            if self.current_nearest_ask_notional is None
            else format(self.current_nearest_ask_notional, "f"),
            "notional_change_pct": None
            if self.notional_change_pct is None
            else round(self.notional_change_pct, 4),
            "previous_total_near_ask_notional": format(self.previous_total_near_ask_notional, "f"),
            "current_total_near_ask_notional": format(self.current_total_near_ask_notional, "f"),
            "aggressive_buy_notional": format(self.aggressive_buy_notional, "f"),
            "mid_price_change_pct": round(self.mid_price_change_pct, 6),
            "classification": self.classification,
            "confidence": round(self.confidence, 4),
        }


@dataclass
class LadderSequence:
    side: str
    classification: str
    sequence_start: datetime
    sequence_end: datetime
    number_of_shifts: int
    start_level: Decimal
    end_level: Decimal
    start_weighted: Decimal | None
    end_weighted: Decimal | None
    confidence_score: float
    notes: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "classification": self.classification,
            "sequence_start": self.sequence_start.isoformat(),
            "sequence_end": self.sequence_end.isoformat(),
            "number_of_shifts": self.number_of_shifts,
            "start_level": format(self.start_level, "f"),
            "end_level": format(self.end_level, "f"),
            "start_weighted": None if self.start_weighted is None else format(self.start_weighted, "f"),
            "end_weighted": None if self.end_weighted is None else format(self.end_weighted, "f"),
            "confidence_score": round(self.confidence_score, 4),
            "notes": self.notes,
        }


def weighted_price(walls: Sequence[Any]) -> Decimal | None:
    if not walls:
        return None
    total_n = sum((w.notional for w in walls), Decimal("0"))
    if total_n <= 0:
        return None
    return sum((w.price * w.notional for w in walls), Decimal("0")) / total_n


def _in_near_band(
    wall: Any,
    *,
    side: str,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    mid: Decimal,
    bucket_size: Decimal,
    near: NearParams,
) -> bool:
    if wall.distance_pct < near.near_min_distance_pct:
        return False
    if wall.distance_pct > near.near_max_distance_pct:
        return False
    if side == "ask":
        if best_ask is not None and wall.price < best_ask:
            return False
        if best_ask is not None:
            buckets_above = (wall.price - best_ask) / bucket_size
            if buckets_above > Decimal(near.near_max_buckets):
                return False
    else:
        if best_bid is not None and wall.price > best_bid:
            return False
        if best_bid is not None:
            buckets_below = (best_bid - wall.price) / bucket_size
            if buckets_below > Decimal(near.near_max_buckets):
                return False
    return True


def select_near_and_dominant(
    *,
    bid_candidates: Sequence[Any],
    ask_candidates: Sequence[Any],
    mid: Decimal,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    bucket_size: Decimal,
    near: NearParams,
) -> NearSnapshotView:
    """Split nearest (price-proximal) vs dominant (largest within distance_max scope).

    Dominant = max notional among *qualified* walls (is_wall) on that side.
    Nearest ask = lowest-priced qualified ask above best ask within near band.
    Nearest bid = highest-priced qualified bid below best bid within near band.
    """
    qual_bids = [w for w in bid_candidates if w.is_wall]
    qual_asks = [w for w in ask_candidates if w.is_wall]
    # Soft fallback: if no qualified walls, use all candidates for nearest search only
    bid_pool = qual_bids or list(bid_candidates)
    ask_pool = qual_asks or list(ask_candidates)

    dominant_bid = max(qual_bids, key=lambda w: w.notional) if qual_bids else (
        max(bid_candidates, key=lambda w: w.notional) if bid_candidates else None
    )
    dominant_ask = max(qual_asks, key=lambda w: w.notional) if qual_asks else (
        max(ask_candidates, key=lambda w: w.notional) if ask_candidates else None
    )

    near_asks = [
        w
        for w in ask_pool
        if _in_near_band(
            w,
            side="ask",
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            bucket_size=bucket_size,
            near=near,
        )
    ]
    near_asks.sort(key=lambda w: w.price)  # nearest to market first
    near_asks = near_asks[: near.near_top_n]

    near_bids = [
        w
        for w in bid_pool
        if _in_near_band(
            w,
            side="bid",
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            bucket_size=bucket_size,
            near=near,
        )
    ]
    near_bids.sort(key=lambda w: w.price, reverse=True)  # nearest to market first
    near_bids = near_bids[: near.near_top_n]

    nearest_ask = near_asks[0] if near_asks else None
    nearest_bid = near_bids[0] if near_bids else None

    # Ensure nearest is never replaced by a far dominant: if dominant is outside near band,
    # nearest stays the near one (already guaranteed by construction).

    total_bid = sum((w.notional for w in near_bids), Decimal("0"))
    total_ask = sum((w.notional for w in near_asks), Decimal("0"))
    denom = total_bid + total_ask
    imbalance = float((total_bid - total_ask) / denom) if denom > 0 else 0.0

    gap = None
    mid_pos = None
    if nearest_bid and nearest_ask and nearest_ask.price > nearest_bid.price:
        gap = nearest_ask.price - nearest_bid.price
        mid_pos = float((mid - nearest_bid.price) / gap) if gap > 0 else None

    wb = weighted_price(near_bids)
    wa = weighted_price(near_asks)
    wgap = None if wb is None or wa is None else wa - wb
    wmid = None if wb is None or wa is None else (wb + wa) / Decimal("2")

    return NearSnapshotView(
        nearest_bid=nearest_bid,
        nearest_ask=nearest_ask,
        dominant_bid=dominant_bid,
        dominant_ask=dominant_ask,
        near_bids=near_bids,
        near_asks=near_asks,
        total_near_bid_notional=total_bid,
        total_near_ask_notional=total_ask,
        near_book_imbalance=imbalance,
        nearest_bid_ask_gap=gap,
        mid_position_between_near_walls=None if mid_pos is None else round(mid_pos, 6),
        near_bid_weighted_price=wb,
        near_ask_weighted_price=wa,
        weighted_liquidity_gap=wgap,
        weighted_liquidity_midpoint=wmid,
    )


def classify_near_ask_transition(
    prev: NearSnapshotView,
    cur: NearSnapshotView,
    *,
    prev_ts: datetime,
    cur_ts: datetime,
    mid_prev: Decimal,
    mid_cur: Decimal,
    bucket_size: Decimal,
    aggressive_buy: Decimal,
    near: NearParams,
) -> NearAskTransition:
    mid_chg = float((mid_cur - mid_prev) / mid_prev * 100) if mid_prev else 0.0
    prev_n = prev.nearest_ask
    cur_n = cur.nearest_ask
    shift = None
    notional_chg = None
    if prev_n and cur_n:
        shift = float((cur_n.price - prev_n.price) / bucket_size)
        if prev_n.notional > 0:
            notional_chg = float((cur_n.notional - prev_n.notional) / prev_n.notional * 100)

    total_prev = prev.total_near_ask_notional
    total_cur = cur.total_near_ask_notional
    total_chg_pct = (
        float((total_cur - total_prev) / total_prev * 100) if total_prev > 0 else 0.0
    )

    classification = NEAR_ASK_STABLE
    confidence = 0.5

    if prev_n and cur_n and shift is not None and abs(shift) <= 1.0 + 1e-9:
        # Prefer total-ladder strength changes over single-wall pull when totals move clearly
        if total_chg_pct >= near.build_notional_pct:
            classification = NEAR_ASK_BUILDING
            confidence = _clip01(0.45 + total_chg_pct / 100.0)
        elif total_chg_pct <= -near.thin_notional_pct:
            classification = NEAR_ASK_THINNING
            confidence = _clip01(0.45 + abs(total_chg_pct) / 100.0)
        else:
            drop = -notional_chg if notional_chg is not None else 0.0
            if drop >= near.pull_drop_pct:
                coverage = float(aggressive_buy / prev_n.notional) if prev_n.notional > 0 else 0.0
                if coverage >= near.consume_trade_coverage:
                    classification = NEAR_ASK_CONSUMED
                    confidence = _clip01(0.4 + coverage)
                else:
                    classification = NEAR_ASK_PULLED
                    confidence = _clip01(0.4 + drop / 100.0)
            else:
                classification = NEAR_ASK_STABLE
                confidence = 0.6
    elif prev_n and cur_n and shift is not None:
        if shift >= 1.0 - 1e-9 and mid_chg >= -0.05:
            classification = NEAR_ASK_MOVING_HIGHER
            confidence = _clip01(0.55 + min(abs(shift), 5) / 10.0)
        elif shift <= -1.0 + 1e-9:
            classification = NEAR_ASK_MOVING_LOWER
            confidence = _clip01(0.55 + min(abs(shift), 5) / 10.0)
        else:
            classification = NEAR_ASK_STABLE
    elif total_chg_pct >= near.build_notional_pct:
        classification = NEAR_ASK_BUILDING
        confidence = 0.5
    elif total_chg_pct <= -near.thin_notional_pct:
        classification = NEAR_ASK_THINNING
        confidence = 0.5

    return NearAskTransition(
        previous_timestamp=prev_ts,
        current_timestamp=cur_ts,
        previous_nearest_ask_price=None if prev_n is None else prev_n.price,
        current_nearest_ask_price=None if cur_n is None else cur_n.price,
        shift_buckets=shift,
        previous_nearest_ask_notional=None if prev_n is None else prev_n.notional,
        current_nearest_ask_notional=None if cur_n is None else cur_n.notional,
        notional_change_pct=notional_chg,
        previous_total_near_ask_notional=total_prev,
        current_total_near_ask_notional=total_cur,
        aggressive_buy_notional=aggressive_buy,
        mid_price_change_pct=mid_chg,
        classification=classification,
        confidence=confidence,
    )


def build_near_ask_transitions(
    snaps: Sequence[Any],
    nears: Sequence[NearSnapshotView],
    near: NearParams,
) -> list[NearAskTransition]:
    out: list[NearAskTransition] = []
    for prev_s, cur_s, prev_n, cur_n in zip(snaps, snaps[1:], nears, nears[1:]):
        out.append(
            classify_near_ask_transition(
                prev_n,
                cur_n,
                prev_ts=prev_s.timestamp,
                cur_ts=cur_s.timestamp,
                mid_prev=prev_s.mid_price,
                mid_cur=cur_s.mid_price,
                bucket_size=cur_s.bucket_size,
                aggressive_buy=cur_s.aggressive_buy_near_ask,
                near=near,
            )
        )
    return out


def detect_ask_ladder_sequences(
    snaps: Sequence[Any],
    nears: Sequence[NearSnapshotView],
    near: NearParams,
) -> list[LadderSequence]:
    """Track Top-N near ask walls; require multi-step confirmation."""
    sequences: list[LadderSequence] = []
    sequences.extend(
        _nearest_ask_runs(snaps, nears, near, direction=1, label=NEAR_ASK_MOVING_HIGHER)
    )
    sequences.extend(
        _nearest_ask_runs(snaps, nears, near, direction=-1, label=NEAR_ASK_MOVING_LOWER)
    )
    sequences.extend(_ladder_shift_runs(snaps, nears, near, direction=1, label=ASK_LADDER_MOVING_HIGHER))
    sequences.extend(_ladder_shift_runs(snaps, nears, near, direction=-1, label=ASK_LADDER_MOVING_LOWER))
    sequences.extend(_ladder_gap_runs(snaps, nears, near))
    return sequences


def _nearest_ask_runs(
    snaps: Sequence[Any],
    nears: Sequence[NearSnapshotView],
    near: NearParams,
    *,
    direction: int,
    label: str,
) -> list[LadderSequence]:
    runs: list[LadderSequence] = []
    i = 0
    while i < len(nears) - 1:
        if nears[i].nearest_ask is None:
            i += 1
            continue
        run_idx = [i]
        j = i + 1
        while j < len(nears):
            prev, cur = nears[run_idx[-1]], nears[j]
            if prev.nearest_ask is None or cur.nearest_ask is None:
                break
            shift = (cur.nearest_ask.price - prev.nearest_ask.price) / snaps[j].bucket_size
            if direction > 0 and shift >= 1:
                run_idx.append(j)
                j += 1
                continue
            if direction < 0 and shift <= -1:
                run_idx.append(j)
                j += 1
                continue
            break
        n_shifts = len(run_idx) - 1
        if n_shifts >= near.sequence_min_shifts:
            start_i, end_i = run_idx[0], run_idx[-1]
            start_w = nears[start_i].nearest_ask
            end_w = nears[end_i].nearest_ask
            assert start_w and end_w
            conf = _clip01(0.4 + 0.2 * min(n_shifts / 3, 1) + 0.2)
            runs.append(
                LadderSequence(
                    side="ask",
                    classification=label,
                    sequence_start=snaps[start_i].timestamp,
                    sequence_end=snaps[end_i].timestamp,
                    number_of_shifts=n_shifts,
                    start_level=start_w.price,
                    end_level=end_w.price,
                    start_weighted=nears[start_i].near_ask_weighted_price,
                    end_weighted=nears[end_i].near_ask_weighted_price,
                    confidence_score=conf,
                    notes="nearest_ask multi-step",
                )
            )
            i = end_i
        else:
            i += 1
    return runs


def _ladder_shift_runs(
    snaps: Sequence[Any],
    nears: Sequence[NearSnapshotView],
    near: NearParams,
    *,
    direction: int,
    label: str,
) -> list[LadderSequence]:
    from orderbook_analyse.wall_movement_tracker import match_walls

    runs: list[LadderSequence] = []
    i = 0
    while i < len(nears) - 1:
        run_idx = [i]
        j = i + 1
        while j < len(nears):
            prev, cur = nears[run_idx[-1]], nears[j]
            if len(prev.near_asks) < 2 or len(cur.near_asks) < 2:
                break
            matches = match_walls(
                prev.near_asks,
                cur.near_asks,
                bucket_size=snaps[j].bucket_size,
                max_buckets=near.match_max_buckets,
            )
            shifts = []
            for a, b, _ in matches:
                shifts.append(float((b.price - a.price) / snaps[j].bucket_size))
            same_dir = [s for s in shifts if (s >= 1 and direction > 0) or (s <= -1 and direction < 0)]
            # Also require weighted ask to move same direction
            wp, wc = prev.near_ask_weighted_price, cur.near_ask_weighted_price
            weighted_ok = False
            if wp is not None and wc is not None:
                weighted_ok = (wc > wp and direction > 0) or (wc < wp and direction < 0)
            if len(same_dir) >= 2 and weighted_ok:
                run_idx.append(j)
                j += 1
                continue
            break
        n_shifts = len(run_idx) - 1
        if n_shifts >= near.sequence_min_shifts:
            start_i, end_i = run_idx[0], run_idx[-1]
            start_level = (
                nears[start_i].near_asks[0].price
                if nears[start_i].near_asks
                else snaps[start_i].mid_price
            )
            end_level = (
                nears[end_i].near_asks[0].price
                if nears[end_i].near_asks
                else snaps[end_i].mid_price
            )
            conf = _clip01(0.45 + 0.25 * min(n_shifts / 3, 1))
            runs.append(
                LadderSequence(
                    side="ask",
                    classification=label,
                    sequence_start=snaps[start_i].timestamp,
                    sequence_end=snaps[end_i].timestamp,
                    number_of_shifts=n_shifts,
                    start_level=start_level,
                    end_level=end_level,
                    start_weighted=nears[start_i].near_ask_weighted_price,
                    end_weighted=nears[end_i].near_ask_weighted_price,
                    confidence_score=conf,
                    notes="topN ask ladder (>=2 walls shifted)",
                )
            )
            i = end_i
        else:
            i += 1
    return runs


def _ladder_gap_runs(
    snaps: Sequence[Any],
    nears: Sequence[NearSnapshotView],
    near: NearParams,
) -> list[LadderSequence]:
    out: list[LadderSequence] = []
    for i in range(len(nears) - 1):
        prev, cur = nears[i], nears[i + 1]
        if prev.nearest_bid_ask_gap is None or cur.nearest_bid_ask_gap is None:
            continue
        if prev.nearest_ask is None or cur.nearest_ask is None:
            continue
        if prev.nearest_bid is None or cur.nearest_bid is None:
            continue
        gap_shrink = cur.nearest_bid_ask_gap < prev.nearest_bid_ask_gap
        gap_grow = cur.nearest_bid_ask_gap > prev.nearest_bid_ask_gap
        ask_closer = cur.nearest_ask.price < prev.nearest_ask.price
        ask_away = cur.nearest_ask.price > prev.nearest_ask.price
        bid_up = cur.nearest_bid.price > prev.nearest_bid.price
        bid_down = cur.nearest_bid.price < prev.nearest_bid.price
        if gap_shrink and (ask_closer or bid_up):
            out.append(
                LadderSequence(
                    side="both",
                    classification=ASK_LADDER_COMPRESSION,
                    sequence_start=snaps[i].timestamp,
                    sequence_end=snaps[i + 1].timestamp,
                    number_of_shifts=1,
                    start_level=prev.nearest_ask.price,
                    end_level=cur.nearest_ask.price,
                    start_weighted=prev.near_ask_weighted_price,
                    end_weighted=cur.near_ask_weighted_price,
                    confidence_score=0.55,
                    notes=f"gap {format(prev.nearest_bid_ask_gap,'f')}→{format(cur.nearest_bid_ask_gap,'f')}",
                )
            )
        elif gap_grow and (ask_away or bid_down):
            out.append(
                LadderSequence(
                    side="both",
                    classification=ASK_LADDER_EXPANSION,
                    sequence_start=snaps[i].timestamp,
                    sequence_end=snaps[i + 1].timestamp,
                    number_of_shifts=1,
                    start_level=prev.nearest_ask.price,
                    end_level=cur.nearest_ask.price,
                    start_weighted=prev.near_ask_weighted_price,
                    end_weighted=cur.near_ask_weighted_price,
                    confidence_score=0.55,
                    notes=f"gap {format(prev.nearest_bid_ask_gap,'f')}→{format(cur.nearest_bid_ask_gap,'f')}",
                )
            )
    return out


def summarize_near_regime(
    snaps: Sequence[Any],
    nears: Sequence[NearSnapshotView],
    ask_transitions: Sequence[NearAskTransition],
    ladder_seqs: Sequence[LadderSequence],
) -> dict[str, Any]:
    if not nears:
        return {
            "near_bid_direction": INCONCLUSIVE,
            "near_ask_direction": INCONCLUSIVE,
            "near_bid_strength_change": INCONCLUSIVE,
            "near_ask_strength_change": INCONCLUSIVE,
            "auction_direction": INCONCLUSIVE,
            "short_term_bias": INCONCLUSIVE,
            "nearest_support": None,
            "nearest_resistance": None,
            "dominant_support": None,
            "dominant_resistance": None,
        }

    first, last = nears[0], nears[-1]

    def _dir(a: Any, b: Any) -> str:
        if a is None or b is None:
            return INCONCLUSIVE
        if b.price > a.price:
            return "HIGHER"
        if b.price < a.price:
            return "LOWER"
        return "STABLE"

    bid_dir = _dir(first.nearest_bid, last.nearest_bid)
    ask_dir = _dir(first.nearest_ask, last.nearest_ask)

    bid_str = INCONCLUSIVE
    if first.total_near_bid_notional > 0:
        chg = float(
            (last.total_near_bid_notional - first.total_near_bid_notional)
            / first.total_near_bid_notional
            * 100
        )
        bid_str = "STRONGER" if chg >= 15 else ("WEAKER" if chg <= -15 else "STABLE")
    ask_str = INCONCLUSIVE
    if first.total_near_ask_notional > 0:
        chg = float(
            (last.total_near_ask_notional - first.total_near_ask_notional)
            / first.total_near_ask_notional
            * 100
        )
        ask_str = "STRONGER" if chg >= 15 else ("WEAKER" if chg <= -15 else "STABLE")

    mid_chg = float((snaps[-1].mid_price - snaps[0].mid_price) / snaps[0].mid_price * 100)

    if bid_dir == "HIGHER" and ask_dir == "HIGHER":
        auction = "HIGHER"
    elif bid_dir == "LOWER" and ask_dir == "LOWER":
        auction = "LOWER"
    elif bid_dir == "HIGHER" and ask_dir == "LOWER":
        auction = COMPRESSION
    elif bid_dir == "LOWER" and ask_dir == "HIGHER":
        auction = EXPANSION
    else:
        auction = MIXED

    # short-term bias diagnostic
    moving_higher = any(s.classification in {NEAR_ASK_MOVING_HIGHER, ASK_LADDER_MOVING_HIGHER} for s in ladder_seqs)
    moving_lower = any(s.classification in {NEAR_ASK_MOVING_LOWER, ASK_LADDER_MOVING_LOWER} for s in ladder_seqs)
    thinning = sum(1 for t in ask_transitions if t.classification == NEAR_ASK_THINNING)
    building = sum(1 for t in ask_transitions if t.classification == NEAR_ASK_BUILDING)

    if auction == COMPRESSION or (
        bid_dir == "HIGHER" and ask_dir == "LOWER"
    ):
        bias = COMPRESSION
    elif auction == EXPANSION:
        bias = EXPANSION
    elif (bid_dir in {"HIGHER", "STABLE"} and (ask_dir == "HIGHER" or ask_str == "WEAKER") and mid_chg >= -0.05) or (
        moving_higher and thinning >= building and mid_chg >= 0
    ):
        bias = BULLISH_LIQUIDITY_SHIFT
    elif (bid_dir in {"LOWER", "STABLE"} and (ask_dir == "LOWER" or ask_str == "STRONGER") and mid_chg <= 0.05) or (
        moving_lower and building >= thinning and mid_chg <= 0
    ):
        bias = BEARISH_LIQUIDITY_SHIFT
    elif auction == MIXED:
        bias = MIXED
    else:
        bias = INCONCLUSIVE

    return {
        "near_bid_direction": bid_dir,
        "near_ask_direction": ask_dir,
        "near_bid_strength_change": bid_str,
        "near_ask_strength_change": ask_str,
        "auction_direction": auction,
        "short_term_bias": bias,
        "nearest_support": None if last.nearest_bid is None else format(last.nearest_bid.price, "f"),
        "nearest_resistance": None if last.nearest_ask is None else format(last.nearest_ask.price, "f"),
        "dominant_support": None if last.dominant_bid is None else format(last.dominant_bid.price, "f"),
        "dominant_resistance": None if last.dominant_ask is None else format(last.dominant_ask.price, "f"),
        "start_nearest_ask": None if first.nearest_ask is None else format(first.nearest_ask.price, "f"),
        "end_nearest_ask": None if last.nearest_ask is None else format(last.nearest_ask.price, "f"),
        "start_dominant_ask": None if first.dominant_ask is None else format(first.dominant_ask.price, "f"),
        "end_dominant_ask": None if last.dominant_ask is None else format(last.dominant_ask.price, "f"),
        "start_near_ask_weighted": None
        if first.near_ask_weighted_price is None
        else format(first.near_ask_weighted_price, "f"),
        "end_near_ask_weighted": None
        if last.near_ask_weighted_price is None
        else format(last.near_ask_weighted_price, "f"),
        "end_total_near_ask_notional": format(last.total_near_ask_notional, "f"),
        "start_total_near_ask_notional": format(first.total_near_ask_notional, "f"),
    }


def earliest_directional_change(
    ladder_seqs: Sequence[LadderSequence],
    ask_transitions: Sequence[NearAskTransition],
) -> dict[str, Any] | None:
    events: list[tuple[datetime, str, str]] = []
    for s in ladder_seqs:
        if s.classification in {
            NEAR_ASK_MOVING_HIGHER,
            NEAR_ASK_MOVING_LOWER,
            ASK_LADDER_MOVING_HIGHER,
            ASK_LADDER_MOVING_LOWER,
            ASK_LADDER_COMPRESSION,
            ASK_LADDER_EXPANSION,
        }:
            events.append((s.sequence_start, s.classification, f"{s.start_level}->{s.end_level}"))
    for t in ask_transitions:
        if t.classification in {NEAR_ASK_MOVING_HIGHER, NEAR_ASK_MOVING_LOWER}:
            events.append(
                (
                    t.current_timestamp,
                    t.classification,
                    f"{t.previous_nearest_ask_price}->{t.current_nearest_ask_price}",
                )
            )
    if not events:
        return None
    events.sort(key=lambda x: x[0])
    ts, label, detail = events[0]
    return {"timestamp": ts.isoformat(), "classification": label, "detail": detail}
