"""ClickHouse signal_generator.candles_1m → TRP Candle.

Same logical semantics as the live collector:
exchange + symbol + interval='1m' + is_closed=1, FINAL for ReplacingMergeTree.
Higher timeframes stay TRP aggregate(); this class never GROUP BYs HTF.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import clickhouse_connect

from .clickhouse_config import ClickHouseConfig, load_clickhouse_config
from .data_source import SOURCE_TF, _as_utc
from .trp_import import load_trp

SOURCE_NAME = "clickhouse_candles_1m"


def _to_utc_dt(ts: datetime) -> datetime:
    return _as_utc(ts)


class ClickHouseResearchCandleSource:
    """Read-only 1m adapter over the collector Source of Truth."""

    def __init__(self, config: ClickHouseConfig | None = None):
        self.config = config or load_clickhouse_config()
        self.table = self.config.table
        self.source_timeframe = SOURCE_TF
        self.source_name = SOURCE_NAME

    def _client(self):
        return clickhouse_connect.get_client(**self.config.connect_kwargs())

    def list_symbol_meta(self) -> list[dict[str, Any]]:
        sql = f"""
            SELECT
                symbol,
                min(open_time) AS first_open,
                max(open_time) AS last_open,
                count() AS candle_count
            FROM {self.config.database}.{self.table} FINAL
            WHERE exchange = {{exchange:String}}
              AND interval = {{interval:String}}
              AND is_closed = 1
            GROUP BY symbol
            HAVING count() > 0
            ORDER BY symbol
        """
        client = self._client()
        try:
            result = client.query(
                sql,
                parameters={
                    "exchange": self.config.exchange,
                    "interval": SOURCE_TF,
                },
            )
        finally:
            client.close()
        out = []
        for symbol, first_open, last_open, candle_count in result.result_rows:
            first = _to_utc_dt(first_open)
            last = _to_utc_dt(last_open)
            out.append(
                {
                    "symbol": str(symbol).upper(),
                    "first_time": int(first.timestamp()),
                    "last_time": int(last.timestamp()),
                    "first_time_iso": first.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "last_time_iso": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "candle_count": int(candle_count),
                    "timeframe": SOURCE_TF,
                    "exchange": self.config.exchange,
                    "source": SOURCE_NAME,
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
            "exchange = {exchange:String}",
            "symbol = {symbol:String}",
            "interval = {interval:String}",
            "is_closed = 1",
        ]
        params: dict[str, Any] = {
            "exchange": self.config.exchange,
            "symbol": sym,
            "interval": SOURCE_TF,
        }
        if start is not None:
            where.append("open_time >= {start:DateTime64(3, 'UTC')}")
            params["start"] = ensure_utc(start)
        if end is not None:
            where.append("open_time <= {end:DateTime64(3, 'UTC')}")
            params["end"] = ensure_utc(end)

        desc = bool(limit and newest_first_limit and start is None)
        order = "open_time DESC" if desc else "open_time ASC"
        sql = f"""
            SELECT open_time, open, high, low, close, volume
            FROM {self.config.database}.{self.table} FINAL
            WHERE {' AND '.join(where)}
            ORDER BY {order}
        """
        if limit:
            sql += " LIMIT {lim:UInt32}"
            params["lim"] = int(limit)

        client = self._client()
        try:
            result = client.query(sql, parameters=params)
            rows = list(result.result_rows)
        finally:
            client.close()

        if desc:
            rows = list(reversed(rows))

        candles = []
        for open_time, open_, high, low, close, volume in rows:
            candles.append(
                Candle(
                    timestamp=_to_utc_dt(open_time),
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(volume),
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
