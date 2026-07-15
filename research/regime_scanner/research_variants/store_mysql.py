"""MySQL store for variant sets and variant-run links."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from research.regime_scanner.mysql_candle_store.config import RegimeDbConfig
from research.regime_scanner.research_variants.schema import VARIANT_SCHEMA_STATEMENTS


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySQLVariantStore:
    def __init__(self, config: RegimeDbConfig) -> None:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("sqlalchemy is required for MySQLVariantStore") from exc
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
            for stmt in VARIANT_SCHEMA_STATEMENTS:
                conn.execute(self._text(stmt.strip()))

    def ensure_variant_set(
        self,
        *,
        variant_set_hash: str,
        name: str,
        description: str,
        variants_json: str,
    ) -> int:
        with self._engine.begin() as conn:
            row = conn.execute(
                self._text(
                    "SELECT id FROM research_variant_sets WHERE variant_set_hash = :h LIMIT 1"
                ),
                {"h": variant_set_hash},
            ).first()
            if row is not None:
                return int(row[0])
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_variant_sets
                      (variant_set_hash, name, description, variants_json)
                    VALUES (:h, :name, :description, CAST(:variants AS JSON))
                    """
                ),
                {
                    "h": variant_set_hash,
                    "name": name,
                    "description": description,
                    "variants": variants_json,
                },
            )
            row = conn.execute(
                self._text(
                    "SELECT id FROM research_variant_sets WHERE variant_set_hash = :h LIMIT 1"
                ),
                {"h": variant_set_hash},
            ).first()
            if row is None:
                raise RuntimeError("variant set insert failed")
            return int(row[0])

    def upsert_variant_run(
        self,
        *,
        variant_set_id: int,
        variant_name: str,
        variant_hash: str,
        run_id: str,
        parameter_hash: str,
        status: str,
        score: float | None,
        rank_position: int | None,
        metadata_json: dict[str, Any],
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_variant_runs (
                      variant_set_id, variant_name, variant_hash, run_id,
                      parameter_hash, status, score, rank_position, metadata_json
                    ) VALUES (
                      :variant_set_id, :variant_name, :variant_hash, :run_id,
                      :parameter_hash, :status, :score, :rank_position,
                      CAST(:metadata_json AS JSON)
                    )
                    ON DUPLICATE KEY UPDATE
                      variant_hash = VALUES(variant_hash),
                      run_id = VALUES(run_id),
                      parameter_hash = VALUES(parameter_hash),
                      status = VALUES(status),
                      score = VALUES(score),
                      rank_position = VALUES(rank_position),
                      metadata_json = VALUES(metadata_json)
                    """
                ),
                {
                    "variant_set_id": int(variant_set_id),
                    "variant_name": variant_name,
                    "variant_hash": variant_hash,
                    "run_id": run_id,
                    "parameter_hash": parameter_hash,
                    "status": status,
                    "score": score,
                    "rank_position": rank_position,
                    "metadata_json": json.dumps(metadata_json or {}, sort_keys=True, default=str),
                },
            )

    def list_variant_runs(self, variant_set_id: int) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(
                    """
                    SELECT * FROM research_variant_runs
                    WHERE variant_set_id = :sid
                    ORDER BY rank_position IS NULL, rank_position, variant_name
                    """
                ),
                {"sid": int(variant_set_id)},
            ).mappings().all()
            return [dict(r) for r in rows]

    def get_variant_set_by_name(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text("SELECT * FROM research_variant_sets WHERE name = :n LIMIT 1"),
                {"n": name},
            ).mappings().first()
            return dict(row) if row else None

    def update_rankings(self, variant_set_id: int, rankings: list[tuple[str, int]]) -> None:
        with self._engine.begin() as conn:
            for variant_name, rank in rankings:
                conn.execute(
                    self._text(
                        """
                        UPDATE research_variant_runs
                        SET rank_position = :rank
                        WHERE variant_set_id = :sid AND variant_name = :name
                        """
                    ),
                    {"sid": int(variant_set_id), "name": variant_name, "rank": int(rank)},
                )

    def append_run_metrics(self, run_id: str, metrics: list[dict[str, Any]]) -> None:
        with self._engine.begin() as conn:
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
                        ON DUPLICATE KEY UPDATE
                          metric_value = VALUES(metric_value),
                          metric_text = VALUES(metric_text),
                          metadata_json = VALUES(metadata_json)
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
