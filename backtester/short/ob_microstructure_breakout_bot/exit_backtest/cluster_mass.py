"""Cluster-mass + delta/OB reachability analysis for upper LLD pools.

Groups ACTIVE upper pools into clusters, scores each cluster by mass, and
labels how price interacted with it on the path from entry to max high.

Reachability is scored as a function of:
  f(cluster_mass, distance_from_entry, delta, OB)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Default matches LLD chart cluster_gap_pct.
DEFAULT_CLUSTER_GAP_PCT = 0.10  # percent of price between pool edges


@dataclass(frozen=True)
class PoolSnap:
    bottom: float
    top: float
    strength: float
    pool_id: str = ""

    @property
    def mid(self) -> float:
        return 0.5 * (self.bottom + self.top)


@dataclass
class ClusterSnap:
    cluster_id: str
    pools: list[PoolSnap]
    bottom: float
    top: float
    n_pools: int
    strength_sum: float
    strength_avg: float
    strength_max: float
    width_pct: float
    width_bps: float
    density_per_pct: float
    dist_from_entry_pct: float
    # interaction vs path to max
    label: str = "ignored"
    reached: bool = False
    touch_ts: str | None = None
    delta_at_touch: float | None = None
    ob_at_touch: float | None = None
    overshoot_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pools"] = [
            {
                "bottom": p.bottom,
                "top": p.top,
                "strength": p.strength,
                "pool_id": p.pool_id,
            }
            for p in self.pools
        ]
        return d


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_active_upper_pools_1m(
    symbol: str,
    as_of: datetime,
    entry: float,
    *,
    lookback_hours: int = 12,
    lookforward_hours: int = 8,
) -> list[PoolSnap]:
    """Causal 1m ACTIVE upper pools above entry at ``as_of``."""
    from dashboard.research_charts.service import liquidity_location_overlay_bundle

    as_of = _ensure_utc(as_of)
    start = int((as_of - timedelta(hours=lookback_hours)).timestamp())
    end = int((as_of + timedelta(hours=lookforward_hours)).timestamp())
    payload = liquidity_location_overlay_bundle(
        symbol=symbol,
        timeframe="1m",
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


def group_pools_into_clusters(
    pools: list[PoolSnap],
    entry: float,
    *,
    gap_pct: float = DEFAULT_CLUSTER_GAP_PCT,
) -> list[ClusterSnap]:
    """Merge nearby upper pools into clusters by edge gap.

    Two consecutive pools join the same cluster when the gap between the lower
    pool's top and the next pool's bottom is <= ``gap_pct`` percent of mid price.
    """
    if not pools:
        return []
    ordered = sorted(pools, key=lambda p: (p.bottom, p.top))
    groups: list[list[PoolSnap]] = [[ordered[0]]]
    for p in ordered[1:]:
        prev = groups[-1][-1]
        mid = 0.5 * (prev.top + p.bottom)
        gap = max(0.0, p.bottom - prev.top)
        gap_pct_val = (gap / mid) * 100.0 if mid > 0 else 0.0
        # Also merge when overlapping / contained.
        if gap_pct_val <= gap_pct or p.bottom <= prev.top:
            groups[-1].append(p)
        else:
            groups.append([p])

    clusters: list[ClusterSnap] = []
    for i, grp in enumerate(groups, 1):
        bottom = min(p.bottom for p in grp)
        top = max(p.top for p in grp)
        strengths = [p.strength for p in grp]
        strength_sum = float(sum(strengths))
        strength_avg = strength_sum / len(grp)
        strength_max = float(max(strengths))
        width_pct = ((top - bottom) / entry) * 100.0 if entry > 0 else 0.0
        width_bps = width_pct * 100.0
        density = (strength_sum / width_pct) if width_pct > 1e-9 else strength_sum
        dist = ((bottom - entry) / entry) * 100.0 if entry > 0 else 0.0
        clusters.append(
            ClusterSnap(
                cluster_id=f"C{i}",
                pools=list(grp),
                bottom=bottom,
                top=top,
                n_pools=len(grp),
                strength_sum=strength_sum,
                strength_avg=strength_avg,
                strength_max=strength_max,
                width_pct=width_pct,
                width_bps=width_bps,
                density_per_pct=float(density),
                dist_from_entry_pct=dist,
            )
        )
    return clusters


def _ob_ratio(symbol: str, when: datetime) -> float | None:
    from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands

    try:
        ob = sample_ob_bands(symbol, when)
        if ob.ask_5bps <= 0:
            return None
        return float(ob.bid_5bps / ob.ask_5bps)
    except Exception:
        return None


def _delta_10m(symbol: str, end: datetime) -> float:
    from ob_microstructure_breakout_bot.data.trades import load_trade_window

    win = load_trade_window(symbol, end - timedelta(minutes=10), end)
    return float(win.delta_notional)


def classify_clusters_vs_path(
    clusters: list[ClusterSnap],
    *,
    symbol: str,
    entry: float,
    bars: list[Any],
    peak_bar: Any,
    peak_high: float,
) -> list[ClusterSnap]:
    """Label each cluster against the path from entry to max high."""
    for c in clusters:
        touch = next(
            (b for b in bars if b.high >= c.bottom and b.ts <= peak_bar.ts),
            None,
        )
        if touch is None:
            c.label = "ignored"
            c.reached = False
            continue

        c.reached = True
        c.touch_ts = touch.ts.isoformat()
        c.delta_at_touch = _delta_10m(symbol, touch.ts + timedelta(minutes=5))
        c.ob_at_touch = _ob_ratio(symbol, touch.ts)
        c.overshoot_pct = ((peak_high - c.top) / entry) * 100.0

        after = [b for b in bars if touch.ts <= b.ts <= peak_bar.ts]
        dipped = any(b.low < c.bottom * 0.999 for b in after[:12])
        cleared_well = peak_high >= c.top * 1.002
        into_but_not_far = peak_high >= c.bottom and peak_high < c.top * 1.002
        near_peak = (
            abs(c.bottom - peak_high) / entry < 0.006
            or c.top >= peak_high * 0.995
            or abs(c.top - peak_high) / entry < 0.004
        )

        if cleared_well and near_peak:
            c.label = "reaction_near_high"
        elif into_but_not_far and near_peak:
            c.label = "reversal_point"
        elif cleared_well and dipped:
            c.label = "zwischenstation"  # passed through after pause
        elif cleared_well:
            c.label = "zwischenstation"
        elif near_peak:
            c.label = "gebremst"
        else:
            c.label = "reaction_point"
    return clusters


def pick_reversal_cluster(clusters: list[ClusterSnap]) -> ClusterSnap | None:
    """Cluster most associated with the turn at/near the high."""
    candidates = [
        c
        for c in clusters
        if c.reached
        and c.label in {"reversal_point", "reaction_near_high", "gebremst", "reaction_point"}
    ]
    if candidates:
        # Prefer highest / strongest near the top.
        return max(candidates, key=lambda c: (c.bottom, c.strength_sum))
    reached = [c for c in clusters if c.reached]
    if not reached:
        return None
    return max(reached, key=lambda c: c.top)


def reachability_bucket(
    *,
    dist_pct: float,
    strength_sum: float,
    confirm_delta: float,
    entry_ob: float | None,
    reached: bool,
) -> str:
    """Coarse reachability class for the matrix."""
    far = dist_pct >= 1.5
    mid = 0.8 <= dist_pct < 1.5
    strong_delta = confirm_delta >= 200_000
    weak_ob = entry_ob is not None and entry_ob < 1.05
    massive = strength_sum >= 8.0
    solid = strength_sum >= 4.0

    if reached:
        if far and not strong_delta:
            return "reached_surprising"  # far without strong delta
        if far and strong_delta:
            return "reachable_with_strong_delta"
        if mid:
            return "reachable_moderate"
        return "leicht_erreichbar"

    # not reached
    if far and not strong_delta:
        return "praktisch_nicht_erreichbar"
    if far and strong_delta and weak_ob:
        return "unrealistisch_trotz_delta_ob_schwach"
    if far and strong_delta and (massive or solid):
        return "nur_mit_starkem_delta_erreichbar_aber_verfehlt"
    if mid and not strong_delta:
        return "nur_mit_starkem_delta_erreichbar"
    return "nicht_erreicht"


def analyze_signal_clusters(
    symbol: str,
    *,
    decision_ts: datetime,
    entry_price: float,
    tier: str,
    confirm_delta: float,
    followthrough_delta: float,
    hold_hours: int = 12,
    gap_pct: float = DEFAULT_CLUSTER_GAP_PCT,
) -> dict[str, Any]:
    """Full cluster-mass analysis for one long signal."""
    from ob_microstructure_breakout_bot.data.bars import load_5m_bars

    decision_ts = _ensure_utc(decision_ts)
    pools = load_active_upper_pools_1m(symbol, decision_ts, entry_price)
    clusters = group_pools_into_clusters(pools, entry_price, gap_pct=gap_pct)

    bars = load_5m_bars(
        symbol,
        decision_ts,
        decision_ts + timedelta(hours=hold_hours),
    )
    bars = [b for b in bars if b.ts >= decision_ts]
    if not bars:
        return {
            "signal_ts": decision_ts.isoformat(),
            "entry_price": entry_price,
            "error": "no_bars",
        }

    peak_bar = max(bars, key=lambda b: b.high)
    peak_high = float(peak_bar.high)
    classify_clusters_vs_path(
        clusters,
        symbol=symbol,
        entry=entry_price,
        bars=bars,
        peak_bar=peak_bar,
        peak_high=peak_high,
    )

    entry_ob = _ob_ratio(symbol, decision_ts)
    d_entry = _delta_10m(symbol, decision_ts + timedelta(minutes=5))
    d_peak = _delta_10m(symbol, peak_bar.ts + timedelta(minutes=5))
    reversal = pick_reversal_cluster(clusters)

    reached = [c for c in clusters if c.reached]
    ignored = [c for c in clusters if not c.reached]
    zwischen = [c for c in clusters if c.label == "zwischenstation"]
    strongest = max(clusters, key=lambda c: c.strength_sum) if clusters else None
    heaviest_reached = max(reached, key=lambda c: c.strength_sum) if reached else None

    # Matrix rows: one per cluster
    matrix_rows = []
    for c in clusters:
        bucket = reachability_bucket(
            dist_pct=c.dist_from_entry_pct,
            strength_sum=c.strength_sum,
            confirm_delta=confirm_delta,
            entry_ob=entry_ob,
            reached=c.reached,
        )
        matrix_rows.append(
            {
                "cluster_id": c.cluster_id,
                "dist_pct": c.dist_from_entry_pct,
                "n_pools": c.n_pools,
                "strength_sum": c.strength_sum,
                "strength_avg": c.strength_avg,
                "width_pct": c.width_pct,
                "density_per_pct": c.density_per_pct,
                "reached": c.reached,
                "label": c.label,
                "delta_at_touch": c.delta_at_touch,
                "ob_at_touch": c.ob_at_touch,
                "confirm_delta": confirm_delta,
                "entry_ob": entry_ob,
                "reachability_class": bucket,
            }
        )

    max_exc_pct = (peak_high - entry_price) / entry_price * 100.0
    outcome = classify_move_outcome(
        max_exc_pct=max_exc_pct,
        strongest=strongest,
        heaviest_reached=heaviest_reached,
        peak_high=peak_high,
        entry=entry_price,
    )

    return {
        "signal_ts": decision_ts.isoformat(),
        "symbol": symbol,
        "tier": tier,
        "entry_price": entry_price,
        "confirm_delta": confirm_delta,
        "followthrough_delta": followthrough_delta,
        "entry_ob": entry_ob,
        "d10_entry_close": d_entry,
        "d10_at_peak": d_peak,
        "max_high": peak_high,
        "max_ts": peak_bar.ts.isoformat(),
        "max_exc_pct": max_exc_pct,
        "outcome": outcome,
        "n_pools": len(pools),
        "n_clusters": len(clusters),
        "n_reached": len(reached),
        "n_ignored": len(ignored),
        "n_zwischenstation": len(zwischen),
        "strongest_cluster": None if strongest is None else strongest.to_dict(),
        "heaviest_reached_cluster": None
        if heaviest_reached is None
        else heaviest_reached.to_dict(),
        "reversal_cluster": None if reversal is None else reversal.to_dict(),
        "clusters": [c.to_dict() for c in clusters],
        "reachability_matrix": matrix_rows,
    }


def classify_move_outcome(
    *,
    max_exc_pct: float,
    strongest: ClusterSnap | None,
    heaviest_reached: ClusterSnap | None,
    peak_high: float,
    entry: float,
) -> str:
    """Phase-D label: working / failed / overshoot."""
    if heaviest_reached is None:
        if max_exc_pct >= 1.0:
            return "working_unmapped"  # big move but no upper cluster tagged
        return "failed_early"
    if max_exc_pct < 0.5:
        return "failed_early"
    if strongest is not None and strongest.reached:
        overshoot = ((peak_high - strongest.top) / entry) * 100.0
        if overshoot >= 1.0:
            return "overshoot_beyond_strong"
        return "working_to_strong_cluster"
    # Reached something but not the strongest-by-mass cluster.
    if max_exc_pct >= 1.0:
        return "working_partial"
    return "failed_early"


def summarize_cross_signal(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if not r.get("error")]
    if not ok:
        return {"n": 0}

    # Aggregate reachability classes
    class_counts: dict[str, int] = {}
    for r in ok:
        for row in r.get("reachability_matrix") or []:
            cls = str(row.get("reachability_class") or "unknown")
            class_counts[cls] = class_counts.get(cls, 0) + 1

    # Dist × delta hit rates for clusters with mass
    bands = [(0.0, 0.8), (0.8, 1.5), (1.5, 2.5), (2.5, 99.0)]
    delta_cuts = [(0.0, 200_000, "delta_lt_200k"), (200_000, 1e18, "delta_ge_200k")]
    grid = []
    for d0, d1 in bands:
        for lo, hi, dname in delta_cuts:
            rows = []
            for r in ok:
                if not (lo <= float(r.get("confirm_delta") or 0) < hi):
                    continue
                for row in r.get("reachability_matrix") or []:
                    dist = float(row.get("dist_pct") or 0)
                    if d0 <= dist < d1:
                        rows.append(row)
            n = len(rows)
            hits = sum(1 for x in rows if x.get("reached"))
            mean_mass = (
                sum(float(x.get("strength_sum") or 0) for x in rows) / n if n else None
            )
            grid.append(
                {
                    "dist_band": f"{d0}-{d1}%",
                    "delta_bucket": dname,
                    "n_clusters": n,
                    "hit_rate": (hits / n) if n else None,
                    "mean_strength_sum": mean_mass,
                }
            )

    # Reversal cluster mass stats
    rev_mass = []
    for r in ok:
        rev = r.get("reversal_cluster")
        if not rev:
            continue
        rev_mass.append(
            {
                "signal_ts": r["signal_ts"],
                "n_pools": rev.get("n_pools"),
                "strength_sum": rev.get("strength_sum"),
                "strength_avg": rev.get("strength_avg"),
                "width_pct": rev.get("width_pct"),
                "dist_pct": rev.get("dist_from_entry_pct"),
                "label": rev.get("label"),
                "max_exc_pct": r.get("max_exc_pct"),
            }
        )

    # Strongest-by-mass hit rate
    strongest_hits = 0
    for r in ok:
        sc = r.get("strongest_cluster")
        if sc and sc.get("reached"):
            strongest_hits += 1

    # Phase D: outcome cohorts
    by_outcome: dict[str, list[dict[str, Any]]] = {}
    for r in ok:
        by_outcome.setdefault(str(r.get("outcome") or "unknown"), []).append(r)

    def _cohort_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        revs = [r.get("reversal_cluster") for r in rows if r.get("reversal_cluster")]
        return {
            "n": len(rows),
            "mean_max_exc_pct": sum(float(r["max_exc_pct"]) for r in rows) / len(rows),
            "mean_confirm_delta": sum(float(r.get("confirm_delta") or 0) for r in rows)
            / len(rows),
            "mean_entry_ob": (
                sum(float(r["entry_ob"]) for r in rows if r.get("entry_ob") is not None)
                / max(1, sum(1 for r in rows if r.get("entry_ob") is not None))
            ),
            "mean_reversal_strength_sum": (
                sum(float(x.get("strength_sum") or 0) for x in revs) / len(revs)
                if revs
                else None
            ),
            "mean_reversal_n_pools": (
                sum(float(x.get("n_pools") or 0) for x in revs) / len(revs) if revs else None
            ),
            "strongest_hit_rate": (
                sum(
                    1
                    for r in rows
                    if (r.get("strongest_cluster") or {}).get("reached")
                )
                / len(rows)
            ),
        }

    return {
        "n_signals": len(ok),
        "mean_max_exc_pct": sum(float(r["max_exc_pct"]) for r in ok) / len(ok),
        "strongest_cluster_hit_rate": strongest_hits / len(ok),
        "reachability_class_counts": class_counts,
        "dist_delta_hit_grid": grid,
        "reversal_cluster_mass": rev_mass,
        "mean_reversal_strength_sum": (
            sum(float(x["strength_sum"] or 0) for x in rev_mass) / len(rev_mass)
            if rev_mass
            else None
        ),
        "mean_reversal_n_pools": (
            sum(float(x["n_pools"] or 0) for x in rev_mass) / len(rev_mass)
            if rev_mass
            else None
        ),
        "mean_reversal_width_pct": (
            sum(float(x["width_pct"] or 0) for x in rev_mass) / len(rev_mass)
            if rev_mass
            else None
        ),
        "outcome_counts": {k: len(v) for k, v in by_outcome.items()},
        "by_outcome": {k: _cohort_stats(v) for k, v in by_outcome.items()},
    }


def pick_calibrated_long_tp_cluster(
    symbol: str,
    *,
    decision_ts: datetime,
    entry_price: float,
    confirm_delta: float,
    entry_ob: float | None,
) -> ClusterSnap | None:
    """Heaviest 1m upper cluster in the Phase-C calibrated band.

    Returns None when entry flow fails the gates or no cluster matches.
    """
    from ob_microstructure_breakout_bot.exit_backtest.thresholds import (
        TP_MAX_TARGET_DIST_PCT,
        TP_MIN_ABS_CONFIRM_DELTA,
        TP_MIN_CLUSTER_STRENGTH_SUM,
        TP_MIN_OB_RATIO_LONG,
        TP_MIN_TARGET_DIST_PCT,
        TP_REQUIRE_OB,
    )

    if abs(float(confirm_delta)) < TP_MIN_ABS_CONFIRM_DELTA:
        return None
    if TP_REQUIRE_OB:
        if entry_ob is None or float(entry_ob) < TP_MIN_OB_RATIO_LONG:
            return None
    elif entry_ob is not None and float(entry_ob) < TP_MIN_OB_RATIO_LONG:
        return None

    pools = load_active_upper_pools_1m(symbol, decision_ts, entry_price)
    clusters = group_pools_into_clusters(
        pools, entry_price, gap_pct=DEFAULT_CLUSTER_GAP_PCT
    )
    cands = [
        c
        for c in clusters
        if TP_MIN_TARGET_DIST_PCT
        <= float(c.dist_from_entry_pct)
        <= TP_MAX_TARGET_DIST_PCT
        and float(c.strength_sum) >= TP_MIN_CLUSTER_STRENGTH_SUM
    ]
    if not cands:
        return None
    return max(cands, key=lambda c: float(c.strength_sum))
