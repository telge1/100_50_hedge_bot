"""Synthetic fixtures for deterministic scanner tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from orderbook_analyse.liquidity_location_pool_lifecycle.ema_context import attach_context

from .config import TF_CONFIRM, TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO, TF_STRUCTURE
from .models import PoolRecord


def _bars(
    start: datetime,
    n: int,
    *,
    freq_min: int,
    base: float,
    drift: float,
) -> pd.DataFrame:
    rows = []
    px = base
    for i in range(n):
        ot = start + timedelta(minutes=i * freq_min)
        o = px
        c = max(0.0001, o + drift)
        h = max(o, c) + abs(drift) * 0.5
        l = min(o, c) - abs(drift) * 0.5
        rows.append({"open_time": ot, "open": o, "high": h, "low": l, "close": c, "volume": 1000.0})
        px = c
    return pd.DataFrame(rows)


def pool(
    *,
    pool_id: str,
    tf: str,
    side: str,
    lower: float,
    upper: float,
    known_at: datetime,
    strength: float = 5.0,
    n: int = 4,
) -> PoolRecord:
    return PoolRecord(
        pool_id=pool_id,
        symbol="DOGEUSDT",
        timeframe=tf,
        side=side,
        lower_edge=lower,
        upper_edge=upper,
        midpoint=(lower + upper) / 2.0,
        component_count=n,
        strength=strength,
        known_at=known_at,
        invalidated_at=None,
        source_timestamp=known_at,
    )


def static_pools(*, known_at: datetime) -> dict[str, list[PoolRecord]]:
    """Pools for pullback-short integration (late pool must not be used)."""
    late = known_at + timedelta(days=1)
    return {
        TF_ENTRY_POOL: [
            pool(pool_id="ask15", tf="15m", side="ASK", lower=0.1010, upper=0.1015, known_at=known_at),
            pool(pool_id="late", tf="15m", side="ASK", lower=0.1005, upper=0.1008, known_at=late),
        ],
        TF_LIQUIDITY: [
            pool(pool_id="bid30", tf="30m", side="BID", lower=0.0980, upper=0.0985, known_at=known_at, strength=8),
        ],
        TF_MACRO: [],
    }


def static_pools_mirrored_long(*, known_at: datetime) -> dict[str, list[PoolRecord]]:
    return {
        TF_ENTRY_POOL: [
            pool(pool_id="bid15", tf="15m", side="BID", lower=0.0990, upper=0.0995, known_at=known_at),
        ],
        TF_LIQUIDITY: [
            pool(pool_id="ask30", tf="30m", side="ASK", lower=0.1020, upper=0.1025, known_at=known_at, strength=8),
        ],
        TF_MACRO: [],
    }


def static_pools_terminal_long(*, known_at: datetime) -> dict[str, list[PoolRecord]]:
    return {
        TF_MACRO: [
            pool(pool_id="bid1h", tf="1h", side="BID", lower=0.0990, upper=0.0995, known_at=known_at, strength=9),
        ],
        TF_ENTRY_POOL: [
            pool(pool_id="ask15t", tf="15m", side="ASK", lower=0.1020, upper=0.1025, known_at=known_at, strength=6),
        ],
        TF_LIQUIDITY: [
            pool(pool_id="ask30t", tf="30m", side="ASK", lower=0.1030, upper=0.1035, known_at=known_at, strength=7),
        ],
    }


def static_pools_terminal_short(*, known_at: datetime) -> dict[str, list[PoolRecord]]:
    return {
        TF_MACRO: [
            pool(pool_id="ask1h", tf="1h", side="ASK", lower=0.1010, upper=0.1015, known_at=known_at, strength=9),
        ],
        TF_ENTRY_POOL: [
            pool(pool_id="bid15t", tf="15m", side="BID", lower=0.0980, upper=0.0985, known_at=known_at, strength=6),
        ],
        TF_LIQUIDITY: [
            pool(pool_id="bid30t", tf="30m", side="BID", lower=0.0970, upper=0.0975, known_at=known_at, strength=7),
        ],
    }


def _bundle_from_1m(df1: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {TF_CONFIRM: df1}
    for tf, mins in (("5m", 5), ("15m", 15), ("30m", 30), ("1h", 60)):
        rows = []
        step = mins
        i = 0
        while i < len(df1):
            chunk = df1.iloc[i : i + step]
            if len(chunk) < step:
                break
            rows.append(
                {
                    "open_time": chunk.iloc[0]["open_time"],
                    "open": float(chunk.iloc[0]["open"]),
                    "high": float(chunk["high"].max()),
                    "low": float(chunk["low"].min()),
                    "close": float(chunk.iloc[-1]["close"]),
                    "volume": float(chunk["volume"].sum()),
                }
            )
            i += step
        out[tf] = pd.DataFrame(rows)
    out[TF_STRUCTURE] = out["5m"]
    out[TF_ENTRY_POOL] = out["15m"]
    out[TF_LIQUIDITY] = out["30m"]
    out[TF_MACRO] = out["1h"]
    for k in list(out.keys()):
        out[k] = attach_context(out[k])
    return out


def _force_bearish_5m(out: dict[str, pd.DataFrame]) -> None:
    df = out[TF_STRUCTURE]
    if df.empty:
        return
    i = len(df) - 1
    px = float(df.iloc[i]["close"])
    df.loc[i, "ema_9"] = px - 0.0002
    df.loc[i, "ema_20"] = px - 0.0001
    df.loc[i, "ema_59"] = px + 0.0001
    df.loc[i, "ema_9_slope_1"] = -0.00005
    df.loc[i, "ema_20_slope_1"] = -0.00003
    if "prior_swing_high" in df.columns:
        df.loc[i, "prior_swing_high"] = px + 0.0005
        if i > 0:
            df.loc[i - 1, "prior_swing_high"] = px + 0.001
    out[TF_STRUCTURE] = df


def _force_bullish_5m(out: dict[str, pd.DataFrame]) -> None:
    df = out[TF_STRUCTURE]
    if df.empty:
        return
    i = len(df) - 1
    px = float(df.iloc[i]["close"])
    df.loc[i, "ema_9"] = px + 0.0002
    df.loc[i, "ema_20"] = px + 0.0001
    df.loc[i, "ema_59"] = px - 0.0001
    df.loc[i, "ema_9_slope_1"] = 0.00005
    df.loc[i, "ema_20_slope_1"] = 0.00003
    out[TF_STRUCTURE] = df


def pullback_short_confirmation_bundle() -> tuple[dict[str, pd.DataFrame], datetime]:
    start = datetime(2026, 8, 15, 10, 0, 0)
    n = 360
    df1 = _bars(start, n, freq_min=1, base=0.1020, drift=-0.00002)
    # approach upper half of 15m ask pool (0.1010–0.1015)
    tail = {
        "open": [0.10115, 0.10125, 0.10128, 0.10130],
        "close": [0.10120, 0.10128, 0.10130, 0.10128],
        "high": [0.10122, 0.10132, 0.10135, 0.10132],
        "low": [0.10108, 0.10124, 0.10128, 0.10126],
    }
    for col, vals in tail.items():
        for j, v in enumerate(vals):
            df1.loc[len(df1) - 4 + j, col] = v
    df1 = attach_context(df1)
    out = _bundle_from_1m(df1)
    _force_bearish_5m(out)
    approach_at = start + timedelta(minutes=n - 4)
    return out, approach_at


def pullback_short_no_rejection_bundle() -> dict[str, pd.DataFrame]:
    candles, _ = pullback_short_confirmation_bundle()
    df1 = candles["1m"].copy()
    i = len(df1) - 1
    # Price approaches but never reaches limit at 0.1013
    tail = {
        "open": [0.10115, 0.10120, 0.10122, 0.10125],
        "close": [0.10118, 0.10122, 0.10124, 0.10126],
        "high": [0.10120, 0.10124, 0.10126, 0.10128],
        "low": [0.10108, 0.10118, 0.10120, 0.10122],
    }
    for col, vals in tail.items():
        for j, v in enumerate(vals):
            df1.loc[i - 3 + j, col] = v
    candles["1m"] = attach_context(df1)
    return candles


def terminal_long_confirmation_bundle() -> tuple[dict[str, pd.DataFrame], datetime]:
    start = datetime(2026, 8, 16, 12, 0, 0)
    n = 360
    df1 = _bars(start, n, freq_min=1, base=0.1005, drift=-0.00002)
    tail = {
        "open": [0.09960, 0.09920, 0.09910, 0.09925],
        "close": [0.09940, 0.09905, 0.09930, 0.09955],
        "high": [0.09965, 0.09915, 0.09935, 0.09960],
        "low": [0.09885, 0.09880, 0.09900, 0.09920],
    }
    for col, vals in tail.items():
        for j, v in enumerate(vals):
            df1.loc[len(df1) - 4 + j, col] = v
    df1 = attach_context(df1)
    out = _bundle_from_1m(df1)
    _force_bullish_5m(out)
    approach_at = start + timedelta(minutes=n - 4)
    return out, approach_at


def terminal_long_no_reclaim_bundle() -> dict[str, pd.DataFrame]:
    candles, _ = terminal_long_confirmation_bundle()
    df1 = candles[TF_CONFIRM].copy()
    df1.loc[len(df1) - 1, ["open", "close", "high", "low"]] = [0.09930, 0.09920, 0.09932, 0.09910]
    candles[TF_CONFIRM] = attach_context(df1)
    return candles


def short_invalidation_bundle() -> dict[str, pd.DataFrame]:
    candles, approach_at = pullback_short_confirmation_bundle()
    df1 = candles["1m"].copy()
    i = len(df1) - 1
    # Keep highs below limit (0.1013) until final bar; 5m close breaks above pool
    for j, (o, c, h, l) in enumerate(
        [
            (0.10115, 0.10118, 0.10122, 0.10108),
            (0.10118, 0.10120, 0.10126, 0.10116),
            (0.10120, 0.10122, 0.10128, 0.10118),
            (0.10122, 0.10124, 0.10129, 0.10120),
        ]
    ):
        df1.loc[i - 3 + j, ["open", "close", "high", "low"]] = [o, c, h, l]
    candles["1m"] = attach_context(df1)
    df5 = candles[TF_STRUCTURE].copy()
    df5.loc[len(df5) - 1, "close"] = 0.1016
    candles[TF_STRUCTURE] = df5
    return candles


def long_invalidation_bundle() -> dict[str, pd.DataFrame]:
    candles, _ = terminal_long_confirmation_bundle()
    df1 = candles["1m"].copy()
    i = len(df1) - 1
    df1.loc[i - 1, ["open", "close", "high", "low"]] = [0.09910, 0.09928, 0.09932, 0.09905]
    df1.loc[i, ["open", "close", "high", "low"]] = [0.09928, 0.09870, 0.09930, 0.09860]
    candles["1m"] = attach_context(df1)
    return candles


def synthetic_doge_bundle() -> dict[str, pd.DataFrame]:
    start = datetime(2026, 8, 10, 0, 0, 0)
    df1 = _bars(start, 400, freq_min=1, base=0.1000, drift=-0.00005)
    return _bundle_from_1m(df1)


def pullback_short_fixture_pools(approach_at: datetime) -> dict[str, list[PoolRecord]]:
    return static_pools(known_at=approach_at - timedelta(hours=2))


def no_reclaim_1m_tail(base: pd.DataFrame) -> pd.DataFrame:
    df = base.copy()
    i = len(df) - 3
    df.loc[i, ["open", "close", "high", "low"]] = [0.1012, 0.10125, 0.1013, 0.1011]
    df.loc[i + 1, ["open", "close", "high", "low"]] = [0.10125, 0.10122, 0.10128, 0.10120]
    df.loc[i + 2, ["open", "close", "high", "low"]] = [0.10122, 0.10124, 0.10126, 0.10121]
    return df
