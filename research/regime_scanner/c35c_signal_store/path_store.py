"""MySQL persistence for post-entry path checkpoints / labels."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from research.regime_scanner.c35c_signal_store.path_schema import C35C_PATH_SCHEMA_STATEMENTS
from research.regime_scanner.c35c_signal_store.schema import C35C_SIGNAL_SCHEMA_STATEMENTS
from research.regime_scanner.mysql_candle_store.config import RegimeDbConfig
from research.regime_scanner.research_runs.schema import RESEARCH_SCHEMA_STATEMENTS
from research.regime_scanner.timeframes import ensure_utc_timestamp


def _naive_utc(ts: object | None) -> datetime | None:
    if ts is None:
        return None
    t = ensure_utc_timestamp(ts).to_pydatetime()
    return t.replace(tzinfo=None)


def _json_blob(obj: Any) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, default=str, sort_keys=True)


class C35cPathStore:
    def __init__(self, config: RegimeDbConfig) -> None:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("sqlalchemy is required for C35cPathStore") from exc
        self._text = text
        self._engine = create_engine(config.sqlalchemy_url, pool_pre_ping=True, future=True)

    def close(self) -> None:
        self._engine.dispose()

    def init_schema(self) -> None:
        with self._engine.begin() as conn:
            for stmt in RESEARCH_SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))
            for stmt in C35C_SIGNAL_SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))
            for stmt in C35C_PATH_SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))

    def find_child_runs(self, parent_run_label: str) -> list[dict[str, Any]]:
        """Child runs: metadata.run_label like '{parent}__SYMBOL'."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(
                    """
                    SELECT r.*
                    FROM research_runs r
                    WHERE r.status = 'completed'
                      AND JSON_UNQUOTE(JSON_EXTRACT(r.metadata_json, '$.run_label'))
                          LIKE :pat
                    ORDER BY r.symbol
                    """
                ),
                {"pat": f"{parent_run_label}__%"},
            ).mappings().all()
            return [dict(r) for r in rows]

    def count_path_checkpoints(self, *, run_id: str, path_version: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    """
                    SELECT COUNT(*) AS n
                    FROM research_signal_path_checkpoints
                    WHERE run_id = :run_id AND path_version = :pv
                    """
                ),
                {"run_id": run_id, "pv": path_version},
            ).first()
            return int(row[0]) if row else 0

    def count_path_labels(self, *, run_id: str, path_version: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    """
                    SELECT COUNT(*) AS n
                    FROM research_signal_path_labels
                    WHERE run_id = :run_id AND path_version = :pv
                    """
                ),
                {"run_id": run_id, "pv": path_version},
            ).first()
            return int(row[0]) if row else 0

    def load_signals_bundle(
        self,
        run_id: str,
        *,
        outcome_version: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        """signals, outcomes_by_sid, trigger_feat_by_sid, fill_feat_by_sid."""
        with self._engine.connect() as conn:
            sigs = [
                dict(r)
                for r in conn.execute(
                    self._text(
                        """
                        SELECT * FROM research_signals
                        WHERE run_id = :run_id
                        ORDER BY id
                        """
                    ),
                    {"run_id": run_id},
                ).mappings().all()
            ]
            for s in sigs:
                meta = s.get("metadata_json")
                if isinstance(meta, (bytes, bytearray)):
                    meta = meta.decode()
                if isinstance(meta, str):
                    try:
                        s["metadata_json"] = json.loads(meta)
                    except Exception:  # noqa: BLE001
                        s["metadata_json"] = {}
            out_sql = """
                SELECT * FROM research_signal_outcomes
                WHERE run_id = :run_id
            """
            params: dict[str, Any] = {"run_id": run_id}
            if outcome_version:
                out_sql += " AND outcome_version = :ov"
                params["ov"] = outcome_version
            outcomes = {
                int(r["signal_id"]): dict(r)
                for r in conn.execute(self._text(out_sql), params).mappings().all()
            }
            feats = [
                dict(r)
                for r in conn.execute(
                    self._text(
                        """
                        SELECT * FROM research_signal_features
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": run_id},
                ).mappings().all()
            ]
        trig: dict[int, dict[str, Any]] = {}
        fill: dict[int, dict[str, Any]] = {}
        for f in feats:
            sid = int(f["signal_id"])
            stage = str(f.get("feature_stage") or "")
            if stage == "trigger":
                trig[sid] = f
            elif stage == "fill":
                fill[sid] = f
        return sigs, outcomes, trig, fill

    def persist_path_bundle(
        self,
        *,
        checkpoints: list[dict[str, Any]],
        labels: list[dict[str, Any]],
        fail_if_existing: bool = True,
    ) -> dict[str, Any]:
        """Transactional insert; unique-key collision → already_exists when fail_if_existing."""
        if not checkpoints and not labels:
            return {"status": "empty", "n_checkpoints": 0, "n_labels": 0}

        run_ids = {str(r["run_id"]) for r in checkpoints + labels}
        path_versions = {str(r["path_version"]) for r in checkpoints + labels}
        if len(run_ids) != 1 or len(path_versions) != 1:
            raise ValueError("persist_path_bundle expects single run_id and path_version")
        run_id = next(iter(run_ids))
        path_version = next(iter(path_versions))

        existing_cp = self.count_path_checkpoints(run_id=run_id, path_version=path_version)
        existing_lb = self.count_path_labels(run_id=run_id, path_version=path_version)
        if existing_cp or existing_lb:
            if fail_if_existing:
                return {
                    "status": "already_exists",
                    "n_checkpoints": existing_cp,
                    "n_labels": existing_lb,
                    "run_id": run_id,
                    "path_version": path_version,
                }
            raise RuntimeError(
                f"path data already exists for run={run_id} version={path_version} "
                f"(cp={existing_cp}, labels={existing_lb})"
            )

        with self._engine.begin() as conn:
            for row in checkpoints:
                conn.execute(
                    self._text(
                        """
                        INSERT INTO research_signal_path_checkpoints (
                          signal_id, run_id, path_version, checkpoint_bar,
                          checkpoint_timestamp, checkpoint_close, bars_since_fill,
                          close_return_pct, directional_close_return_pct,
                          mfe_so_far_pct, mae_so_far_pct, mfe_so_far_atr, mae_so_far_atr,
                          max_high_so_far, min_low_so_far,
                          entry_reclaimed, entry_lost, breakout_level_lost,
                          breakout_level_reclaimed, protected_level_broken,
                          ema9, ema20, ema9_20_aligned, ema9_20_lost,
                          adx, di_plus, di_minus, directional_di_spread,
                          micro_counter_bos, micro_counter_choch, major_structure_opposed,
                          checkpoint_candle_direction, checkpoint_body_atr, checkpoint_range_atr,
                          close_location_in_range, adverse_candle_count, favorable_candle_count,
                          direction_change_count, no_positive_mfe, small_mfe, deep_mae,
                          availability, feature_json
                        ) VALUES (
                          :signal_id, :run_id, :path_version, :checkpoint_bar,
                          :checkpoint_timestamp, :checkpoint_close, :bars_since_fill,
                          :close_return_pct, :directional_close_return_pct,
                          :mfe_so_far_pct, :mae_so_far_pct, :mfe_so_far_atr, :mae_so_far_atr,
                          :max_high_so_far, :min_low_so_far,
                          :entry_reclaimed, :entry_lost, :breakout_level_lost,
                          :breakout_level_reclaimed, :protected_level_broken,
                          :ema9, :ema20, :ema9_20_aligned, :ema9_20_lost,
                          :adx, :di_plus, :di_minus, :directional_di_spread,
                          :micro_counter_bos, :micro_counter_choch, :major_structure_opposed,
                          :checkpoint_candle_direction, :checkpoint_body_atr, :checkpoint_range_atr,
                          :close_location_in_range, :adverse_candle_count, :favorable_candle_count,
                          :direction_change_count, :no_positive_mfe, :small_mfe, :deep_mae,
                          :availability, CAST(:feature_json AS JSON)
                        )
                        """
                    ),
                    self._checkpoint_payload(row),
                )
            for row in labels:
                conn.execute(
                    self._text(
                        """
                        INSERT INTO research_signal_path_labels (
                          signal_id, run_id, path_version, path_type,
                          path_thresholds_json, label_json
                        ) VALUES (
                          :signal_id, :run_id, :path_version, :path_type,
                          CAST(:path_thresholds_json AS JSON), CAST(:label_json AS JSON)
                        )
                        """
                    ),
                    self._label_payload(row),
                )

        return {
            "status": "persisted",
            "n_checkpoints": len(checkpoints),
            "n_labels": len(labels),
            "run_id": run_id,
            "path_version": path_version,
        }

    def load_checkpoints(
        self,
        *,
        path_version: str,
        run_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT * FROM research_signal_path_checkpoints
            WHERE path_version = :pv
        """
        params: dict[str, Any] = {"pv": path_version}
        if run_ids:
            placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
            sql += f" AND run_id IN ({placeholders})"
            for i, rid in enumerate(run_ids):
                params[f"r{i}"] = rid
        with self._engine.connect() as conn:
            return [dict(r) for r in conn.execute(self._text(sql), params).mappings().all()]

    def load_labels(
        self,
        *,
        path_version: str,
        run_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT * FROM research_signal_path_labels
            WHERE path_version = :pv
        """
        params: dict[str, Any] = {"pv": path_version}
        if run_ids:
            placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
            sql += f" AND run_id IN ({placeholders})"
            for i, rid in enumerate(run_ids):
                params[f"r{i}"] = rid
        with self._engine.connect() as conn:
            return [dict(r) for r in conn.execute(self._text(sql), params).mappings().all()]

    def _checkpoint_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "signal_id",
            "run_id",
            "path_version",
            "checkpoint_bar",
            "checkpoint_close",
            "bars_since_fill",
            "close_return_pct",
            "directional_close_return_pct",
            "mfe_so_far_pct",
            "mae_so_far_pct",
            "mfe_so_far_atr",
            "mae_so_far_atr",
            "max_high_so_far",
            "min_low_so_far",
            "entry_reclaimed",
            "entry_lost",
            "breakout_level_lost",
            "breakout_level_reclaimed",
            "protected_level_broken",
            "ema9",
            "ema20",
            "ema9_20_aligned",
            "ema9_20_lost",
            "adx",
            "di_plus",
            "di_minus",
            "directional_di_spread",
            "micro_counter_bos",
            "micro_counter_choch",
            "major_structure_opposed",
            "checkpoint_candle_direction",
            "checkpoint_body_atr",
            "checkpoint_range_atr",
            "close_location_in_range",
            "adverse_candle_count",
            "favorable_candle_count",
            "direction_change_count",
            "no_positive_mfe",
            "small_mfe",
            "deep_mae",
            "availability",
        ]
        out = {k: row.get(k) for k in keys}
        out["checkpoint_timestamp"] = _naive_utc(row.get("checkpoint_timestamp"))
        out["feature_json"] = _json_blob(row.get("feature_json"))
        out["availability"] = row.get("availability") or "ok"
        return out

    def _label_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "signal_id": int(row["signal_id"]),
            "run_id": str(row["run_id"]),
            "path_version": str(row["path_version"]),
            "path_type": str(row["path_type"]),
            "path_thresholds_json": _json_blob(row.get("path_thresholds_json")),
            "label_json": _json_blob(row.get("label_json")),
        }
