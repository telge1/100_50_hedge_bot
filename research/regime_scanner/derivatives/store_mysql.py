"""MySQL target store for curated derivatives 5m tables (regime_scanner_research)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from research.regime_scanner.derivatives.aggregate_5m import BucketRecord
from research.regime_scanner.derivatives.config import RegimeDbConfig
from research.regime_scanner.derivatives.schema import SCHEMA_STATEMENTS
from research.regime_scanner.derivatives.store_memory import UpsertStats


def _naive_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime rejected")
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


class MySQLDerivativeStore:
    """Persist/verify against regime_scanner_research. Additive writes only."""

    def __init__(self, config: RegimeDbConfig) -> None:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("sqlalchemy is required for MySQLDerivativeStore") from exc
        self._text = text
        self._engine = create_engine(
            config.sqlalchemy_url,
            pool_pre_ping=True,
            future=True,
        )

    def close(self) -> None:
        self._engine.dispose()

    def init_schema(self) -> None:
        with self._engine.begin() as conn:
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))

    def upsert_buckets(self, buckets: list[BucketRecord]) -> UpsertStats:
        """Upsert all three fact tables for the given buckets. Hash-gated updates."""
        if not buckets:
            return UpsertStats()
        stats = UpsertStats()
        # Transactional: caller may wrap per-symbol; here one txn for the batch.
        with self._engine.begin() as conn:
            for b in buckets:
                s = self._upsert_one(conn, b)
                stats.inserted += s.inserted
                stats.updated += s.updated
                stats.unchanged += s.unchanged
        return stats

    def upsert_buckets_for_symbol(self, buckets: list[BucketRecord]) -> UpsertStats:
        """Single-transaction upsert for one symbol's buckets."""
        return self.upsert_buckets(buckets)

    def _fetch_hash(
        self,
        conn: Any,
        table: str,
        *,
        symbol: str,
        bucket_start: datetime,
        import_version: str,
    ) -> str | None:
        allowed = {
            "research_open_interest_5m",
            "research_liquidations_5m",
            "research_orderflow_5m",
        }
        if table not in allowed:
            raise ValueError(f"disallowed table: {table}")
        sql = self._text(
            f"""
            SELECT source_hash FROM {table}
            WHERE symbol = :symbol AND bucket_start = :bucket_start
              AND import_version = :import_version
            LIMIT 1
            """
        )
        row = conn.execute(
            sql,
            {
                "symbol": symbol,
                "bucket_start": _naive_utc(bucket_start),
                "import_version": import_version,
            },
        ).first()
        return str(row[0]) if row else None

    def _upsert_one(self, conn: Any, b: BucketRecord) -> UpsertStats:
        stats = UpsertStats()
        existing = self._fetch_hash(
            conn,
            "research_open_interest_5m",
            symbol=b.symbol,
            bucket_start=b.bucket_start,
            import_version=b.import_version,
        )
        if existing is None:
            self._insert_all(conn, b)
            stats.inserted = 1
        elif existing == b.source_hash:
            stats.unchanged = 1
        else:
            self._update_all(conn, b)
            stats.updated = 1
        return stats

    def _insert_all(self, conn: Any, b: BucketRecord) -> None:
        common = {
            "symbol": b.symbol,
            "bucket_start": _naive_utc(b.bucket_start),
            "bucket_end": _naive_utc(b.bucket_end),
            "source_first_timestamp": _naive_utc(b.source_first_timestamp),
            "source_last_timestamp": _naive_utc(b.source_last_timestamp),
            "source_row_count": b.source_row_count,
            "expected_source_rows": b.expected_source_rows,
            "coverage_ratio": b.coverage_ratio,
            "data_available": 1 if b.data_available else 0,
            "sequence_id": b.sequence_id,
            "source_database": b.source_database,
            "source_table": b.source_table,
            "import_version": b.import_version,
            "source_hash": b.source_hash,
        }
        conn.execute(
            self._text(
                """
                INSERT INTO research_open_interest_5m (
                  symbol, bucket_start, bucket_end, open_interest, open_interest_usd,
                  source_first_timestamp, source_last_timestamp, source_row_count,
                  expected_source_rows, coverage_ratio, data_available, gap_before_seconds,
                  sequence_id, source_database, source_table, import_version, source_hash
                ) VALUES (
                  :symbol, :bucket_start, :bucket_end, :open_interest, :open_interest_usd,
                  :source_first_timestamp, :source_last_timestamp, :source_row_count,
                  :expected_source_rows, :coverage_ratio, :data_available, :gap_before_seconds,
                  :sequence_id, :source_database, :source_table, :import_version, :source_hash
                )
                """
            ),
            {
                **common,
                "open_interest": b.open_interest,
                "open_interest_usd": b.open_interest_usd,
                "gap_before_seconds": b.gap_before_seconds,
            },
        )
        conn.execute(
            self._text(
                """
                INSERT INTO research_liquidations_5m (
                  symbol, bucket_start, bucket_end,
                  long_liquidation_usd, short_liquidation_usd, total_liquidation_usd,
                  liquidation_event_count,
                  source_first_timestamp, source_last_timestamp, source_row_count,
                  expected_source_rows, coverage_ratio, data_available,
                  sequence_id, source_database, source_table, import_version, source_hash
                ) VALUES (
                  :symbol, :bucket_start, :bucket_end,
                  :long_liquidation_usd, :short_liquidation_usd, :total_liquidation_usd,
                  :liquidation_event_count,
                  :source_first_timestamp, :source_last_timestamp, :source_row_count,
                  :expected_source_rows, :coverage_ratio, :data_available,
                  :sequence_id, :source_database, :source_table, :import_version, :source_hash
                )
                """
            ),
            {
                **common,
                "long_liquidation_usd": b.long_liquidation_usd,
                "short_liquidation_usd": b.short_liquidation_usd,
                "total_liquidation_usd": b.total_liquidation_usd,
                "liquidation_event_count": b.liquidation_event_count,
            },
        )
        conn.execute(
            self._text(
                """
                INSERT INTO research_orderflow_5m (
                  symbol, bucket_start, bucket_end,
                  buy_volume, sell_volume, total_volume, delta, delta_ratio,
                  spread_mean, spread_max,
                  source_first_timestamp, source_last_timestamp, source_row_count,
                  expected_source_rows, coverage_ratio, data_available,
                  sequence_id, source_database, source_table, import_version, source_hash
                ) VALUES (
                  :symbol, :bucket_start, :bucket_end,
                  :buy_volume, :sell_volume, :total_volume, :delta, :delta_ratio,
                  :spread_mean, :spread_max,
                  :source_first_timestamp, :source_last_timestamp, :source_row_count,
                  :expected_source_rows, :coverage_ratio, :data_available,
                  :sequence_id, :source_database, :source_table, :import_version, :source_hash
                )
                """
            ),
            {
                **common,
                "buy_volume": b.buy_volume,
                "sell_volume": b.sell_volume,
                "total_volume": b.total_volume,
                "delta": b.delta,
                "delta_ratio": b.delta_ratio,
                "spread_mean": b.spread_mean,
                "spread_max": b.spread_max,
            },
        )

    def _update_all(self, conn: Any, b: BucketRecord) -> None:
        common = {
            "symbol": b.symbol,
            "bucket_start": _naive_utc(b.bucket_start),
            "import_version": b.import_version,
            "bucket_end": _naive_utc(b.bucket_end),
            "source_first_timestamp": _naive_utc(b.source_first_timestamp),
            "source_last_timestamp": _naive_utc(b.source_last_timestamp),
            "source_row_count": b.source_row_count,
            "expected_source_rows": b.expected_source_rows,
            "coverage_ratio": b.coverage_ratio,
            "data_available": 1 if b.data_available else 0,
            "sequence_id": b.sequence_id,
            "source_hash": b.source_hash,
        }
        conn.execute(
            self._text(
                """
                UPDATE research_open_interest_5m SET
                  bucket_end=:bucket_end, open_interest=:open_interest,
                  open_interest_usd=:open_interest_usd,
                  source_first_timestamp=:source_first_timestamp,
                  source_last_timestamp=:source_last_timestamp,
                  source_row_count=:source_row_count,
                  expected_source_rows=:expected_source_rows,
                  coverage_ratio=:coverage_ratio, data_available=:data_available,
                  gap_before_seconds=:gap_before_seconds, sequence_id=:sequence_id,
                  source_hash=:source_hash
                WHERE symbol=:symbol AND bucket_start=:bucket_start
                  AND import_version=:import_version
                """
            ),
            {
                **common,
                "open_interest": b.open_interest,
                "open_interest_usd": b.open_interest_usd,
                "gap_before_seconds": b.gap_before_seconds,
            },
        )
        conn.execute(
            self._text(
                """
                UPDATE research_liquidations_5m SET
                  bucket_end=:bucket_end,
                  long_liquidation_usd=:long_liquidation_usd,
                  short_liquidation_usd=:short_liquidation_usd,
                  total_liquidation_usd=:total_liquidation_usd,
                  liquidation_event_count=:liquidation_event_count,
                  source_first_timestamp=:source_first_timestamp,
                  source_last_timestamp=:source_last_timestamp,
                  source_row_count=:source_row_count,
                  expected_source_rows=:expected_source_rows,
                  coverage_ratio=:coverage_ratio, data_available=:data_available,
                  sequence_id=:sequence_id, source_hash=:source_hash
                WHERE symbol=:symbol AND bucket_start=:bucket_start
                  AND import_version=:import_version
                """
            ),
            {
                **common,
                "long_liquidation_usd": b.long_liquidation_usd,
                "short_liquidation_usd": b.short_liquidation_usd,
                "total_liquidation_usd": b.total_liquidation_usd,
                "liquidation_event_count": b.liquidation_event_count,
            },
        )
        conn.execute(
            self._text(
                """
                UPDATE research_orderflow_5m SET
                  bucket_end=:bucket_end,
                  buy_volume=:buy_volume, sell_volume=:sell_volume,
                  total_volume=:total_volume, delta=:delta, delta_ratio=:delta_ratio,
                  spread_mean=:spread_mean, spread_max=:spread_max,
                  source_first_timestamp=:source_first_timestamp,
                  source_last_timestamp=:source_last_timestamp,
                  source_row_count=:source_row_count,
                  expected_source_rows=:expected_source_rows,
                  coverage_ratio=:coverage_ratio, data_available=:data_available,
                  sequence_id=:sequence_id, source_hash=:source_hash
                WHERE symbol=:symbol AND bucket_start=:bucket_start
                  AND import_version=:import_version
                """
            ),
            {
                **common,
                "buy_volume": b.buy_volume,
                "sell_volume": b.sell_volume,
                "total_volume": b.total_volume,
                "delta": b.delta,
                "delta_ratio": b.delta_ratio,
                "spread_mean": b.spread_mean,
                "spread_max": b.spread_max,
            },
        )

    def get_buckets(
        self,
        *,
        symbols: list[str],
        import_version: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        from sqlalchemy import bindparam

        sql = self._text(
            """
            SELECT o.symbol, o.bucket_start, o.bucket_end,
                   o.open_interest, o.open_interest_usd,
                   l.long_liquidation_usd, l.short_liquidation_usd, l.total_liquidation_usd,
                   f.buy_volume, f.sell_volume, f.total_volume, f.delta, f.delta_ratio,
                   f.spread_mean, f.spread_max,
                   o.source_row_count, o.coverage_ratio, o.data_available,
                   o.sequence_id, o.source_hash, o.import_version
            FROM research_open_interest_5m o
            JOIN research_liquidations_5m l
              ON o.symbol=l.symbol AND o.bucket_start=l.bucket_start
             AND o.import_version=l.import_version
            JOIN research_orderflow_5m f
              ON o.symbol=f.symbol AND o.bucket_start=f.bucket_start
             AND o.import_version=f.import_version
            WHERE o.symbol IN :symbols AND o.import_version = :import_version
              AND (:start_ts IS NULL OR o.bucket_start >= :start_ts)
              AND (:end_ts IS NULL OR o.bucket_start < :end_ts)
            ORDER BY o.symbol, o.bucket_start
            """
        ).bindparams(bindparam("symbols", expanding=True))
        with self._engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "symbols": tuple(symbols),
                    "import_version": import_version,
                    "start_ts": _naive_utc(start) if start else None,
                    "end_ts": _naive_utc(end) if end else None,
                },
            ).mappings().all()
            return [dict(r) for r in rows]

    def record_import_run(self, label: str, payload: dict[str, Any]) -> None:
        import json

        sql = self._text(
            """
            INSERT INTO research_derivative_import_runs (
              import_label, import_version, source_database, source_table,
              source_min_timestamp, source_max_timestamp, target_timeframe,
              symbols_requested, symbols_completed, status, dry_run,
              source_query_hash, config_hash,
              rows_read, buckets_generated, rows_inserted, rows_updated,
              rows_unchanged, rows_rejected,
              started_at, finished_at, error_message, metadata_json
            ) VALUES (
              :import_label, :import_version, :source_database, :source_table,
              :source_min_timestamp, :source_max_timestamp, :target_timeframe,
              :symbols_requested, :symbols_completed, :status, :dry_run,
              :source_query_hash, :config_hash,
              :rows_read, :buckets_generated, :rows_inserted, :rows_updated,
              :rows_unchanged, :rows_rejected,
              :started_at, :finished_at, :error_message, :metadata_json
            )
            ON DUPLICATE KEY UPDATE
              status=VALUES(status),
              symbols_completed=VALUES(symbols_completed),
              rows_read=VALUES(rows_read),
              buckets_generated=VALUES(buckets_generated),
              rows_inserted=VALUES(rows_inserted),
              rows_updated=VALUES(rows_updated),
              rows_unchanged=VALUES(rows_unchanged),
              rows_rejected=VALUES(rows_rejected),
              finished_at=VALUES(finished_at),
              error_message=VALUES(error_message),
              metadata_json=VALUES(metadata_json)
            """
        )
        meta = payload.get("metadata_json")
        with self._engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "import_label": label,
                    "import_version": payload["import_version"],
                    "source_database": payload["source_database"],
                    "source_table": payload["source_table"],
                    "source_min_timestamp": payload.get("source_min_timestamp"),
                    "source_max_timestamp": payload.get("source_max_timestamp"),
                    "target_timeframe": payload.get("target_timeframe", "5m"),
                    "symbols_requested": json.dumps(payload["symbols_requested"]),
                    "symbols_completed": json.dumps(payload.get("symbols_completed")),
                    "status": payload["status"],
                    "dry_run": 1 if payload.get("dry_run") else 0,
                    "source_query_hash": payload.get("source_query_hash"),
                    "config_hash": payload.get("config_hash"),
                    "rows_read": payload.get("rows_read", 0),
                    "buckets_generated": payload.get("buckets_generated", 0),
                    "rows_inserted": payload.get("rows_inserted", 0),
                    "rows_updated": payload.get("rows_updated", 0),
                    "rows_unchanged": payload.get("rows_unchanged", 0),
                    "rows_rejected": payload.get("rows_rejected", 0),
                    "started_at": payload.get("started_at"),
                    "finished_at": payload.get("finished_at"),
                    "error_message": payload.get("error_message"),
                    "metadata_json": json.dumps(meta) if meta is not None else None,
                },
            )

    def fetch_ohlcv_bucket_starts(
        self,
        *,
        symbols: list[str],
        start: datetime,
        end: datetime,
        exchange: str = "bybit",
        timeframe: str = "5m",
    ) -> dict[str, set[datetime]]:
        """Read market_candles open_time for join reconciliation (read-only)."""
        from sqlalchemy import bindparam

        sql = self._text(
            """
            SELECT symbol, open_time
            FROM market_candles
            WHERE exchange = :exchange
              AND timeframe = :timeframe
              AND symbol IN :symbols
              AND open_time >= :start_ts
              AND open_time < :end_ts
            """
        ).bindparams(bindparam("symbols", expanding=True))
        out: dict[str, set[datetime]] = {s: set() for s in symbols}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "exchange": exchange,
                    "timeframe": timeframe,
                    "symbols": tuple(symbols),
                    "start_ts": _naive_utc(start),
                    "end_ts": _naive_utc(end),
                },
            ).all()
            for sym, ot in rows:
                ts = ot if isinstance(ot, datetime) else datetime.fromisoformat(str(ot))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                out[str(sym)].add(ts.astimezone(timezone.utc))
        return out
