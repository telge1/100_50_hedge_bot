"""Historical close import: staged → catalog → [sidecar] → publish."""

from __future__ import annotations

from typing import Any, Sequence

from ..active_snapshot.faults import FaultInjector
from ..active_snapshot.publish_guard import LineIdentity
from ..active_snapshot.publisher import commit_publish_ledger, resolve_existing_publish
from ..active_snapshot.staged_writer import write_active_snapshot_staged
from ..active_snapshot.writer import snapshot_id_for_state
from ...clickhouse_config import load_clickhouse_config
from ..active_snapshot.pool_catalog_schema import TABLE_ACTIVE_POOL
from .catalog_integrity import (
    validate_catalog_rows_in_memory,
    verify_ch_catalog_identity,
)
from .catalog_write import (
    CatalogIncompleteError,
    catalog_row_count,
    write_active_pool_catalog_from_json_rows,
)
from .pilot_overlap import verify_ch_catalog_matches_import_bundle
from .provenance_sidecar import ProvenanceSidecarRow
from .provenance_sidecar_store import ProvenanceSidecarStore
from .write_guards import require_valid_test_run_uuid
from ..active_snapshot.extract import ActiveStateSnapshot


def import_historical_close(
    state: ActiveStateSnapshot,
    catalog_rows: Sequence[dict[str, Any]],
    *,
    pipeline_line_id: str,
    test_run_id: str,
    line: LineIdentity,
    faults: FaultInjector | None = None,
    sidecar_store: ProvenanceSidecarStore | None = None,
    sidecar_row: ProvenanceSidecarRow | None = None,
) -> dict[str, Any]:
    """
    Default (legacy) order: staged → catalog → publish.

    When ``sidecar_store`` and ``sidecar_row`` are set (batch historical import):
    staged → catalog → sidecar write+read-back verify → publish.

    Publish never runs without verified sidecar on the batch path.
    """
    import clickhouse_connect

    faults = faults or FaultInjector()
    require_valid_test_run_uuid(test_run_id, context="import_historical_close")
    sid = snapshot_id_for_state(state)
    expected = state.active_pool_count
    batch_path = sidecar_store is not None
    if batch_path and sidecar_row is None:
        raise ValueError("sidecar_row required when sidecar_store is set")
    validate_catalog_rows_in_memory(catalog_rows, state)

    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        existing = resolve_existing_publish(
            client,
            db,
            state,
            pipeline_line_id=pipeline_line_id,
            snapshot_id=sid,
            allow_existing_publish=True,
        )
        cat_n = catalog_row_count(sid) if expected else 0
        if existing and cat_n == expected:
            verify_ch_catalog_identity(
                client, db, TABLE_ACTIVE_POOL, sid, state.active_pool_ids
            )
            verify_ch_catalog_matches_import_bundle(catalog_rows, snapshot_id=sid)
            if batch_path:
                sidecar_store.require_before_publish(sidecar_row)
            return {
                "publish": existing,
                "catalog": {"status": "skipped_existing", "count": cat_n},
                "staged": {"status": "skipped_existing"},
                "sidecar": {"status": "verified_existing"},
            }
        if existing and cat_n != expected:
            if cat_n > 0:
                raise CatalogIncompleteError(
                    f"snapshot {sid}: published but catalog partial ({cat_n}/{expected}); "
                    "STOP — manual catalog cleanup required"
                )
            cat = write_active_pool_catalog_from_json_rows(
                catalog_rows,
                snapshot_id=sid,
                expected_count=expected,
                state=state,
            )
            verify_ch_catalog_identity(
                client, db, TABLE_ACTIVE_POOL, sid, state.active_pool_ids
            )
            sidecar_out = _persist_sidecar(batch_path, sidecar_store, sidecar_row)
            pub = commit_publish_ledger(
                state,
                pipeline_line_id=pipeline_line_id,
                line=line,
                faults=faults,
            )
            return {
                "staged": {"status": "skipped_existing"},
                "catalog": cat,
                "sidecar": sidecar_out,
                "publish": pub,
            }
    finally:
        client.close()

    staged = write_active_snapshot_staged(
        state,
        test_run_id=test_run_id,
        faults=faults,
    )
    cat = write_active_pool_catalog_from_json_rows(
        catalog_rows,
        snapshot_id=sid,
        expected_count=expected,
        state=state,
    )
    if cat.get("status") == "skipped_existing":
        verify_ch_catalog_matches_import_bundle(catalog_rows, snapshot_id=sid)

    ch_cfg2 = load_clickhouse_config()
    client2 = clickhouse_connect.get_client(**ch_cfg2.connect_kwargs())
    try:
        if catalog_row_count(sid) != expected:
            raise CatalogIncompleteError(
                f"post-write catalog count {catalog_row_count(sid)} != expected {expected}"
            )
        verify_ch_catalog_identity(
            client2, ch_cfg2.database, TABLE_ACTIVE_POOL, sid, state.active_pool_ids
        )
    finally:
        client2.close()

    sidecar_out = _persist_sidecar(batch_path, sidecar_store, sidecar_row)
    if batch_path:
        sidecar_store.require_before_publish(sidecar_row)

    pub = commit_publish_ledger(
        state,
        pipeline_line_id=pipeline_line_id,
        line=line,
        faults=faults,
    )
    return {"staged": staged, "catalog": cat, "sidecar": sidecar_out, "publish": pub}


def _persist_sidecar(
    batch_path: bool,
    store: ProvenanceSidecarStore | None,
    row: ProvenanceSidecarRow | None,
) -> dict[str, str]:
    if not batch_path or store is None or row is None:
        return {"status": "not_required"}
    result = store.write_and_verify(row)
    return {"status": result}
