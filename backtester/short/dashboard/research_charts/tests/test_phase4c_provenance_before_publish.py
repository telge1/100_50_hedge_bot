"""Phase 4C: sidecar before publish, recovery, pilot provenance."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    first_regulatory_batch_spec,
    validate_batch,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import (
    execute_batch_import,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_visibility import (
    LEGACY_PUBLISH_WITHOUT_SIDECAR,
    NEW_BATCH_PUBLISH_WITHOUT_SIDECAR,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close import (
    import_historical_close,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_provenance import (
    validate_pilot_provenance,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar import (
    IMPORT_WRITE_ORDER,
    ProvenanceSidecarRow,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_resume import (
    batch_success_criteria_met,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar import (
    InMemoryProvenanceRegistry,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_store import (
    MemoryProvenanceSidecarStore,
    ProvenanceSidecarConflictError,
)


def _row(sid: str = "s1", sha: str = "a") -> ProvenanceSidecarRow:
    return ProvenanceSidecarRow(
        snapshot_id=sid,
        pipeline_line_id="line",
        import_batch_id="b1",
        relative_json_path="production/results/x.json",
        dest_sha256=sha,
        frozen_input_id="f",
        frozen_price_hash="",
        frozen_revision_hash="",
        provenance_lookup_version="v1",
        catalog_manifest_id="doge",
        validated_at="2026-01-01T00:00:00+00:00",
    )


def test_write_order_sidecar_before_publish():
    assert "sidecar write" in IMPORT_WRITE_ORDER[3]
    assert IMPORT_WRITE_ORDER.index("publish ledger") > IMPORT_WRITE_ORDER.index(
        "sidecar write + read-back verify (explicit; not MergeTree ORDER BY alone)"
    )


def test_memory_store_write_and_readback():
    store = MemoryProvenanceSidecarStore()
    assert store.write_and_verify(_row()) == "written"
    assert store.write_and_verify(_row()) == "skipped_identical"
    store.require_before_publish(_row())


def test_publish_blocked_without_sidecar():
    store = MemoryProvenanceSidecarStore()
    with pytest.raises(ProvenanceSidecarConflictError, match="required before publish"):
        store.require_before_publish(_row())


def test_conflicting_sidecar_on_retry():
    store = MemoryProvenanceSidecarStore()
    store.write_and_verify(_row(sha="aaa"))
    with pytest.raises(ProvenanceSidecarConflictError):
        store.write_and_verify(_row(sha="bbb"))


def test_abort_before_sidecar_blocks_publish():
    store = MemoryProvenanceSidecarStore()

    def boom(row):
        raise ProvenanceSidecarConflictError("simulated abort during sidecar insert")

    store.write_and_verify = boom  # type: ignore[method-assign]
    state = mock.Mock(active_pool_count=0, active_pool_ids=[])
    line = mock.Mock()
    with (
        mock.patch("clickhouse_connect.get_client") as gc,
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.resolve_existing_publish",
            return_value=None,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.write_active_snapshot_staged",
            return_value={"status": "written"},
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.write_active_pool_catalog_from_json_rows",
            return_value={"status": "written", "count": 0},
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.catalog_row_count",
            return_value=0,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.commit_publish_ledger",
        ) as pub,
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.snapshot_id_for_state",
            return_value="s1",
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.validate_catalog_rows_in_memory"
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.verify_ch_catalog_identity"
        ),
    ):
        client = mock.MagicMock()
        gc.return_value = client
        with pytest.raises(ProvenanceSidecarConflictError):
            import_historical_close(
                state,
                [],
                pipeline_line_id="line",
                test_run_id="00000000-0000-4000-8000-000000000001",
                line=line,
                sidecar_store=store,
                sidecar_row=_row(),
            )
    pub.assert_not_called()


def test_import_close_batch_path_calls_sidecar_before_publish():
    store = MemoryProvenanceSidecarStore()
    row = _row()
    state = mock.Mock(active_pool_count=0, active_pool_ids=[])
    line = mock.Mock()
    with (
        mock.patch("clickhouse_connect.get_client") as gc,
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.resolve_existing_publish",
            return_value=None,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.write_active_snapshot_staged",
            return_value={"status": "written"},
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.write_active_pool_catalog_from_json_rows",
            return_value={"status": "written", "count": 0},
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.catalog_row_count",
            return_value=0,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.commit_publish_ledger",
            return_value={"status": "published"},
        ) as pub,
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.snapshot_id_for_state",
            return_value="s1",
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.validate_catalog_rows_in_memory"
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.verify_ch_catalog_identity"
        ),
    ):
        gc.return_value.__enter__ = lambda s: s
        client = mock.MagicMock()
        gc.return_value = client
        client.close = mock.Mock()
        outcome = import_historical_close(
            state,
            [],
            pipeline_line_id="line",
            test_run_id="00000000-0000-4000-8000-000000000001",
            line=line,
            sidecar_store=store,
            sidecar_row=row,
        )
    assert outcome["sidecar"]["status"] in ("written", "skipped_identical")
    pub.assert_called_once()


def test_publish_without_sidecar_legacy_allowed():
    assert LEGACY_PUBLISH_WITHOUT_SIDECAR == "ALLOWED_FOR_READ"
    assert NEW_BATCH_PUBLISH_WITHOUT_SIDECAR == "FORBIDDEN"


def test_pilot_provenance_revalidate():
    body = validate_pilot_provenance(CATALOG_ROOT)
    assert body["ok"] is True
    assert body["planned_sidecar_rows"] == 10


def test_first_50_batch_dry_run_unchanged():
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    vr = validate_batch(CATALOG_ROOT, spec)
    assert not vr["errors"]
    assert vr["summary"]["member_rows"] == 3903
    assert vr["provenance_rows_planned"] == 50


def test_batch_success_requires_provenance_registry():
    reg = InMemoryProvenanceRegistry()
    reg.register(_row("s1"))
    ok, errs = batch_success_criteria_met(
        all_published_on_line=True,
        readback_ok=True,
        provenance_registry=reg,
        snapshot_ids=["s1", "s2"],
        progress_written=True,
        pipeline_line_id="line",
    )
    assert not ok
    assert any("provenance" in e.lower() for e in errs)


def test_batch_success_not_ok_on_readback_failure():
    reg = InMemoryProvenanceRegistry()
    reg.register(_row("s1"))
    ok, errs = batch_success_criteria_met(
        all_published_on_line=True,
        readback_ok=False,
        provenance_registry=reg,
        snapshot_ids=["s1"],
        progress_written=True,
        pipeline_line_id="line",
    )
    assert not ok
    assert any("readback" in e for e in errs)


def test_execute_batch_import_requires_write_gate():
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    with pytest.raises(RuntimeError, match="HISTORICAL_JSON_IMPORT_ALLOW_WRITE"):
        execute_batch_import(CATALOG_ROOT, spec, sidecar_store=MemoryProvenanceSidecarStore())
