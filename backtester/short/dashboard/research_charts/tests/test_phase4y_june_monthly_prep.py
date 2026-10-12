"""Phase 4Y: June monthly sequence stop/resume and plan loading (no CH writes)."""

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
    build_phase4y_june2026_combo_manifest,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)

PLAN = (
    _dash
    / "live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4y_june2026_monthly_run_plan_v1.json"
)


def _fake_result(status: str) -> ControlledOrchestratorResult:
    return ControlledOrchestratorResult(
        run_id="00000000-0000-4000-8000-000000005201",
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
def june_plan(tmp_path) -> MonthlySequencePlan:
    backup = "00000000-0000-0000-0000-000000000099"
    manifests = []
    for combo in ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10"):
        m = build_phase4y_june2026_combo_manifest(CATALOG_ROOT, combo, backup_recovery_id=backup)
        p = tmp_path / f"{combo}.json"
        p.write_text(json.dumps(m.canonical_dict()), encoding="utf-8")
        manifests.append(
            MonthlySequenceStep(combo_folder=combo, manifest_path=str(p), manifest=m)
        )
    return MonthlySequencePlan(
        schema_version=1,
        approved_month="2026-06",
        backup_recovery_id=backup,
        combo_order=("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10"),
        steps=tuple(manifests),
    )


def test_june_manifests_four_distinct_run_ids():
    backup = "ebfdfb7d-ba12-4c2a-af6b-ff9c5ca57b02"
    ids = set()
    for combo in ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10"):
        m = build_phase4y_june2026_combo_manifest(CATALOG_ROOT, combo, backup_recovery_id=backup)
        ids.add(m.run_id)
        assert m.approved_month == "2026-06"
        assert m.max_snapshots in (2880, 180)
    assert len(ids) == 4


def test_monthly_sequence_stops_after_failed_combo(tmp_path, june_plan):
    state = tmp_path / "state.json"
    calls = {"n": 0}

    def fake_dry_run(manifest, manifest_path=None):
        calls["n"] += 1
        if calls["n"] == 2:
            return _fake_result("preflight_failed")
        return _fake_result("dry_run_ok")

    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_sequence.ControlledMonthlyImportOrchestrator"
    ) as orch_cls:
        inst = mock.Mock()
        inst.dry_run.side_effect = fake_dry_run
        orch_cls.return_value = inst
        runner = ControlledMonthlySequenceRunner(CATALOG_ROOT, progress_dir=tmp_path / "prog")
        res = runner.run(june_plan, mode="dry_run", resume_state_path=state)

    assert res.status == "failed"
    assert res.stopped_at_combo == "15m_PD5"
    assert calls["n"] == 2


@pytest.mark.skipif(not PLAN.is_file(), reason="phase4y plan not generated yet")
def test_load_committed_june_plan():
    plan = load_monthly_sequence_plan(PLAN)
    assert plan.approved_month == "2026-06"
    assert sum(s.manifest.max_snapshots for s in plan.steps) == 8820
