"""Load 4h candles and the engine pools the chart already clusters.

The phase code keeps the LiquidityPool objects. A stripped copy cannot be clustered.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pool_pattern.profile import PatternProfile


TRP_ROOT = Path("/home/telgenbuescher/projects/trading_research_platform")


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Observation:
    as_of: datetime
    open: float
    high: float
    low: float
    close: float
    e9: float
    e20: float
    e59: float
    e200: float


def ensure_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    extra = [
        TRP_ROOT,
        root / "backtester" / "short" / "dashboard",
        root / "backtester" / "short",
        root / "backtester",
        Path("/home/telgenbuescher/projects/orderbook_analyse/src"),
        Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src"),
        root,
    ]
    for path in extra:
        text = str(path)
        if path.exists() and text not in sys.path:
            sys.path.insert(0, text)


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_4h(symbol: str, start: datetime, end: datetime):
    """Candles plus pools_all from run_liquidity_location. Display amount is not used."""
    ensure_paths()
    from dashboard.research_charts.service import _candles_from_packed, lld_config_for_timeframe, load_candles
    from dashboard.research_charts.trp_import import load_trp

    packed = load_candles(symbol, "4h", start=int(start.timestamp()), end=int(end.timestamp()), limit=50000)
    candles = _candles_from_packed(packed)
    bars = [
        Bar(ts=_utc(c.timestamp), open=float(c.open), high=float(c.high), low=float(c.low), close=float(c.close))
        for c in candles
    ]
    trp = load_trp()
    cfg = lld_config_for_timeframe(trp["LiquidityLocationConfig"].defaults(), "4h")
    result = trp["run_liquidity_location"](candles, cfg)
    pools = list(getattr(result, "pools_all", None) or result.pools)
    return bars, pools


def load_15m(symbol: str, start: datetime, end: datetime) -> list[Bar]:
    return load_bars(symbol, "15m", start, end)


def load_1h(symbol: str, start: datetime, end: datetime) -> list[Bar]:
    return load_bars(symbol, "1h", start, end)


def load_bars(symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Bar]:
    ensure_paths()
    from dashboard.research_charts.service import _candles_from_packed, load_candles

    packed = load_candles(symbol, timeframe, start=int(start.timestamp()), end=int(end.timestamp()), limit=50000)
    candles = _candles_from_packed(packed)
    return [
        Bar(ts=_utc(c.timestamp), open=float(c.open), high=float(c.high), low=float(c.low), close=float(c.close))
        for c in candles
    ]


def observations(bars: list[Bar], *, bar_hours: int = 4) -> list[Observation]:
    ensure_paths()
    from indicators.ema import ema

    closes = [bar.close for bar in bars]
    e9 = ema(closes, 9)
    e20 = ema(closes, 20)
    e59 = ema(closes, 59)
    e200 = ema(closes, 200)
    out: list[Observation] = []
    for index, bar in enumerate(bars):
        if e9[index] is None or e20[index] is None or e59[index] is None or e200[index] is None:
            continue
        out.append(
            Observation(
                as_of=_utc(bar.ts) + timedelta(hours=bar_hours),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                e9=float(e9[index]),
                e20=float(e20[index]),
                e59=float(e59[index]),
                e200=float(e200[index]),
            )
        )
    return out


def frozen_profile() -> PatternProfile:
    return PatternProfile()
