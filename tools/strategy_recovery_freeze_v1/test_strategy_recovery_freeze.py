"""Checks for pool landscape freeze candidate artifacts."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAND = ROOT / "strategy_freeze" / "pool_landscape_v1_candidate"

REQUIRED = [
    "STRATEGY_SPEC_CANDIDATE.md",
    "USER_CONFIRMED_RULES.md",
    "MANUAL_REFERENCE_CASES.md",
    "OPEN_STRATEGY_DECISIONS.md",
    "IMPLEMENTATION_DEVIATIONS.md",
    "BACKTESTER_INTEGRATION_CONTRACT.md",
    "RESEARCH_PARAMETERS.json",
    "SOURCE_PROVENANCE.json",
]


def test_candidate_files_exist():
    for name in REQUIRED:
        assert (CAND / name).is_file(), name


def test_not_final_approved():
    spec = (CAND / "STRATEGY_SPEC_CANDIDATE.md").read_text(encoding="utf-8")
    assert "FINAL_APPROVED" not in spec.split("approval")[1][:200] or "NOT" in spec
    prov = json.loads((CAND / "SOURCE_PROVENANCE.json").read_text())
    assert prov["status"] == "NOT_FINAL_APPROVED"


def test_lmv2_not_in_strategy_lineage():
    prov = json.loads((CAND / "SOURCE_PROVENANCE.json").read_text())
    paths = [s["path"] for s in prov["sources"]]
    assert not any("historical_lmv2/strategy_loop" in p for p in paths)


def test_c003_label_preserved():
    manual = (CAND / "MANUAL_REFERENCE_CASES.md").read_text(encoding="utf-8")
    assert "MANUALLY_CONFIRMED_SHORT" in manual
    assert "do not" in manual.lower() or "do **not**" in manual


def test_min_age_not_hard_confirmed():
    params = json.loads((CAND / "RESEARCH_PARAMETERS.json").read_text())
    assert params["parameters"]["min_pool_age_bars"]["hard_entry_gate_confirmed"] is False
