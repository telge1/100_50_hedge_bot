"""Phase 4W: May monthly sequence stop/resume and plan loading (no CH writes)."""

from __future__ import annotations

import json
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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator import (
    ControlledOrchestratorResult,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_sequence import (
    ControlledMonthlySequenceRunner,
    MonthlySequencePlan,
    MonthlySequenceStep,
    load_monthly_sequence_plan,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    build_phase4w_may2026_combo_manifest,
    load_controlled_run_manifest,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)

PLAN = (
    _dash
    / "live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4w_may2026_monthly_run_plan_v1.json"
)


def _fake_result(status: str, combo: str) -> ControlledOrchestratorResult:
    return ControlledOrchestratorResult(
        run_id="00000000-0000-4000-8000-000000005101",
        status=status,
        manifest_path=None,
        stems=["s"] * 10,
        snapshot_ids=["id"] * 10,
        expected_writes={"meta_rows": 10},
        batch_specs=[{"batch_id": "b1"}],
        ch_preflight=None,
        resource_baseline_before={},
        resource_baseline_after=None,
        resume_states=[],
        errors=[] if status == "dry_run_ok" else ["simulated failure"],
        stopped_reason="",
    )


@pytest.fixture
def may_plan(tmp_path) -> MonthlySequencePlan:
    backup = "00000000-0000-0000-0000-000000000099"
    manifests = []
    for combo in ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10"):
        m = build_phase4w_may2026_combo_manifest(CATALOG_ROOT, combo, backup_recovery_id=backup)
        p = tmp_path / f"{combo}.json"
        p.write_text(json.dumps(m.canonical_dict()), encoding="utf-8")
        manifests.append(
            MonthlySequenceStep(combo_folder=combo, manifest_path=str(p), manifest=m)
        )
    return MonthlySequencePlan(
        schema_version=1,
        approved_month="2026-05",
        backup_recovery_id=backup,
        combo_order=("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10"),
        steps=tuple(manifests),
    )


def test_may_manifests_four_distinct_run_ids():
    backup = "df88d380-ec06-4007-9658-b69ee50878df"
    ids = set()
    for combo in ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10"):
        m = build_phase4w_may2026_combo_manifest(CATALOG_ROOT, combo, backup_recovery_id=backup)
        ids.add(m.run_id)
        assert m.approved_month == "2026-05"
        assert m.batch_size == 50
    assert len(ids) == 4


def test_monthly_sequence_stops_after_failed_combo(tmp_path, may_plan):
    state = tmp_path / "state.json"
    calls = {"n": 0}

    def fake_dry_run(manifest, manifest_path=None):
        calls["n"] += 1
        if calls["n"] == 2:
            return _fake_result("preflight_failed", manifest.combo_folder)
        return _fake_result("dry_run_ok", manifest.combo_folder)

    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_sequence.ControlledMonthlyImportOrchestrator"
    ) as orch_cls:
        inst = mock.Mock()
        inst.dry_run.side_effect = fake_dry_run
        orch_cls.return_value = inst
        runner = ControlledMonthlySequenceRunner(CATALOG_ROOT, progress_dir=tmp_path / "prog")
        res = runner.run(may_plan, mode="dry_run", resume_state_path=state)

    assert res.status == "failed"
    assert res.stopped_at_combo == "15m_PD5"
    assert calls["n"] == 2
    saved = json.loads(state.read_text())
    assert saved["next_combo_index"] == 1


@pytest.mark.skipif(not PLAN.is_file(), reason="phase4w plan not generated yet")
def test_load_committed_monthly_plan():
    plan = load_monthly_sequence_plan(PLAN)
    assert plan.combo_order == ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10")
    for step in plan.steps:
        loaded = load_controlled_run_manifest(Path(step.manifest_path))
        assert loaded.backup_recovery_id == plan.backup_recovery_id
