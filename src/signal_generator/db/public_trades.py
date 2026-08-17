"""ClickHouse repository for orderbook_analysis.public_trades_canonical."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from signal_generator.bybit.public_trades.csv_parse import ParsedPublicTrade
from signal_generator.bybit.public_trades.guards import (
    CANONICAL_DATABASE,
    CANONICAL_FQN,
    CANONICAL_TABLE,
    assert_canonical_sql,
    assert_canonical_table,
    assert_source,
)
from signal_generator.db.client import ClickHouseClient
from signal_generator.db.setup import MIGRATIONS_DIR, _split_sql_statements

CANONICAL_COLUMNS = [
    "trade_ts",
    "ingest_timestamp",
    "symbol",
    "trade_id",
    "side",
    "price",
    "size",
    "notional",
    "tick_direction",
    "is_rpi_trade",
    "source",
    "source_file",
    "exchange",
]

INSERT_SETTINGS = {
    "priority": 16,
    "max_insert_threads": 1,
    "max_threads": 1,
    "max_block_size": 8192,
}

CREATE_MIGRATION = "002_public_trades_canonical.sql"


def trade_to_row(
    trade: ParsedPublicTrade,
    *,
    ingest_timestamp: datetime,
    source: str,
) -> list[Any]:
    return [
        trade.trade_ts,
        ingest_timestamp,
        trade.symbol,
        trade.trade_id,
        trade.side,
        trade.price,
        trade.size,
        trade.notional,
        trade.tick_direction,
        int(trade.is_rpi_trade),
        assert_source(source),
        trade.source_file,
        "bybit",
    ]


class CanonicalPublicTradeRepository:
    TABLE = CANONICAL_TABLE
    DATABASE = CANONICAL_DATABASE

    def __init__(self, client: ClickHouseClient) -> None:
        self._client = client

    def ensure_table(self) -> None:
        path = MIGRATIONS_DIR / CREATE_MIGRATION
        text = path.read_text(encoding="utf-8")
        for statement in _split_sql_statements(text):
            if "INSERT INTO" in statement.upper():
                continue
            assert_canonical_sql(statement)
            self._client.command(statement)

    def insert_trades(
        self,
        trades: Sequence[ParsedPublicTrade],
        *,
        ingest_timestamp: datetime,
        source: str = "archive",
    ) -> int:
        if not trades:
            return 0
        assert_canonical_table(CANONICAL_FQN)
        rows = [
            trade_to_row(t, ingest_timestamp=ingest_timestamp, source=source)
            for t in trades
        ]
        self._client.insert(
            CANONICAL_TABLE,
            rows,
            CANONICAL_COLUMNS,
            database=CANONICAL_DATABASE,
            settings=INSERT_SETTINGS,
        )
        return len(rows)

    def physical_and_logical_counts(
        self,
        *,
        symbols: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        where, params = _window_where(symbols, start, end)
        physical = self._client.query(
            f"SELECT count() FROM {CANONICAL_FQN} {where}",
            parameters=params,
        ).result_rows[0][0]
        logical = self._client.query(
            f"SELECT uniqExact(symbol, trade_id) FROM {CANONICAL_FQN} {where}",
            parameters=params,
        ).result_rows[0][0]
        final_count = self._client.query(
            f"SELECT count() FROM {CANONICAL_FQN} FINAL {where}",
            parameters=params,
        ).result_rows[0][0]
        vol = self._client.query(
            f"""
            SELECT
                sum(size),
                sum(notional)
            FROM (
                SELECT
                    symbol,
                    trade_id,
                    any(size) AS size,
                    any(notional) AS notional
                FROM {CANONICAL_FQN}
                {where}
                GROUP BY symbol, trade_id
            )
            """,
            parameters=params,
        ).result_rows[0]
        return {
            "physical_rows": int(physical),
            "logical_unique_rows": int(logical),
            "final_rows": int(final_count),
            "logical_size_sum": vol[0],
            "logical_notional_sum": vol[1],
        }

    def filter_existing_trade_ids(
        self,
        symbol: str,
        trade_ids: Sequence[str],
        *,
        chunk_size: int = 5000,
    ) -> set[str]:
        """Return subset of trade_ids already present for symbol (any source/time)."""
        if not trade_ids:
            return set()
        symbol = symbol.upper()
        existing: set[str] = set()
        ids = list(trade_ids)
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            q = self._client.query(
                f"""
                SELECT trade_id
                FROM {CANONICAL_FQN}
                WHERE symbol = {{symbol:String}}
                  AND trade_id IN {{ids:Array(String)}}
                """,
                parameters={"symbol": symbol, "ids": chunk},
            )
            existing.update(str(r[0]) for r in q.result_rows)
        return existing

    def insert_trades_skip_existing(
        self,
        trades: Sequence[ParsedPublicTrade],
        *,
        ingest_timestamp: datetime,
        source: str = "archive",
        chunk_size: int = 5000,
    ) -> tuple[int, int]:
        """Insert only trade_ids not yet in table. Returns (inserted, skipped_existing)."""
        if not trades:
            return 0, 0
        by_symbol: dict[str, list[ParsedPublicTrade]] = {}
        for t in trades:
            by_symbol.setdefault(t.symbol.upper(), []).append(t)
        inserted = 0
        skipped = 0
        for symbol, sym_trades in by_symbol.items():
            ids = [t.trade_id for t in sym_trades]
            existing = self.filter_existing_trade_ids(symbol, ids, chunk_size=chunk_size)
            new_trades = [t for t in sym_trades if t.trade_id not in existing]
            skipped += len(sym_trades) - len(new_trades)
            inserted += self.insert_trades(
                new_trades, ingest_timestamp=ingest_timestamp, source=source
            )
        return inserted, skipped

    def count_by_symbol_day(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> list[tuple[str, str, int]]:
        q = self._client.query(
            f"""
            SELECT symbol, toString(toDate(trade_ts)) AS d, uniqExact(trade_id)
            FROM {CANONICAL_FQN}
            WHERE trade_ts >= {{start:DateTime64(3, 'UTC')}}
              AND trade_ts < {{end:DateTime64(3, 'UTC')}}
            GROUP BY symbol, d
            ORDER BY symbol, d
            """,
            parameters={"start": start, "end": end},
        )
        return [(str(r[0]), str(r[1]), int(r[2])) for r in q.result_rows]

    def exists(self) -> bool:
        return bool(
            self._client.query(f"EXISTS TABLE {CANONICAL_FQN}").result_rows[0][0]
        )


def _window_where(
    symbols: Sequence[str] | None,
    start: datetime | None,
    end: datetime | None,
) -> tuple[str, dict[str, Any]]:
    clauses = ["1 = 1"]
    params: dict[str, Any] = {}
    if symbols:
        clauses.append("symbol IN {symbols:Array(String)}")
        params["symbols"] = [s.upper() for s in symbols]
    if start is not None:
        clauses.append("trade_ts >= {start:DateTime64(3, 'UTC')}")
        params["start"] = start
    if end is not None:
        clauses.append("trade_ts < {end:DateTime64(3, 'UTC')}")
        params["end"] = end
    return "WHERE " + " AND ".join(clauses), params


def migration_sql_path() -> Path:
    return MIGRATIONS_DIR / CREATE_MIGRATION
