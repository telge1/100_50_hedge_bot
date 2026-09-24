"""Mirrored 5m lower-pool bounce backtest (structure + OB/delta + SL/TP).

This is the long-side mirror of ``pool_bounce_backtest.py``:
- rank 1 / rank 2 only
- flow gate at touch
- entry only after confirmed reversal candle
- TP pools loaded at the long-entry candle, not earlier
- SL = touched pool bottom - 0.2%
- TP = nearest ACTIVE upper pool at entry with Entry->TP room >= 0.8%
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ob_microstructure_breakout_bot.data.bars import load_5m_bars
from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import (
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

MIN_UPPER_GAP_PCT = 0.8
WATCH_BEFORE_PCT = 0.8
SL_BELOW_POOL_BOTTOM_PCT = 0.2

FLOW_OB_OK = 1.05
FLOW_DELTA_OK = 100_000.0
FLOW_OB_STRONG = 1.20
FLOW_DELTA_STRONG = 200_000.0

FLOW_OB_OK_MIRROR = 1.0 / FLOW_OB_OK
FLOW_OB_STRONG_MIRROR = 1.0 / FLOW_OB_STRONG


@dataclass
class BounceBacktestRow:
    decision_ts: str
    touch_ts: str | None
    rank: int
    cluster_id: str
    pool_bottom: float
    pool_top: float
    strength_sum: float
    next_upper_pool_gap_pct: float | None
    next_upper_pool_bottom: float | None
    tp_room_pct: float | None
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
    entry_price: float
    side: str = "long"
    trade_taken: bool = False
    trade_skip_reason: str = ""
    long_entry_ts: str | None = None
    long_entry_price: float | None = None
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
    entry: float,
    *,
    lookback_hours: int = 12,
    lookforward_hours: int = 6,
    timeframe: str = "5m",
) -> list[PoolSnap]:
    """Return ACTIVE lower pools below entry known at ``as_of``."""
    from dashboard.research_charts.service import liquidity_location_overlay_bundle

    as_of = _ensure_utc(as_of)
    start = int((as_of - timedelta(hours=lookback_hours)).timestamp())
    end = int((as_of + timedelta(hours=lookforward_hours)).timestamp())
    payload = liquidity_location_overlay_bundle(
        symbol=symbol,
        timeframe=str(timeframe or "5m"),
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
        b = float(bottom)
        t = float(top)
        if t >= float(entry):
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
    pool_top: float,
    watch_before_pct: float = WATCH_BEFORE_PCT,
) -> Any | None:
    """First bar whose low reaches within ``watch_before_pct`` above pool top."""
    trigger = pool_top * (1.0 + watch_before_pct / 100.0)
    for b in bars:
        if float(b.low) <= trigger:
            return b
    return None


def measure_cluster_bounce_long(
    cluster: ClusterSnap,
    *,
    rank: int,
    entry: float,
    bars: list[Any],
    hold_bars: int = BOUNCE_HOLD_BARS,
    bounce_min_pct: float = BOUNCE_MIN_PCT,
    pierce_clear_pct: float = PIERCE_CLEAR_PCT,
) -> BounceBacktestRow:
    """First touch into a lower cluster -> bounce / pierce metrics."""
    touch_idx = next(
        (i for i, b in enumerate(bars) if float(b.low) <= cluster.top),
        None,
    )
    if touch_idx is None:
        return BounceBacktestRow(
            decision_ts="",
            touch_ts=None,
            rank=rank,
            cluster_id=cluster.cluster_id,
            pool_bottom=float(cluster.bottom),
            pool_top=float(cluster.top),
            strength_sum=float(cluster.strength_sum),
            next_upper_pool_gap_pct=None,
            next_upper_pool_bottom=None,
            tp_room_pct=None,
            tradeable_gap=False,
            flow_confirmed=False,
            flow_strong=False,
            watch_ts=None,
            bounce_pct=None,
            pierce_pct=None,
            outcome="not_reached",
            ob_ratio_at_watch=None,
            delta_at_watch=None,
            ob_ratio_at_touch=None,
            delta_at_touch=None,
            entry_price=float(entry),
        )

    touch = bars[touch_idx]
    after = bars[touch_idx : touch_idx + max(1, hold_bars)]
    trough_i = min(range(len(after)), key=lambda i: float(after[i].low))
    local_low = float(after[trough_i].low)
    after_trough = after[trough_i:]
    local_high = max(float(b.high) for b in after_trough)
    bounce_pct = ((local_high - local_low) / entry) * 100.0 if entry > 0 else 0.0
    pierce_pct = ((cluster.bottom - local_low) / entry) * 100.0 if entry > 0 else 0.0
    pierced = pierce_pct >= pierce_clear_pct
    pulled_back = bounce_pct >= bounce_min_pct
    left_zone = local_high > cluster.top
    bounced = pulled_back and (left_zone or not pierced)

    if pierced and bounced:
        outcome = "pierce_then_fade"
    elif pierced and not bounced:
        outcome = "pierce"
    elif bounced:
        outcome = "bounce"
    else:
        outcome = "weak_reaction"

    return BounceBacktestRow(
        decision_ts="",
        touch_ts=touch.ts.isoformat(),
        rank=rank,
        cluster_id=cluster.cluster_id,
        pool_bottom=float(cluster.bottom),
        pool_top=float(cluster.top),
        strength_sum=float(cluster.strength_sum),
        next_upper_pool_gap_pct=None,
        next_upper_pool_bottom=None,
        tp_room_pct=None,
        tradeable_gap=False,
        flow_confirmed=False,
        flow_strong=False,
        watch_ts=None,
        bounce_pct=float(bounce_pct),
        pierce_pct=float(pierce_pct),
        outcome=outcome,
        ob_ratio_at_watch=None,
        delta_at_watch=None,
        ob_ratio_at_touch=None,
        delta_at_touch=None,
        entry_price=float(entry),
    )


def find_long_reversal_entry(
    bars: list[Any],
    *,
    touch_idx: int,
    pool_bottom: float,
    pool_top: float,
    sl_below_pct: float = SL_BELOW_POOL_BOTTOM_PCT,
    hold_bars: int = BOUNCE_HOLD_BARS,
) -> dict[str, Any]:
    """Find long fill after confirmed reversal; TP is selected later."""
    stop = float(pool_bottom) * (1.0 - sl_below_pct / 100.0)
    end = min(len(bars), touch_idx + max(1, hold_bars))
    for i in range(touch_idx, end):
        b = bars[i]
        if float(b.low) <= stop:
            return {
                "ok": False,
                "reason": "sl_before_entry",
                "entry_idx": None,
                "entry_ts": None,
                "entry_price": None,
                "stop_price": stop,
            }
        if float(b.close) > float(pool_top):
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


def simulate_long_bounce_trade(
    bars: list[Any],
    *,
    touch_idx: int,
    pool_bottom: float,
    pool_top: float,
    tp_price: float,
    sl_below_pct: float = SL_BELOW_POOL_BOTTOM_PCT,
    hold_bars: int = BOUNCE_HOLD_BARS,
) -> dict[str, Any]:
    """Long after confirmed reversal; SL below pool bottom, TP = next upper bottom."""
    stop = float(pool_bottom) * (1.0 - sl_below_pct / 100.0)
    tp = float(tp_price)
    end = min(len(bars), touch_idx + max(1, hold_bars))
    entry_i: int | None = None

    for i in range(touch_idx, end):
        b = bars[i]
        if float(b.low) <= stop:
            return {
                "trade_taken": False,
                "trade_skip_reason": "sl_before_entry",
                "long_entry_ts": None,
                "long_entry_price": None,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": stop,
                "exit_reason": "sl_before_entry",
                "pnl_pct": None,
            }
        if float(b.close) > float(pool_top):
            entry_i = i
            break

    if entry_i is None:
        return {
            "trade_taken": False,
            "trade_skip_reason": "no_reversal_candle",
            "long_entry_ts": None,
            "long_entry_price": None,
            "stop_price": stop,
            "tp_price": tp,
            "exit_ts": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
        }

    entry_bar = bars[entry_i]
    entry_px = float(entry_bar.close)
    if tp <= entry_px:
        return {
            "trade_taken": False,
            "trade_skip_reason": "tp_not_above_entry",
            "long_entry_ts": entry_bar.ts.isoformat(),
            "long_entry_price": entry_px,
            "stop_price": stop,
            "tp_price": tp,
            "exit_ts": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
        }

    for j in range(entry_i + 1, end):
        b = bars[j]
        hit_sl = float(b.low) <= stop
        hit_tp = float(b.high) >= tp
        if hit_sl and hit_tp:
            pnl = (stop - entry_px) / entry_px * 100.0
            return {
                "trade_taken": True,
                "trade_skip_reason": "",
                "long_entry_ts": entry_bar.ts.isoformat(),
                "long_entry_price": entry_px,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": stop,
                "exit_reason": "sl",
                "pnl_pct": float(pnl),
            }
        if hit_sl:
            pnl = (stop - entry_px) / entry_px * 100.0
            return {
                "trade_taken": True,
                "trade_skip_reason": "",
                "long_entry_ts": entry_bar.ts.isoformat(),
                "long_entry_price": entry_px,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": stop,
                "exit_reason": "sl",
                "pnl_pct": float(pnl),
            }
        if hit_tp:
            pnl = (tp - entry_px) / entry_px * 100.0
            return {
                "trade_taken": True,
                "trade_skip_reason": "",
                "long_entry_ts": entry_bar.ts.isoformat(),
                "long_entry_price": entry_px,
                "stop_price": stop,
                "tp_price": tp,
                "exit_ts": b.ts.isoformat(),
                "exit_price": tp,
                "exit_reason": "tp",
                "pnl_pct": float(pnl),
            }

    last = bars[end - 1] if end > entry_i else entry_bar
    exit_px = float(last.close)
    pnl = (exit_px - entry_px) / entry_px * 100.0
    return {
        "trade_taken": True,
        "trade_skip_reason": "",
        "long_entry_ts": entry_bar.ts.isoformat(),
        "long_entry_price": entry_px,
        "stop_price": stop,
        "tp_price": tp,
        "exit_ts": last.ts.isoformat(),
        "exit_price": exit_px,
        "exit_reason": "timeout",
        "pnl_pct": float(pnl),
    }
def analyze_signal_long_bounce_backtest(
    symbol: str,
    *,
    decision_ts: datetime,
    entry_price: float,
    tier: str = "",
    source: str = "",
    hold_hours: int = 24,
    hold_bars: int = BOUNCE_HOLD_BARS,
    max_ranks: int = 2,
    sample_flow: bool = True,
) -> dict[str, Any]:
    """One long signal -> ranked lower clusters with mirrored short logic."""
    decision_ts = _ensure_utc(decision_ts)
    lower = load_active_lower_pools_5m(
        symbol, decision_ts, entry_price, lookforward_hours=8
    )
    clusters = group_pools_into_clusters(lower, entry_price, gap_pct=0.10)
    ranked = rank_clusters_by_mass(clusters)[: max(1, max_ranks)]

    bars = load_5m_bars(
        symbol,
        decision_ts,
        decision_ts + timedelta(hours=hold_hours),
    )
    bars = [b for b in bars if b.ts >= decision_ts]

    rows: list[BounceBacktestRow] = []
    for i, c in enumerate(ranked, 1):
        outcome = measure_cluster_bounce_long(
            c,
            rank=i,
            entry=entry_price,
            bars=bars,
            hold_bars=hold_bars,
        )

        watch_bar = _first_watch_bar(
            bars, pool_top=c.top, watch_before_pct=WATCH_BEFORE_PCT
        )
        watch_ts = watch_bar.ts.isoformat() if watch_bar is not None else None
        ob_watch = delta_watch = None
        ob_touch = delta_touch = None
        if sample_flow:
            if watch_bar is not None:
                ob_watch = _ob_ratio(symbol, watch_bar.ts)
                delta_watch = _delta_10m(symbol, watch_bar.ts + timedelta(minutes=5))
            touch_bar = next(
                (b for b in bars if float(b.low) <= c.top),
                None,
            )
            if touch_bar is not None:
                ob_touch = _ob_ratio(symbol, touch_bar.ts)
                delta_touch = _delta_10m(symbol, touch_bar.ts + timedelta(minutes=5))

        flow_confirmed = (
            ob_touch is not None
            and delta_touch is not None
            and ob_touch <= FLOW_OB_OK_MIRROR
            and delta_touch <= -FLOW_DELTA_OK
        )
        flow_strong = (
            ob_touch is not None
            and delta_touch is not None
            and ob_touch <= FLOW_OB_STRONG_MIRROR
            and delta_touch <= -FLOW_DELTA_STRONG
        )

        gap = upper_bottom = room = None
        tradeable = False
        trade: dict[str, Any] = {
            "trade_taken": False,
            "trade_skip_reason": "",
            "long_entry_ts": None,
            "long_entry_price": None,
            "stop_price": None,
            "tp_price": None,
            "exit_ts": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
        }
        touch_bar = next((b for b in bars if float(b.low) <= c.top), None)
        if outcome.outcome == "not_reached" or touch_bar is None:
            trade["trade_skip_reason"] = "not_reached"
        else:
            touch_idx = next(
                (j for j, b in enumerate(bars) if float(b.low) <= c.top),
                None,
            )
            if touch_idx is None:
                trade["trade_skip_reason"] = "not_reached"
            else:
                entry_info = find_long_reversal_entry(
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
                    upper_at_entry = load_active_upper_pools_5m(symbol, entry_ts, entry_px)
                    upper_bottom, room = select_tp_upper_pool(
                        entry_price=entry_px,
                        upper_pools=upper_at_entry,
                        min_room_pct=MIN_UPPER_GAP_PCT,
                    )
                    if upper_bottom is not None:
                        gap = (float(upper_bottom) - float(c.bottom)) / float(c.bottom) * 100.0
                    tradeable = (
                        upper_bottom is not None
                        and room is not None
                        and room >= MIN_UPPER_GAP_PCT
                    )
                    if not tradeable:
                        trade["trade_skip_reason"] = (
                            "no_upper_pool" if upper_bottom is None else "gap_too_small"
                        )
                        trade["long_entry_ts"] = entry_ts.isoformat()
                        trade["long_entry_price"] = entry_px
                    else:
                        trade = simulate_long_bounce_trade(
                            bars,
                            touch_idx=touch_idx,
                            pool_bottom=float(c.bottom),
                            pool_top=float(c.top),
                            tp_price=float(upper_bottom),
                            hold_bars=hold_bars,
                        )

        rows.append(
            BounceBacktestRow(
                decision_ts=decision_ts.isoformat(),
                touch_ts=outcome.touch_ts,
                rank=i,
                cluster_id=c.cluster_id,
                pool_bottom=float(c.bottom),
                pool_top=float(c.top),
                strength_sum=float(c.strength_sum),
                next_upper_pool_gap_pct=gap,
                next_upper_pool_bottom=upper_bottom,
                tp_room_pct=room,
                tradeable_gap=bool(tradeable),
                flow_confirmed=bool(flow_confirmed),
                flow_strong=bool(flow_strong),
                watch_ts=watch_ts,
                bounce_pct=outcome.bounce_pct,
                pierce_pct=outcome.pierce_pct,
                outcome=str(outcome.outcome or "not_reached"),
                ob_ratio_at_watch=ob_watch,
                delta_at_watch=delta_watch,
                ob_ratio_at_touch=ob_touch,
                delta_at_touch=delta_touch,
                entry_price=float(entry_price),
                trade_taken=bool(trade.get("trade_taken")),
                trade_skip_reason=str(trade.get("trade_skip_reason") or ""),
                long_entry_ts=trade.get("long_entry_ts"),
                long_entry_price=trade.get("long_entry_price"),
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
        "n_lower_pools": len(lower),
        "n_upper_pools_at_entry": "per-row (loaded at long_entry_ts)",
        "n_clusters": len(clusters),
        "tp_pool_as_of": "long_entry_ts",
        "min_upper_gap_pct": MIN_UPPER_GAP_PCT,
        "watch_before_pct": WATCH_BEFORE_PCT,
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
        gap_and_flow = [e for e in tradeable_reached if e.get("flow_confirmed")]
        gap_and_flow_bounce = [e for e in gap_and_flow if e.get("outcome") == "bounce"]
        trades = [e for e in evs if e.get("trade_taken")]
        flow_trades = [e for e in trades if e.get("flow_confirmed")]
        pnls = [float(e["pnl_pct"]) for e in trades if e.get("pnl_pct") is not None]
        flow_pnls = [float(e["pnl_pct"]) for e in flow_trades if e.get("pnl_pct") is not None]
        wins = [p for p in pnls if p > 0]
        flow_wins = [p for p in flow_pnls if p > 0]
        by_exit: dict[str, int] = {}
        for e in trades:
            key = str(e.get("exit_reason") or "unknown")
            by_exit[key] = by_exit.get(key, 0) + 1
        bps = [float(e["bounce_pct"]) for e in bounced if e.get("bounce_pct") is not None]
        gaps = [
            float(e["next_upper_pool_gap_pct"])
            for e in evs
            if e.get("next_upper_pool_gap_pct") is not None
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
            "mean_upper_gap_pct": (sum(gaps) / len(gaps)) if gaps else None,
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

