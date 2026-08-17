"""Candle vs public-trade 1m reconciliation (read-only candles)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from signal_generator.bybit.public_trades.urls import utc_day_bounds
from signal_generator.db.client import ClickHouseClient
from signal_generator.db.public_trades import CANONICAL_FQN

# Relative volume mismatch vs candle base/quote. Documented; not silently widened.
VOLUME_REL_TOL = Decimal("0.005")
PRICE_ABS_TOL = Decimal("0")


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _rel_diff(left: Decimal, right: Decimal) -> Decimal:
    scale = max(abs(right), abs(left), Decimal("1e-12"))
    return abs(left - right) / scale


def classify_minute(
    *,
    candle: dict[str, Any] | None,
    trades: dict[str, Any] | None,
) -> str:
    has_candle = candle is not None
    n_trades = int(trades["n_trades"]) if trades else 0
    if not has_candle and n_trades > 0:
        return "CANDLE_MISSING"
    if not has_candle:
        return "CANDLE_MISSING"
    vol = _dec(candle["volume"])
    if n_trades == 0:
        if vol == 0:
            return "NO_TRADES_EXPECTED"
        return "PUBLIC_TRADES_MISSING"
    assert trades is not None
    mismatches: list[str] = []
    if _rel_diff(_dec(trades["base_volume"]), vol) > VOLUME_REL_TOL:
        mismatches.append("base_volume")
    turnover = _dec(candle.get("turnover"))
    if turnover > 0 and _rel_diff(_dec(trades["quote_volume"]), turnover) > VOLUME_REL_TOL:
        mismatches.append("quote_volume")
    # OHLC: trade high/low must match candle high/low; open/close = first/last trade.
    for field in ("open", "high", "low", "close"):
        if abs(_dec(trades[field]) - _dec(candle[field])) > PRICE_ABS_TOL:
            mismatches.append(field)
    if mismatches:
        return "CANDLE_MISMATCH"
    return "MATCH"


def reconcile_symbol_day(
    client: ClickHouseClient,
    *,
    symbol: str,
    day: date,
) -> list[dict[str, Any]]:
    start, end = utc_day_bounds(day)
    candles_q = client.query(
        """
        SELECT
            open_time,
            open,
            high,
            low,
            close,
            volume,
            turnover
        FROM signal_generator.candles_1m FINAL
        WHERE exchange = 'bybit'
          AND symbol = {symbol:String}
          AND interval = '1m'
          AND is_closed = 1
          AND open_time >= {start:DateTime64(3, 'UTC')}
          AND open_time < {end:DateTime64(3, 'UTC')}
        ORDER BY open_time
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    )
    candles = {
        row[0]: {
            "open_time": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "turnover": row[6],
        }
        for row in candles_q.result_rows
    }
    trades_q = client.query(
        f"""
        SELECT
            toStartOfMinute(trade_ts) AS minute,
            argMin(price, trade_ts) AS open,
            max(price) AS high,
            min(price) AS low,
            argMax(price, trade_ts) AS close,
            sum(size) AS base_volume,
            sum(notional) AS quote_volume,
            count() AS n_trades
        FROM {CANONICAL_FQN} FINAL
        WHERE symbol = {{symbol:String}}
          AND trade_ts >= {{start:DateTime64(3, 'UTC')}}
          AND trade_ts < {{end:DateTime64(3, 'UTC')}}
        GROUP BY minute
        ORDER BY minute
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    )
    trades = {
        row[0]: {
            "minute": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "base_volume": row[5],
            "quote_volume": row[6],
            "n_trades": int(row[7]),
        }
        for row in trades_q.result_rows
    }
    minutes = sorted(set(candles) | set(trades))
    out: list[dict[str, Any]] = []
    for minute in minutes:
        c = candles.get(minute)
        t = trades.get(minute)
        klass = classify_minute(candle=c, trades=t)
        out.append(
            {
                "symbol": symbol,
                "day": day.isoformat(),
                "minute_utc": minute.isoformat() if hasattr(minute, "isoformat") else str(minute),
                "class": klass,
                "candle_volume": str(c["volume"]) if c else "",
                "trade_base_volume": str(t["base_volume"]) if t else "",
                "candle_turnover": str(c["turnover"]) if c else "",
                "trade_quote_volume": str(t["quote_volume"]) if t else "",
                "n_trades": t["n_trades"] if t else 0,
                "candle_open": str(c["open"]) if c else "",
                "trade_open": str(t["open"]) if t else "",
                "candle_high": str(c["high"]) if c else "",
                "trade_high": str(t["high"]) if t else "",
                "candle_low": str(c["low"]) if c else "",
                "trade_low": str(t["low"]) if t else "",
                "candle_close": str(c["close"]) if c else "",
                "trade_close": str(t["close"]) if t else "",
            }
        )
    return out


def summarize_classes(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    return counts
