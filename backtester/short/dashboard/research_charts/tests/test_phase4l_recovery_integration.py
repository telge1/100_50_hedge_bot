"""Phase 4L: recovery bridge + orchestrator SAFE_RESUME integration (no CH writes)."""

from __future__ import annotations

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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_orchestrator import (
    BatchOrchestratorResult,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator import (
    ControlledMonthlyImportOrchestrator,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    build_phase4i_april_final_30_manifest,
    resolve_manifest_stems,
    iter_manifest_batch_specs,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
    assess_scope_ch_states,
    assert_scope_recovery_safe,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.recovery_orchestrator_bridge import (
    close_resume_allowed_for_write,
    find_blocked_close_resumes,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_resume import (
    assess_close_resume_states,
)


def test_catalog_without_sidecar_safe_through_bridge():
    sid = "abc"
    layers = {
        "meta_complete": 1,
        "members": 5,
        "catalog": 5,
        "publish_any": 0,
        "sidecar_any": 0,
    }
    scope = assess_scope_ch_states(
        [sid],
        pipeline_line_id="line",
        published_on_line=set(),
        layer_by_snapshot={sid: layers},
    )
    assert scope[0]["recovery_action"] == "SAFE_RESUME"
    assert_scope_recovery_safe(scope)
    close = assess_close_resume_states(
        [("stem", sid)],
        pipeline_line_id="line",
        published_on_line=set(),
        meta_snapshot_ids={sid},
        pilot_ids=set(),
    )
    assert close[0].classification == "meta_without_publish"
    assert close_resume_allowed_for_write(
        close[0].classification, scope[0]["recovery_action"]
    )
    assert not find_blocked_close_resumes(
        [{"snapshot_id": sid, "classification": close[0].classification}],
        {sid: scope[0]["recovery_action"]},
    )


def test_sidecar_without_publish_safe_through_bridge():
    sid = "def"
    layers = {
        "meta_complete": 1,
        "members": 2,
        "catalog": 2,
        "publish_any": 0,
        "sidecar_any": 1,
    }
    scope = assess_scope_ch_states(
        [sid],
        pipeline_line_id="line",
        published_on_line=set(),
        layer_by_snapshot={sid: layers},
    )
    assert scope[0]["recovery_action"] == "SAFE_RESUME"
    close = assess_close_resume_states(
        [("s", sid)],
        pipeline_line_id="line",
        published_on_line=set(),
        meta_snapshot_ids={sid},
    )
    assert not find_blocked_close_resumes(
        [{"snapshot_id": sid, "classification": close[0].classification}],
        {sid: scope[0]["recovery_action"]},
    )


def test_orphan_member_blocked():
    sid = "orphan"
    scope = assess_scope_ch_states(
        [sid],
        pipeline_line_id="line",
        published_on_line=set(),
        layer_by_snapshot={
            sid: {
                "meta_complete": 0,
                "members": 3,
                "catalog": 0,
                "publish_any": 0,
                "sidecar_any": 0,
            }
        },
    )
    with pytest.raises(Exception):
        assert_scope_recovery_safe(scope)


def test_orchestrator_write_path_allows_catalog_resume(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", "1")
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", "1")
    manifest = build_phase4i_april_final_30_manifest(CATALOG_ROOT)
    stems = resolve_manifest_stems(CATALOG_ROOT, manifest)
    spec = iter_manifest_batch_specs(CATALOG_ROOT, manifest, stems)[0]
    sid = "622ef9e24ef2913a0bd3f8429b26b60bf6da1a37cfc8bb930dc31431822e439a"
    layers = {
        sid: {
            "meta_complete": 1,
            "members": 90,
            "catalog": 90,
            "publish_any": 0,
            "sidecar_any": 0,
        }
    }
    orch = ControlledMonthlyImportOrchestrator(
        CATALOG_ROOT, progress_dir=Path(f"/tmp/p4l_resume_{uuid.uuid4().hex}")
    )

    def fake_run_batch(*args, **kwargs):
        return BatchOrchestratorResult(
            spec.batch_id,
            "readback_ok",
            {"snapshot_ids": [sid]},
            [],
            {},
            [],
            {"write": 1.0, "readback": 0.5, "total": 1.5},
        )

    monkeypatch.setattr(orch._batch, "run_batch", fake_run_batch)
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator.collect_resource_baseline",
        lambda *a, **k: {
            "disk": {"free_bytes": 10**12},
            "clickhouse": {"orphan_member_snapshots": [], "parts_and_merges": {"active_merges_lmv2": 0}},
        },
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator.check_optional_limits",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator.load_scope_layer_map_for_snapshots",
        lambda ids: {i: layers.get(i, {}) for i in ids},
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state.fetch_published_snapshot_ids",
        lambda *a, **k: set(),
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state.fetch_meta_complete_snapshot_ids",
        lambda *a, **k: {sid},
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state.ch_client_and_db",
        lambda: (mock.Mock(), "db"),
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_overlap.pilot_snapshot_ids_from_catalog",
        lambda *a, **k: set(),
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator.validate_batch",
        lambda *a, **k: {
            "errors": [],
            "snapshot_ids": list(stems) if stems else [sid],
            "summary": {},
        },
    )
    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator.check_wall_clock_limits",
        lambda *a, **k: [],
    )

    res = orch._run(
        manifest,
        mode="write_and_readback",
        test_run_id=str(uuid.uuid4()),
    )
    assert res.status == "readback_ok"
    assert not any("blocked for write" in e for e in res.errors)
