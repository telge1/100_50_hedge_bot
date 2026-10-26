"""Phase 1D unit tests (no ClickHouse)."""

from __future__ import annotations

import json
from pathlib import Path

from contrast_lib import landscape_fingerprint, upper_gap_chain_above_ref
from timestamp_forensics import build_timestamp_resolution_report

ROOT = Path(__file__).resolve().parents[2]


def test_n3_still_unverified():
    rep = build_timestamp_resolution_report()
    assert rep["n3_summary"]["identity_verdict"] == "NEEDS_TIMESTAMP_VERIFICATION"
    assert rep["verified_rejected_short_available"] is False


def test_gap_chain_filter():
    gaps = [
        {
            "side": "upper",
            "gap_bottom_price": 0.086,
            "gap_pct": 0.6,
            "large_gap": True,
        },
        {
            "side": "upper",
            "gap_bottom_price": 0.085,
            "gap_pct": 0.2,
            "large_gap": False,
        },
    ]
    out = upper_gap_chain_above_ref(gaps, [], 0.084)
    assert out["upper_gaps_above_ref"] == 2
    assert out["upper_large_gaps_above_ref"] == 1


def test_fingerprint_on_minimal_bundle():
    b = {
        "case_id": "X",
        "spec": {"as_of_utc": "2026-01-01T00:00:00Z", "manual_entry_hint": 0.1},
        "reference_price": 0.1,
        "parity": {"ok": True},
        "depth2_intervals": [
            {"side": "upper", "bottom_price": 0.1001, "top_price": 0.1002, "depth": 2}
        ],
        "gaps_flat": [],
        "gaps_ranked": {"united_upper": [], "united_lower": []},
        "ema": {"status": "NOT_VERIFIED"},
    }
    fp = landscape_fingerprint(b)
    assert fp["upper_envelope"]["nearest_above"] is not None


def test_frozen_s5_file_exists_if_phase1c_ran():
    p = ROOT / "results/phase1c_depth2_manual_case_parity_v1/S5/landscape_profile.json"
    if not p.is_file():
        return
    data = json.loads(p.read_text())
    assert data["parity"]["ok"] is True
