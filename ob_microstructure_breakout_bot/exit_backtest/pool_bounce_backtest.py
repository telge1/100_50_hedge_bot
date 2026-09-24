"""Full-history 5m pool bounce backtest (structure + OB/delta + SL/TP).

Implements ``results/ob_pool_5m_bounce_rule.md``:

- rank upper 5m clusters by mass
- first touch candle as event anchor
- ``next_lower_pool_gap_pct`` from touched pool **top** to nearest pool below
- tradeable only when gap >= 0.8%
- watch OB/delta from ~0.8% before pool bottom
- short SL = pool top + 0.2%; TP = next lower pool top
- outcomes: bounce | pierce | weak_reaction | not_reached
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
from ob_microstructure_breakout_bot.exit_backtest.pool_bounce import (
    BOUNCE_HOLD_BARS,
    BOUNCE_MIN_PCT,
    PIERCE_CLEAR_PCT,
    load_active_upper_pools_5m,
    rank_clusters_by_mass,
)

# From bounce_rule.md
MIN_LOWER_GAP_PCT = 0.8  # min TP room: pool_bottom → next lower pool top
WATCH_BEFORE_PCT = 0.8  # start OB/delta watch before pool bottom
SL_ABOVE_POOL_TOP_PCT = 0.2  # short stop above touched pool top

# Flow confirmation at touch (from first bounce backtest)
FLOW_OB_OK = 1.05
FLOW_DELTA_OK = 100_000.0
FLOW_OB_STRONG = 1.20
FLOW_DELTA_STRONG = 200_000.0


@dataclass
class BounceBacktestRow:
    decision_ts: str
    touch_ts: str | None
    rank: int
    cluster_id: str
    pool_bottom: float
    pool_top: float
    strength_sum: float
    next_lower_pool_gap_pct: float | None
    next_lower_pool_top: float | None
    tp_room_pct: float | None  # pool_bottom → TP (must be >= MIN_LOWER_GAP_PCT)
    tradeable_gap: bool
    flow_confirmed: bool
    flow_strong: bool
    watch_ts: str | None
    bounce_pct: float | None
    pierce_pct: float | None
    outcome: str
    ob_ratio_at_watch: float | None
    delta_at_watch: float | None
    ob_ratio_at_touch: float | None
    delta_at_touch: float | None
    entry_price: float  # long-signal entry (context), not bounce short fill
    side: str = "short"
    trade_taken: bool = False
    trade_skip_reason: str = ""
    short_entry_ts: str | None = None
    short_entry_price: float | None = None
    stop_price: float | None = None
    tp_price: float | None = None
    exit_ts: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_pct: float | None = None
    source: str = ""
    tier: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_active_lower_pools_5m(
    symbol: str,
    as_of: datetime,
    *,
    lookback_hours: int = 12,
    lookforward_hours: int = 8,
) -> list[PoolSnap]:
    """Causal 5m ACTIVE lower (support) pools at ``as_of``."""
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
        if md.get("side") != "lower":
            continue
        if str(md.get("pool_status") or "").upper() != "ACTIVE":
            continue
        bottom = ov.get("bottom_price")
        top = ov.get("top_price")
        if bottom is None or top is None:
            continue
        pools.append(
            PoolSnap(
                bottom=float(bottom),
                top=float(top),
                strength=float(md.get("strength") or 0.0),
                pool_id=str(md.get("pool_id") or ov.get("id") or ""),
            )
        )
    pools.sort(key=lambda p: (p.top, p.bottom), reverse=True)
    return pools


def nearest_lower_pool_gap(
    touched_top: float,
    lower_pools: list[PoolSnap],
) -> tuple[float | None, float | None]:
    """Gap % from touched pool top down to nearest lower pool top.

    Returns ``(gap_pct, lower_pool_top)``. Informational; tradeability uses
    ``tp_room_from_bottom_pct`` instead.
    """
    cands = [p for p in lower_pools if p.top < touched_top]
    if not cands:
        return None, None
    # Closest support = highest top still below touched top.
    nearest = max(cands, key=lambda p: p.top)
    if touched_top <= 0:
        return None, float(nearest.top)
    gap = (touched_top - nearest.top) / touched_top * 100.0
    return float(gap), float(nearest.top)


def tp_room_from_bottom_pct(pool_bottom: float, lower_pool_top: float) -> float | None:
    """Expected short reward % from pool bottom down to TP (next lower top)."""
    if pool_bottom <= 0 or lower_pool_top >= pool_bottom:
        return None
    return (pool_bottom - lower_pool_top) / pool_bottom * 100.0


def select_tp_lower_pool(
    *,
    pool_bottom: float,
    pool_top: float,
    lower_pools: list[PoolSnap],
    min_room_pct: float = MIN_LOWER_GAP_PCT,
) -> tuple[float | None, float | None, float | None]:
    """Pick TP at entry/touch time: nearest lower top with room >= min.

    Returns ``(gap_from_touched_top_pct, lower_pool_top, tp_room_from_bottom_pct)``.
    Skips nearer pools that are too close (< min_room_pct).
    """
    cands = [p for p in lower_pools if p.top < float(pool_top)]
    if not cands:
        return None, None, None
    # Nearest first (highest top still below touched top).
    for p in sorted(cands, key=lambda x: x.top, reverse=True):
        room = tp_room_from_bottom_pct(float(pool_bottom), float(p.top))
        if room is None or room < min_room_pct:
            continue
        gap = (float(pool_top) - float(p.top)) / float(pool_top) * 100.0
        return float(gap), float(p.top), float(room)
    return None, None, None


def _ob_ratio(symbol: str, when: datetime) -> float | None:
    from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands

    try:
        ob = sample_ob_bands(symbol, when)
        if ob.ask_5bps <= 0:
            return None
        return float(ob.bid_5bps / ob.ask_5bps)
    except Exception:
        return None


def _delta_10m(symbol: str, end: datetime) -> float | None:
    from ob_microstructure_breakout_bot.data.trades import load_trade_window

    try:
        win = load_trade_window(symbol, end - timedelta(minutes=10), end)
        return float(win.delta_notional)
    except Exception:
        return None


def _first_watch_bar(
    bars: list[Any],
    *,
    pool_bottom: float,
    watch_before_pct: float = WATCH_BEFORE_PCT,
) -> Any | None:
    """First bar whose high reaches within ``watch_before_pct`` of pool bottom."""
    trigger = pool_bottom * (1.0 - watch_before_pct / 100.0)
    for b in bars:
        if float(b.high) >= trigger:
            return b
    return None


def find_short_reversal_entry(
    bars: list[Any],
    *,
    touch_idx: int,
    pool_bottom: float,
    pool_top: float,
    sl_above_pct: float = SL_ABOVE_POOL_TOP_PCT,
    hold_bars: int = BOUNCE_HOLD_BARS,
) -> dict[str, Any]:
    """Find short fill after confirmed reversal; no TP yet."""
    stop = float(pool_top) * (1.0 + sl_above_pct / 100.0)
    end = min(len(bars), touch_idx + max(1, hold_bars))
    for i in range(touch_idx, end):
        b = bars[i]
        if float(b.high) >= stop:
            return {
                "ok": False,
                "reason": "sl_before_entry",
                "entry_idx": None,
                "entry_ts": None,
                "entry_price": None,
                "stop_price": stop,
            }
        if float(b.close) < float(pool_bottom):
            return {
                "ok": True,
                "reason": "",
                "entry_idx": i,
                "entry_ts": b.ts,
                "entry_price": float(b.close),
                "stop_price": stop,
            }
    return {
        "ok": False,
        "reason": "no_reversal_candle",
        "entry_idx": None,
        "entry_ts": None,
        "entry_price": None,
        "stop_price": stop,
    }


def simulate_short_bounce_trade(
    bars: list[Any],
    *,
    touch_idx: int,
    pool_bottom: float,
    pool_top: float,
    tp_price: float,
    sl_above_pct: float = SL_ABOVE_POOL_TOP_PCT,
    hold_bars: int = BOUNCE_HOLD_BARS,
) -> dict[str, Any]:
    """Short after confirmed reversal; SL = pool_top+buffer, TP = next lower top.

    Confirmed reversal = first bar at/after touch whose close is below pool bottom.
    If price tags SL before that confirmation, skip (never entered).
    Same-bar SL+TP: SL wins (conservative).
    """
    stop = float(pool_top) * (1.0 + sl_above_pct / 100.0)
    tp = float(tp_price)
    if tp >= float(pool_bottom):
        return {
            "trade_taken": False,
            "trade_skip_reason": "tp_not_below_pool",
            "short_entry_ts": None,
            "short_entry_price": None,
            "stop_price": stop,
            "tp_price": tp,
            "exit_ts": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
        }

    entry_i: int | None = None
    end = min(len(bars), touch_idx + max(1, hold_bars))
    for i in range(touch_idx, end):
        b = bars[i]
        # Pierced stop before entry → no trade.
        if float(b.high) >= stop:
            return {
                "trade_taken": False,
                "trade_skip_reason": "sl_before_entry",
                "short_entry_ts": None,
                "short_entry_price": None,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": stop,
                "exit_reason": "sl_before_entry",
                "pnl_pct": None,
            }
        if float(b.close) < float(pool_bottom):
            entry_i = i
            break

    if entry_i is None:
        return {
            "trade_taken": False,
            "trade_skip_reason": "no_reversal_candle",
            "short_entry_ts": None,
            "short_entry_price": None,
            "stop_price": stop,
            "tp_price": tp,
            "exit_ts": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
        }

    entry_bar = bars[entry_i]
    entry_px = float(entry_bar.close)
    # Manage from next bar (fill at reversal close; avoid same-bar double-count).
    for j in range(entry_i + 1, end):
        b = bars[j]
        hit_sl = float(b.high) >= stop
        hit_tp = float(b.low) <= tp
        if hit_sl and hit_tp:
            pnl = (entry_px - stop) / entry_px * 100.0
            return {
                "trade_taken": True,
                "trade_skip_reason": "",
                "short_entry_ts": entry_bar.ts.isoformat(),
                "short_entry_price": entry_px,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": stop,
                "exit_reason": "sl",
                "pnl_pct": float(pnl),
            }
        if hit_sl:
            pnl = (entry_px - stop) / entry_px * 100.0
            return {
                "trade_taken": True,
                "trade_skip_reason": "",
                "short_entry_ts": entry_bar.ts.isoformat(),
                "short_entry_price": entry_px,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": stop,
                "exit_reason": "sl",
                "pnl_pct": float(pnl),
            }
        if hit_tp:
            pnl = (entry_px - tp) / entry_px * 100.0
            return {
                "trade_taken": True,
                "trade_skip_reason": "",
                "short_entry_ts": entry_bar.ts.isoformat(),
                "short_entry_price": entry_px,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": tp,
                "exit_reason": "tp",
                "pnl_pct": float(pnl),
            }

    last = bars[end - 1] if end > entry_i else entry_bar
    exit_px = float(last.close)
    pnl = (entry_px - exit_px) / entry_px * 100.0
    return {
        "trade_taken": True,
        "trade_skip_reason": "",
        "short_entry_ts": entry_bar.ts.isoformat(),
        "short_entry_price": entry_px,
        "stop_price": stop,
        "tp_price": tp,
        "exit_ts": last.ts.isoformat(),
        "exit_price": exit_px,
        "exit_reason": "timeout",
        "pnl_pct": float(pnl),
    }


def measure_touch_outcome(
    cluster: ClusterSnap,
    *,
    bars: list[Any],
    entry: float,
    hold_bars: int = BOUNCE_HOLD_BARS,
    bounce_min_pct: float = BOUNCE_MIN_PCT,
    pierce_clear_pct: float = PIERCE_CLEAR_PCT,
) -> dict[str, Any]:
    """Touch-candle bounce/pierce metrics for an upper pool."""
    touch_idx = next(
        (i for i, b in enumerate(bars) if float(b.high) >= cluster.bottom),
        None,
    )
    if touch_idx is None:
        return {
            "reached": False,
            "touch_ts": None,
            "touch_bar": None,
            "bounce_pct": None,
            "pierce_pct": None,
            "outcome": "not_reached",
        }

    touch = bars[touch_idx]
    after = bars[touch_idx : touch_idx + max(1, hold_bars)]
    peak_i = max(range(len(after)), key=lambda i: float(after[i].high))
    local_high = float(after[peak_i].high)
    after_peak = after[peak_i:]
    local_low = min(float(b.low) for b in after_peak)
    bounce_pct = ((local_high - local_low) / entry) * 100.0 if entry > 0 else 0.0
    pierce_pct = ((local_high - cluster.top) / entry) * 100.0 if entry > 0 else 0.0
    pierced = pierce_pct >= pierce_clear_pct
    pulled_back = bounce_pct >= bounce_min_pct
    left_zone = local_low < cluster.bottom
    # Valid bounce: meaningful pullback that leaves the zone (or never pierced).
    bounced = pulled_back and (left_zone or not pierced)

    if bounced:
        outcome = "bounce"
    elif pierced:
        outcome = "pierce"
    else:
        outcome = "weak_reaction"

    return {
        "reached": True,
        "touch_ts": touch.ts.isoformat(),
        "touch_bar": touch,
        "bounce_pct": float(bounce_pct),
        "pierce_pct": float(pierce_pct),
        "outcome": outcome,
        "local_high": local_high,
        "local_low": local_low,
    }


def analyze_signal_bounce_backtest(
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
    min_lower_gap_pct: float = MIN_LOWER_GAP_PCT,
    watch_before_pct: float = WATCH_BEFORE_PCT,
    max_ranks: int = 5,
    sample_flow: bool = True,
) -> dict[str, Any]:
    """One long signal → ranked upper clusters with MD-rule columns."""
    from ob_microstructure_breakout_bot.data.bars import load_5m_bars

    decision_ts = _ensure_utc(decision_ts)
    # Upper pools: which resistance to watch (as-of search start).
    upper = load_active_upper_pools_5m(symbol, decision_ts, entry_price)
    clusters = group_pools_into_clusters(upper, entry_price, gap_pct=gap_pct)
    ranked = rank_clusters_by_mass(clusters)[: max(1, max_ranks)]

    bars = load_5m_bars(
        symbol,
        decision_ts,
        decision_ts + timedelta(hours=hold_hours),
    )
    bars = [b for b in bars if b.ts >= decision_ts]

    rows: list[BounceBacktestRow] = []
    for i, c in enumerate(ranked, 1):
        outcome = measure_touch_outcome(
            c,
            bars=bars,
            entry=entry_price,
            hold_bars=hold_bars,
            bounce_min_pct=bounce_min_pct,
        )

        watch_bar = _first_watch_bar(
            bars, pool_bottom=c.bottom, watch_before_pct=watch_before_pct
        )
        watch_ts = watch_bar.ts.isoformat() if watch_bar is not None else None
        ob_watch = delta_watch = None
        ob_touch = delta_touch = None
        if sample_flow:
            if watch_bar is not None:
                ob_watch = _ob_ratio(symbol, watch_bar.ts)
                delta_watch = _delta_10m(symbol, watch_bar.ts + timedelta(minutes=5))
            touch_bar = outcome.get("touch_bar")
            if touch_bar is not None:
                ob_touch = _ob_ratio(symbol, touch_bar.ts)
                delta_touch = _delta_10m(symbol, touch_bar.ts + timedelta(minutes=5))

        flow_confirmed = (
            ob_touch is not None
            and delta_touch is not None
            and ob_touch >= FLOW_OB_OK
            and delta_touch >= FLOW_DELTA_OK
        )
        flow_strong = (
            ob_touch is not None
            and delta_touch is not None
            and ob_touch >= FLOW_OB_STRONG
            and delta_touch >= FLOW_DELTA_STRONG
        )

        # TP pools: ONLY as-of short entry candle — never from day-before search start.
        gap = lower_top = room = None
        tradeable = False
        trade: dict[str, Any] = {
            "trade_taken": False,
            "trade_skip_reason": "",
            "short_entry_ts": None,
            "short_entry_price": None,
            "stop_price": None,
            "tp_price": None,
            "exit_ts": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
        }
        touch_bar = outcome.get("touch_bar")
        if outcome.get("outcome") == "not_reached" or touch_bar is None:
            trade["trade_skip_reason"] = "not_reached"
        else:
            touch_idx = next(
                (j for j, b in enumerate(bars) if float(b.high) >= c.bottom),
                None,
            )
            if touch_idx is None:
                trade["trade_skip_reason"] = "not_reached"
            else:
                entry_info = find_short_reversal_entry(
                    bars,
                    touch_idx=touch_idx,
                    pool_bottom=float(c.bottom),
                    pool_top=float(c.top),
                    hold_bars=hold_bars,
                )
                trade["stop_price"] = entry_info.get("stop_price")
                if not entry_info.get("ok"):
                    trade["trade_skip_reason"] = str(entry_info.get("reason") or "no_entry")
                    trade["exit_reason"] = trade["trade_skip_reason"]
                else:
                    entry_ts = entry_info["entry_ts"]
                    entry_px = float(entry_info["entry_price"])
                    # Causal lower pools at the actual short entry bar.
                    lower_at_entry = load_active_lower_pools_5m(symbol, entry_ts)
                    gap, lower_top, room = select_tp_lower_pool(
                        pool_bottom=entry_px,
                        pool_top=entry_px,
                        lower_pools=lower_at_entry,
                        min_room_pct=min_lower_gap_pct,
                    )
                    # Also keep informative gap vs touched pool top when TP chosen.
                    if lower_top is not None:
                        gap = (float(c.top) - float(lower_top)) / float(c.top) * 100.0
                        room = (entry_px - float(lower_top)) / entry_px * 100.0
                    tradeable = (
                        lower_top is not None
                        and room is not None
                        and room >= min_lower_gap_pct
                    )
                    if not tradeable:
                        trade["trade_skip_reason"] = (
                            "no_lower_pool" if lower_top is None else "gap_too_small"
                        )
                        trade["short_entry_ts"] = entry_ts.isoformat()
                        trade["short_entry_price"] = entry_px
                    else:
                        trade = simulate_short_bounce_trade(
                            bars,
                            touch_idx=touch_idx,
                            pool_bottom=float(c.bottom),
                            pool_top=float(c.top),
                            tp_price=float(lower_top),
                            hold_bars=hold_bars,
                        )

        rows.append(
            BounceBacktestRow(
                decision_ts=decision_ts.isoformat(),
                touch_ts=outcome.get("touch_ts"),
                rank=i,
                cluster_id=c.cluster_id,
                pool_bottom=float(c.bottom),
                pool_top=float(c.top),
                strength_sum=float(c.strength_sum),
                next_lower_pool_gap_pct=gap,
                next_lower_pool_top=lower_top,
                tp_room_pct=room,
                tradeable_gap=bool(tradeable),
                flow_confirmed=bool(flow_confirmed),
                flow_strong=bool(flow_strong),
                watch_ts=watch_ts,
                bounce_pct=outcome.get("bounce_pct"),
                pierce_pct=outcome.get("pierce_pct"),
                outcome=str(outcome.get("outcome") or "not_reached"),
                ob_ratio_at_watch=ob_watch,
                delta_at_watch=delta_watch,
                ob_ratio_at_touch=ob_touch,
                delta_at_touch=delta_touch,
                entry_price=float(entry_price),
                trade_taken=bool(trade.get("trade_taken")),
                trade_skip_reason=str(trade.get("trade_skip_reason") or ""),
                short_entry_ts=trade.get("short_entry_ts"),
                short_entry_price=trade.get("short_entry_price"),
                stop_price=trade.get("stop_price"),
                tp_price=trade.get("tp_price"),
                exit_ts=trade.get("exit_ts"),
                exit_price=trade.get("exit_price"),
                exit_reason=trade.get("exit_reason"),
                pnl_pct=trade.get("pnl_pct"),
                source=source,
                tier=tier,
            )
        )

    return {
        "symbol": symbol,
        "decision_ts": decision_ts.isoformat(),
        "entry_price": entry_price,
        "tier": tier,
        "source": source,
        "n_upper_pools": len(upper),
        "n_lower_pools_at_entry": "per-row (loaded at short_entry_ts)",
        "n_clusters": len(clusters),
        "tp_pool_as_of": "short_entry_ts",
        "min_lower_gap_pct": min_lower_gap_pct,
        "watch_before_pct": watch_before_pct,
        "rows": [r.to_dict() for r in rows],
    }


def summarize_backtest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate by rank and by tradeable_gap filter."""
    flat: list[dict[str, Any]] = []
    for sig in rows:
        flat.extend(sig.get("rows") or [])

    def _agg(evs: list[dict[str, Any]]) -> dict[str, Any]:
        reached = [e for e in evs if e.get("outcome") != "not_reached"]
        bounced = [e for e in reached if e.get("outcome") == "bounce"]
        pierced = [e for e in reached if e.get("outcome") == "pierce"]
        weak = [e for e in reached if e.get("outcome") == "weak_reaction"]
        tradeable = [e for e in evs if e.get("tradeable_gap")]
        tradeable_reached = [e for e in tradeable if e.get("outcome") != "not_reached"]
        tradeable_bounce = [e for e in tradeable_reached if e.get("outcome") == "bounce"]
        flow_ok = [e for e in reached if e.get("flow_confirmed")]
        flow_ok_bounce = [e for e in flow_ok if e.get("outcome") == "bounce"]
        gap_and_flow = [
            e
            for e in tradeable_reached
            if e.get("flow_confirmed")
        ]
        gap_and_flow_bounce = [e for e in gap_and_flow if e.get("outcome") == "bounce"]
        trades = [e for e in evs if e.get("trade_taken")]
        flow_trades = [e for e in trades if e.get("flow_confirmed")]
        pnls = [float(e["pnl_pct"]) for e in trades if e.get("pnl_pct") is not None]
        flow_pnls = [
            float(e["pnl_pct"]) for e in flow_trades if e.get("pnl_pct") is not None
        ]
        wins = [p for p in pnls if p > 0]
        flow_wins = [p for p in flow_pnls if p > 0]
        by_exit: dict[str, int] = {}
        for e in trades:
            key = str(e.get("exit_reason") or "unknown")
            by_exit[key] = by_exit.get(key, 0) + 1
        bps = [
            float(e["bounce_pct"])
            for e in bounced
            if e.get("bounce_pct") is not None
        ]
        gaps = [
            float(e["next_lower_pool_gap_pct"])
            for e in evs
            if e.get("next_lower_pool_gap_pct") is not None
        ]
        return {
            "n": len(evs),
            "n_reached": len(reached),
            "n_bounce": len(bounced),
            "n_pierce": len(pierced),
            "n_weak": len(weak),
            "bounce_rate_of_reached": (len(bounced) / len(reached)) if reached else None,
            "pierce_rate_of_reached": (len(pierced) / len(reached)) if reached else None,
            "mean_bounce_pct_when_bounce": (sum(bps) / len(bps)) if bps else None,
            "n_tradeable_gap": len(tradeable),
            "tradeable_bounce_rate": (
                len(tradeable_bounce) / len(tradeable_reached)
                if tradeable_reached
                else None
            ),
            "n_flow_confirmed": len(flow_ok),
            "flow_confirmed_bounce_rate": (
                len(flow_ok_bounce) / len(flow_ok) if flow_ok else None
            ),
            "n_gap_and_flow": len(gap_and_flow),
            "gap_and_flow_bounce_rate": (
                len(gap_and_flow_bounce) / len(gap_and_flow) if gap_and_flow else None
            ),
            "mean_lower_gap_pct": (sum(gaps) / len(gaps)) if gaps else None,
            "n_trades": len(trades),
            "n_flow_trades": len(flow_trades),
            "trade_winrate": (len(wins) / len(pnls)) if pnls else None,
            "trade_mean_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
            "trade_sum_pnl_pct": sum(pnls) if pnls else None,
            "flow_trade_winrate": (len(flow_wins) / len(flow_pnls)) if flow_pnls else None,
            "flow_trade_mean_pnl_pct": (
                (sum(flow_pnls) / len(flow_pnls)) if flow_pnls else None
            ),
            "flow_trade_sum_pnl_pct": sum(flow_pnls) if flow_pnls else None,
            "by_exit_reason": by_exit,
        }

    by_rank: dict[str, Any] = {}
    ranks = sorted({int(e.get("rank") or 0) for e in flat if e.get("rank")})
    for r in ranks:
        by_rank[f"rank_{r}"] = _agg([e for e in flat if int(e.get("rank") or 0) == r])

    by_source: dict[str, Any] = {}
    sources = sorted({str(e.get("source") or "unknown") for e in flat})
    for src in sources:
        by_source[src] = _agg([e for e in flat if str(e.get("source") or "unknown") == src])

    return {
        "n_signals": len(rows),
        "n_rows": len(flat),
        "overall": _agg(flat),
        "tradeable_only": _agg([e for e in flat if e.get("tradeable_gap")]),
        "by_rank": by_rank,
        "by_source": by_source,
    }
