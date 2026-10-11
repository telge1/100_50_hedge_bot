"""Provenance sidecar planning + in-memory conflict checks (no ClickHouse I/O)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from research_charts.historical_lmv2.provenance_lookup import relative_dest_for

from .provenance_sidecar_design import TABLE_IMPORT_PROVENANCE, provenance_row_from_plan


class ProvenanceSidecarConflictError(Exception):
    """Conflicting or duplicate provenance; batch must not publish."""


@dataclass
class ProvenanceSidecarRow:
    snapshot_id: str
    pipeline_line_id: str
    import_batch_id: str
    relative_json_path: str
    dest_sha256: str
    frozen_input_id: str
    frozen_price_hash: str
    frozen_revision_hash: str
    provenance_lookup_version: str
    catalog_manifest_id: str
    validated_at: str

    def as_ch_dict(self) -> dict[str, str]:
        return {
            "snapshot_id": self.snapshot_id,
            "pipeline_line_id": self.pipeline_line_id,
            "import_batch_id": self.import_batch_id,
            "relative_json_path": self.relative_json_path,
            "dest_sha256": self.dest_sha256,
            "frozen_input_id": self.frozen_input_id,
            "frozen_price_hash": self.frozen_price_hash,
            "frozen_revision_hash": self.frozen_revision_hash,
            "provenance_lookup_version": self.provenance_lookup_version,
            "catalog_manifest_id": self.catalog_manifest_id,
            "validated_at": self.validated_at,
        }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_sidecar_row_from_bundle(
    bundle: Mapping[str, Any],
    *,
    pipeline_line_id: str,
    import_batch_id: str,
    catalog_manifest_id: str,
    provenance_lookup_version: str,
    catalog_root: Path,
    validated_at: datetime | None = None,
) -> ProvenanceSidecarRow:
    """Requires bundle provenance already validated via ``validate_provenance``."""
    path = Path(bundle["path"])
    rel = relative_dest_for(catalog_root, path)
    digest = sha256_file(path)
    plan_prov = {
        "relative_dest": rel,
        "frozen_input_id": str(bundle.get("frozen_input_id") or ""),
        "frozen_price_hash": str(bundle.get("frozen_price_hash") or ""),
        "frozen_revision_hash": str(bundle.get("frozen_revision_hash") or ""),
    }
    row = provenance_row_from_plan(
        plan_prov,
        snapshot_id=str(bundle["snapshot_id"]),
        pipeline_line_id=pipeline_line_id,
        import_batch_id=import_batch_id,
        catalog_manifest_id=catalog_manifest_id,
        provenance_lookup_version=provenance_lookup_version,
        dest_sha256=digest,
    )
    ts = (validated_at or datetime.now(timezone.utc)).isoformat()
    return ProvenanceSidecarRow(validated_at=ts, **row)


# Batch historical import: provenance persisted and read back before publish ledger.
IMPORT_WRITE_ORDER = (
    "validate JSON + validate_provenance (sha256 + frozen id)",
    "staged meta/member",
    "catalog (skip if snapshot_id rows exist; overlap verify)",
    "sidecar write + read-back verify (explicit; not MergeTree ORDER BY alone)",
    "pre-publish integrity (catalog count + member/catalog ids)",
    "publish ledger",
    "batch readback fail_closed",
    "progress JSON (auxiliary; CH authoritative)",
)


@dataclass
class InMemoryProvenanceRegistry:
    """Test double / pre-CH batch gate."""

    rows: list[ProvenanceSidecarRow] = field(default_factory=list)
    by_snapshot_line: dict[tuple[str, str], ProvenanceSidecarRow] = field(default_factory=dict)
    by_path_line: dict[tuple[str, str], str] = field(default_factory=dict)

    def register(self, row: ProvenanceSidecarRow) -> None:
        key = (row.snapshot_id, row.pipeline_line_id)
        existing = self.by_snapshot_line.get(key)
        if existing:
            if (
                existing.dest_sha256 != row.dest_sha256
                or existing.frozen_input_id != row.frozen_input_id
                or existing.relative_json_path != row.relative_json_path
            ):
                raise ProvenanceSidecarConflictError(
                    f"provenance conflict for snapshot_id {row.snapshot_id}"
                )
            return  # identical canonical original provenance (any import_batch_id)
        path_key = (row.relative_json_path, row.pipeline_line_id)
        path_sid = self.by_path_line.get(path_key)
        if path_sid and path_sid != row.snapshot_id:
            raise ProvenanceSidecarConflictError(
                f"path {row.relative_json_path} bound to {path_sid}, not {row.snapshot_id}"
            )
        self.rows.append(row)
        self.by_snapshot_line[key] = row
        self.by_path_line[path_key] = row.snapshot_id

    def require_complete(self, snapshot_ids: Sequence[str], *, pipeline_line_id: str) -> None:
        missing = [
            s
            for s in snapshot_ids
            if (s, pipeline_line_id) not in self.by_snapshot_line
        ]
        if missing:
            raise ProvenanceSidecarConflictError(
                f"missing provenance for {len(missing)} snapshots (e.g. {missing[0]})"
            )


def provenance_lookup_version(catalog_root: Path) -> str:
    lookup = catalog_root / "production" / "provenance_lookup.json"
    if not lookup.is_file():
        return "provenance_lookup_v1:missing"
    digest = hashlib.sha256(lookup.read_bytes()).hexdigest()[:16]
    return f"provenance_lookup_v1:{digest}"
