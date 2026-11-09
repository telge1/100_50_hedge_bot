"""Provenance sidecar persistence (read-before-write; publish gated on verified row)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Literal, Protocol, Sequence

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from .provenance_identity import (
    assert_canonical_original_match,
    check_path_binding,
    evaluate_physical_rows,
)
from .provenance_sidecar import (
    InMemoryProvenanceRegistry,
    ProvenanceSidecarConflictError,
    ProvenanceSidecarRow,
)
from .provenance_sidecar_design import TABLE_IMPORT_PROVENANCE

WriteResult = Literal["written", "skipped_identical"]

_SIDECAR_WRITE_ENV = "HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE"

_SELECT_COLS = """
    snapshot_id,
    pipeline_line_id,
    import_batch_id,
    relative_json_path,
    dest_sha256,
    frozen_input_id,
    frozen_price_hash,
    frozen_revision_hash,
    provenance_lookup_version,
    catalog_manifest_id,
    validated_at
"""


def _validated_at_to_str(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def row_from_ch_mapping(row: dict[str, Any]) -> ProvenanceSidecarRow:
    return ProvenanceSidecarRow(
        snapshot_id=str(row["snapshot_id"]),
        pipeline_line_id=str(row["pipeline_line_id"]),
        import_batch_id=str(row["import_batch_id"]),
        relative_json_path=str(row["relative_json_path"]),
        dest_sha256=str(row["dest_sha256"]),
        frozen_input_id=str(row["frozen_input_id"]),
        frozen_price_hash=str(row.get("frozen_price_hash") or ""),
        frozen_revision_hash=str(row.get("frozen_revision_hash") or ""),
        provenance_lookup_version=str(row["provenance_lookup_version"]),
        catalog_manifest_id=str(row["catalog_manifest_id"]),
        validated_at=_validated_at_to_str(row["validated_at"]),
    )


def row_from_ch_tuple(t: Sequence[Any]) -> ProvenanceSidecarRow:
    return ProvenanceSidecarRow(
        snapshot_id=str(t[0]),
        pipeline_line_id=str(t[1]),
        import_batch_id=str(t[2]),
        relative_json_path=str(t[3]),
        dest_sha256=str(t[4]),
        frozen_input_id=str(t[5]),
        frozen_price_hash=str(t[6] or ""),
        frozen_revision_hash=str(t[7] or ""),
        provenance_lookup_version=str(t[8]),
        catalog_manifest_id=str(t[9]),
        validated_at=_validated_at_to_str(t[10]),
    )


def evaluate_physical_duplicates(rows: Sequence[ProvenanceSidecarRow]) -> str | None:
    if len(rows) <= 1:
        return None
    first = rows[0]
    try:
        evaluate_physical_rows(rows, snapshot_id=first.snapshot_id, pipeline_line_id=first.pipeline_line_id)
    except ProvenanceSidecarConflictError as exc:
        return str(exc)
    return None


class ProvenanceSidecarStore(Protocol):
    def get(self, snapshot_id: str, pipeline_line_id: str) -> ProvenanceSidecarRow | None: ...

    def read(self, snapshot_id: str, pipeline_line_id: str) -> ProvenanceSidecarRow | None: ...

    def list_physical_rows(
        self, snapshot_id: str, pipeline_line_id: str
    ) -> list[ProvenanceSidecarRow]: ...

    def write_and_verify(self, row: ProvenanceSidecarRow) -> WriteResult: ...

    def require_before_publish(self, row: ProvenanceSidecarRow) -> None: ...


class MemoryProvenanceSidecarStore:
    """In-memory store for tests and dry-run wiring (no ClickHouse)."""

    def __init__(self) -> None:
        self._registry = InMemoryProvenanceRegistry()
        self._physical: list[ProvenanceSidecarRow] = []

    def get(self, snapshot_id: str, pipeline_line_id: str) -> ProvenanceSidecarRow | None:
        return self.read(snapshot_id, pipeline_line_id)

    def read(self, snapshot_id: str, pipeline_line_id: str) -> ProvenanceSidecarRow | None:
        physical = self.list_physical_rows(snapshot_id, pipeline_line_id)
        ev = evaluate_physical_rows(physical, snapshot_id=snapshot_id, pipeline_line_id=pipeline_line_id)
        return ev.logical

    def list_physical_rows(
        self, snapshot_id: str, pipeline_line_id: str
    ) -> list[ProvenanceSidecarRow]:
        return [
            r
            for r in self._physical
            if r.snapshot_id == snapshot_id and r.pipeline_line_id == pipeline_line_id
        ]

    def evaluate_physical_duplicates(self, rows: Sequence[ProvenanceSidecarRow]) -> str | None:
        return evaluate_physical_duplicates(rows)

    def write_and_verify(self, row: ProvenanceSidecarRow) -> WriteResult:
        path_rows = [
            r for r in self._physical if r.relative_json_path == row.relative_json_path
        ]
        check_path_binding(path_rows, row)
        prior = self.read(row.snapshot_id, row.pipeline_line_id)
        if prior is not None:
            assert_canonical_original_match(prior, row)
            return "skipped_identical"
        self._registry.register(row)
        self._physical.append(row)
        stored = self.read(row.snapshot_id, row.pipeline_line_id)
        if stored is None:
            raise ProvenanceSidecarConflictError(
                f"sidecar read-back missing for {row.snapshot_id}"
            )
        assert_canonical_original_match(stored, row)
        return "written"

    def require_before_publish(self, row: ProvenanceSidecarRow) -> None:
        stored = self.read(row.snapshot_id, row.pipeline_line_id)
        if stored is None:
            raise ProvenanceSidecarConflictError(
                f"sidecar required before publish for {row.snapshot_id}"
            )
        assert_canonical_original_match(stored, row)


class ClickHouseProvenanceSidecarStore:
    """
    ClickHouse-backed sidecar (``research_lmv2_snapshot_import_provenance_v1``).

    Reads are always allowed (read-only queries). INSERT requires
    ``HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`` (in addition to batch write gate).
    """

    def __init__(self, client: Any | None = None, database: str | None = None) -> None:
        self._client = client
        self._database = database

    def _connect(self) -> tuple[Any, str]:
        if self._client is not None:
            db = self._database or load_clickhouse_config().database
            return self._client, db
        ch_cfg = load_clickhouse_config()
        return clickhouse_connect.get_client(**ch_cfg.connect_kwargs()), ch_cfg.database

    def _close_if_owned(self, client: Any) -> None:
        if self._client is None and client is not None:
            client.close()

    def get(self, snapshot_id: str, pipeline_line_id: str) -> ProvenanceSidecarRow | None:
        return self.read(snapshot_id, pipeline_line_id)

    def list_physical_rows(
        self, snapshot_id: str, pipeline_line_id: str
    ) -> list[ProvenanceSidecarRow]:
        client, db = self._connect()
        try:
            q = f"""
            SELECT {_SELECT_COLS}
            FROM {db}.{TABLE_IMPORT_PROVENANCE}
            WHERE snapshot_id = {{sid:String}}
              AND pipeline_line_id = {{line:String}}
            """
            result = client.query(
                q,
                parameters={"sid": snapshot_id, "line": pipeline_line_id},
            )
            return [row_from_ch_tuple(r) for r in result.result_rows]
        finally:
            self._close_if_owned(client)

    def evaluate_physical_duplicates(self, rows: Sequence[ProvenanceSidecarRow]) -> str | None:
        return evaluate_physical_duplicates(rows)

    def read(self, snapshot_id: str, pipeline_line_id: str) -> ProvenanceSidecarRow | None:
        physical = self.list_physical_rows(snapshot_id, pipeline_line_id)
        ev = evaluate_physical_rows(physical, snapshot_id=snapshot_id, pipeline_line_id=pipeline_line_id)
        return ev.logical

    def _fetch_path_collisions(self, client: Any, db: str, row: ProvenanceSidecarRow) -> list[ProvenanceSidecarRow]:
        q = f"""
        SELECT {_SELECT_COLS}
        FROM {db}.{TABLE_IMPORT_PROVENANCE}
        WHERE relative_json_path = {{path:String}}
          AND pipeline_line_id = {{line:String}}
        """
        result = client.query(
            q,
            parameters={"path": row.relative_json_path, "line": row.pipeline_line_id},
        )
        return [row_from_ch_tuple(r) for r in result.result_rows]

    def write_and_verify(self, row: ProvenanceSidecarRow) -> WriteResult:
        if os.environ.get(_SIDECAR_WRITE_ENV) != "1":
            raise ProvenanceSidecarConflictError(
                "sidecar CH write blocked: DDL not deployed or "
                f"{_SIDECAR_WRITE_ENV} not set"
            )
        client, db = self._connect()
        try:
            path_hits = self._fetch_path_collisions(client, db, row)
            check_path_binding(path_hits, row)
            prior = self.read(row.snapshot_id, row.pipeline_line_id)
            if prior is not None:
                assert_canonical_original_match(prior, row)
                return "skipped_identical"
            client.insert(
                f"{db}.{TABLE_IMPORT_PROVENANCE}",
                [
                    (
                        row.snapshot_id,
                        row.pipeline_line_id,
                        row.import_batch_id,
                        row.relative_json_path,
                        row.dest_sha256,
                        row.frozen_input_id,
                        row.frozen_price_hash,
                        row.frozen_revision_hash,
                        row.provenance_lookup_version,
                        row.catalog_manifest_id,
                        row.validated_at,
                    )
                ],
                column_names=[
                    "snapshot_id",
                    "pipeline_line_id",
                    "import_batch_id",
                    "relative_json_path",
                    "dest_sha256",
                    "frozen_input_id",
                    "frozen_price_hash",
                    "frozen_revision_hash",
                    "provenance_lookup_version",
                    "catalog_manifest_id",
                    "validated_at",
                ],
            )
            stored = self.read(row.snapshot_id, row.pipeline_line_id)
            if stored is None:
                raise ProvenanceSidecarConflictError(
                    f"sidecar read-back missing for {row.snapshot_id}"
                )
            assert_canonical_original_match(stored, row)
            return "written"
        finally:
            self._close_if_owned(client)

    def require_before_publish(self, row: ProvenanceSidecarRow) -> None:
        stored = self.read(row.snapshot_id, row.pipeline_line_id)
        if stored is None:
            raise ProvenanceSidecarConflictError(
                f"sidecar missing in CH for {row.snapshot_id} before publish"
            )
        assert_canonical_original_match(stored, row)


def default_sidecar_store_for_batch() -> ProvenanceSidecarStore:
    """Production batch imports use CH store once DDL + env gate exist."""
    return ClickHouseProvenanceSidecarStore()
