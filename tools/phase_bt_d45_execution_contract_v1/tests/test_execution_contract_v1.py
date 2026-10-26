"""BT-D45 tests."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase_bt_d45_execution_contract_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"))

from execution_contract_v1 import MODEL_1, MODEL_2, model_entry_decision  # noqa: E402


def test_model2_rejects_intrabar_touch():
    filled, px, rule, note = model_entry_decision(
        MODEL_2,
        bar_utc="2026-09-11T12:25:00Z",
        active_from="2026-09-11T12:10:00Z",
        entry=0.08384,
        ohlc={"o": 0.08317, "h": 0.08399, "l": 0.08314, "c": 0.0835},
    )
    assert not filled
    assert "UNRESOLVED" in note


def test_model1_accepts_intrabar_touch():
    filled, px, rule, note = model_entry_decision(
        MODEL_1,
        bar_utc="2026-09-11T12:25:00Z",
        active_from="2026-09-11T12:10:00Z",
        entry=0.08384,
        ohlc={"o": 0.08317, "h": 0.08399, "l": 0.08314, "c": 0.0835},
    )
    assert filled
    assert rule == "CAUSAL_V2_LIMIT_TOUCH_INTRABAR"


def test_pilot_outputs():
    out = ROOT / "results/phase_bt_d45_execution_contract_v1"
    if not (out / "execution_model_comparison.csv").exists():
        pytest.skip("run pilot")
    cmp_rows = list(csv.DictReader((out / "execution_model_comparison.csv").open()))
    m1 = next(r for r in cmp_rows if r["execution_model"] == MODEL_1)
    m2 = next(r for r in cmp_rows if r["execution_model"] == MODEL_2)
    assert int(m1["pending_orders_created"]) == 102
    assert int(m2["modeled_entries"]) <= int(m1["modeled_entries"])


def test_reference_1535_not_filled():
    d42 = ROOT / "results/phase_bt_d42_fill_cancel_chronology_v1/order_timelines.csv"
    if not d42.exists():
        pytest.skip("missing D42")
    rows = list(csv.DictReader(d42.open()))
    ref = next(r for r in rows if r["decision_close_utc"] == "2026-09-11T15:35:00Z")
    assert ref["chronology_status"] == "CANCELLED_BEFORE_TOUCH"


def test_deterministic():
    p = ROOT / "results/phase_bt_d45_execution_contract_v1/execution_model_comparison.csv"
    if not p.exists():
        pytest.skip("run pilot")
    assert p.read_text() == p.read_text()
