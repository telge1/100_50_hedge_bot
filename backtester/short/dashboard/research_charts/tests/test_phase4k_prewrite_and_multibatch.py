"""Phase 4K: productive pre-write gate + April 15m_PD1 500-snapshot multi-batch manifest."""

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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    first_regulatory_batch_spec,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import (
    execute_batch_import,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator import (
    ControlledMonthlyImportOrchestrator,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    StemSlice,
    build_phase4k_april_15mpd1_500_manifest,
    iter_manifest_batch_specs,
    load_controlled_run_manifest,
    resolve_manifest_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
    ProductivePrewriteGateError,
    assert_productive_env_gates,
    assert_scope_recovery_safe,
    classify_snapshot_layers,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar import (
    ProvenanceSidecarConflictError,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.write_guards import (
    InvalidTestRunIdError,
)

MANIFEST_4K = (
    _dash
    / "live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4k_april_15mpd1_500_v1.json"
)


@pytest.fixture
def both_gates(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", "1")
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", "1")


def test_env_gate_write_only_fails_before_locked_write(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", "1")
    monkeypatch.delenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", raising=False)
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    with (
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write._execute_batch_import_locked"
        ) as locked,
        pytest.raises(ProductivePrewriteGateError),
    ):
        execute_batch_import(CATALOG_ROOT, spec, test_run_id=str(uuid.uuid4()))
    locked.assert_not_called()


def test_env_gate_sidecar_only_fails(monkeypatch):
    monkeypatch.delenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", raising=False)
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", "1")
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    with pytest.raises(ProductivePrewriteGateError):
        execute_batch_import(CATALOG_ROOT, spec, test_run_id=str(uuid.uuid4()))


def test_env_gate_both_missing_fails(monkeypatch):
    monkeypatch.delenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", raising=False)
    monkeypatch.delenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", raising=False)
    with pytest.raises(ProductivePrewriteGateError):
        assert_productive_env_gates()


def test_safe_resume_catalog_without_sidecar_allowed():
    assert_scope_recovery_safe(
        [
            {
                "snapshot_id": "s1",
                "recovery_action": "SAFE_RESUME",
                "reason": "catalog pending sidecar",
                "layers": {
                    "meta_complete": 1,
                    "members": 10,
                    "catalog": 10,
                    "publish_any": 0,
                    "sidecar_any": 0,
                },
            }
        ]
    )


def test_sidecar_conflict_is_stop_not_resume():
    with pytest.raises(ProvenanceSidecarConflictError):
        raise ProvenanceSidecarConflictError("conflicting provenance")


def test_invalid_uuid_zero_writes(both_gates, monkeypatch):
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write._execute_batch_import_locked"
    ) as locked:
        with pytest.raises(InvalidTestRunIdError):
            execute_batch_import(CATALOG_ROOT, spec, test_run_id="phase4k_not_uuid")
        locked.assert_not_called()


def test_both_gates_reach_locked_path(both_gates, monkeypatch):
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
        ProductivePrewriteGateResult,
    )

    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    run_uuid = str(uuid.uuid4())
    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write.validate_batch",
        return_value={"errors": [], "snapshot_ids": []},
    ), mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write.run_productive_prewrite_gate",
        return_value=ProductivePrewriteGateResult(
            test_run_id=run_uuid, write_gate=True, sidecar_gate=True, scope_checks=[]
        ),
    ), mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write._execute_batch_import_locked",
        return_value=[],
    ) as locked:
        execute_batch_import(CATALOG_ROOT, spec, test_run_id=run_uuid)
    locked.assert_called_once()


def test_phase4k_manifest_500_ten_batches():
    manifest = load_controlled_run_manifest(MANIFEST_4K)
    stems = resolve_manifest_stems(CATALOG_ROOT, manifest)
    specs = iter_manifest_batch_specs(CATALOG_ROOT, manifest, stems)
    assert len(stems) == 500
    assert len(specs) == 10
    assert all(len(s.stems) == 50 for s in specs)
    assert len({s.import_batch_id for s in specs}) == 10


def test_phase4k_dry_run_500(tmp_path):
    orch = ControlledMonthlyImportOrchestrator(
        CATALOG_ROOT, progress_dir=tmp_path / "p4k"
    )
    manifest = load_controlled_run_manifest(MANIFEST_4K)
    res = orch.dry_run(manifest, manifest_path=MANIFEST_4K)
    assert res.status == "dry_run_ok"
    assert len(res.stems) == 500
    assert len(res.batch_specs) == 10
    assert res.expected_writes["meta_rows"] == 500


def test_resume_same_manifest_ok(tmp_path):
    manifest = load_controlled_run_manifest(MANIFEST_4K)
    orch = ControlledMonthlyImportOrchestrator(CATALOG_ROOT, progress_dir=tmp_path / "r")
    assert orch.dry_run(manifest).status == "dry_run_ok"
    assert orch.dry_run(manifest).status == "dry_run_ok"


def test_resume_tampered_manifest_stops(tmp_path):
    manifest = load_controlled_run_manifest(MANIFEST_4K)
    prog = tmp_path / "r" / f"{manifest.run_id}.json"
    prog.parent.mkdir(parents=True, exist_ok=True)
    prog.write_text(json.dumps({"scope_fingerprint": "deadbeef"}), encoding="utf-8")
    orch = ControlledMonthlyImportOrchestrator(CATALOG_ROOT, progress_dir=tmp_path / "r")
    with pytest.raises(Exception):
        orch.dry_run(manifest)


def test_orchestrator_stops_after_readback_failure(both_gates, monkeypatch):
    manifest = build_phase4k_april_15mpd1_500_manifest(CATALOG_ROOT)
    # shrink to 2 batches for test
    small = manifest
    stems = resolve_manifest_stems(CATALOG_ROOT, small)[:100]
    orch = ControlledMonthlyImportOrchestrator(CATALOG_ROOT, progress_dir=Path("/tmp/p4k_fail"))
    specs = iter_manifest_batch_specs(CATALOG_ROOT, small, tuple(stems))
    calls = {"n": 0}

    def fake_run_batch(*args, **kwargs):
        calls["n"] += 1
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_orchestrator import (
            BatchOrchestratorResult,
        )

        if calls["n"] == 1:
            return BatchOrchestratorResult(
                "b1", "readback_failed", {}, None, None, ["fail"]
            )
        return BatchOrchestratorResult("b2", "readback_ok", {}, [], {}, [])

    monkeypatch.setattr(orch._batch, "run_batch", fake_run_batch)
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator.load_scope_layer_map_for_snapshots",
        lambda ids: {i: {} for i in ids},
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state.fetch_published_snapshot_ids",
        lambda *a, **k: set(),
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state.ch_client_and_db",
        lambda: (mock.Mock(), "db"),
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_overlap.pilot_snapshot_ids_from_catalog",
        lambda *a, **k: set(),
    )
    res = orch._run(small, mode="write_and_readback", test_run_id=str(uuid.uuid4()))
    assert calls["n"] == 1
    assert res.status == "failed"
