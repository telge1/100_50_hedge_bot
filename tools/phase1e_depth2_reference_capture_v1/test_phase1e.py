"""Phase 1E tests (no ClickHouse for core logic)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from capture_lib import build_capture_record, is_valid_decision_time
from entry_geometry import classify_entry_vs_depth2

ROOT = Path(__file__).resolve().parents[2]
TOOL = Path(__file__).resolve().parent


def test_n3_no_timestamp():
    rec = build_capture_record(
        "N3",
        "NO_SHORT",
        None,
        None,
        "test",
        "image(20261022-091843).png",
    )
    assert rec["timestamp_status"] == "NEEDS_TIMESTAMP_VERIFICATION"
    assert rec["landscape_status"] == "SKIPPED_NO_TIMESTAMP"
    assert rec["decision"] == "NO_SHORT"


def test_unknown_time_treated_invalid():
    assert not is_valid_decision_time("UNKNOWN")


def test_pool_at_entry():
    ivs = [
        {
            "side": "upper",
            "bottom_price": 0.08506,
            "top_price": 0.08508,
            "depth": 3,
            "origins": ["a"],
        }
    ]
    g = classify_entry_vs_depth2(ivs, 0.085071)
    assert g["geometry_status"] == "POOL_AT_ENTRY"


def test_nearby_s1_scale():
    ivs = [
        {
            "side": "upper",
            "bottom_price": 0.08386,
            "top_price": 0.08389,
            "depth": 2,
            "origins": ["a"],
        }
    ]
    g = classify_entry_vs_depth2(ivs, 0.08384)
    assert g["geometry_status"] == "NEARBY_DEPTH2_POOL"
    assert g["distance_to_nearest_pct"] < 0.05


def test_pilot_cli():
    proc = subprocess.run(
        [sys.executable, str(TOOL / "capture_case.py"), "pilot"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    summary = json.loads(
        (ROOT / "results/phase1e_depth2_reference_capture_v1/pilot_summary.json").read_text()
    )
    assert summary["verdict"] == "DEPTH2_REFERENCE_CAPTURE_READY"
    n3 = json.loads(
        (
            ROOT
            / "results/phase1e_depth2_reference_capture_v1/cases/N3/capture_record.json"
        ).read_text()
    )
    assert n3["landscape_status"] == "SKIPPED_NO_TIMESTAMP"
    s5 = json.loads(
        (
            ROOT
            / "results/phase1e_depth2_reference_capture_v1/cases/S5/capture_record.json"
        ).read_text()
    )
    assert s5["landscape_status"] == "COMPLETE"
    assert s5["entry_geometry"]["geometry_status"] == "POOL_AT_ENTRY"

