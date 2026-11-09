"""5m cluster-pool bounce / reversal analysis (no OB).

Question: when price enters the heaviest upper 5m cluster, how often does it
bounce, and by how many percent does it reverse? Then the same for the next-
smaller clusters ranked by mass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import (
    DEFAULT_CLUSTER_GAP_PCT,
    ClusterSnap,
    PoolSnap,
    group_pools_into_clusters,
)

# Look-forward after first touch into a cluster (5m bars).
BOUNCE_HOLD_BARS = 36  # 3 hours
# Minimum reverse from local high after touch to count as bounce.
BOUNCE_MIN_PCT = 0.25
# Clear through: high exceeds cluster top by this fraction of entry.
PIERCE_CLEAR_PCT = 0.15


@dataclass
class BounceEvent:
    rank: int  # 1 = heaviest by strength_sum
    cluster_id: str
    strength_sum: float
    n_pools: int
    dist_from_entry_pct: float
    width_pct: float
    bottom: float
    top: float
    reached: bool
    touch_ts: str | None
    touch_price: float | None  # first bar high that entered
    local_high: float | None  # max high in bounce window after touch
    local_low: float | None  # min low in bounce window after touch
    bounce_pct: float | None  # (local_high - local_low) / entry * 100 from high
    reverse_from_high_pct: float | None  # same, explicit
    reverse_from_entry_into_cluster_pct: float | None
    pierced: bool
    bounced: bool
    outcome: str  # bounce | pierce | weak_reaction | not_reached

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_active_upper_pools_5m(
    symbol: str,
    as_of: datetime,
    entry: float,
    *,
    lookback_hours: int = 12,
    lookforward_hours: int = 8,
) -> list[PoolSnap]:
    """Causal 5m ACTIVE upper pools above entry (with strength)."""
    from dashboard.research_charts.service import liquidity_location_overlay_bundle

    as_of = _ensure_utc(as_of)
    start = int((as_of - timedelta(hours=lookback_hours)).timestamp())
    end = int((as_of + timedelta(hours=lookforward_hours)).timestamp())
    payload = liquidity_location_overlay_bundle(
        symbol=symbol,
        timeframe="5m",
        start=start,
        end=end,
        allow_stale=True,
        liquidity_location_as_of=as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    pools: list[PoolSnap] = []
    for ov in (payload.get("liquidity") or {}).get("overlays") or []:
        md = ov.get("metadata") or {}
        if md.get("side") != "upper":
            continue
        if str(md.get("pool_status") or "").upper() != "ACTIVE":
            continue
        bottom = ov.get("bottom_price")
        top = ov.get("top_price")
        if bottom is None or top is None:
            continue
        b = float(bottom)
        t = float(top)
        if b <= entry:
            continue
        pools.append(
            PoolSnap(
                bottom=b,
                top=t,
                strength=float(md.get("strength") or 0.0),
                pool_id=str(md.get("pool_id") or ov.get("id") or ""),
            )
        )
    pools.sort(key=lambda p: (p.bottom, p.top))
    return pools


def rank_clusters_by_mass(clusters: list[ClusterSnap]) -> list[ClusterSnap]:
    """Heaviest first (rank 1)."""
    return sorted(clusters, key=lambda c: (c.strength_sum, c.n_pools, c.top), reverse=True)


def measure_cluster_bounce(
    cluster: ClusterSnap,
    *,
    rank: int,
    entry: float,
    bars: list[Any],
    hold_bars: int = BOUNCE_HOLD_BARS,
    bounce_min_pct: float = BOUNCE_MIN_PCT,
    pierce_clear_pct: float = PIERCE_CLEAR_PCT,
) -> BounceEvent:
    """First touch into cluster → bounce / pierce metrics (price only, no OB).

    Bounce = after the local high following first entry, price pulls back by at
    least ``bounce_min_pct`` of entry. Pierce = local high clears cluster top.
    """
    touch_idx = next(
        (i for i, b in enumerate(bars) if float(b.high) >= cluster.bottom),
        None,
    )
    if touch_idx is None:
        return BounceEvent(
            rank=rank,
            cluster_id=cluster.cluster_id,
            strength_sum=float(cluster.strength_sum),
            n_pools=int(cluster.n_pools),
            dist_from_entry_pct=float(cluster.dist_from_entry_pct),
            width_pct=float(cluster.width_pct),
            bottom=float(cluster.bottom),
            top=float(cluster.top),
            reached=False,
            touch_ts=None,
            touch_price=None,
            local_high=None,
            local_low=None,
            bounce_pct=None,
            reverse_from_high_pct=None,
            reverse_from_entry_into_cluster_pct=None,
            pierced=False,
            bounced=False,
            outcome="not_reached",
        )

    touch = bars[touch_idx]
    after = bars[touch_idx : touch_idx + max(1, hold_bars)]
    # Peak after entry into cluster, then pullback from that peak.
    peak_i = max(range(len(after)), key=lambda i: float(after[i].high))
    local_high = float(after[peak_i].high)
    after_peak = after[peak_i:]
    local_low = min(float(b.low) for b in after_peak)
    reverse_from_high = ((local_high - local_low) / entry) * 100.0 if entry > 0 else 0.0
    reverse_from_entry = (
        ((float(touch.high) - local_low) / entry) * 100.0 if entry > 0 else 0.0
    )
    clear_pct = ((local_high - cluster.top) / entry) * 100.0 if entry > 0 else 0.0
    pierced = clear_pct >= pierce_clear_pct
    # Bounce requires a real pullback from the post-touch peak.
    pulled_back = reverse_from_high >= bounce_min_pct
    left_zone = local_low < cluster.bottom
    bounced = pulled_back and (left_zone or not pierced)

    if pierced and bounced:
        outcome = "pierce_then_fade"
    elif pierced and not bounced:
        outcome = "pierce"
    elif bounced:
        outcome = "bounce"
    else:
        outcome = "weak_reaction"

    return BounceEvent(
        rank=rank,
        cluster_id=cluster.cluster_id,
        strength_sum=float(cluster.strength_sum),
        n_pools=int(cluster.n_pools),
        dist_from_entry_pct=float(cluster.dist_from_entry_pct),
        width_pct=float(cluster.width_pct),
        bottom=float(cluster.bottom),
        top=float(cluster.top),
        reached=True,
        touch_ts=touch.ts.isoformat(),
        touch_price=float(touch.high),
        local_high=float(local_high),
        local_low=float(local_low),
        bounce_pct=float(reverse_from_high),
        reverse_from_high_pct=float(reverse_from_high),
        reverse_from_entry_into_cluster_pct=float(reverse_from_entry),
        pierced=bool(pierced),
        bounced=bool(bounced),
        outcome=outcome,
    )


def analyze_signal_bounce(
    symbol: str,
    *,
    decision_ts: datetime,
    entry_price: float,
    tier: str = "",
    source: str = "",
    hold_hours: int = 24,
    gap_pct: float = DEFAULT_CLUSTER_GAP_PCT,
    hold_bars: int = BOUNCE_HOLD_BARS,
    bounce_min_pct: float = BOUNCE_MIN_PCT,
    max_ranks: int = 5,
) -> dict[str, Any]:
    """Rank 5m upper clusters by mass and measure bounce on first touch (no OB)."""
    from ob_microstructure_breakout_bot.data.bars import load_5m_bars

    decision_ts = _ensure_utc(decision_ts)
    pools = load_active_upper_pools_5m(symbol, decision_ts, entry_price)
    clusters = group_pools_into_clusters(pools, entry_price, gap_pct=gap_pct)
    ranked = rank_clusters_by_mass(clusters)[: max(1, max_ranks)]

    bars = load_5m_bars(
        symbol,
        decision_ts,
        decision_ts + timedelta(hours=hold_hours),
    )
    bars = [b for b in bars if b.ts >= decision_ts]

    events: list[BounceEvent] = []
    for i, c in enumerate(ranked, 1):
        events.append(
            measure_cluster_bounce(
                c,
                rank=i,
                entry=entry_price,
                bars=bars,
                hold_bars=hold_bars,
                bounce_min_pct=bounce_min_pct,
            )
        )

    reached = [e for e in events if e.reached]
    bounced = [e for e in events if e.bounced]
    return {
        "symbol": symbol,
        "decision_ts": decision_ts.isoformat(),
        "entry_price": entry_price,
        "tier": tier,
        "source": source,
        "timeframe": "5m",
        "n_pools": len(pools),
        "n_clusters": len(clusters),
        "n_ranked": len(ranked),
        "n_reached": len(reached),
        "n_bounced": len(bounced),
        "events": [e.to_dict() for e in events],
    }


def summarize_bounce_by_rank(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate bounce rate / magnitude per mass-rank across signals."""
    by_rank: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        for ev in row.get("events") or []:
            r = int(ev.get("rank") or 0)
            by_rank.setdefault(r, []).append(ev)

    out: dict[str, Any] = {}
    for rank, evs in sorted(by_rank.items()):
        reached = [e for e in evs if e.get("reached")]
        bounced = [e for e in reached if e.get("bounced")]
        pierced = [e for e in reached if e.get("pierced")]
        bounce_pcts = [
            float(e["bounce_pct"])
            for e in reached
            if e.get("bounce_pct") is not None
        ]
        bounced_pcts = [
            float(e["bounce_pct"])
            for e in bounced
            if e.get("bounce_pct") is not None
        ]
        outcomes: dict[str, int] = {}
        for e in reached:
            k = str(e.get("outcome") or "unknown")
            outcomes[k] = outcomes.get(k, 0) + 1
        out[f"rank_{rank}"] = {
            "rank": rank,
            "n_signals_with_cluster": len(evs),
            "n_reached": len(reached),
            "n_bounced": len(bounced),
            "n_pierced": len(pierced),
            "reach_rate": (len(reached) / len(evs)) if evs else None,
            "bounce_rate_of_reached": (len(bounced) / len(reached)) if reached else None,
            "pierce_rate_of_reached": (len(pierced) / len(reached)) if reached else None,
            "mean_bounce_pct_all_reached": (
                sum(bounce_pcts) / len(bounce_pcts) if bounce_pcts else None
            ),
            "mean_bounce_pct_when_bounced": (
                sum(bounced_pcts) / len(bounced_pcts) if bounced_pcts else None
            ),
            "median_bounce_pct_when_bounced": (
                sorted(bounced_pcts)[len(bounced_pcts) // 2] if bounced_pcts else None
            ),
            "mean_strength_sum": (
                sum(float(e.get("strength_sum") or 0) for e in evs) / len(evs)
                if evs
                else None
            ),
            "mean_dist_pct": (
                sum(float(e.get("dist_from_entry_pct") or 0) for e in evs) / len(evs)
                if evs
                else None
            ),
            "by_outcome": outcomes,
        }
    return out
