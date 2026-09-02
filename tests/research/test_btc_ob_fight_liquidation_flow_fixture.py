"""Fixture parity and SUPERSEDED marker for explanatory audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.btc_ob_fight.liquidation_flow_contract import assert_canonical_input_allowed

ROOT = Path(__file__).resolve().parents[2]
RUN_017 = ROOT / "results" / "btc_ob_fight_cases" / "20260831T190000Z" / "run_017"
EXPL_OUT = ROOT / "results" / "btc_ob_fight_explanatory_audit_20260831_1900_v1"
SUPERSEDED = EXPL_OUT / "SUPERSEDED.json"


@pytest.mark.skipif(not RUN_017.is_dir(), reason="run_017 fixture missing")
def test_run_017_oi_liquidation_fixture_counts():
    oi = json.loads((RUN_017 / "oi_liquidation_facts.json").read_text())
    ls = oi["liquidation_summary"]
    assert oi["liquidation_count"] == 60
    assert ls["short_count"] == 59
    assert ls["long_count"] == 1


@pytest.mark.skipif(not SUPERSEDED.is_file(), reason="SUPERSEDED.json missing")
def test_explanatory_audit_superseded_marker():
    doc = json.loads(SUPERSEDED.read_text())
    assert doc["do_not_use_for_research"] is True
    assert doc["superseded_by"] == "liquidation_flow_facts_v1"
    assert "double counting" in doc["reason"]


def test_explanatory_audit_path_blocked_for_canonical():
    with pytest.raises(ValueError):
        assert_canonical_input_allowed(str(EXPL_OUT / "liquidation_trade_association_sensitivity.csv"))
