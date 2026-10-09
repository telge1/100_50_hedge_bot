"""ClickHouse partial-bucket forming bar for Research UI only (not scanner decisions)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .trp_import import load_trp

# Primary HTF bucket for OHLC when synthesizing a 1m-compatible forming tick.
_DEFAULT_PARTIAL_TF = "15m"


def forming_bar_from_clickhouse(
    symbol: str,
    *,
    now: datetime | None = None,
    timeframe: str = _DEFAULT_PARTIAL_TF,
    get_1m_candles: Callable[..., list] | None = None,
) -> dict[str, Any] | None:
    """Build a forming tick from closed 1m rows in the current HTF bucket.

    Returns a dict compatible with collector ``/api/collector/forming`` and
    ``apply_live_forming_tip`` (``time`` = unix seconds of the latest closed 1m
    open in the bucket; OHLC = partial aggregate of the HTF bucket).
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    tf = str(timeframe or _DEFAULT_PARTIAL_TF)
    trp = load_trp()
    floor_utc = trp["floor_utc"]
    aggregate = trp["aggregate"]
    now_utc = now if now is not None else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    bucket_open = floor_utc(now_utc, tf)

    if get_1m_candles is not None:
        bars_1m = get_1m_candles(sym, bucket_open)
    else:
        from .service import get_source

        bars_1m = get_source().get_1m_candles(
            sym, start=bucket_open, newest_first_limit=False
        )
    if not bars_1m:
        return None

    partials = aggregate(bars_1m, tf, strict_complete_buckets=False)
    partial = None
    for row in partials:
        if row.timestamp == bucket_open:
            partial = row
            break
    if partial is None:
        return None

    last_1m = bars_1m[-1]
    last_open = int(last_1m.unix_seconds)
    lag = max(0.0, (now_utc - last_1m.timestamp).total_seconds())

    return {
        "time": last_open,
        "open": float(partial.open),
        "high": float(partial.high),
        "low": float(partial.low),
        "close": float(partial.close),
        "volume": float(partial.volume),
        "forming": True,
        "forming_source": "clickhouse_fallback",
        "forming_lag_seconds": round(lag, 3),
    }
