"""MySQL persistence for C3.5c A6 signals / features / outcomes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from research.regime_scanner.c35c_signal_store.schema import C35C_SIGNAL_SCHEMA_STATEMENTS
from research.regime_scanner.mysql_candle_store.config import RegimeDbConfig
from research.regime_scanner.research_runs.schema import (
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


def _json_blob(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, default=str, sort_keys=True)


class C35cSignalStore:
    """Additive persist layer; reuses research_runs / research_signals."""

    def __init__(self, config: RegimeDbConfig) -> None:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("sqlalchemy is required for C35cSignalStore") from exc
        self._text = text
        self._engine = create_engine(config.sqlalchemy_url, pool_pre_ping=True, future=True)

    def close(self) -> None:
        self._engine.dispose()

    def init_schema(self) -> None:
        """Init base research schema + additive feature/outcome tables."""
        from research.regime_scanner.research_runs.schema import RESEARCH_SCHEMA_STATEMENTS

        with self._engine.begin() as conn:
            for stmt in RESEARCH_SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))
            for stmt in C35C_SIGNAL_SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))

    def find_completed_run_by_label(self, run_label: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    """
                    SELECT r.*, p.parameter_hash AS parameter_hash_value
                    FROM research_runs r
                    LEFT JOIN research_parameter_sets p ON p.id = r.parameter_set_id
                    WHERE r.status = :status
                      AND JSON_UNQUOTE(JSON_EXTRACT(r.metadata_json, '$.run_label')) = :label
                    ORDER BY r.started_at DESC
                    LIMIT 1
                    """
                ),
                {"status": RUN_STATUS_COMPLETED, "label": run_label},
            ).mappings().first()
            return dict(row) if row else None

    def find_run_by_fingerprint(self, run_fingerprint: str) -> dict[str, Any] | None:
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
                {"fp": run_fingerprint, "status": RUN_STATUS_COMPLETED},
            ).mappings().first()
            return dict(row) if row else None

    def ensure_parameter_set(
        self, *, parameter_hash: str, scanner_name: str, params: dict[str, Any]
    ) -> int:
        blob = json.dumps(params, sort_keys=True, default=str)
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
                {"h": parameter_hash, "scanner_name": scanner_name, "params": blob},
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

    def persist_bundle(
        self,
        *,
        run_row: dict[str, Any],
        signals: list[dict[str, Any]],
        features: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
        metrics: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomic persist: run → signals → features → outcomes. Full rollback on error."""
        metrics = metrics or []
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
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
                ),
                self._run_insert_payload(run_row, status=RUN_STATUS_RUNNING),
            )

            signal_id_by_key: dict[str, int] = {}
            for sig in signals:
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
                    self._signal_payload(run_row["run_id"], sig),
                )
                row = conn.execute(
                    self._text(
                        """
                        SELECT id FROM research_signals
                        WHERE run_id = :run_id AND signal_key = :signal_key
                        LIMIT 1
                        """
                    ),
                    {"run_id": run_row["run_id"], "signal_key": sig["signal_key"]},
                ).first()
                if row is None:
                    raise RuntimeError(f"signal insert missing id for {sig['signal_key']}")
                signal_id_by_key[sig["signal_key"]] = int(row[0])

            for feat in features:
                sid = signal_id_by_key[feat["signal_key"]]
                conn.execute(
                    self._text(
                        """
                        INSERT INTO research_signal_features (
                          signal_id, run_id, feature_version, feature_stage, feature_timestamp,
                          ema9, ema20, ema50, ema59, ema200,
                          ema9_slope_3, ema20_slope_3, ema59_slope_3, ema200_slope_3,
                          ema9_20_distance_pct, ema20_59_distance_pct, ema59_200_distance_pct,
                          adx, di_plus, di_minus, di_spread_signed, di_spread_abs, di_spread_dir_norm,
                          atr, atr_pct, dist_ema_atr, move_since_arm_atr, breakout_candle_atr,
                          pullback_depth_atr, dist_breakout_atr, dist_protected_atr,
                          major_direction, structure_state, protected_high, protected_low,
                          breakout_level, pullback_high, pullback_low, lh_confirmed, hl_confirmed,
                          entry_candle_return_pct, entry_candle_body_pct, entry_candle_range_pct,
                          entry_upper_wick_ratio, entry_lower_wick_ratio, entry_close_position,
                          entry_bullish, volume, volume_ratio, hour_utc, day_of_week, month, split,
                          feature_json
                        ) VALUES (
                          :signal_id, :run_id, :feature_version, :feature_stage, :feature_timestamp,
                          :ema9, :ema20, :ema50, :ema59, :ema200,
                          :ema9_slope_3, :ema20_slope_3, :ema59_slope_3, :ema200_slope_3,
                          :ema9_20_distance_pct, :ema20_59_distance_pct, :ema59_200_distance_pct,
                          :adx, :di_plus, :di_minus, :di_spread_signed, :di_spread_abs, :di_spread_dir_norm,
                          :atr, :atr_pct, :dist_ema_atr, :move_since_arm_atr, :breakout_candle_atr,
                          :pullback_depth_atr, :dist_breakout_atr, :dist_protected_atr,
                          :major_direction, :structure_state, :protected_high, :protected_low,
                          :breakout_level, :pullback_high, :pullback_low, :lh_confirmed, :hl_confirmed,
                          :entry_candle_return_pct, :entry_candle_body_pct, :entry_candle_range_pct,
                          :entry_upper_wick_ratio, :entry_lower_wick_ratio, :entry_close_position,
                          :entry_bullish, :volume, :volume_ratio, :hour_utc, :day_of_week, :month, :split,
                          CAST(:feature_json AS JSON)
                        )
                        """
                    ),
                    self._feature_payload(run_row["run_id"], sid, feat),
                )

            for outc in outcomes:
                sid = signal_id_by_key[outc["signal_key"]]
                conn.execute(
                    self._text(
                        """
                        INSERT INTO research_signal_outcomes (
                          signal_id, run_id, outcome_version, exit_model,
                          tp_pct, sl_pct, horizon_bars, cost_pct, same_bar_policy,
                          exit_timestamp, exit_price, exit_reason,
                          gross_pnl_pct, net_pnl_pct, is_winner,
                          tp_first, sl_first, same_bar_ambiguous, time_exit, data_end,
                          bars_held, bars_to_tp, bars_to_sl, mfe_pct, mae_pct,
                          mae_before_tp_pct, reclaimed_after_adverse, max_underwater_bars,
                          outcome_json
                        ) VALUES (
                          :signal_id, :run_id, :outcome_version, :exit_model,
                          :tp_pct, :sl_pct, :horizon_bars, :cost_pct, :same_bar_policy,
                          :exit_timestamp, :exit_price, :exit_reason,
                          :gross_pnl_pct, :net_pnl_pct, :is_winner,
                          :tp_first, :sl_first, :same_bar_ambiguous, :time_exit, :data_end,
                          :bars_held, :bars_to_tp, :bars_to_sl, :mfe_pct, :mae_pct,
                          :mae_before_tp_pct, :reclaimed_after_adverse, :max_underwater_bars,
                          CAST(:outcome_json AS JSON)
                        )
                        """
                    ),
                    self._outcome_payload(run_row["run_id"], sid, outc),
                )

            for m in metrics:
                conn.execute(
                    self._text(
                        """
                        INSERT INTO research_run_metrics
                          (run_id, metric_name, metric_value, metric_text, metadata_json)
                        VALUES
                          (:run_id, :metric_name, :metric_value, :metric_text, CAST(:metadata_json AS JSON))
                        """
                    ),
                    {
                        "run_id": run_row["run_id"],
                        "metric_name": m["metric_name"],
                        "metric_value": m.get("metric_value"),
                        "metric_text": m.get("metric_text"),
                        "metadata_json": _json_blob(m.get("metadata_json")),
                    },
                )

            conn.execute(
                self._text(
                    """
                    UPDATE research_runs SET
                      status = :status,
                      finished_at = :finished_at,
                      duration_seconds = :duration_seconds,
                      signal_hash = :signal_hash,
                      combined_output_hash = :combined_output_hash,
                      candle_hash_5m = :candle_hash_5m,
                      metadata_json = CAST(:metadata_json AS JSON)
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_row["run_id"],
                    "status": RUN_STATUS_COMPLETED,
                    "finished_at": _utcnow_naive(),
                    "duration_seconds": run_row.get("duration_seconds"),
                    "signal_hash": run_row.get("signal_hash"),
                    "combined_output_hash": run_row.get("combined_output_hash"),
                    "candle_hash_5m": run_row.get("candle_hash_5m"),
                    "metadata_json": _json_blob(run_row.get("metadata_json")),
                },
            )

        return {
            "run_id": run_row["run_id"],
            "n_signals": len(signals),
            "n_features": len(features),
            "n_outcomes": len(outcomes),
            "signal_ids": signal_id_by_key,
        }

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

    def load_signals(self, run_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(
                    """
                    SELECT * FROM research_signals
                    WHERE run_id = :id
                    ORDER BY timestamp, direction, signal_key
                    """
                ),
                {"id": run_id},
            ).mappings().all()
            return [dict(r) for r in rows]

    def load_features(self, run_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(
                    """
                    SELECT * FROM research_signal_features
                    WHERE run_id = :id
                    ORDER BY signal_id, feature_stage
                    """
                ),
                {"id": run_id},
            ).mappings().all()
            return [dict(r) for r in rows]

    def load_outcomes(self, run_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(
                    """
                    SELECT * FROM research_signal_outcomes
                    WHERE run_id = :id
                    ORDER BY signal_id
                    """
                ),
                {"id": run_id},
            ).mappings().all()
            return [dict(r) for r in rows]

    def count_table(self, table: str) -> int:
        allowed = {
            "research_runs",
            "research_signals",
            "research_signal_features",
            "research_signal_outcomes",
        }
        if table not in allowed:
            raise ValueError(f"disallowed table count: {table}")
        with self._engine.connect() as conn:
            row = conn.execute(self._text(f"SELECT COUNT(*) FROM {table}")).first()
            return int(row[0]) if row else 0

    def _run_insert_payload(self, row: dict[str, Any], *, status: str) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "run_fingerprint": row["run_fingerprint"],
            "parameter_set_id": int(row["parameter_set_id"]),
            "exchange": row.get("exchange") or "bybit",
            "symbol": str(row["symbol"]).upper(),
            "data_source": row.get("data_source") or "mysql",
            "start_time": _naive_utc(row["start_time"]),
            "end_time": _naive_utc(row["end_time"]),
            "warmup_start": _naive_utc(row["warmup_start"]),
            "decision_time": _naive_utc(row.get("decision_time")),
            "status": status,
            "started_at": _naive_utc(row.get("started_at")) or _utcnow_naive(),
            "git_commit": row.get("git_commit"),
            "git_branch": row.get("git_branch"),
            "working_tree_dirty": 1 if row.get("working_tree_dirty") else 0,
            "candle_hash_5m": row.get("candle_hash_5m"),
            "candle_hash_15m": row.get("candle_hash_15m"),
            "candle_hash_30m": row.get("candle_hash_30m"),
            "metadata_json": _json_blob(row.get("metadata_json")),
        }

    def _signal_payload(self, run_id: str, sig: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "signal_key": sig["signal_key"],
            "timestamp": _naive_utc(sig["timestamp"]),
            "direction": sig.get("direction"),
            "signal_type": sig.get("signal_type"),
            "setup_id": None if sig.get("setup_id") is None else str(sig["setup_id"]),
            "status": sig.get("status") or "filled",
            "entry_time": _naive_utc(sig.get("entry_time")),
            "entry_price": sig.get("entry_price"),
            "invalidation_time": _naive_utc(sig.get("invalidation_time")),
            "invalidation_price": sig.get("invalidation_price"),
            "reason": sig.get("reason"),
            "metadata_json": _json_blob(sig.get("metadata_json")),
        }

    def _feature_payload(self, run_id: str, signal_id: int, feat: dict[str, Any]) -> dict[str, Any]:
        cols = [
            "feature_version",
            "feature_stage",
            "ema9",
            "ema20",
            "ema50",
            "ema59",
            "ema200",
            "ema9_slope_3",
            "ema20_slope_3",
            "ema59_slope_3",
            "ema200_slope_3",
            "ema9_20_distance_pct",
            "ema20_59_distance_pct",
            "ema59_200_distance_pct",
            "adx",
            "di_plus",
            "di_minus",
            "di_spread_signed",
            "di_spread_abs",
            "di_spread_dir_norm",
            "atr",
            "atr_pct",
            "dist_ema_atr",
            "move_since_arm_atr",
            "breakout_candle_atr",
            "pullback_depth_atr",
            "dist_breakout_atr",
            "dist_protected_atr",
            "major_direction",
            "structure_state",
            "protected_high",
            "protected_low",
            "breakout_level",
            "pullback_high",
            "pullback_low",
            "lh_confirmed",
            "hl_confirmed",
            "entry_candle_return_pct",
            "entry_candle_body_pct",
            "entry_candle_range_pct",
            "entry_upper_wick_ratio",
            "entry_lower_wick_ratio",
            "entry_close_position",
            "entry_bullish",
            "volume",
            "volume_ratio",
            "hour_utc",
            "day_of_week",
            "month",
            "split",
        ]
        out = {"signal_id": signal_id, "run_id": run_id, "feature_timestamp": _naive_utc(feat["feature_timestamp"])}
        for c in cols:
            out[c] = feat.get(c)
        out["feature_json"] = _json_blob(feat.get("feature_json"))
        return out

    def _outcome_payload(self, run_id: str, signal_id: int, outc: dict[str, Any]) -> dict[str, Any]:
        return {
            "signal_id": signal_id,
            "run_id": run_id,
            "outcome_version": outc["outcome_version"],
            "exit_model": outc["exit_model"],
            "tp_pct": float(outc["tp_pct"]),
            "sl_pct": float(outc["sl_pct"]),
            "horizon_bars": int(outc["horizon_bars"]),
            "cost_pct": float(outc["cost_pct"]),
            "same_bar_policy": outc.get("same_bar_policy") or "conservative_sl",
            "exit_timestamp": _naive_utc(outc.get("exit_timestamp")),
            "exit_price": outc.get("exit_price"),
            "exit_reason": outc.get("exit_reason"),
            "gross_pnl_pct": outc.get("gross_pnl_pct"),
            "net_pnl_pct": outc.get("net_pnl_pct"),
            "is_winner": None if outc.get("is_winner") is None else (1 if outc["is_winner"] else 0),
            "tp_first": None if outc.get("tp_first") is None else (1 if outc["tp_first"] else 0),
            "sl_first": None if outc.get("sl_first") is None else (1 if outc["sl_first"] else 0),
            "same_bar_ambiguous": None
            if outc.get("same_bar_ambiguous") is None
            else (1 if outc["same_bar_ambiguous"] else 0),
            "time_exit": None if outc.get("time_exit") is None else (1 if outc["time_exit"] else 0),
            "data_end": None if outc.get("data_end") is None else (1 if outc["data_end"] else 0),
            "bars_held": outc.get("bars_held"),
            "bars_to_tp": outc.get("bars_to_tp"),
            "bars_to_sl": outc.get("bars_to_sl"),
            "mfe_pct": outc.get("mfe_pct"),
            "mae_pct": outc.get("mae_pct"),
            "mae_before_tp_pct": outc.get("mae_before_tp_pct"),
            "reclaimed_after_adverse": None
            if outc.get("reclaimed_after_adverse") is None
            else (1 if outc["reclaimed_after_adverse"] else 0),
            "max_underwater_bars": outc.get("max_underwater_bars"),
            "outcome_json": _json_blob(outc.get("outcome_json")),
        }
