"""Repository for generated trading signals."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence
from uuid import UUID

from signal_generator.db.client import ClickHouseClient

SIGNAL_COLUMNS = [
    "signal_id",
    "generated_at",
    "candle_open_time",
    "candle_close_time",
    "symbol",
    "timeframe",
    "direction",
    "signal_type",
    "signal_price",
    "stoch_k",
    "stoch_d",
    "wave_state",
    "tier_a",
    "tier_a_context",
    "rank_score",
    "selected",
    "selection_reason",
    "trend_15m",
    "trend_30m",
    "trend_1h",
    "trend_4h",
    "signal_bias",
    "traded",
    "trade_id",
    "generator_version",
    "strategy_version",
    "metadata",
    "ingested_at",
]


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass(slots=True)
class Signal:
    symbol: str
    timeframe: str
    direction: str
    signal_type: str
    signal_price: Decimal | float | str
    candle_open_time: datetime
    candle_close_time: datetime
    generator_version: str
    strategy_version: str
    signal_id: UUID | None = None
    generated_at: datetime | None = None
    stoch_k: float | None = None
    stoch_d: float | None = None
    wave_state: str | None = None
    tier_a: bool = False
    tier_a_context: str = ""
    rank_score: float | None = None
    selected: bool = False
    selection_reason: str = ""
    trend_15m: str | None = None
    trend_30m: str | None = None
    trend_1h: str | None = None
    trend_4h: str | None = None
    signal_bias: str | None = None
    traded: bool = False
    trade_id: str | None = None
    metadata: str = "{}"
    ingested_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError(f"direction must be LONG or SHORT, got {self.direction!r}")
        if self.signal_id is None:
            self.signal_id = uuid.uuid4()
        if self.generated_at is None:
            self.generated_at = datetime.now(timezone.utc)

    def as_row(self) -> list[Any]:
        assert self.signal_id is not None
        assert self.generated_at is not None
        ingested = self.ingested_at or datetime.now(timezone.utc)
        return [
            self.signal_id,
            _ensure_utc(self.generated_at),
            _ensure_utc(self.candle_open_time),
            _ensure_utc(self.candle_close_time),
            self.symbol,
            self.timeframe,
            self.direction,
            self.signal_type,
            self.signal_price,
            self.stoch_k,
            self.stoch_d,
            self.wave_state,
            1 if self.tier_a else 0,
            self.tier_a_context,
            self.rank_score,
            1 if self.selected else 0,
            self.selection_reason,
            self.trend_15m,
            self.trend_30m,
            self.trend_1h,
            self.trend_4h,
            self.signal_bias,
            1 if self.traded else 0,
            self.trade_id,
            self.generator_version,
            self.strategy_version,
            self.metadata,
            _ensure_utc(ingested),
        ]


class SignalRepository:
    TABLE = "signals"

    def __init__(self, client: ClickHouseClient) -> None:
        self._client = client

    def insert_signal(self, signal: Signal) -> UUID:
        self.insert_signals([signal])
        assert signal.signal_id is not None
        return signal.signal_id

    def insert_signals(self, signals: Sequence[Signal]) -> int:
        if not signals:
            return 0
        rows = [s.as_row() for s in signals]
        self._client.insert(self.TABLE, rows, SIGNAL_COLUMNS)
        return len(rows)

    def get_signals(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        timeframe: str | None = None,
        time_field: str = "candle_open_time",
    ) -> list[dict[str, Any]]:
        """Load signals for chart / research in [start, end).

        ``time_field`` is ``candle_open_time`` (default, chart-aligned) or
        ``generated_at``.
        """
        if time_field not in ("candle_open_time", "generated_at", "candle_close_time"):
            raise ValueError(f"Unsupported time_field: {time_field!r}")
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        tf_clause = ""
        params: dict[str, Any] = {
            "symbol": symbol,
            "start": start,
            "end": end,
        }
        if timeframe is not None:
            tf_clause = "AND timeframe = {timeframe:String}"
            params["timeframe"] = timeframe

        result = self._client.query(
            f"""
            SELECT
                signal_id, generated_at, candle_open_time, candle_close_time,
                symbol, timeframe, direction, signal_type, signal_price,
                stoch_k, stoch_d, wave_state,
                tier_a, tier_a_context,
                rank_score, selected, selection_reason,
                trend_15m, trend_30m, trend_1h, trend_4h, signal_bias,
                traded, trade_id,
                generator_version, strategy_version,
                metadata, ingested_at
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE symbol = {{symbol:String}}
              AND {time_field} >= {{start:DateTime64(3, 'UTC')}}
              AND {time_field} < {{end:DateTime64(3, 'UTC')}}
              {tf_clause}
            ORDER BY {time_field} ASC, signal_id ASC
            """,
            parameters=params,
        )
        return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

    def query_signals(
        self,
        *,
        start: datetime,
        end: datetime,
        symbols: Sequence[str] | None = None,
        timeframe: str | None = None,
        direction: str | None = None,
        tier_a: bool | None = None,
        selected: bool | None = None,
        strategy_version: str | None = None,
        time_field: str = "candle_close_time",
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paginated signal feed for dashboards. Returns (rows, total_count).

        Filter window is half-open on ``time_field``: ``[start, end)``.
        Default sort: newest ``candle_close_time`` first (display SoT).
        """
        if time_field not in ("candle_open_time", "generated_at", "candle_close_time"):
            raise ValueError(f"Unsupported time_field: {time_field!r}")
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        lim = max(1, min(int(limit), 2000))
        off = max(0, int(offset))

        clauses = [
            f"{time_field} >= {{start:DateTime64(3, 'UTC')}}",
            f"{time_field} < {{end:DateTime64(3, 'UTC')}}",
        ]
        params: dict[str, Any] = {"start": start, "end": end, "lim": lim, "off": off}

        if symbols:
            syms = [s.upper() for s in symbols if s]
            if syms:
                clauses.append("symbol IN {symbols:Array(String)}")
                params["symbols"] = syms
        if timeframe:
            clauses.append("timeframe = {timeframe:String}")
            params["timeframe"] = str(timeframe)
        if direction:
            d = str(direction).upper()
            if d in ("LONG", "SHORT"):
                clauses.append("direction = {direction:String}")
                params["direction"] = d
        if tier_a is not None:
            clauses.append("tier_a = {tier_a:UInt8}")
            params["tier_a"] = 1 if tier_a else 0
        if selected is not None:
            clauses.append("selected = {selected:UInt8}")
            params["selected"] = 1 if selected else 0
        if strategy_version:
            clauses.append("strategy_version = {strategy_version:String}")
            params["strategy_version"] = str(strategy_version)

        where = " AND ".join(clauses)
        count_r = self._client.query(
            f"""
            SELECT count() AS c
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE {where}
            """,
            parameters=params,
        )
        total = int(count_r.result_rows[0][0]) if count_r.result_rows else 0

        result = self._client.query(
            f"""
            SELECT
                signal_id, generated_at, candle_open_time, candle_close_time,
                symbol, timeframe, direction, signal_type, signal_price,
                stoch_k, stoch_d, wave_state,
                tier_a, tier_a_context,
                rank_score, selected, selection_reason,
                trend_15m, trend_30m, trend_1h, trend_4h, signal_bias,
                traded, trade_id,
                generator_version, strategy_version,
                metadata, ingested_at
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE {where}
            ORDER BY candle_close_time DESC, generated_at DESC, signal_id ASC
            LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}
            """,
            parameters=params,
        )
        rows = [
            dict(zip(result.column_names, row, strict=True))
            for row in result.result_rows
        ]
        return rows, total

    def delete_test_rows(self, *, generator_version_prefix: str = "TEST_") -> None:
        self._client.command(
            f"""
            ALTER TABLE {self._client.database}.{self.TABLE}
            DELETE WHERE startsWith(generator_version, {{prefix:String}})
            """,
            parameters={"prefix": generator_version_prefix},
        )

    def delete_by_generator_version(self, generator_version: str) -> None:
        self._client.command(
            f"""
            ALTER TABLE {self._client.database}.{self.TABLE}
            DELETE WHERE generator_version = {{gv:String}}
            """,
            parameters={"gv": generator_version},
        )
