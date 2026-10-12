"""Phase 4O: timing, backup provenance, post-import verification, wall-clock gates."""

from __future__ import annotations

import inspect
import json
import os
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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_progress import (
    BatchProgressReport,
    write_progress_report,
    load_progress_report,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    run_batch_readback_gate,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_resource_monitor import (
    check_wall_clock_limits,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator import (
    ControlledMonthlyImportOrchestrator,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    build_phase4o_april_15mpd1_stems500_2880_manifest,
    load_controlled_run_manifest,
    resolve_manifest_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.post_import_verification import (
    verify_manifest_file,
)

MANIFEST_4O = (
    _dash
    / "live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4o_april_15mpd1_stems500_2880_v1.json"
)


def test_run_batch_readback_gate_signature_has_no_fail_closed_kw():
    sig = inspect.signature(run_batch_readback_gate)
    assert "fail_closed" not in sig.parameters


def test_check_wall_clock_limits_missing_timing_fails_when_env_set(monkeypatch):
    monkeypatch.setenv("HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS", "120")
    monkeypatch.setenv("HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS", "60")
    errs = check_wall_clock_limits(batch_seconds=None, readback_seconds=None)
    assert any("timing missing" in e for e in errs)
    assert not check_wall_clock_limits(batch_seconds=10.0, readback_seconds=5.0)


def test_batch_progress_persists_timing_seconds(tmp_path):
    path = tmp_path / "batch.json"
    prog = BatchProgressReport(
        batch_id="b1",
        pipeline_line_id="line",
        combo_folder="15m_PD1",
        month="2026-04",
        import_batch_id="import_b1",
        status="readback_ok",
        closes_planned=1,
        timing_seconds={"validate": 0.1, "write": 1.0, "readback": 0.5, "total": 1.6},
        resource_check_seconds=0.2,
    )
    write_progress_report(path, prog)
    loaded = load_progress_report(path)
    assert loaded.timing_seconds["total"] == 1.6
    assert loaded.resource_check_seconds == 0.2


def test_phase4o_manifest_2380_forty_eight_batches():
    manifest = load_controlled_run_manifest(MANIFEST_4O)
    built = build_phase4o_april_15mpd1_stems500_2880_manifest(CATALOG_ROOT)
    assert manifest.scope_fingerprint == built.scope_fingerprint
    stems = resolve_manifest_stems(CATALOG_ROOT, manifest)
    assert len(stems) == 2380
    assert stems[0] == "20260406T050000Z"
    assert stems[-1] == "20260430T234500Z"
    assert len(manifest.batch_import_ids) == 48
    assert manifest.backup_recovery_id == "923fc087-9e6b-47f1-bbaf-37d765b1182a"


def test_phase4o_full_dry_run(tmp_path):
    orch = ControlledMonthlyImportOrchestrator(CATALOG_ROOT, progress_dir=tmp_path / "p4o")
    manifest = load_controlled_run_manifest(MANIFEST_4O)
    res = orch.dry_run(manifest, manifest_path=MANIFEST_4O)
    assert res.status == "dry_run_ok"
    assert len(res.stems) == 2380
    assert len(res.batch_specs) == 48
    assert res.expected_writes["meta_rows"] == 2380


@mock.patch(
    "research_charts.live_micro_lld_v2_ch_test.historical_json_import.post_import_verification.run_batch_readback_gate"
)
def test_post_import_verification_calls_readback_without_fail_closed(mock_rb, tmp_path):
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
        BatchSpec,
    )
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
        iter_manifest_batch_specs,
    )

    mock_rb.return_value = {"parity": [], "sidecar": []}
    manifest = load_controlled_run_manifest(MANIFEST_4O)
    stems = resolve_manifest_stems(CATALOG_ROOT, manifest)
    spec = iter_manifest_batch_specs(CATALOG_ROOT, manifest, stems)[0]
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.post_import_verification import (
        verify_manifest_scope_readback,
    )

    verify_manifest_scope_readback(CATALOG_ROOT, manifest)
    assert mock_rb.called
    _, kwargs = mock_rb.call_args
    assert "fail_closed" not in kwargs


def test_backup_provenance_env_override(monkeypatch):
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.backup_provenance import (
        VERIFIED_BACKUP_ENV,
        resolve_verified_backup_id,
    )

    manifest = load_controlled_run_manifest(MANIFEST_4O)
    monkeypatch.setenv(VERIFIED_BACKUP_ENV, "923fc087-9e6b-47f1-bbaf-37d765b1182a")
    assert resolve_verified_backup_id(manifest) == "923fc087-9e6b-47f1-bbaf-37d765b1182a"
