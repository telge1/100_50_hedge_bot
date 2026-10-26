from __future__ import annotations

import copy
import json
from pathlib import Path

from config_validate_v1 import load_and_validate_config
from density_bridge_v1 import FixtureDensitySessionV1
from gap_detection_v1 import has_upper_large_gap_above_pool
from short_research_engine_v1 import run_short_research_backtest

TOOLS = Path(__file__).resolve().parents[1]
FIXTURE = TOOLS / "fixtures/synthetic_integration_v1.json"
CFG = TOOLS / "configs/synthetic_fixture_v1.json"


def test_gap_threshold_changes_gate():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    uppers = fix["snapshots"]["2026-01-01T12:05:00Z"]["upper_bands"]
    assert has_upper_large_gap_above_pool(uppers, 0.1, 0.5)
    assert not has_upper_large_gap_above_pool(uppers, 0.1, 50.0)


def test_gap_config_changes_signal_count():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = FixtureDensitySessionV1(fix["snapshots"])
    low = load_and_validate_config(CFG)
    high = copy.deepcopy(low)
    high["short_entry"]["upper_gap_min_pct"] = 50.0
    r_low = run_short_research_backtest(low, session, fix["prices"])
    r_high = run_short_research_backtest(high, session, fix["prices"])
    assert len(r_low["signals"]) > len(r_high["signals"])


def test_synthetic_pnl_fields():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cfg = load_and_validate_config(CFG)
    session = FixtureDensitySessionV1(fix["snapshots"])
    result = run_short_research_backtest(cfg, session, fix["prices"])
    pnl = result["pnl"]
    assert pnl is not None
    for key in (
        "net_pnl_usdt",
        "gross_pnl_usdt",
        "trading_fees_usdt",
        "ending_equity_usdt",
        "max_drawdown_pct",
        "profit_factor",
    ):
        assert key in pnl


def test_deterministic_repeat():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cfg = load_and_validate_config(CFG)
    session = FixtureDensitySessionV1(fix["snapshots"])
    a = run_short_research_backtest(cfg, session, fix["prices"])
    b = run_short_research_backtest(cfg, session, fix["prices"])
    assert a["signals"] == b["signals"]
    assert a["config_hash"] == b["config_hash"]
