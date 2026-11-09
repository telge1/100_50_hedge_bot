"""
Read-only design sketch — **no DDL executed** (Phase 4A).

Minimal sidecar for historical mixed-frozen forensic traceability:

``research_lmv2_snapshot_import_provenance_v1`` (MergeTree)

Existing CH meta/member/catalog/publish already carry snapshot_id, fingerprints, pipeline_line_id
on publish/catalog rows. The sidecar records **immutable file provenance** and **import batch**
without overloading ReplacingMergeTree meta.
"""

from __future__ import annotations

TABLE_IMPORT_PROVENANCE = "research_lmv2_snapshot_import_provenance_v1"


def sidecar_ddl(database: str) -> str:
    """ClickHouse DDL — document only; do not execute without explicit approval."""
    return f"""
CREATE TABLE IF NOT EXISTS {database}.{TABLE_IMPORT_PROVENANCE} (
    snapshot_id String,
    pipeline_line_id String,
    import_batch_id String,
    relative_json_path String,
    dest_sha256 String,
    frozen_input_id String,
    frozen_price_hash String,
    frozen_revision_hash String,
    provenance_lookup_version String,
    catalog_manifest_id String,
    validated_at DateTime64(3, 'UTC'),
    imported_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = MergeTree()
ORDER BY (snapshot_id, pipeline_line_id, relative_json_path)
SETTINGS index_granularity = 8192
""".strip()

# Note: ``import_batch_id`` is stored for resume forensics but is **not** part of the
# logical uniqueness key; application enforces one canonical original provenance per
# ``(snapshot_id, pipeline_line_id)`` via read-before-write (no ``FINAL`` dedup).


PROVENANCE_CONFLICT_RULES = """
MergeTree ``ORDER BY (snapshot_id, pipeline_line_id, relative_json_path)`` does **not** enforce
uniqueness. Application must **read-before-write** and **read-after-write** (see
``ClickHouseProvenanceSidecarStore`` / ``MemoryProvenanceSidecarStore``).

1. **One logical row per (snapshot_id, pipeline_line_id)** for batch imports; path binding canonical.
2. **Re-import same batch** (idempotent resume): if read-back matches, SKIP INSERT;
   otherwise FAIL (no UPDATE).
3. **Same snapshot_id, new dest_sha256 or frozen_input_id**: FAIL CLOSED — indicates catalog
   tampering or wrong file; do not overwrite.
4. **Same relative path, different snapshot_id**: FAIL CLOSED — path/snapshot binding is canonical.
5. **Pilot overlap**: pilot line already published 10 snapshot_ids; production line import must
   INSERT provenance with production ``pipeline_line_id`` and MUST NOT mutate pilot sidecar rows.
6. Mixed-frozen IDs (**84c224…**, **cfae257…**, **fe5493…**) stored verbatim; no normalization.
"""


def provenance_row_from_plan(
    plan_provenance: dict[str, str],
    *,
    snapshot_id: str,
    pipeline_line_id: str,
    import_batch_id: str,
    catalog_manifest_id: str,
    provenance_lookup_version: str,
    dest_sha256: str = "",
) -> dict[str, str]:
    """Shape for future sidecar INSERT (not wired in Phase 4A)."""
    rel = plan_provenance.get("relative_dest") or plan_provenance.get("source_path") or ""
    return {
        "snapshot_id": snapshot_id,
        "pipeline_line_id": pipeline_line_id,
        "import_batch_id": import_batch_id,
        "relative_json_path": rel,
        "dest_sha256": dest_sha256,
        "frozen_input_id": str(plan_provenance.get("frozen_input_id") or ""),
        "frozen_price_hash": str(plan_provenance.get("frozen_price_hash") or ""),
        "frozen_revision_hash": str(plan_provenance.get("frozen_revision_hash") or ""),
        "provenance_lookup_version": provenance_lookup_version,
        "catalog_manifest_id": catalog_manifest_id,
    }
