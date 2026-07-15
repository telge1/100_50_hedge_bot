"""MySQL store for window sets and variant-window run links."""

from __future__ import annotations

import json
from typing import Any


class MySQLWindowStore:
    def __init__(self, variant_store: Any) -> None:
        self._engine = variant_store._engine
        self._text = variant_store._text

    def ensure_window_set(
        self,
        *,
        window_set_hash: str,
        name: str,
        description: str,
        windows_json: str,
    ) -> int:
        with self._engine.begin() as conn:
            row = conn.execute(
                self._text(
                    "SELECT id FROM research_window_sets WHERE window_set_hash = :h LIMIT 1"
                ),
                {"h": window_set_hash},
            ).first()
            if row is not None:
                return int(row[0])
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_window_sets
                      (window_set_hash, name, description, windows_json)
                    VALUES (:h, :name, :description, CAST(:windows AS JSON))
                    """
                ),
                {
                    "h": window_set_hash,
                    "name": name,
                    "description": description,
                    "windows": windows_json,
                },
            )
            row = conn.execute(
                self._text(
                    "SELECT id FROM research_window_sets WHERE window_set_hash = :h LIMIT 1"
                ),
                {"h": window_set_hash},
            ).first()
            if row is None:
                raise RuntimeError("window set insert failed")
            return int(row[0])

    def upsert_variant_window_run(self, **kwargs: Any) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_variant_window_runs (
                      variant_set_id, window_set_id, variant_name, window_name,
                      variant_hash, window_hash, run_id, parameter_hash,
                      status, score, degenerate, metadata_json
                    ) VALUES (
                      :variant_set_id, :window_set_id, :variant_name, :window_name,
                      :variant_hash, :window_hash, :run_id, :parameter_hash,
                      :status, :score, :degenerate, CAST(:metadata_json AS JSON)
                    )
                    ON DUPLICATE KEY UPDATE
                      variant_hash = VALUES(variant_hash),
                      window_hash = VALUES(window_hash),
                      run_id = VALUES(run_id),
                      parameter_hash = VALUES(parameter_hash),
                      status = VALUES(status),
                      score = VALUES(score),
                      degenerate = VALUES(degenerate),
                      metadata_json = VALUES(metadata_json)
                    """
                ),
                {
                    "variant_set_id": int(kwargs["variant_set_id"]),
                    "window_set_id": int(kwargs["window_set_id"]),
                    "variant_name": kwargs["variant_name"],
                    "window_name": kwargs["window_name"],
                    "variant_hash": kwargs["variant_hash"],
                    "window_hash": kwargs["window_hash"],
                    "run_id": kwargs["run_id"],
                    "parameter_hash": kwargs["parameter_hash"],
                    "status": kwargs["status"],
                    "score": kwargs.get("score"),
                    "degenerate": 1 if kwargs.get("degenerate") else 0,
                    "metadata_json": json.dumps(
                        kwargs.get("metadata_json") or {}, sort_keys=True, default=str
                    ),
                },
            )

    def list_variant_window_runs(
        self,
        *,
        variant_set_id: int,
        window_set_id: int,
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(
                    """
                    SELECT * FROM research_variant_window_runs
                    WHERE variant_set_id = :vsid AND window_set_id = :wsid
                    ORDER BY window_name, variant_name
                    """
                ),
                {"vsid": int(variant_set_id), "wsid": int(window_set_id)},
            ).mappings().all()
            return [dict(r) for r in rows]

    def get_window_set_by_name(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text("SELECT * FROM research_window_sets WHERE name = :n LIMIT 1"),
                {"n": name},
            ).mappings().first()
            return dict(row) if row else None

    def get_variant_window_run(
        self,
        *,
        variant_set_id: int,
        window_set_id: int,
        variant_name: str,
        window_name: str,
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    """
                    SELECT * FROM research_variant_window_runs
                    WHERE variant_set_id = :vsid AND window_set_id = :wsid
                      AND variant_name = :variant AND window_name = :window
                    LIMIT 1
                    """
                ),
                {
                    "vsid": int(variant_set_id),
                    "wsid": int(window_set_id),
                    "variant": variant_name,
                    "window": window_name,
                },
            ).mappings().first()
            return dict(row) if row else None
