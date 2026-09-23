"""Causal 5m upper liquidity pools as of an entry timestamp."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# 5m catches the small intermediate pools that 1h often skipped.
POOL_TIMEFRAME = "5m"


@dataclass(frozen=True)
class UpperPool:
    bottom: float
    top: float
    pool_id: str
    status: str


def _parse_as_of(as_of: datetime) -> str:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)
    return as_of.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_upper_pools_as_of(
    symbol: str,
    as_of: datetime,
    *,
    lookback_hours: int = 12,
    lookforward_hours: int = 6,
    timeframe: str = POOL_TIMEFRAME,
) -> list[UpperPool]:
    """Return ACTIVE upper LLD zones known at ``as_of`` (no lookahead).

    Default timeframe is ``5m`` so small intermediate pools above entry are
    visible for TP / sparse-ladder decisions.
    """
    from dashboard.research_charts.service import liquidity_location_overlay_bundle

    start = int((as_of - timedelta(hours=lookback_hours)).timestamp())
    end = int((as_of + timedelta(hours=lookforward_hours)).timestamp())
    payload = liquidity_location_overlay_bundle(
        symbol=symbol,
        timeframe=str(timeframe or POOL_TIMEFRAME),
        start=start,
        end=end,
        allow_stale=True,
        liquidity_location_as_of=_parse_as_of(as_of),
    )
    overlays = (payload.get("liquidity") or {}).get("overlays") or []
    pools: list[UpperPool] = []
    for ov in overlays:
        md: dict[str, Any] = ov.get("metadata") or {}
        if md.get("side") != "upper":
            continue
        if str(md.get("pool_status") or "").upper() != "ACTIVE":
            continue
        bottom = ov.get("bottom_price")
        top = ov.get("top_price")
        if bottom is None or top is None:
            continue
        pools.append(
            UpperPool(
                bottom=float(bottom),
                top=float(top),
                pool_id=str(md.get("pool_id") or ov.get("id") or ""),
                status=str(md.get("pool_status")),
            )
        )
    # Deduplicate near-identical bottoms, keep lowest bottom first
    pools.sort(key=lambda p: (p.bottom, p.top))
    deduped: list[UpperPool] = []
    for p in pools:
        if deduped and abs(p.bottom - deduped[-1].bottom) / max(deduped[-1].bottom, 1e-12) < 0.0005:
            # Prefer slightly taller / stronger top within same cluster
            if p.top > deduped[-1].top:
                deduped[-1] = p
            continue
        deduped.append(p)
    return deduped


def pools_above_price(pools: list[UpperPool], price: float) -> list[UpperPool]:
    return [p for p in pools if p.bottom > price]


def pick_meaningful_ladder(
    above: list[UpperPool],
    *,
    min_sep_pct: float,
) -> tuple[UpperPool | None, UpperPool | None]:
    """Nearest overhead pool + next pool at least ``min_sep_pct`` further up.

    Skips 5m micro-duplicates so strong-delta continuation can target the next
    real liquidity cluster instead of a 0.05% neighbor.
    """
    if not above:
        return None, None
    first = above[0]
    if min_sep_pct <= 0:
        second = above[1] if len(above) > 1 else None
        return first, second
    threshold = first.bottom * (1.0 + float(min_sep_pct))
    second = None
    for p in above[1:]:
        if p.bottom >= threshold:
            second = p
            break
    return first, second
