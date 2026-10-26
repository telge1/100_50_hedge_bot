"""BT-D43 replay tests."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase_bt_d43_single_order_replay_v1"))

from single_order_replay_v1 import replay_from_d41_events  # noqa: E402


def test_max_one_pending():
    out = ROOT / "results/phase_bt_d43_single_order_replay_v1/state_transitions.csv"
    if not out.exists():
        pytest.skip("run run_replay.py")
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    pending = 0
    for r in rows:
        if r["event_type"] == "CREATE_PENDING":
            assert pending == 0
            pending = 1
        elif r["event_type"] in ("CANCEL_PENDING", "REPLACE_PENDING"):
            pending = 0
    assert True


def test_replay_deterministic():
    d41 = ROOT / "results/phase_bt_d41_pending_lifecycle_v1/lifecycle_events.csv"
    d42 = ROOT / "results/phase_bt_d42_fill_cancel_chronology_v1/order_timelines.csv"
    if not d41.exists() or not d42.exists():
        pytest.skip("missing D41/D42 outputs")
    events = list(csv.DictReader(d41.open(encoding="utf-8")))
    tl = {r["order_id"]: r for r in csv.DictReader(d42.open(encoding="utf-8"))}
    a = replay_from_d41_events(events, tl)
    b = replay_from_d41_events(events, tl)
    assert len(a.transitions) == len(b.transitions)
    assert a.max_simultaneous_pending <= 1


def test_lineage_replace_count():
    lin = ROOT / "results/phase_bt_d43_single_order_replay_v1/order_lineage.csv"
    if not lin.exists():
        pytest.skip("run run_replay.py")
    rows = list(csv.DictReader(lin.open(encoding="utf-8")))
    assert len(rows) == 12


def test_scanner_emits_match_create_count():
    d41 = ROOT / "results/phase_bt_d41_pending_lifecycle_v1/lifecycle_events.csv"
    if not d41.exists():
        pytest.skip("missing D41")
    events = list(csv.DictReader(d41.open(encoding="utf-8")))
    planned = sum(1 for e in events if e["event_type"] == "PLANNED_SCANNER")
    creates = sum(1 for e in events if e["event_type"] == "CREATE")
    assert planned == creates == 102


def test_no_invariant_violation_in_replay():
    out = ROOT / "results/phase_bt_d43_single_order_replay_v1/state_transitions.csv"
    if not out.exists():
        pytest.skip("run run_replay.py")
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert not any(r["event_type"] == "INVARIANT_VIOLATION" for r in rows)


def test_touch_before_cancel_branch_exists():
    br = ROOT / "results/phase_bt_d43_single_order_replay_v1/branch_outcomes.csv"
    if not br.exists():
        pytest.skip("run run_replay.py")
    rows = list(csv.DictReader(br.open(encoding="utf-8")))
    assert any(r["chronology_status"] == "EXECUTION_POSSIBLE" for r in rows)


def test_cancel_before_touch_branch_exists():
    br = ROOT / "results/phase_bt_d43_single_order_replay_v1/branch_outcomes.csv"
    if not br.exists():
        pytest.skip("run run_replay.py")
    rows = list(csv.DictReader(br.open(encoding="utf-8")))
    assert any(r["chronology_status"] == "CANCELLED_BEFORE_TOUCH" for r in rows)


def test_reference_1535_not_touched_before_replace():
    ref = ROOT / "results/phase_bt_d43_single_order_replay_v1/reference_1535.json"
    if not ref.exists():
        pytest.skip("run run_replay.py")
    import json

    doc = json.loads(ref.read_text(encoding="utf-8"))
    assert doc["replacement_at_15_40"]["chronology"] == "CANCELLED_BEFORE_TOUCH"
    assert doc["touch_intervals"] == []
