"""Phase 6 unit tests (publish guard, faults, parity helpers — no CH)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.faults import (
    FaultInjector,
    SimulatedCrash,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_guard import (
    deterministic_publish_id,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    RunnerResult,
    compare_pipeline_results,
    enumerate_decision_closes,
)


def test_deterministic_publish_id():
    a = deterministic_publish_id("snap1")
    b = deterministic_publish_id("snap1")
    c = deterministic_publish_id("snap2")
    assert a == b
    assert a != c


def test_fault_injector_raises():
    inj = FaultInjector(points={"before_write"})
    try:
        inj.maybe_raise("before_write")
        assert False, "expected crash"
    except SimulatedCrash as e:
        assert e.point == "before_write"


def test_enumerate_decision_closes_5m():
    start = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    closes = enumerate_decision_closes(start, 3, "5m")
    assert len(closes) == 3
    assert (closes[1] - closes[0]).total_seconds() == 300


def test_compare_pipeline_results_pass():
    dc = "2026-09-10T12:00:00+00:00"
    full = RunnerResult("full", "r1", snapshot_ids={dc: "sid1"}, fingerprints={dc: "fp1"})
    resume = RunnerResult("resume", "r2", snapshot_ids={dc: "sid1"}, fingerprints={dc: "fp1"})
    exp = [datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)]
    rep = compare_pipeline_results(full, resume, exp)
    assert rep["pass"] is True


def test_compare_pipeline_results_detects_mismatch():
    dc = "2026-09-10T12:00:00+00:00"
    full = RunnerResult("full", "r1", snapshot_ids={dc: "sid1"}, fingerprints={dc: "fp1"})
    resume = RunnerResult("resume", "r2", snapshot_ids={dc: "sid2"}, fingerprints={dc: "fp1"})
    exp = [datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)]
    rep = compare_pipeline_results(full, resume, exp)
    assert rep["pass"] is False
    assert rep["snapshot_id_mismatch"]
