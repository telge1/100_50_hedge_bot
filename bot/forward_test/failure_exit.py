"""Live failure exit for an open short.

Not bar-timed. On every poll, close only when price is back at the entry,
a thick active pool sits just above that entry, and live OB + delta have
flipped against the short. Uses data available at ``now`` only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bot.forward_test.config import (
    FAILURE_DELTA_MIN,
    FAILURE_MAX_POOL_DIST_PCT,
    FAILURE_MIN_POOL_STRENGTH,
    FAILURE_NEAR_ENTRY_PCT,
    FAILURE_OB_MIN,
    POOL_LOOKBACK_HOURS,
    POOL_LOOKFORWARD_HOURS,
)


def short_failure_ready(
    *,
    last_price: float,
    entry_price: float,
    stop_price: float,
    ob_ratio: float | None,
    delta_10m: float | None,
    pool_strength: float | None,
    pool_dist_pct: float | None,
) -> tuple[bool, str]:
    """Pure gate. All conditions required. No IO."""
    if entry_price <= 0 or last_price <= 0:
        return False, "bad_price"
    near_floor = float(entry_price) * (1.0 - float(FAILURE_NEAR_ENTRY_PCT) / 100.0)
    if last_price < near_floor:
        return False, "price_not_near_entry"
    if stop_price > 0 and last_price >= float(stop_price):
        # Hard SL zone — let the bar SL book it, do not steal the fill.
        return False, "at_or_through_sl"
    if pool_strength is None or float(pool_strength) < float(FAILURE_MIN_POOL_STRENGTH):
        return False, "no_thick_pool_above"
    if pool_dist_pct is None or float(pool_dist_pct) > float(FAILURE_MAX_POOL_DIST_PCT):
        return False, "pool_not_close_above"
    if pool_dist_pct < 0:
        return False, "pool_not_above_entry"
    if ob_ratio is None or float(ob_ratio) < float(FAILURE_OB_MIN):
        return False, "ob_not_strong"
    if delta_10m is None or float(delta_10m) <= float(FAILURE_DELTA_MIN):
        return False, "delta_not_positive"
    return True, "failure_exit"


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _last_trade_price(symbol: str, now: datetime) -> float | None:
    from ob_microstructure_breakout_bot.data.trades import load_trade_window

    now = _utc(now)
    try:
        win = load_trade_window(symbol, now - timedelta(minutes=2), now)
    except Exception:
        return None
    px = getattr(win, "close_price", None)
    if px is None or float(px) <= 0:
        return None
    return float(px)


def _delta_10m(symbol: str, now: datetime) -> float | None:
    from ob_microstructure_breakout_bot.data.trades import load_trade_window

    now = _utc(now)
    try:
        win = load_trade_window(symbol, now - timedelta(minutes=10), now)
        return float(win.delta_notional)
    except Exception:
        return None


def _thick_pool_above_entry(symbol: str, now: datetime, entry_price: float) -> tuple[float | None, float | None, float | None]:
    """Nearest thick ACTIVE upper cluster above entry.

    Returns ``(strength_sum, dist_pct, pool_bottom)``.
    """
    from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import group_pools_into_clusters
    from ob_microstructure_breakout_bot.exit_backtest.pool_bounce import load_active_upper_pools_5m

    pools = load_active_upper_pools_5m(
        symbol,
        _utc(now),
        float(entry_price),
        lookback_hours=POOL_LOOKBACK_HOURS,
        lookforward_hours=POOL_LOOKFORWARD_HOURS,
    )
    clusters = group_pools_into_clusters(pools, entry=float(entry_price))
    thick = [
        c
        for c in clusters
        if float(c.strength_sum) >= float(FAILURE_MIN_POOL_STRENGTH) and float(c.bottom) > float(entry_price)
    ]
    if not thick:
        return None, None, None
    nearest = min(thick, key=lambda c: float(c.bottom))
    dist = (float(nearest.bottom) - float(entry_price)) / float(entry_price) * 100.0
    return float(nearest.strength_sum), float(dist), float(nearest.bottom)


def evaluate_short_failure(
    symbol: str,
    *,
    entry_price: float,
    stop_price: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Snapshot live OB, delta, price and overhead pool. No lookahead."""
    from bot.forward_test.ob_live import sample_ob1000_ratio
    from bot.forward_test.paths import ensure_import_paths

    ensure_import_paths()
    as_of = _utc(now or datetime.now(timezone.utc))
    last_price = _last_trade_price(symbol, as_of)
    ob = sample_ob1000_ratio(symbol, as_of)
    delta = _delta_10m(symbol, as_of)
    strength, dist, pool_bottom = (None, None, None)
    try:
        strength, dist, pool_bottom = _thick_pool_above_entry(symbol, as_of, float(entry_price))
    except Exception:
        strength, dist, pool_bottom = None, None, None

    ready = False
    reason = "no_price"
    if last_price is not None:
        ready, reason = short_failure_ready(
            last_price=last_price,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            ob_ratio=ob,
            delta_10m=delta,
            pool_strength=strength,
            pool_dist_pct=dist,
        )
    return {
        "ok": ready,
        "reason": reason,
        "last_price": last_price,
        "ob_ratio": ob,
        "delta_10m": delta,
        "pool_strength": strength,
        "pool_dist_pct": dist,
        "pool_bottom": pool_bottom,
        "as_of": as_of,
    }
