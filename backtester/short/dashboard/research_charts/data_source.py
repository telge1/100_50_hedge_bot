"""MySQL market_candles → TRP Candle. Fallback adapter only.

Normal Research operation uses ClickHouseResearchCandleSource
(signal_generator.candles_1m). This class remains for fallback/tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

from .mysql_config import MysqlConfig, load_mysql_config
from .trp_import import load_trp

SOURCE_TF = "1m"


def _as_utc(ts: datetime) -> datetime:
    """DATETIME(6) is stored as UTC wall-clock; session TZ must not leak."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _to_naive_utc(ts: datetime) -> datetime:
    return _as_utc(ts).replace(tzinfo=None)


class MySQLResearchCandleSource:
    """Read-only 1m adapter. Does not query stored 5m/15m/... rows."""

    def __init__(self, config: MysqlConfig | None = None):
        self.config = config or load_mysql_config()
        self.table = "market_candles"
        self.source_timeframe = SOURCE_TF
        self.source_name = "mysql_market_candles_1m"

    def _connect(self):
        return pymysql.connect(cursorclass=DictCursor, **self.config.connect_kwargs())

    def list_symbol_meta(self) -> list[dict[str, Any]]:
        sql = f"""
            SELECT symbol,
                   MIN(open_time) AS first_open,
                   MAX(open_time) AS last_open,
                   COUNT(*) AS candle_count
            FROM {self.table}
            WHERE exchange = %s
              AND timeframe = %s
              AND is_closed = 1
            GROUP BY symbol
            HAVING COUNT(*) > 0
            ORDER BY symbol
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (self.config.exchange, SOURCE_TF))
                rows = cur.fetchall()
        out = []
        for row in rows:
            first = _as_utc(row["first_open"])
            last = _as_utc(row["last_open"])
            out.append(
                {
                    "symbol": str(row["symbol"]),
                    "first_time": int(first.timestamp()),
                    "last_time": int(last.timestamp()),
                    "first_time_iso": first.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "last_time_iso": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "candle_count": int(row["candle_count"]),
                    "timeframe": SOURCE_TF,
                    "exchange": self.config.exchange,
                }
            )
        return out

    def list_symbols(self) -> list[str]:
        return [row["symbol"] for row in self.list_symbol_meta()]

    def get_1m_candles(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
        *,
        newest_first_limit: bool = True,
    ) -> list:
        trp = load_trp()
        Candle = trp["Candle"]
        ensure_utc = trp["ensure_utc"]
        sym = str(symbol or "").strip().upper()
        if not sym:
            return []

        where = [
            "exchange = %s",
            "symbol = %s",
            "timeframe = %s",
            "is_closed = 1",
        ]
        params: list[Any] = [self.config.exchange, sym, SOURCE_TF]
        if start is not None:
            where.append("open_time >= %s")
            params.append(_to_naive_utc(ensure_utc(start)))
        if end is not None:
            where.append("open_time <= %s")
            params.append(_to_naive_utc(ensure_utc(end)))

        order = "open_time DESC" if (limit and newest_first_limit and start is None) else "open_time ASC"
        sql = f"""
            SELECT open_time, open, high, low, close, volume
            FROM {self.table}
            WHERE {' AND '.join(where)}
            ORDER BY {order}
        """
        if limit:
            sql += " LIMIT %s"
            params.append(int(limit))

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        if order.endswith("DESC"):
            rows = list(reversed(rows))

        candles = []
        for row in rows:
            candles.append(
                Candle(
                    timestamp=_as_utc(row["open_time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    symbol=sym,
                    timeframe=SOURCE_TF,
                )
            )
        return candles

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list:
        """DataSource-compatible: 1m SQL, HTF via TRP aggregate(strict)."""
        trp = load_trp()
        aggregate = trp["aggregate"]
        expected_source_bars = trp["expected_source_bars"]
        timeframe_seconds = trp["timeframe_seconds"]
        tf = str(timeframe)
        timeframe_seconds(tf)
        if tf == SOURCE_TF:
            return self.get_1m_candles(symbol, start=start, end=end, newest_first_limit=False)

        pad_seconds = expected_source_bars(SOURCE_TF, tf) * timeframe_seconds(SOURCE_TF)
        src_start = start
        if start is not None:
            src_start = datetime.fromtimestamp(int(start.timestamp()) - pad_seconds, tz=timezone.utc)
        source = self.get_1m_candles(symbol, start=src_start, end=end, newest_first_limit=False)
        return aggregate(source, tf, strict_complete_buckets=True)
