"""4h candles and liquidity pools for one coin."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Pool:
    side: str
    bottom: float
    top: float
    born: datetime
    invalidated: datetime | None


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _paths() -> None:
    root = Path(__file__).resolve().parents[1]
    extra = [
        root / "backtester" / "short" / "dashboard",
        root / "backtester" / "short",
        root / "backtester",
        Path("/home/telgenbuescher/projects/orderbook_analyse/src"),
        Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src"),
        root,
    ]
    for path in extra:
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


def load_market(symbol: str, start: datetime, end: datetime) -> tuple[list[Bar], list[Pool]]:
    """4h bars and pools from start through end. Caller includes EMA warmup."""
    _paths()
    from dashboard.research_charts.service import _candles_from_packed, lld_config_for_timeframe, load_candles
    from dashboard.research_charts.trp_import import load_trp

    packed = load_candles(
        symbol,
        "4h",
        start=int(start.timestamp()),
        end=int(end.timestamp()),
        limit=50000,
    )
    candles = _candles_from_packed(packed)
    bars = [
        Bar(
            ts=_utc(c.timestamp),
            open=float(c.open),
            high=float(c.high),
            low=float(c.low),
            close=float(c.close),
        )
        for c in candles
    ]
    trp = load_trp()
    cfg = lld_config_for_timeframe(trp["LiquidityLocationConfig"].defaults(), "4h")
    result = trp["run_liquidity_location"](candles, cfg)
    raw = getattr(result, "pools_all", None) or result.pools
    pools: list[Pool] = []
    for pool in raw:
        born = getattr(pool, "created_timestamp", None)
        if born is None:
            continue
        invalidated = getattr(pool, "invalidated_timestamp", None)
        pools.append(
            Pool(
                side=str(pool.side),
                bottom=float(pool.bottom_price),
                top=float(pool.top_price),
                born=_utc(born),
                invalidated=_utc(invalidated) if invalidated is not None else None,
            )
        )
    return bars, pools
