"""Bucket bounds from wall sequence metadata."""

from __future__ import annotations

import re
from decimal import Decimal

from orderbook_analyse.dynamic_wall_detector import assign_bucket_price, choose_bucket_size


_RES_BPS_RE = re.compile(r"auto_(\d+(?:\.\d+)?)bps", re.IGNORECASE)


def parse_resolution_bps(resolution: str, default_bps: float = 10.0) -> float:
    m = _RES_BPS_RE.search(str(resolution or ""))
    if m:
        return float(m.group(1))
    return default_bps


def infer_tick_size(price: float) -> Decimal:
    """Heuristic tick for altcoins near 0.x (research only)."""
    if price >= 100:
        return Decimal("0.1")
    if price >= 1:
        return Decimal("0.001")
    if price >= 0.1:
        return Decimal("0.0001")
    return Decimal("0.00001")


def resolve_bucket(
    *,
    wall_price: float,
    side: str,
    resolution: str,
    neighbor_buckets: int = 2,
    tick_size: float | None = None,
) -> dict[str, float]:
    """Return primary bucket price and analysis low/high including neighbors."""
    px = Decimal(str(wall_price))
    tick = Decimal(str(tick_size)) if tick_size is not None else infer_tick_size(wall_price)
    bps = parse_resolution_bps(resolution)
    bucket_size = choose_bucket_size(px, tick, bps)
    side_l = str(side).lower()
    if side_l in {"ask", "sell"}:
        side_key = "ask"
    else:
        side_key = "bid"
    primary = assign_bucket_price(px, bucket_size, side_key)
    span = bucket_size * Decimal(max(0, int(neighbor_buckets)))
    low = primary - span
    high = primary + span
    # Include full primary bucket width for ask (ceil) / bid (floor) band.
    # Expand slightly so level prices that round into neighbor buckets are kept.
    if side_key == "ask":
        # ask primary bucket covers (primary - bucket_size, primary]
        band_low = primary - bucket_size
        band_high = primary
    else:
        band_low = primary
        band_high = primary + bucket_size
    analysis_low = min(low, band_low) - (bucket_size * Decimal("0.0000001"))
    analysis_high = max(high, band_high) + (bucket_size * Decimal("0.0000001"))
    return {
        "bucket_size": float(bucket_size),
        "primary_bucket_price": float(primary),
        "band_low": float(band_low),
        "band_high": float(band_high),
        "analysis_low": float(analysis_low),
        "analysis_high": float(analysis_high),
        "target_bps": float(bps),
        "tick_size": float(tick),
    }


def price_in_primary_bucket(price: float, *, band_low: float, band_high: float, side: str) -> bool:
    if str(side).lower() in {"ask", "sell"}:
        return band_low < price <= band_high + 1e-15
    return band_low - 1e-15 <= price < band_high


def ticks_between(price_a: float, price_b: float, tick_size: float) -> float:
    if tick_size <= 0:
        return 0.0
    return abs(price_a - price_b) / tick_size
