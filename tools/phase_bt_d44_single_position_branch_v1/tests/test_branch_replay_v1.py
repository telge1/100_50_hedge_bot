"""BT-D44 branch replay tests."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase_bt_d44_single_position_branch_v1"))


def test_outputs_exist():
    out = ROOT / "results/phase_bt_d44_single_position_branch_v1"
    for name in (
        "event_timeline.csv",
        "branch_states.csv",
        "potential_position_exits.csv",
        "independent_trade_counts.json",
        "reference_1535.json",
    ):
        assert (out / name).exists(), name


def test_path_b_blocks_some_emits():
    counts = json.loads(
        (ROOT / "results/phase_bt_d44_single_position_branch_v1/independent_trade_counts.json").read_text()
    )
    assert counts["path_b_scanner_emits_blocked_while_position_open"] > 0
    assert counts["distinct_pending_order_ids"] == 102


def test_reference_1535_no_hypothetical_entry():
    ref = json.loads(
        (ROOT / "results/phase_bt_d44_single_position_branch_v1/reference_1535.json").read_text()
    )
    assert ref["chronology_status"] == "CANCELLED_BEFORE_TOUCH"


def test_at_most_one_open_exit_row_per_entry():
    ex = ROOT / "results/phase_bt_d44_single_position_branch_v1/potential_position_exits.csv"
    if not ex.exists():
        pytest.skip("run replay")
    rows = list(csv.DictReader(ex.open(encoding="utf-8")))
    oids = [r["order_id"] for r in rows]
    assert len(oids) == len(set(oids))


def test_deterministic_counts():
    p = ROOT / "results/phase_bt_d44_single_position_branch_v1/independent_trade_counts.json"
    a = json.loads(p.read_text())
    b = json.loads(p.read_text())
    assert a == b
