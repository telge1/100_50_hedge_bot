"""Read-only ClickHouse loaders for the market profile.

Binning happens server-side. A single BTCUSDT day holds ~2.7M trades, so
pulling ticks to bin them in Python would dominate the runtime for no gain —
the profile only ever needs per-bin aggregates.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from . import CANDLES_FQN, TRADES_FQN
from .anchor import as_utc
from .contracts import ProfileBin

QSET = {"max_execution_time": 300, "receive_timeout": 320}

# Nice-number ladder for the price step, so bin edges stay human-readable
# and stable across windows of similar span.
_STEP_MANTISSAS = (1.0, 2.0, 2.5, 5.0, 10.0)


def default_client() -> Any:
    return get_clickhouse_client()


def _q(client: Any, sql: str, params: dict | None = None) -> list[tuple]:
    return client.query(sql, parameters=params or {}, settings=QSET).result_rows


def fetch_window_ohlc(
    client: Any, symbol: str, start: datetime, end: datetime
) -> tuple[float, float, float, float] | None:
    """Window OHLC from 1m candles as ``(open, high, low, close)``.

    Cheap compared to the trade scan and needed twice: the high/low sets the
    bin grid, the open/close feeds the directional metric.
    """
    rows = _q(
        client,
        f"""
        SELECT
          argMin(open, open_time) AS o,
          max(high)               AS h,
          min(low)                AS l,
          argMax(close, open_time) AS c
        FROM {CANDLES_FQN} FINAL
        WHERE symbol={{s:String}} AND interval='1m'
          AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}
        """,
        {"s": symbol, "a": as_utc(start), "b": as_utc(end)},
    )
    if not rows or any(v is None for v in rows[0][:4]):
        return None
    o, h, low, c = (float(v) for v in rows[0][:4])
    if not all(math.isfinite(v) for v in (o, h, low, c)) or h < low:
        return None
    return o, h, low, c


def resolve_price_step(price_low: float, price_high: float, target_bins: int) -> float:
    """Pick a nice-number price step yielding roughly `target_bins` buckets.

    Deriving the step from the window's own span keeps the code symbol
    agnostic: BTC at ~8e4 and DOGE at ~2e-1 both land near `target_bins`
    without a per-symbol tick-size table.
    """
    if target_bins <= 0:
        raise ValueError("target_bins must be positive")
    span = float(price_high) - float(price_low)
    if not math.isfinite(span) or span <= 0:
        raise ValueError("price range must be positive to derive a step")
    raw = span / float(target_bins)
    exp = math.floor(math.log10(raw))
    base = raw / (10.0**exp)
    for m in _STEP_MANTISSAS:
        if base <= m:
            return m * (10.0**exp)
    return 10.0 * (10.0**exp)


def fetch_volume_at_price(
    client: Any,
    symbol: str,
    start: datetime,
    end: datetime,
    price_step: float,
    *,
    use_final: bool = True,
) -> list[ProfileBin]:
    """Volume-at-price for a window, aggregated in ClickHouse.

    `side` is the taker aggressor, so the buy/sell split is real aggression
    rather than an uptick/downtick guess. Bins with no trades are absent here
    and are densified by :func:`densify_bins`.

    `use_final` deduplicates the ReplacingMergeTree. It costs roughly 60x the
    runtime of a non-FINAL scan, so it can be disabled for exploratory sweeps
    once parity has been confirmed for the range.
    """
    step = float(price_step)
    if not math.isfinite(step) or step <= 0:
        raise ValueError("price_step must be positive")
    final = "FINAL" if use_final else ""
    rows = _q(
        client,
        f"""
        SELECT
          toInt64(floor(toFloat64(price)/{{step:Float64}})) AS bin_index,
          sum(toFloat64(size))                             AS volume,
          sumIf(toFloat64(size), side='Buy')               AS buy_volume,
          sumIf(toFloat64(size), side='Sell')              AS sell_volume,
          count()                                          AS trades,
          sum(toFloat64(size)*toFloat64(price))            AS notional
        FROM {TRADES_FQN} {final}
        PREWHERE symbol={{s:String}}
        WHERE trade_ts>={{a:DateTime64(3,'UTC')}} AND trade_ts<{{b:DateTime64(3,'UTC')}}
        GROUP BY bin_index
        ORDER BY bin_index
        """,
        {"s": symbol, "a": as_utc(start), "b": as_utc(end), "step": step},
    )
    out: list[ProfileBin] = []
    for bin_index, volume, buy_volume, sell_volume, trades, notional in rows:
        idx = int(bin_index)
        lo = idx * step
        out.append(
            ProfileBin(
                bin_index=idx,
                price_low=lo,
                price_high=lo + step,
                price_mid=lo + step / 2.0,
                volume=float(volume or 0.0),
                buy_volume=float(buy_volume or 0.0),
                sell_volume=float(sell_volume or 0.0),
                trades=int(trades or 0),
                notional=float(notional or 0.0),
            )
        )
    return out


def densify_bins(bins: list[ProfileBin], price_step: float) -> list[ProfileBin]:
    """Insert zero-volume bins so the histogram has no implicit holes.

    Gaps matter: a price the market skipped is a low-volume node, and it can
    only be detected if the empty bin is present.
    """
    if not bins:
        return []
    step = float(price_step)
    by_index = {b.bin_index: b for b in bins}
    lo, hi = min(by_index), max(by_index)
    out: list[ProfileBin] = []
    for idx in range(lo, hi + 1):
        existing = by_index.get(idx)
        if existing is not None:
            out.append(existing)
            continue
        edge = idx * step
        out.append(
            ProfileBin(
                bin_index=idx,
                price_low=edge,
                price_high=edge + step,
                price_mid=edge + step / 2.0,
                volume=0.0,
                buy_volume=0.0,
                sell_volume=0.0,
                trades=0,
                notional=0.0,
            )
        )
    return out
