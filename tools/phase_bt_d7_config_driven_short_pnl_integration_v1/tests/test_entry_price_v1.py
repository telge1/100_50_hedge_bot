from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from config_validate_v1 import ConfigValidationError, load_and_validate_config, validate_config_dict
from entry_price_v1 import compute_short_entry_level
from density_bridge_v1 import FixtureDensitySessionV1
from short_research_engine_v1 import run_short_research_backtest

TOOLS = Path(__file__).resolve().parents[1]
FIXTURE = TOOLS / "fixtures/synthetic_integration_v1.json"
CFG = TOOLS / "configs/synthetic_fixture_v1.json"


def test_offset_formula_example():
    se = {"entry_price_reference": "upper_pool_lower_edge", "entry_offset_pct": 0.1}
    assert compute_short_entry_level(0.086810, 0.087, se) == pytest.approx(0.08672319)


def test_upper_edge_reference():
    se = {"entry_price_reference": "upper_pool_upper_edge"}
    assert compute_short_entry_level(0.0995, 0.1, se) == 0.1


def test_baseline_lower_edge_zero_offset():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    base = load_and_validate_config(CFG)
    upper = copy.deepcopy(base)
    upper["short_entry"]["entry_price_reference"] = "upper_pool_upper_edge"
    off = copy.deepcopy(base)
    off["short_entry"]["entry_price_reference"] = "upper_pool_lower_edge"
    off["short_entry"]["entry_offset_pct"] = 0.1

    session = FixtureDensitySessionV1(fix["snapshots"])
    r_base = run_short_research_backtest(base, session, fix["prices"])
    r_upper = run_short_research_backtest(upper, session, fix["prices"])
    r_off = run_short_research_backtest(off, session, fix["prices"])
    assert r_base["signals"]
    s0 = r_base["signals"][0]
    assert s0["entry_level"] == s0["pool_lower"]
    assert r_upper["signals"][0]["entry_level"] == r_upper["signals"][0]["pool_upper"]
    assert r_off["signals"][0]["entry_level"] < r_off["signals"][0]["pool_lower"]
    assert r_off["signals"][0]["signal_snapshot"]["entry_price_planned"] == r_off["signals"][0]["entry_level"]


def test_upper_edge_with_offset_fails_validation():
    c = json.loads(CFG.read_text(encoding="utf-8"))
    c["short_entry"]["entry_price_reference"] = "upper_pool_upper_edge"
    c["short_entry"]["entry_offset_pct"] = 0.1
    with pytest.raises(ConfigValidationError) as e:
        validate_config_dict(c)
    assert e.value.code == "ENTRY_OFFSET_REFERENCE_MISMATCH"


def test_marketable_sell_limit_legacy_vs_causal():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prices = dict(fix["prices"])
    prices["2026-01-01T12:10:00Z"] = {"o": 0.102, "h": 0.103, "l": 0.101, "c": 0.102}
    cfg = load_and_validate_config(CFG)
    cfg["short_entry"]["entry_price_reference"] = "upper_pool_lower_edge"
    session = FixtureDensitySessionV1(fix["snapshots"])
    legacy = copy.deepcopy(cfg)
    legacy["short_entry"]["pending_limit_fill_model"] = "legacy_v1"
    causal = copy.deepcopy(cfg)
    causal["short_entry"]["pending_limit_fill_model"] = "causal_sell_limit_v2"
    r_leg = run_short_research_backtest(legacy, session, prices)
    r_cau = run_short_research_backtest(causal, session, prices)
    assert r_leg["signals"]
    assert not r_leg["trades"]
    assert r_cau["trades"]
