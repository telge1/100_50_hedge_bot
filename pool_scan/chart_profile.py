"""Research-chart candle window ``research_chart_default_v1``.

This is one pinned profile. It does not reproduce an arbitrary browser preset.

For one timeframe and one ``as_of``:

- chart history starts 30 calendar days before ``as_of``
- 300 complete bars immediately before that history start are warm-up
- detection runs on that combined series and on no later bar
- the drawn pools are ``select_lld_pools_for_chart`` on that series

Bars whose close is after ``as_of`` stay out of the series, so the engine's
``active`` flag is the state at this ``as_of`` and not the end of a longer file.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pool_scan.clock import available_at, bar_close, break_known_at, ensure_trp

PROFILE_ID = "research_chart_default_v1"
HISTORY_DAYS = 30
WARMUP_BARS = 300
TIMEFRAMES = ("4h", "30m", "15m")

_TF_SECONDS = {"15m": 900, "30m": 1800, "4h": 14400}


def ensure_chart_imports() -> None:
    ensure_trp()
    root = Path(__file__).resolve().parents[1]
    dashboard = root / "backtester" / "short" / "dashboard"
    for path in (dashboard, root / "backtester" / "short"):
        text = str(path)
        if path.exists() and text not in sys.path:
            sys.path.insert(0, text)


def timeframe_seconds(timeframe: str) -> int:
    try:
        return _TF_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe {timeframe}") from exc


def history_start(as_of: datetime) -> datetime:
    return as_of - timedelta(days=HISTORY_DAYS)


def requested_warmup_start(as_of: datetime, timeframe: str) -> datetime:
    return history_start(as_of) - timedelta(seconds=WARMUP_BARS * timeframe_seconds(timeframe))


@dataclass(frozen=True)
class ProfileSeries:
    timeframe: str
    as_of: datetime
    candles: tuple
    history_start: datetime
    requested_warmup_start: datetime
    warmup_bars_available: int
    restricted: bool

    @property
    def last_open(self) -> datetime | None:
        if not self.candles:
            return None
        return self.candles[-1].timestamp

    @property
    def last_close(self) -> datetime | None:
        if not self.candles:
            return None
        return bar_close(self.candles[-1].timestamp, self.timeframe)


def profile_series(candles, as_of: datetime) -> ProfileSeries:
    """Closed bars inside the profile. A still-open bucket is not included."""
    ordered = tuple(sorted(candles, key=lambda candle: candle.timestamp))
    if not ordered:
        raise ValueError("profile series needs candles")
    timeframe = ordered[0].timeframe
    for candle in ordered:
        if candle.timeframe != timeframe:
            raise ValueError("profile series mixes timeframes")
    start = history_start(as_of)
    warm = requested_warmup_start(as_of, timeframe)
    closed = [
        candle
        for candle in ordered
        if candle.timestamp >= warm and bar_close(candle.timestamp, timeframe) <= as_of
    ]
    warmup_count = sum(1 for candle in closed if candle.timestamp < start)
    return ProfileSeries(
        timeframe=timeframe,
        as_of=as_of,
        candles=tuple(closed),
        history_start=start,
        requested_warmup_start=warm,
        warmup_bars_available=warmup_count,
        restricted=warmup_count < WARMUP_BARS,
    )


def chart_config(timeframe: str):
    """Same display scaling the research chart applies. Lengths stay at defaults."""
    ensure_chart_imports()
    from research_charts.service import lld_config_for_timeframe
    from dashboard.research_charts.trp_import import load_trp

    trp = load_trp()
    return lld_config_for_timeframe(trp["LiquidityLocationConfig"].defaults(), timeframe)


def run_profile_engine(series: ProfileSeries):
    ensure_chart_imports()
    from dashboard.research_charts.trp_import import load_trp

    if not series.candles:
        return None
    trp = load_trp()
    return trp["run_liquidity_location"](list(series.candles), chart_config(series.timeframe))


def _order_key(pool) -> tuple:
    side = 0 if getattr(pool, "side", "") == "upper" else 1
    return (int(getattr(pool, "created_index", 0) or 0), side, str(getattr(pool, "pool_id", "")))


def _life_hours(pool) -> float:
    if bool(getattr(pool, "active", False)):
        return 1e9
    inv = getattr(pool, "invalidated_timestamp", None)
    created = getattr(pool, "created_timestamp", None)
    if inv is None or created is None:
        return 0.0
    return max(0.0, (inv - created).total_seconds() / 3600.0)


def _strength(pool) -> float:
    try:
        return float(getattr(pool, "strength", None) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def chart_selection_reasons(pools_all, amount: int, tip: datetime) -> dict[str, str]:
    """Why ``select_lld_pools_for_chart`` keeps each pool. Same groups, same order.

    The returned ids are checked against that function by the caller.
    """
    ensure_chart_imports()
    from research_charts.service import (
        LLD_DENSE_LOOKBACK_DAYS,
        LLD_DENSE_MIN_LIFE_HOURS,
        LLD_DENSE_SHORT_KEEP_FRAC,
        LLD_OLDER_MIN_LIFE_HOURS,
        MAX_LLD_AMOUNT,
        MAX_LLD_CHART_POOLS,
    )

    cap = max(1, min(int(amount), MAX_LLD_CHART_POOLS, MAX_LLD_AMOUNT))
    dense_from = tip - timedelta(days=float(LLD_DENSE_LOOKBACK_DAYS))

    def intersects(pool) -> bool:
        created = getattr(pool, "created_timestamp", None)
        if created is None:
            return False
        end = getattr(pool, "invalidated_timestamp", None)
        if bool(getattr(pool, "active", False)) or end is None:
            end = tip
        return created <= tip and end >= dense_from

    active = sorted(
        [pool for pool in pools_all if bool(getattr(pool, "active", False))],
        key=_order_key,
    )
    dense_inv = [
        pool
        for pool in pools_all
        if not bool(getattr(pool, "active", False)) and intersects(pool)
    ]
    dense_long = [pool for pool in dense_inv if _life_hours(pool) >= float(LLD_DENSE_MIN_LIFE_HOURS)]
    dense_short = sorted(
        [pool for pool in dense_inv if _life_hours(pool) < float(LLD_DENSE_MIN_LIFE_HOURS)],
        key=_order_key,
    )
    frac = max(0.0, min(1.0, float(LLD_DENSE_SHORT_KEEP_FRAC)))
    if dense_short and frac < 1.0:
        step = max(1, int(round(1.0 / max(frac, 1e-6))))
        dense_short = dense_short[::step]
    dense_ids = {pool.pool_id for pool in dense_long + dense_short + active}
    older = [
        pool
        for pool in pools_all
        if not bool(getattr(pool, "active", False))
        and pool.pool_id not in dense_ids
        and _life_hours(pool) >= float(LLD_OLDER_MIN_LIFE_HOURS)
    ]
    older = sorted(older, key=lambda pool: (_life_hours(pool), _strength(pool), _order_key(pool)), reverse=True)
    if len(active) >= cap:
        chosen = list(active[-cap:])
        groups = [(pool, "active") for pool in chosen]
    else:
        groups = [(pool, "active") for pool in active]
        groups += [
            (pool, "dense_long")
            for pool in sorted(dense_long, key=lambda pool: (_life_hours(pool), _strength(pool)), reverse=True)
        ]
        groups += [
            (pool, "dense_short")
            for pool in sorted(dense_short, key=lambda pool: (_strength(pool), _order_key(pool)), reverse=True)
        ]
        groups += [(pool, "older_long") for pool in older]
    reasons: dict[str, str] = {}
    for pool, reason in groups:
        if pool.pool_id in reasons:
            continue
        reasons[pool.pool_id] = reason
        if len(reasons) >= cap:
            break
    return reasons


def select_chart_pools(pools_all, amount: int, tip: datetime) -> list[tuple[object, str]]:
    """Research-chart display slice plus the group that admitted each pool."""
    ensure_chart_imports()
    from research_charts.service import select_lld_pools_for_chart

    picked = select_lld_pools_for_chart(list(pools_all), int(amount), tip=tip)
    reasons = chart_selection_reasons(pools_all, int(amount), tip)
    picked_ids = [pool.pool_id for pool in picked]
    if picked_ids != list(reasons):
        missing = [pool_id for pool_id in picked_ids if pool_id not in reasons]
        extra = [pool_id for pool_id in reasons if pool_id not in set(picked_ids)]
        raise RuntimeError(
            "chart selection reasons diverged from select_lld_pools_for_chart: "
            f"missing={missing[:5]} extra={extra[:5]}"
        )
    return [(pool, reasons[pool.pool_id]) for pool in picked]


def project_causal_pools(pools, as_of: datetime) -> list:
    """Copies whose ``active`` flag ignores a break that is not known yet.

    ``select_lld_pools_for_chart`` reads ``active`` and ``invalidated_timestamp``.
    A break stored on a bar open is known only at that bar's close.
    """
    import copy

    projected = []
    for pool in pools:
        known = break_known_at(pool)
        if known is not None and known <= as_of:
            projected.append(pool)
            continue
        if not bool(getattr(pool, "active", False)) or getattr(pool, "invalidated_timestamp", None) is not None:
            clone = copy.copy(pool)
            clone.active = True
            clone.invalidated_timestamp = None
            clone.invalidated_index = None
            projected.append(clone)
            continue
        projected.append(pool)
    return projected


def pools_known_at(pools, as_of: datetime) -> list:
    return [pool for pool in pools if available_at(pool) <= as_of]
