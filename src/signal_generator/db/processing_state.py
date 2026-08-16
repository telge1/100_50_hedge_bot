"""Persistent per-(symbol, timeframe, strategy_version) processing watermarks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from signal_generator.db.client import ClickHouseClient


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass(slots=True)
class ProcessingState:
    symbol: str
    timeframe: str
    strategy_version: str
    last_processed_candle_open_time: datetime
    last_processed_available_at: datetime
    exchange: str = "bybit"
    metadata: str = "{}"
    updated_at: datetime | None = None


class ProcessingStateRepository:
    TABLE = "signal_processing_state"

    def __init__(self, client: ClickHouseClient) -> None:
        self._client = client

    def get(
        self,
        symbol: str,
        timeframe: str,
        strategy_version: str,
        *,
        exchange: str = "bybit",
    ) -> ProcessingState | None:
        result = self._client.query(
            f"""
            SELECT
                exchange, symbol, timeframe, strategy_version,
                last_processed_candle_open_time, last_processed_available_at,
                updated_at, metadata
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE exchange = {{exchange:String}}
              AND symbol = {{symbol:String}}
              AND timeframe = {{timeframe:String}}
              AND strategy_version = {{strategy_version:String}}
            LIMIT 1
            """,
            parameters={
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy_version": strategy_version,
            },
        )
        if not result.result_rows:
            return None
        row = dict(zip(result.column_names, result.result_rows[0], strict=True))
        return ProcessingState(
            exchange=str(row["exchange"]),
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            strategy_version=str(row["strategy_version"]),
            last_processed_candle_open_time=_ensure_utc(row["last_processed_candle_open_time"]),
            last_processed_available_at=_ensure_utc(row["last_processed_available_at"]),
            updated_at=_ensure_utc(row["updated_at"]) if row.get("updated_at") else None,
            metadata=str(row.get("metadata") or "{}"),
        )

    def upsert(self, state: ProcessingState) -> None:
        updated = state.updated_at or datetime.now(timezone.utc)
        self._client.insert(
            self.TABLE,
            [
                [
                    state.exchange,
                    state.symbol,
                    state.timeframe,
                    state.strategy_version,
                    _ensure_utc(state.last_processed_candle_open_time),
                    _ensure_utc(state.last_processed_available_at),
                    _ensure_utc(updated),
                    state.metadata,
                ]
            ],
            [
                "exchange",
                "symbol",
                "timeframe",
                "strategy_version",
                "last_processed_candle_open_time",
                "last_processed_available_at",
                "updated_at",
                "metadata",
            ],
        )

    def list_for_strategy(
        self, strategy_version: str, *, exchange: str = "bybit"
    ) -> list[ProcessingState]:
        result = self._client.query(
            f"""
            SELECT
                exchange, symbol, timeframe, strategy_version,
                last_processed_candle_open_time, last_processed_available_at,
                updated_at, metadata
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE exchange = {{exchange:String}}
              AND strategy_version = {{strategy_version:String}}
            ORDER BY symbol, timeframe
            """,
            parameters={"exchange": exchange, "strategy_version": strategy_version},
        )
        out: list[ProcessingState] = []
        for row in result.result_rows:
            d = dict(zip(result.column_names, row, strict=True))
            out.append(
                ProcessingState(
                    exchange=str(d["exchange"]),
                    symbol=str(d["symbol"]),
                    timeframe=str(d["timeframe"]),
                    strategy_version=str(d["strategy_version"]),
                    last_processed_candle_open_time=_ensure_utc(
                        d["last_processed_candle_open_time"]
                    ),
                    last_processed_available_at=_ensure_utc(d["last_processed_available_at"]),
                    updated_at=_ensure_utc(d["updated_at"]) if d.get("updated_at") else None,
                    metadata=str(d.get("metadata") or "{}"),
                )
            )
        return out

    def seed_watermarks_from_strategy(
        self,
        *,
        source_strategy_version: str,
        target_strategy_version: str,
        symbols: list[str] | None = None,
        exchange: str = "bybit",
    ) -> int:
        """Copy watermarks source→target when target is empty (new strategy version).

        Avoids multi-day cold catch-up that blocks LIVE. Historical research uses
        existing signal rows + TRADE_NO_BE50 outcomes; live continues from tip.
        """
        if source_strategy_version == target_strategy_version:
            return 0
        existing = self.list_for_strategy(target_strategy_version, exchange=exchange)
        if existing:
            return 0
        src = self.list_for_strategy(source_strategy_version, exchange=exchange)
        if not src:
            return 0
        sym_set = {s.upper() for s in symbols} if symbols else None
        n = 0
        for st in src:
            if sym_set is not None and st.symbol.upper() not in sym_set:
                continue
            self.upsert(
                ProcessingState(
                    exchange=st.exchange,
                    symbol=st.symbol,
                    timeframe=st.timeframe,
                    strategy_version=target_strategy_version,
                    last_processed_candle_open_time=st.last_processed_candle_open_time,
                    last_processed_available_at=st.last_processed_available_at,
                    metadata='{"seeded_from":"%s"}' % source_strategy_version,
                )
            )
            n += 1
        return n

    def delete_for_strategy(self, strategy_version: str, *, exchange: str = "bybit") -> None:
        """Test / replay cleanup via mutation."""
        self._client.command(
            f"""
            ALTER TABLE {self._client.database}.{self.TABLE}
            DELETE WHERE exchange = {{exchange:String}}
              AND strategy_version = {{strategy_version:String}}
            """,
            parameters={"exchange": exchange, "strategy_version": strategy_version},
        )
