"""EMA59-touch-triggered cluster-mass analysis.

Same trigger model as DOGE OB calibration:
  - start at EMA59 touch (``bar_ts``)
  - measure max excursion above (long) / below (short) the EMA59 band
  - attach causal upper/lower pool clusters and score reachability
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ob_microstructure_breakout_bot.backtest.scan_touches import (
    TouchEvent,
    detect_ema59_touches,
)
from ob_microstructure_breakout_bot.data.bars import Bar5m, load_5m_bars
from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands
from ob_microstructure_breakout_bot.data.trades import load_trade_window
from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import (
    DEFAULT_CLUSTER_GAP_PCT,
    ClusterSnap,
    PoolSnap,
    classify_clusters_vs_path,
    group_pools_into_clusters,
    pick_reversal_cluster,
    reachability_bucket,
)
from ob_microstructure_breakout_bot.models import TouchDirection


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _ob_ratio(symbol: str, when: datetime) -> float | None:
    try:
        ob = sample_ob_bands(symbol, when)
        if ob.ask_5bps <= 0:
            return None
        return float(ob.bid_5bps / ob.ask_5bps)
    except Exception:
        return None


def _delta_window(symbol: str, start: datetime, end: datetime) -> float:
    win = load_trade_window(symbol, start, end)
    return float(win.delta_notional)


def load_pools_for_side(
    symbol: str,
    as_of: datetime,
    *,
    side: str,
    ref_price: float,
    lookback_hours: int = 12,
    lookforward_hours: int = 8,
) -> list[PoolSnap]:
    """ACTIVE pools on the continuation side of the move."""
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
    want = "upper" if side == "long" else "lower"
    pools: list[PoolSnap] = []
    for ov in (payload.get("liquidity") or {}).get("overlays") or []:
        md = ov.get("metadata") or {}
        if md.get("side") != want:
            continue
        if str(md.get("pool_status") or "").upper() != "ACTIVE":
            continue
        bottom = ov.get("bottom_price")
        top = ov.get("top_price")
        if bottom is None or top is None:
            continue
        b = float(bottom)
        t = float(top)
        if side == "long" and b <= ref_price:
            continue
        if side == "short" and t >= ref_price:
            continue
        pools.append(
            PoolSnap(
                bottom=b,
                top=t,
                strength=float(md.get("strength") or 0.0),
                pool_id=str(md.get("pool_id") or ov.get("id") or ""),
            )
        )
    if side == "long":
        pools.sort(key=lambda p: (p.bottom, p.top))
    else:
        # For shorts: nearest below first (highest top first)
        pools.sort(key=lambda p: (-p.top, -p.bottom))
    return pools


def _max_excursion_long(
    bars: list[Bar5m],
    *,
    touch_idx: int,
    ema59: float,
    hold_bars: int,
) -> tuple[Bar5m, float, float, float]:
    """Return peak bar, max_high, exc_vs_ema59_pct, exc_vs_touch_close_pct."""
    window = bars[touch_idx : touch_idx + 1 + hold_bars]
    peak = max(window, key=lambda b: b.high)
    touch_close = bars[touch_idx].close
    max_high = float(peak.high)
    vs_ema = (max_high - ema59) / ema59 * 100.0
    vs_touch = (max_high - touch_close) / touch_close * 100.0
    return peak, max_high, vs_ema, vs_touch


def _max_excursion_short(
    bars: list[Bar5m],
    *,
    touch_idx: int,
    ema59: float,
    hold_bars: int,
) -> tuple[Bar5m, float, float, float]:
    window = bars[touch_idx : touch_idx + 1 + hold_bars]
    trough = min(window, key=lambda b: b.low)
    touch_close = bars[touch_idx].close
    max_low = float(trough.low)
    vs_ema = (ema59 - max_low) / ema59 * 100.0  # positive = below EMA59
    vs_touch = (touch_close - max_low) / touch_close * 100.0
    return trough, max_low, vs_ema, vs_touch


def analyze_touch_clusters(
    symbol: str,
    touch: TouchEvent,
    bars: list[Bar5m],
    *,
    hold_bars: int = 144,  # 12h of 5m bars
    gap_pct: float = DEFAULT_CLUSTER_GAP_PCT,
    context_bars: int = 6,
    confirm_bars: int = 2,
    followthrough_bars: int = 2,
) -> dict[str, Any]:
    """Analyze one EMA59 touch: excursion vs EMA59 + pool cluster path."""
    by_ts = {b.ts: i for i, b in enumerate(bars)}
    idx = by_ts.get(touch.bar_ts)
    if idx is None:
        return {"touch_ts": touch.bar_ts.isoformat(), "error": "touch_bar_missing"}

    side = "long" if touch.direction == TouchDirection.FROM_BELOW else "short"
    ema59 = float(touch.ema.ema59)
    touch_price = float(touch.bar.close)
    ref_price = touch_price  # pool ladder relative to touch close

    if side == "long":
        peak_bar, extreme_px, vs_ema, vs_touch = _max_excursion_long(
            bars, touch_idx=idx, ema59=ema59, hold_bars=hold_bars
        )
        # Reuse long cluster path classifier with synthetic "entry"=touch close
        # and peak high as max.
        path_bars = [b for b in bars[idx:] if b.ts <= peak_bar.ts + timedelta(hours=1)]
        # Ensure peak is in path
        if peak_bar not in path_bars:
            path_bars = bars[idx : idx + hold_bars + 1]
    else:
        peak_bar, extreme_px, vs_ema, vs_touch = _max_excursion_short(
            bars, touch_idx=idx, ema59=ema59, hold_bars=hold_bars
        )
        path_bars = bars[idx : idx + hold_bars + 1]

    # Confirm / FT deltas aligned with scanner windows
    confirm_slice = bars[idx : idx + confirm_bars]
    ft_slice = bars[idx + confirm_bars : idx + confirm_bars + followthrough_bars]
    ctx_slice = bars[max(0, idx - context_bars) : idx]

    def _sum_delta(slice_bars: list[Bar5m]) -> float:
        if not slice_bars:
            return 0.0
        return float(sum(b.buy_notional - b.sell_notional for b in slice_bars))

    confirm_delta = _sum_delta(confirm_slice)
    followthrough_delta = _sum_delta(ft_slice)
    context_delta = _sum_delta(ctx_slice)

    # Public-trade windows ending at touch bar close / confirm end
    touch_close_ts = touch.bar_ts + timedelta(minutes=5)
    d10_touch = _delta_window(
        symbol, touch_close_ts - timedelta(minutes=10), touch_close_ts
    )
    entry_ob = _ob_ratio(symbol, touch.bar_ts)

    pools = load_pools_for_side(symbol, touch.bar_ts, side=side, ref_price=ref_price)
    # Cluster grouping always sorts by bottom ascending; for shorts we still
    # group by price proximity the same way.
    if side == "short":
        # Flip to ascending for grouping, then keep metadata
        pools_sorted = sorted(pools, key=lambda p: (p.bottom, p.top))
    else:
        pools_sorted = pools
    clusters = group_pools_into_clusters(pools_sorted, ref_price, gap_pct=gap_pct)
    if side == "short":
        # Distance below entry must be positive for reachability bands.
        for c in clusters:
            c.dist_from_entry_pct = ((ref_price - c.top) / ref_price) * 100.0
            c.width_pct = ((c.top - c.bottom) / ref_price) * 100.0
            c.width_bps = c.width_pct * 100.0
            c.density_per_pct = (
                c.strength_sum / c.width_pct if c.width_pct > 1e-9 else c.strength_sum
            )

    if side == "long":
        classify_clusters_vs_path(
            clusters,
            symbol=symbol,
            entry=ref_price,
            bars=path_bars,
            peak_bar=peak_bar,
            peak_high=float(extreme_px),
        )
    else:
        # Short path: treat "reach" as price low <= cluster top
        for c in clusters:
            touch_bar = next(
                (b for b in path_bars if b.low <= c.top and b.ts <= peak_bar.ts),
                None,
            )
            if touch_bar is None:
                c.label = "ignored"
                c.reached = False
                continue
            c.reached = True
            c.touch_ts = touch_bar.ts.isoformat()
            c.delta_at_touch = _delta_window(
                symbol,
                touch_bar.ts + timedelta(minutes=5) - timedelta(minutes=10),
                touch_bar.ts + timedelta(minutes=5),
            )
            c.ob_at_touch = _ob_ratio(symbol, touch_bar.ts)
            c.overshoot_pct = (c.bottom - extreme_px) / ref_price * 100.0
            cleared = extreme_px <= c.bottom * 0.998
            near_ext = abs(c.top - extreme_px) / ref_price < 0.006 or c.bottom <= extreme_px * 1.005
            if cleared and near_ext:
                c.label = "reaction_near_high"
            elif cleared:
                c.label = "zwischenstation"
            elif near_ext:
                c.label = "reversal_point"
            else:
                c.label = "reaction_point"

    reversal = pick_reversal_cluster(clusters)
    reached = [c for c in clusters if c.reached]
    strongest = max(clusters, key=lambda c: c.strength_sum) if clusters else None
    heaviest_reached = max(reached, key=lambda c: c.strength_sum) if reached else None

    matrix_rows = []
    for c in clusters:
        bucket = reachability_bucket(
            dist_pct=c.dist_from_entry_pct,
            strength_sum=c.strength_sum,
            confirm_delta=abs(confirm_delta),
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

    # Crossed back through EMA59 after extreme?
    after = [b for b in bars[idx:] if b.ts >= peak_bar.ts][:36]
    if side == "long":
        reclaimed = any(b.close < ema59 for b in after)
    else:
        reclaimed = any(b.close > ema59 for b in after)

    return {
        "touch_ts": touch.bar_ts.isoformat(),
        "symbol": symbol,
        "side": side,
        "direction": touch.direction.value,
        "is_first_in_cluster": touch.is_first_in_cluster,
        "ema59": ema59,
        "ema9": float(touch.ema.ema9),
        "ema20": float(touch.ema.ema20),
        "touch_open": float(touch.bar.open),
        "touch_high": float(touch.bar.high),
        "touch_low": float(touch.bar.low),
        "touch_close": touch_price,
        "confirm_delta": confirm_delta,
        "followthrough_delta": followthrough_delta,
        "context_delta": context_delta,
        "d10_touch_close": d10_touch,
        "touch_ob": entry_ob,
        "extreme_price": extreme_px,
        "extreme_ts": peak_bar.ts.isoformat(),
        "max_exc_vs_ema59_pct": vs_ema,
        "max_exc_vs_touch_pct": vs_touch,
        "reclaimed_ema59_after_extreme": reclaimed,
        "n_pools": len(pools),
        "n_clusters": len(clusters),
        "n_reached": len(reached),
        "strongest_cluster": None if strongest is None else strongest.to_dict(),
        "heaviest_reached_cluster": None
        if heaviest_reached is None
        else heaviest_reached.to_dict(),
        "reversal_cluster": None if reversal is None else reversal.to_dict(),
        "clusters": [c.to_dict() for c in clusters],
        "reachability_matrix": matrix_rows,
    }


def summarize_touch_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if not r.get("error")]
    if not ok:
        return {"n": 0}

    by_side: dict[str, list[dict[str, Any]]] = {"long": [], "short": []}
    for r in ok:
        by_side.setdefault(str(r.get("side") or "unknown"), []).append(r)

    def _side_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        return {
            "n": len(rows),
            "mean_exc_vs_ema59_pct": sum(float(r["max_exc_vs_ema59_pct"]) for r in rows)
            / len(rows),
            "median_exc_vs_ema59_pct": sorted(
                float(r["max_exc_vs_ema59_pct"]) for r in rows
            )[len(rows) // 2],
            "p75_exc_vs_ema59_pct": sorted(
                float(r["max_exc_vs_ema59_pct"]) for r in rows
            )[int((len(rows) - 1) * 0.75)],
            "mean_confirm_delta": sum(float(r.get("confirm_delta") or 0) for r in rows)
            / len(rows),
            "strongest_hit_rate": sum(
                1 for r in rows if (r.get("strongest_cluster") or {}).get("reached")
            )
            / len(rows),
            "mean_reversal_strength_sum": (
                sum(
                    float((r.get("reversal_cluster") or {}).get("strength_sum") or 0)
                    for r in rows
                    if r.get("reversal_cluster")
                )
                / max(1, sum(1 for r in rows if r.get("reversal_cluster")))
            ),
            "pct_reclaimed_ema59": sum(
                1 for r in rows if r.get("reclaimed_ema59_after_extreme")
            )
            / len(rows),
        }

    # Dist x |delta| grid using abs confirm delta
    bands = [(0.0, 0.8), (0.8, 1.5), (1.5, 2.5), (2.5, 99.0)]
    delta_cuts = [(0.0, 200_000, "abs_delta_lt_200k"), (200_000, 1e18, "abs_delta_ge_200k")]
    grid = []
    for d0, d1 in bands:
        for lo, hi, dname in delta_cuts:
            rows = []
            for r in ok:
                if not (lo <= abs(float(r.get("confirm_delta") or 0)) < hi):
                    continue
                for row in r.get("reachability_matrix") or []:
                    dist = float(row.get("dist_pct") or 0)
                    if d0 <= dist < d1:
                        rows.append(row)
            n = len(rows)
            hits = sum(1 for x in rows if x.get("reached"))
            grid.append(
                {
                    "dist_band": f"{d0}-{d1}%",
                    "delta_bucket": dname,
                    "n_clusters": n,
                    "hit_rate": (hits / n) if n else None,
                    "mean_strength_sum": (
                        sum(float(x.get("strength_sum") or 0) for x in rows) / n if n else None
                    ),
                }
            )

    return {
        "n_touches": len(ok),
        "by_side": {k: _side_stats(v) for k, v in by_side.items()},
        "dist_delta_hit_grid": grid,
    }
