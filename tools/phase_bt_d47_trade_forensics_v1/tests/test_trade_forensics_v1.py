"""BT-D47 forensics tests."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "results/phase_bt_d47_trade_forensics_v1"


def test_nineteen_positions():
    rows = list(csv.DictReader((OUT / "positions_audit.csv").open()))
    assert len(rows) == 19


def test_overlay_counts():
    doc = json.loads((OUT / "chart_overlay_events.json").read_text())
    entries = [r for r in doc["records"] if r["event_type"] == "SIMULATED_ENTRY"]
    sl = [r for r in doc["records"] if r["event_type"] == "SIMULATED_EXIT_SL"]
    tp = [r for r in doc["records"] if r["event_type"] == "SIMULATED_EXIT_TP"]
    open_ = [r for r in doc["records"] if r["event_type"] == "SIMULATED_POSITION_OPEN"]
    assert len(entries) == 19
    assert len(sl) == 14
    assert len(tp) == 4
    assert len(open_) == 1
    assert doc["scanner_boxes_included"] is False


def test_suppressed_66():
    rows = list(csv.DictReader((OUT / "suppressed_emits_audit.csv").open()))
    assert len(rows) == 66


def test_no_scanner_in_overlay():
    doc = json.loads((OUT / "chart_overlay_events.json").read_text())
    assert "SIGNAL" not in json.dumps(doc)
