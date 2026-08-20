"""ClickHouse-backed PublicTradeSource (thin wrap of public_trades SELECT)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

from orderbook_analyse.dynamic_wall_detector import ReadOnlyClickHouse, connect_readonly
from orderbook_analyse.public_trade_source.protocol import (
    NormalizedPublicTrade,
    TradeCoverageReport,
)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _as_decimal(value) -> Decimal:
    return Decimal(str(value))


class ClickHousePublicTradeSource:
    """Reads ``orderbook_analysis.public_trades`` with CH Buy/Sell semantics."""

    source_name = "clickhouse"

    def __init__(self, db: ReadOnlyClickHouse | None = None) -> None:
        self._db = db

    @property
    def db(self) -> ReadOnlyClickHouse:
        if self._db is None:
            self._db = connect_readonly()
        return self._db

    def iter_trades(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[NormalizedPublicTrade]:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        result = self.db.query(
            """
            SELECT
                trade_ts, symbol, side, quantity, price, notional,
                trade_id, tick_direction
            FROM public_trades
            WHERE symbol = %(symbol)s
              AND trade_ts >= %(start)s
              AND trade_ts < %(end)s
            ORDER BY trade_ts, trade_id
            """,
            parameters={"symbol": symbol, "start": start, "end": end},
        )
        cols = list(result.column_names)
        for row in result.result_rows:
            d = dict(zip(cols, row, strict=True))
            ts = d["trade_ts"]
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            yield NormalizedPublicTrade(
                trade_ts=ts,
                symbol=str(d["symbol"]),
                side=str(d["side"]),
                size=_as_decimal(d["quantity"]),
                price=_as_decimal(d["price"]),
                notional=_as_decimal(d["notional"]),
                trade_id=str(d["trade_id"]),
                tick_direction=str(d.get("tick_direction") or ""),
                source=self.source_name,
                notional_source="clickhouse",
            )

    def coverage(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> TradeCoverageReport:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        report = TradeCoverageReport(
            symbol=symbol,
            requested_start=start,
            requested_end=end,
            source=self.source_name,
        )
        try:
            row = self.db.query(
                """
                SELECT
                    min(trade_ts) AS tmin,
                    max(trade_ts) AS tmax,
                    count() AS n,
                    countIf(side = 'Buy') AS buys,
                    countIf(side = 'Sell') AS sells
                FROM public_trades
                WHERE symbol = %(symbol)s
                  AND trade_ts >= %(start)s
                  AND trade_ts < %(end)s
                """,
                parameters={"symbol": symbol, "start": start, "end": end},
            ).first_item
        except Exception as exc:  # noqa: BLE001
            report.valid = False
            report.reason = f"clickhouse_error: {exc}"
            return report

        n = int(row["n"] or 0)
        report.trades_emitted = n
        report.rows_read = n
        report.buy_count = int(row["buys"] or 0)
        report.sell_count = int(row["sells"] or 0)
        tmin, tmax = row["tmin"], row["tmax"]
        if tmin is not None:
            if getattr(tmin, "tzinfo", None) is None:
                tmin = tmin.replace(tzinfo=timezone.utc)
            report.actual_first_ts = tmin
        if tmax is not None:
            if getattr(tmax, "tzinfo", None) is None:
                tmax = tmax.replace(tzinfo=timezone.utc)
            report.actual_last_ts = tmax
        report.valid = n > 0
        report.reason = "ok" if report.valid else "no_trades"
        return report
