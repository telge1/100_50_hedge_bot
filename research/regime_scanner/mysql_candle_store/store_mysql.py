"""Optional SQLAlchemy/MySQL backend for the candle store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from research.regime_scanner.mysql_candle_store.config import RegimeDbConfig
from research.regime_scanner.mysql_candle_store.schema import (
    ALLOWED_SOURCES,
    ENSURE_TIMEFRAME_BIN_COLLATION_SQL,
    SCHEMA_SQL,
)
from research.regime_scanner.mysql_candle_store.source_policy import resolve_candle_upsert
from research.regime_scanner.mysql_candle_store.store_memory import UpsertStats
from research.regime_scanner.timeframes import ensure_utc_timestamp


def _naive_utc(ts: object) -> datetime:
    t = ensure_utc_timestamp(ts).to_pydatetime()
    return t.replace(tzinfo=None)


class MySQLCandleStore:
    """MySQL-backed candle store using SQLAlchemy (requires PyMySQL)."""

    def __init__(self, config: RegimeDbConfig) -> None:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("sqlalchemy is required for MySQLCandleStore") from exc
        self._text = text
        self._engine = create_engine(
            config.sqlalchemy_url,
            pool_pre_ping=True,
            future=True,
        )

    def close(self) -> None:
        self._engine.dispose()

    def init_schema(self) -> None:
        statements = [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]
        with self._engine.begin() as conn:
            for stmt in statements:
                conn.execute(self._text(stmt))
        # Separate connection: MySQL DDL commits implicitly; keep ALTER reliable
        # so ``1m`` (minute) and ``1M`` (month) remain distinct under UNIQUE.
        with self._engine.begin() as conn:
            coll = conn.execute(
                self._text(
                    """
                    SELECT COLLATION_NAME FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'market_candles'
                      AND COLUMN_NAME = 'timeframe'
                    """
                )
            ).scalar()
            if coll and str(coll).lower() != "utf8mb4_bin":
                conn.execute(self._text(ENSURE_TIMEFRAME_BIN_COLLATION_SQL))

    def _fetch_one(
        self,
        conn: Any,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        open_time: datetime,
    ) -> dict[str, Any] | None:
        sql = self._text(
            """
            SELECT exchange, symbol, timeframe, open_time, close_time,
                   open, high, low, close, volume, is_closed,
                   source, source_timeframe, source_hash
            FROM market_candles
            WHERE exchange = :exchange AND symbol = :symbol
              AND timeframe = :timeframe AND open_time = :open_time
            LIMIT 1
            """
        )
        row = conn.execute(
            sql,
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": open_time,
            },
        ).mappings().first()
        return dict(row) if row is not None else None

    def upsert_candles(self, rows: list[dict[str, Any]]) -> UpsertStats:
        if not rows:
            return UpsertStats()
        insert_sql = self._text(
            """
            INSERT INTO market_candles (
              exchange, symbol, timeframe, open_time, close_time,
              open, high, low, close, volume,
              is_closed, source, source_timeframe, source_hash
            ) VALUES (
              :exchange, :symbol, :timeframe, :open_time, :close_time,
              :open, :high, :low, :close, :volume,
              :is_closed, :source, :source_timeframe, :source_hash
            )
            """
        )
        update_sql = self._text(
            """
            UPDATE market_candles SET
              close_time = :close_time,
              open = :open,
              high = :high,
              low = :low,
              close = :close,
              volume = :volume,
              is_closed = :is_closed,
              source = :source,
              source_timeframe = :source_timeframe,
              source_hash = :source_hash,
              updated_at = CURRENT_TIMESTAMP(6)
            WHERE exchange = :exchange AND symbol = :symbol
              AND timeframe = :timeframe AND open_time = :open_time
            """
        )
        stats = UpsertStats()
        with self._engine.begin() as conn:
            for raw in rows:
                source = str(raw["source"])
                if source not in ALLOWED_SOURCES:
                    raise ValueError(f"unsupported source: {source}")
                payload = {
                    "exchange": str(raw["exchange"]),
                    "symbol": str(raw["symbol"]),
                    "timeframe": str(raw["timeframe"]),
                    "open_time": _naive_utc(raw["open_time"]),
                    "close_time": _naive_utc(raw["close_time"]),
                    "open": float(raw["open"]),
                    "high": float(raw["high"]),
                    "low": float(raw["low"]),
                    "close": float(raw["close"]),
                    "volume": float(raw["volume"]),
                    "is_closed": 1 if raw["is_closed"] else 0,
                    "source": source,
                    "source_timeframe": raw.get("source_timeframe"),
                    "source_hash": raw.get("source_hash"),
                }
                existing = self._fetch_one(
                    conn,
                    exchange=payload["exchange"],
                    symbol=payload["symbol"],
                    timeframe=payload["timeframe"],
                    open_time=payload["open_time"],
                )
                existing_norm = None
                if existing is not None:
                    existing_norm = dict(existing)
                    existing_norm["is_closed"] = bool(existing_norm["is_closed"])
                    existing_norm["open_time"] = _naive_utc(existing_norm["open_time"])
                    existing_norm["close_time"] = _naive_utc(existing_norm["close_time"])
                decision = resolve_candle_upsert(existing_norm, payload)
                if decision.action == "insert":
                    conn.execute(insert_sql, payload)
                    stats.inserted += 1
                elif decision.action == "update":
                    conn.execute(update_sql, payload)
                    stats.updated += 1
                elif decision.action == "unchanged":
                    stats.unchanged += 1
                elif decision.action == "skip_protected":
                    stats.skipped_protected += 1
                else:
                    stats.conflicts += 1
                    stats.conflict_details.append(
                        {
                            "exchange": payload["exchange"],
                            "symbol": payload["symbol"],
                            "timeframe": payload["timeframe"],
                            "open_time": ensure_utc_timestamp(payload["open_time"]).isoformat(),
                            "reason": decision.reason,
                            "existing_source": existing_norm.get("source") if existing_norm else None,
                            "incoming_source": source,
                        }
                    )
        return stats

    def fetch_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_time: object | None = None,
        end_time: object | None = None,
        decision_time: object | None = None,
        closed_only: bool = True,
        source: str | None = None,
    ) -> pd.DataFrame:
        clauses = [
            "exchange = :exchange",
            "symbol = :symbol",
            "timeframe = :timeframe",
        ]
        params: dict[str, Any] = {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
        }
        if closed_only:
            clauses.append("is_closed = 1")
        if source is not None:
            clauses.append("source = :source")
            params["source"] = source
        if start_time is not None:
            clauses.append("open_time >= :start_time")
            params["start_time"] = _naive_utc(start_time)
        if end_time is not None:
            clauses.append("open_time <= :end_time")
            params["end_time"] = _naive_utc(end_time)
        if decision_time is not None:
            clauses.append("close_time <= :decision_time")
            params["decision_time"] = _naive_utc(decision_time)
        sql = self._text(
            f"""
            SELECT open_time, close_time, open, high, low, close, volume,
                   is_closed, source, source_timeframe
            FROM market_candles
            WHERE {' AND '.join(clauses)}
            ORDER BY open_time ASC
            """
        )
        with self._engine.connect() as conn:
            result = conn.execute(sql, params)
            rows = [dict(r._mapping) for r in result]
        empty_cols = [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_time",
            "close_time",
            "is_closed",
            "source",
            "source_timeframe",
        ]
        if not rows:
            return pd.DataFrame(columns=empty_cols)
        frame = pd.DataFrame(rows)
        frame["timestamp"] = pd.to_datetime(frame["open_time"], utc=True)
        frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
        frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
        return frame.reset_index(drop=True)

    def count_candles(self, *, exchange: str, symbol: str, timeframe: str) -> int:
        sql = self._text(
            """
            SELECT COUNT(*) AS n FROM market_candles
            WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
            """
        )
        with self._engine.connect() as conn:
            n = conn.execute(
                sql, {"exchange": exchange, "symbol": symbol, "timeframe": timeframe}
            ).scalar_one()
        return int(n)

    def insert_validation_run(self, row: dict[str, Any]) -> int:
        import json

        sql = self._text(
            """
            INSERT INTO data_validation_runs (
              validation_type, exchange, symbol, timeframe,
              canonical_source, comparison_source, input_path, input_sha256,
              common_start, common_end, row_count, shared_buckets,
              ohlc_mismatches, volume_mismatches, volume_within_tolerance,
              max_open_diff, max_high_diff, max_low_diff, max_close_diff, max_volume_diff,
              deterministic_output_hash, metadata_json
            ) VALUES (
              :validation_type, :exchange, :symbol, :timeframe,
              :canonical_source, :comparison_source, :input_path, :input_sha256,
              :common_start, :common_end, :row_count, :shared_buckets,
              :ohlc_mismatches, :volume_mismatches, :volume_within_tolerance,
              :max_open_diff, :max_high_diff, :max_low_diff, :max_close_diff, :max_volume_diff,
              :deterministic_output_hash, CAST(:metadata_json AS JSON)
            )
            """
        )
        meta = row.get("metadata_json")
        if meta is not None and not isinstance(meta, str):
            meta = json.dumps(meta, sort_keys=True, default=str)
        params = {
            "validation_type": row.get("validation_type"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "canonical_source": row.get("canonical_source"),
            "comparison_source": row.get("comparison_source"),
            "input_path": row.get("input_path"),
            "input_sha256": row.get("input_sha256"),
            "common_start": _naive_utc(row["common_start"]) if row.get("common_start") else None,
            "common_end": _naive_utc(row["common_end"]) if row.get("common_end") else None,
            "row_count": row.get("row_count"),
            "shared_buckets": row.get("shared_buckets"),
            "ohlc_mismatches": row.get("ohlc_mismatches"),
            "volume_mismatches": row.get("volume_mismatches"),
            "volume_within_tolerance": row.get("volume_within_tolerance"),
            "max_open_diff": row.get("max_open_diff"),
            "max_high_diff": row.get("max_high_diff"),
            "max_low_diff": row.get("max_low_diff"),
            "max_close_diff": row.get("max_close_diff"),
            "max_volume_diff": row.get("max_volume_diff"),
            "deterministic_output_hash": row.get("deterministic_output_hash"),
            "metadata_json": meta,
        }
        with self._engine.begin() as conn:
            result = conn.execute(sql, params)
            return int(result.lastrowid or 0)

    def wipe_timeframe(self, *, exchange: str, symbol: str, timeframe: str) -> int:
        sql = self._text(
            """
            DELETE FROM market_candles
            WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
            """
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                sql, {"exchange": exchange, "symbol": symbol, "timeframe": timeframe}
            )
            return int(result.rowcount or 0)
