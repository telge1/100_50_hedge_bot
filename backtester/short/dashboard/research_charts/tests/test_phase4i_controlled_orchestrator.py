"""Phase 4I: controlled monthly import orchestrator (manifest-bound, no CH writes in CI)."""

from __future__ import annotations

import json
import sys
import uuid
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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import (
    execute_batch_import,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator import (
    ControlledMonthlyImportOrchestrator,
    classify_close_layers_for_recovery,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    ControlledRunManifest,
    StemSlice,
    compute_scope_fingerprint,
    load_controlled_run_manifest,
    resolve_manifest_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_import_lock import (
    HistoricalImportLockError,
    historical_import_write_lock,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.write_guards import (
    InvalidTestRunIdError,
    require_valid_test_run_uuid,
)

MANIFEST_PATH = (
    _dash
    / "live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4i_april_4hpd10_final_30_v1.json"
)


def test_phase4i_manifest_resolves_30_stems():
    manifest = load_controlled_run_manifest(MANIFEST_PATH)
    stems = resolve_manifest_stems(CATALOG_ROOT, manifest)
    assert len(stems) == 30
    assert manifest.stem_slice.start_index == 150
    assert manifest.stem_slice.end_index == 180
    assert manifest.max_batches == 1
    assert manifest.pipeline_line_id.endswith("4hpd10_v1")


def test_phase4i_dry_run_stops_at_scope(tmp_path):
    orch = ControlledMonthlyImportOrchestrator(
        CATALOG_ROOT, progress_dir=tmp_path / "progress"
    )
    manifest = load_controlled_run_manifest(MANIFEST_PATH)
    res = orch.dry_run(manifest, manifest_path=MANIFEST_PATH)
    assert res.status == "dry_run_ok"
    assert len(res.stems) == 30
    assert len(res.batch_specs) == 1
    assert res.expected_writes["meta_rows"] == 30
    assert res.expected_writes["publish_rows"] == 30
    assert res.expected_writes["sidecar_rows"] == 30
    assert res.errors == []


def test_manifest_fingerprint_blocks_tampered_bounds():
    manifest = load_controlled_run_manifest(MANIFEST_PATH)
    bad = ControlledRunManifest(
        schema_version=manifest.schema_version,
        run_id=manifest.run_id,
        approved_month=manifest.approved_month,
        combo_folder=manifest.combo_folder,
        pipeline_line_id=manifest.pipeline_line_id,
        stem_slice=StemSlice(150, 181),
        max_snapshots=31,
        max_batches=manifest.max_batches,
        batch_size=manifest.batch_size,
        import_batch_id=manifest.import_batch_id,
        backup_recovery_id=manifest.backup_recovery_id,
        scope_fingerprint=manifest.scope_fingerprint,
    )
    with pytest.raises(Exception):
        resolve_manifest_stems(CATALOG_ROOT, bad)


def test_resume_rejects_changed_scope_fingerprint(tmp_path):
    manifest = load_controlled_run_manifest(MANIFEST_PATH)
    prog = tmp_path / f"{manifest.run_id}.json"
    prog.write_text(
        json.dumps(
            {
                "scope_fingerprint": "deadbeef",
                "batches_completed": 0,
            }
        ),
        encoding="utf-8",
    )
    orch = ControlledMonthlyImportOrchestrator(
        CATALOG_ROOT, progress_dir=tmp_path
    )
    with pytest.raises(Exception):
        orch.dry_run(manifest)


def test_invalid_test_run_id_rejected():
    with pytest.raises(InvalidTestRunIdError):
        require_valid_test_run_uuid("phase4i_final_30_apr2026_4hpd10_v1")


def test_crash_recovery_classifications():
    assert classify_close_layers_for_recovery(
        catalog_complete=False, sidecar_present=False, publish_present=False
    )[0] == "SAFE_RESUME"
    assert classify_close_layers_for_recovery(
        catalog_complete=True, sidecar_present=True, publish_present=False
    )[0] == "SAFE_RESUME"
    assert classify_close_layers_for_recovery(
        catalog_complete=True, sidecar_present=False, publish_present=False
    )[0] == "STOP_MANUAL"
    assert classify_close_layers_for_recovery(
        catalog_complete=False, sidecar_present=True, publish_present=True
    )[0] == "STOP_MANUAL"
    assert classify_close_layers_for_recovery(
        catalog_complete=True, sidecar_present=True, publish_present=True
    )[0] == "COMPLETE"
    assert classify_close_layers_for_recovery(
        catalog_complete=True,
        sidecar_present=False,
        publish_present=True,
    )[0] == "STOP_MANUAL"
    assert classify_close_layers_for_recovery(
        provenance_canonical_match=False,
        catalog_complete=False,
        sidecar_present=False,
        publish_present=False,
    )[0] == "STOP_CONFLICT"


def test_nested_lock_avoided_when_write_lock_held(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", "1")
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", "1")
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
        first_regulatory_batch_spec,
    )

    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    lock_calls = 0

    def counting_lock(*args, **kwargs):
        nonlocal lock_calls
        lock_calls += 1
        return historical_import_write_lock(*args, **kwargs)

    with (
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write.historical_import_write_lock",
            side_effect=counting_lock,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write._execute_batch_import_locked",
            return_value=[],
        ),
    ):
        execute_batch_import(CATALOG_ROOT, spec, test_run_id=str(uuid.uuid4()), write_lock_held=True)
        assert lock_calls == 0
        execute_batch_import(CATALOG_ROOT, spec, test_run_id=str(uuid.uuid4()), write_lock_held=False)
        assert lock_calls == 1


def test_max_batches_guard():
    manifest = load_controlled_run_manifest(MANIFEST_PATH)
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
        iter_manifest_batch_specs,
    )

    stems = resolve_manifest_stems(CATALOG_ROOT, manifest)
    bad = ControlledRunManifest(
        schema_version=manifest.schema_version,
        run_id=manifest.run_id,
        approved_month=manifest.approved_month,
        combo_folder=manifest.combo_folder,
        pipeline_line_id=manifest.pipeline_line_id,
        stem_slice=manifest.stem_slice,
        max_snapshots=manifest.max_snapshots,
        max_batches=0,
        batch_size=manifest.batch_size,
        import_batch_id=manifest.import_batch_id,
        backup_recovery_id=manifest.backup_recovery_id,
        scope_fingerprint=manifest.scope_fingerprint,
    )
    with pytest.raises(Exception):
        iter_manifest_batch_specs(CATALOG_ROOT, bad, stems)


def test_concurrent_writer_nonblocking_lock(tmp_path):
    lock_path = tmp_path / "test.flock"
    with historical_import_write_lock(lock_path):
        with pytest.raises(HistoricalImportLockError):
            with historical_import_write_lock(lock_path, blocking=False):
                pass
