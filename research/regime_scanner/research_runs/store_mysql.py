"""MySQL-backed research-run store (separate from market_candles)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from research.regime_scanner.mysql_candle_store.config import RegimeDbConfig
from research.regime_scanner.research_runs.parameters import parameters_json
from research.regime_scanner.research_runs.schema import (
    RESEARCH_SCHEMA_STATEMENTS,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
)
from research.regime_scanner.timeframes import ensure_utc_timestamp


def _naive_utc(ts: object | None) -> datetime | None:
    if ts is None:
        return None
    t = ensure_utc_timestamp(ts).to_pydatetime()
    return t.replace(tzinfo=None)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySQLResearchStore:
    def __init__(self, config: RegimeDbConfig) -> None:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("sqlalchemy is required for MySQLResearchStore") from exc
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
            for stmt in RESEARCH_SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))

    def ensure_parameter_set(
        self, *, parameter_hash: str, scanner_name: str, params: Any
    ) -> int:
        blob = parameters_json(params)
        with self._engine.begin() as conn:
            row = conn.execute(
                self._text(
                    "SELECT id FROM research_parameter_sets WHERE parameter_hash = :h LIMIT 1"
                ),
                {"h": parameter_hash},
            ).first()
            if row is not None:
                return int(row[0])
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_parameter_sets
                      (parameter_hash, scanner_name, parameters_json)
                    VALUES (:h, :scanner_name, CAST(:params AS JSON))
                    """
                ),
                {
                    "h": parameter_hash,
                    "scanner_name": scanner_name,
                    "params": blob,
                },
            )
            row = conn.execute(
                self._text(
                    "SELECT id FROM research_parameter_sets WHERE parameter_hash = :h LIMIT 1"
                ),
                {"h": parameter_hash},
            ).first()
            if row is None:
                raise RuntimeError("parameter set insert failed")
            return int(row[0])

    def create_running_run(self, row: dict[str, Any]) -> None:
        sql = self._text(
            """
            INSERT INTO research_runs (
              run_id, run_fingerprint, parameter_set_id, exchange, symbol, data_source,
              start_time, end_time, warmup_start, decision_time, status, started_at,
              git_commit, git_branch, working_tree_dirty,
              candle_hash_5m, candle_hash_15m, candle_hash_30m,
              metadata_json
            ) VALUES (
              :run_id, :run_fingerprint, :parameter_set_id, :exchange, :symbol, :data_source,
              :start_time, :end_time, :warmup_start, :decision_time, :status, :started_at,
              :git_commit, :git_branch, :working_tree_dirty,
              :candle_hash_5m, :candle_hash_15m, :candle_hash_30m,
              CAST(:metadata_json AS JSON)
            )
            """
        )
        payload = self._run_insert_payload(row, status=RUN_STATUS_RUNNING)
        with self._engine.begin() as conn:
            conn.execute(sql, payload)

    def save_completed_run(
        self,
        *,
        run_id: str,
        updates: dict[str, Any],
        trend_states: list[dict[str, Any]],
        structure_events: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
    ) -> None:
        with self._engine.begin() as conn:
            self._insert_child_rows(conn, run_id, trend_states, structure_events, signals, metrics)
            upd = dict(updates)
            upd["run_id"] = run_id
            upd["status"] = RUN_STATUS_COMPLETED
            conn.execute(
                self._text(
                    """
                    UPDATE research_runs SET
                      status = :status,
                      run_fingerprint = :run_fingerprint,
                      finished_at = :finished_at,
                      duration_seconds = :duration_seconds,
                      trend_state_hash = :trend_state_hash,
                      structure_event_hash = :structure_event_hash,
                      price_action_hash = :price_action_hash,
                      momentum_hash = :momentum_hash,
                      signal_hash = :signal_hash,
                      combined_output_hash = :combined_output_hash,
                      candle_hash_5m = :candle_hash_5m,
                      candle_hash_15m = :candle_hash_15m,
                      candle_hash_30m = :candle_hash_30m,
                      metadata_json = CAST(:metadata_json AS JSON)
                    WHERE run_id = :run_id
                    """
                ),
                self._finalize_payload(upd),
            )

    def mark_failed(self, run_id: str, *, error_type: str, error_message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    """
                    UPDATE research_runs SET
                      status = :status,
                      finished_at = :finished_at,
                      error_type = :error_type,
                      error_message = :error_message
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": RUN_STATUS_FAILED,
                    "finished_at": _utcnow_naive(),
                    "error_type": error_type[:128],
                    "error_message": (error_message or "")[:4000],
                },
            )

    def mark_interrupted(self, run_id: str, *, reason: str) -> int:
        """Mark a `running` run as interrupted (never treated as completed). Returns rows affected."""
        with self._engine.begin() as conn:
            result = conn.execute(
                self._text(
                    """
                    UPDATE research_runs SET
                      status = :status,
                      finished_at = :finished_at,
                      error_type = :error_type,
                      error_message = :error_message
                    WHERE run_id = :run_id AND status = :running
                    """
                ),
                {
                    "run_id": run_id,
                    "status": "interrupted",
                    "finished_at": _utcnow_naive(),
                    "error_type": "interrupted",
                    "error_message": (reason or "")[:4000],
                    "running": RUN_STATUS_RUNNING,
                },
            )
            return int(result.rowcount)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    """
                    SELECT r.*, p.parameter_hash AS parameter_hash_value
                    FROM research_runs r
                    LEFT JOIN research_parameter_sets p ON p.id = r.parameter_set_id
                    WHERE r.run_id = :id
                    LIMIT 1
                    """
                ),
                {"id": run_id},
            ).mappings().first()
            return dict(row) if row else None

    def find_run_by_fingerprint(
        self,
        run_fingerprint: str,
        *,
        status: str = "completed",
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    """
                    SELECT r.*, p.parameter_hash AS parameter_hash_value
                    FROM research_runs r
                    LEFT JOIN research_parameter_sets p ON p.id = r.parameter_set_id
                    WHERE r.run_fingerprint = :fp AND r.status = :status
                    ORDER BY r.started_at DESC
                    LIMIT 1
                    """
                ),
                {"fp": run_fingerprint, "status": status},
            ).mappings().first()
            return dict(row) if row else None

    def list_runs(
        self,
        *,
        symbol: str | None = None,
        status: str | None = None,
        parameter_hash: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: dict[str, Any] = {"limit": int(limit)}
        if symbol:
            clauses.append("r.symbol = :symbol")
            params["symbol"] = symbol.upper()
        if status:
            clauses.append("r.status = :status")
            params["status"] = status
        if parameter_hash:
            clauses.append("p.parameter_hash = :parameter_hash")
            params["parameter_hash"] = parameter_hash
        sql = f"""
            SELECT r.*, p.parameter_hash AS parameter_hash_value
            FROM research_runs r
            JOIN research_parameter_sets p ON p.id = r.parameter_set_id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.started_at DESC
            LIMIT :limit
        """
        with self._engine.connect() as conn:
            rows = conn.execute(self._text(sql), params).mappings().all()
            return [dict(r) for r in rows]

    def load_trend_states(self, run_id: str) -> list[dict[str, Any]]:
        return self._load_rows(
            "research_trend_states",
            run_id,
            order_by="timestamp, event_key",
        )

    def load_structure_events(self, run_id: str) -> list[dict[str, Any]]:
        return self._load_rows(
            "research_structure_events",
            run_id,
            order_by="timestamp, event_type, event_key",
        )

    def load_signals(self, run_id: str) -> list[dict[str, Any]]:
        return self._load_rows(
            "research_signals",
            run_id,
            order_by="timestamp, direction, signal_key",
        )

    def count_candles(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(self._text("SELECT COUNT(*) FROM market_candles")).first()
            return int(row[0]) if row else 0

    def count_validation_runs(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(self._text("SELECT COUNT(*) FROM data_validation_runs")).first()
            return int(row[0]) if row else 0

    def _load_rows(self, table: str, run_id: str, *, order_by: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(f"SELECT * FROM {table} WHERE run_id = :id ORDER BY {order_by}"),
                {"id": run_id},
            ).mappings().all()
            return [dict(r) for r in rows]

    def _insert_child_rows(
        self,
        conn: Any,
        run_id: str,
        trend_states: list[dict[str, Any]],
        structure_events: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
    ) -> None:
        for row in trend_states:
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_trend_states (
                      run_id, event_key, timestamp, state, previous_state, direction,
                      strength, transition_reason, confirmation_count,
                      protective_high, protective_low, metadata_json
                    ) VALUES (
                      :run_id, :event_key, :timestamp, :state, :previous_state, :direction,
                      :strength, :transition_reason, :confirmation_count,
                      :protective_high, :protective_low, CAST(:metadata_json AS JSON)
                    )
                    """
                ),
                self._trend_payload(run_id, row),
            )
        for row in structure_events:
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_structure_events (
                      run_id, event_key, timestamp, event_type, direction, price,
                      swing_type, protective_level, structure_state, metadata_json
                    ) VALUES (
                      :run_id, :event_key, :timestamp, :event_type, :direction, :price,
                      :swing_type, :protective_level, :structure_state,
                      CAST(:metadata_json AS JSON)
                    )
                    """
                ),
                self._structure_payload(run_id, row),
            )
        for row in signals:
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_signals (
                      run_id, signal_key, timestamp, direction, signal_type, setup_id,
                      status, entry_time, entry_price, invalidation_time, invalidation_price,
                      reason, metadata_json
                    ) VALUES (
                      :run_id, :signal_key, :timestamp, :direction, :signal_type, :setup_id,
                      :status, :entry_time, :entry_price, :invalidation_time, :invalidation_price,
                      :reason, CAST(:metadata_json AS JSON)
                    )
                    """
                ),
                self._signal_payload(run_id, row),
            )
        for row in metrics:
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_run_metrics (
                      run_id, metric_name, metric_value, metric_text, metadata_json
                    ) VALUES (
                      :run_id, :metric_name, :metric_value, :metric_text,
                      CAST(:metadata_json AS JSON)
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "metric_name": row["metric_name"],
                    "metric_value": row.get("metric_value"),
                    "metric_text": row.get("metric_text"),
                    "metadata_json": json.dumps(row.get("metadata_json") or {}),
                },
            )

    @staticmethod
    def _json_blob(value: Any) -> str:
        return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)

    def _run_insert_payload(self, row: dict[str, Any], *, status: str) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "run_fingerprint": row["run_fingerprint"],
            "parameter_set_id": int(row["parameter_set_id"]),
            "exchange": row["exchange"],
            "symbol": row["symbol"],
            "data_source": row["data_source"],
            "start_time": _naive_utc(row["start_time"]),
            "end_time": _naive_utc(row["end_time"]),
            "warmup_start": _naive_utc(row["warmup_start"]),
            "decision_time": _naive_utc(row.get("decision_time")),
            "status": status,
            "started_at": _naive_utc(row.get("started_at") or _utcnow_naive()),
            "git_commit": row.get("git_commit"),
            "git_branch": row.get("git_branch"),
            "working_tree_dirty": 1 if row.get("working_tree_dirty") else 0,
            "candle_hash_5m": row.get("candle_hash_5m"),
            "candle_hash_15m": row.get("candle_hash_15m"),
            "candle_hash_30m": row.get("candle_hash_30m"),
            "metadata_json": self._json_blob(row.get("metadata_json")),
        }

    def _finalize_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "run_fingerprint": row.get("run_fingerprint"),
            "status": row["status"],
            "finished_at": _naive_utc(row.get("finished_at") or _utcnow_naive()),
            "duration_seconds": row.get("duration_seconds"),
            "trend_state_hash": row.get("trend_state_hash"),
            "structure_event_hash": row.get("structure_event_hash"),
            "price_action_hash": row.get("price_action_hash"),
            "momentum_hash": row.get("momentum_hash"),
            "signal_hash": row.get("signal_hash"),
            "combined_output_hash": row.get("combined_output_hash"),
            "candle_hash_5m": row.get("candle_hash_5m"),
            "candle_hash_15m": row.get("candle_hash_15m"),
            "candle_hash_30m": row.get("candle_hash_30m"),
            "metadata_json": self._json_blob(row.get("metadata_json")),
        }

    def _trend_payload(self, run_id: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "event_key": row["event_key"],
            "timestamp": _naive_utc(row["timestamp"]),
            "state": row["state"],
            "previous_state": row.get("previous_state"),
            "direction": row.get("direction"),
            "strength": row.get("strength"),
            "transition_reason": row.get("transition_reason"),
            "confirmation_count": row.get("confirmation_count"),
            "protective_high": row.get("protective_high"),
            "protective_low": row.get("protective_low"),
            "metadata_json": self._json_blob(row.get("metadata_json")),
        }

    def _structure_payload(self, run_id: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "event_key": row["event_key"],
            "timestamp": _naive_utc(row["timestamp"]),
            "event_type": row["event_type"],
            "direction": row.get("direction"),
            "price": row.get("price"),
            "swing_type": row.get("swing_type"),
            "protective_level": row.get("protective_level"),
            "structure_state": row.get("structure_state"),
            "metadata_json": self._json_blob(row.get("metadata_json")),
        }

    def _signal_payload(self, run_id: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "signal_key": row["signal_key"],
            "timestamp": _naive_utc(row["timestamp"]),
            "direction": row.get("direction"),
            "signal_type": row["signal_type"],
            "setup_id": row.get("setup_id"),
            "status": row.get("status"),
            "entry_time": _naive_utc(row.get("entry_time")),
            "entry_price": row.get("entry_price"),
            "invalidation_time": _naive_utc(row.get("invalidation_time")),
            "invalidation_price": row.get("invalidation_price"),
            "reason": row.get("reason"),
            "metadata_json": self._json_blob(row.get("metadata_json")),
        }
