"""Deterministic parent/child pool geometry for nested ask edge shorts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import PoolRecord, _utc_naive
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size

from .config import GAP_ATR_BUCKETS, GAP_PCT_BUCKETS, GAP_SEPARATION_TICKS


def pools_overlap(a: PoolRecord, b: PoolRecord) -> bool:
    lo = max(a.lower_edge, b.lower_edge)
    hi = min(a.upper_edge, b.upper_edge)
    return hi > lo


def overlap_interval(a: PoolRecord, b: PoolRecord) -> tuple[float, float] | None:
    lo = max(a.lower_edge, b.lower_edge)
    hi = min(a.upper_edge, b.upper_edge)
    if hi <= lo:
        return None
    return lo, hi


def active_asks(pools: Iterable[PoolRecord], as_of: datetime) -> list[PoolRecord]:
    return [p for p in pools if p.side == "ASK" and p.is_active_at(as_of)]


def active_bids(pools: Iterable[PoolRecord], as_of: datetime) -> list[PoolRecord]:
    return [p for p in pools if p.side == "BID" and p.is_active_at(as_of)]


def bucket_label(value: float | None, buckets: tuple[tuple[float, float, str], ...]) -> str:
    if value is None or value != value:
        return "NA"
    for lo, hi, label in buckets:
        if lo <= value < hi:
            return label
    return buckets[-1][2]


@dataclass(frozen=True)
class NestedStructure:
    parent_15m: PoolRecord
    parent_5m: PoolRecord
    child_1m: PoolRecord
    parent_zone_low: float
    parent_zone_high: float
    overlap_5m_15m: bool
    overlap_1m_5m: bool
    overlap_1m_15m: bool
    child_pool_age_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_15m_id": self.parent_15m.pool_id,
            "parent_5m_id": self.parent_5m.pool_id,
            "child_1m_id": self.child_1m.pool_id,
            "parent_15m_edges": [self.parent_15m.lower_edge, self.parent_15m.upper_edge],
            "parent_5m_edges": [self.parent_5m.lower_edge, self.parent_5m.upper_edge],
            "child_1m_edges": [self.child_1m.lower_edge, self.child_1m.upper_edge],
            "parent_zone_low": self.parent_zone_low,
            "parent_zone_high": self.parent_zone_high,
            "child_pool_low": self.child_1m.lower_edge,
            "child_pool_high": self.child_1m.upper_edge,
            "overlap_5m_15m": self.overlap_5m_15m,
            "overlap_1m_5m": self.overlap_1m_5m,
            "overlap_1m_15m": self.overlap_1m_15m,
            "child_pool_age_at_decision_seconds": self.child_pool_age_seconds,
            "parent_15m_available_at": self.parent_15m.available_at.isoformat(),
            "parent_5m_available_at": self.parent_5m.available_at.isoformat(),
            "child_1m_available_at": self.child_1m.available_at.isoformat(),
        }


def select_nested_ask_structure(
    *,
    asks_15m: list[PoolRecord],
    asks_5m: list[PoolRecord],
    asks_1m: list[PoolRecord],
    price: float,
    as_of: datetime,
) -> NestedStructure | None:
    """Return nearest qualified nest, or None."""
    ranked = rank_nested_ask_structures(
        asks_15m=asks_15m, asks_5m=asks_5m, asks_1m=asks_1m, price=price, as_of=as_of
    )
    return ranked[0] if ranked else None


def rank_nested_ask_structures(
    *,
    asks_15m: list[PoolRecord],
    asks_5m: list[PoolRecord],
    asks_1m: list[PoolRecord],
    price: float,
    as_of: datetime,
) -> list[NestedStructure]:
    """All valid nests, nearest child lower-edge above price first.

    Selection does not use outcomes. Child must overlap both HTF parents and
    have midpoint inside the 5m∩15m parent zone.
    """
    as_of_n = _utc_naive(as_of)
    candidates: list[NestedStructure] = []

    for p15 in asks_15m:
        for p5 in asks_5m:
            ov_htf = overlap_interval(p15, p5)
            if ov_htf is None:
                continue
            zone_lo, zone_hi = ov_htf
            for c1 in asks_1m:
                if not (pools_overlap(c1, p5) and pools_overlap(c1, p15)):
                    continue
                if c1.lower_edge < zone_lo - 1e-12 or c1.upper_edge > zone_hi + 1e-12:
                    if not (zone_lo <= c1.midpoint <= zone_hi):
                        continue
                if price >= c1.lower_edge:
                    continue
                age = (as_of_n - _utc_naive(c1.available_at)).total_seconds()
                candidates.append(
                    NestedStructure(
                        parent_15m=p15,
                        parent_5m=p5,
                        child_1m=c1,
                        parent_zone_low=zone_lo,
                        parent_zone_high=zone_hi,
                        overlap_5m_15m=True,
                        overlap_1m_5m=True,
                        overlap_1m_15m=True,
                        child_pool_age_seconds=age,
                    )
                )

    candidates.sort(
        key=lambda s: (
            s.child_1m.lower_edge - price,
            s.parent_zone_high - s.parent_zone_low,
            -s.child_pool_age_seconds,
            s.child_1m.pool_id,
        )
    )
    return candidates


def upper_gap_metrics(
    *,
    parent_zone_high: float,
    asks: list[PoolRecord],
    as_of: datetime,
    atr: float,
    symbol: str,
) -> dict[str, Any]:
    tick = tick_size(symbol)
    sep = GAP_SEPARATION_TICKS * tick
    above = [
        p
        for p in asks
        if p.is_active_at(as_of) and p.lower_edge >= parent_zone_high + sep
    ]
    if not above:
        return {
            "next_ask_pool_id": None,
            "next_ask_pool_low": None,
            "upper_gap_abs": None,
            "upper_gap_pct": None,
            "upper_gap_atr": None,
            "upper_gap_atr_bucket": "NA",
            "upper_gap_pct_bucket": "NA",
        }
    nxt = min(above, key=lambda p: p.lower_edge)
    gap_abs = nxt.lower_edge - parent_zone_high
    gap_pct = gap_abs / parent_zone_high * 100.0 if parent_zone_high > 0 else None
    gap_atr = gap_abs / atr if atr and atr > 0 else None
    return {
        "next_ask_pool_id": nxt.pool_id,
        "next_ask_pool_low": nxt.lower_edge,
        "upper_gap_abs": gap_abs,
        "upper_gap_pct": gap_pct,
        "upper_gap_atr": gap_atr,
        "upper_gap_atr_bucket": bucket_label(gap_atr, GAP_ATR_BUCKETS),
        "upper_gap_pct_bucket": bucket_label(gap_pct, GAP_PCT_BUCKETS),
    }


def bid_liquidity_below(
    *,
    entry: float,
    bids: list[PoolRecord],
    as_of: datetime,
    atr: float,
) -> dict[str, Any]:
    below = [p for p in bids if p.is_active_at(as_of) and p.upper_edge < entry]
    if not below:
        return {
            "bid_pool_count_below": 0,
            "nearest_bid_pool_id": None,
            "nearest_bid_pool_high": None,
            "nearest_bid_pool_mid": None,
            "distance_to_nearest_bid_pct": None,
            "distance_to_nearest_bid_atr": None,
            "cumulative_bid_pool_score_below": 0.0,
            "number_of_distinct_bid_targets": 0,
            "bid_pools": [],
        }
    below_sorted = sorted(below, key=lambda p: entry - p.upper_edge)
    nearest = below_sorted[0]
    dist = entry - nearest.upper_edge
    score = 0.0
    for p in below:
        w = max(p.upper_edge - p.lower_edge, 0.0)
        s = float(p.strength or 0.0) + float(p.component_count or 1)
        score += s * (1.0 + w)
    return {
        "bid_pool_count_below": len(below),
        "nearest_bid_pool_id": nearest.pool_id,
        "nearest_bid_pool_high": nearest.upper_edge,
        "nearest_bid_pool_mid": nearest.midpoint,
        "distance_to_nearest_bid_pct": dist / entry * 100.0 if entry > 0 else None,
        "distance_to_nearest_bid_atr": dist / atr if atr and atr > 0 else None,
        "cumulative_bid_pool_score_below": score,
        "number_of_distinct_bid_targets": len({p.pool_id for p in below}),
        "bid_pools": [
            {
                "pool_id": p.pool_id,
                "timeframe": p.timeframe,
                "lower_edge": p.lower_edge,
                "upper_edge": p.upper_edge,
                "midpoint": p.midpoint,
                "available_at": p.available_at.isoformat(),
            }
            for p in below_sorted
        ],
    }


def structural_stop(
    *,
    structure: NestedStructure,
    atr: float,
    symbol: str,
) -> dict[str, Any]:
    from .config import MAX_STOP_DISTANCE_PCT, STOP_ATR_BUFFER, STOP_TICK_BUFFER

    tick = tick_size(symbol)
    entry = structure.child_1m.lower_edge
    stop_reference = max(
        structure.child_1m.upper_edge,
        structure.parent_5m.upper_edge,
        structure.parent_15m.upper_edge,
    )
    buf = max(STOP_TICK_BUFFER * tick, (atr if atr and atr > 0 else 0.0) * STOP_ATR_BUFFER)
    stop_loss = stop_reference + buf
    dist_pct = (stop_loss - entry) / entry * 100.0 if entry > 0 else None
    fixed_1pct = entry * (1.0 + MAX_STOP_DISTANCE_PCT / 100.0)
    return {
        "entry_price": entry,
        "child_pool_high": structure.child_1m.upper_edge,
        "parent_5m_pool_high": structure.parent_5m.upper_edge,
        "parent_15m_pool_high": structure.parent_15m.upper_edge,
        "stop_reference": stop_reference,
        "stop_buffer": buf,
        "stop_loss": stop_loss,
        "stop_distance_pct": dist_pct,
        "stop_distance_atr": (stop_loss - entry) / atr if atr and atr > 0 else None,
        "stop_too_wide": bool(dist_pct is not None and dist_pct > MAX_STOP_DISTANCE_PCT),
        "fixed_1pct_stop": fixed_1pct,
        "fixed_1pct_distance_pct": MAX_STOP_DISTANCE_PCT,
    }
