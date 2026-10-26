"""BT-D23 setup scanner tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D23 = ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1"
for p in (str(BT_D6), str(BT_D7), str(BT_D23)):
    if p not in sys.path:
        sys.path.insert(0, p)

from chart_adapter_v1 import setups_document_to_overlay_bundle  # noqa: E402
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from setup_dedup_v1 import SetupDedupTrackerV1, SetupFingerprint  # noqa: E402
from setup_scanner_v1 import measure_upper_gap_pct, scan_pool_first_setups  # noqa: E402

PHASE4D = ROOT / "tools" / "phase4d_pool_landscape_backtester_v2"
if str(PHASE4D) not in sys.path:
    sys.path.insert(0, str(PHASE4D))
from strategy_rules import short_sl  # noqa: E402


def _minimal_config(**short_entry_overrides):
    se = {
        "entry_pool_selection_mode": "pool_first_structural_v1",
        "entry_price_reference": "upper_pool_lower_edge",
        "entry_offset_pct": 0.0,
        "pool_min_age_bars": 0,
        "upper_gap_min_pct": 0.5,
        "upper_gap_measurement_mode": "adjacent_union_interval_gap_pct_v1",
        "upper_pool_proximity_pct": 0.05,
        "pending_limit_enabled": True,
        "pending_limit_expiry_bars": None,
        "pending_limit_fill_model": "causal_sell_limit_v2",
        "pool_cancel_identity_mode": "origin_set_v1",
    }
    se.update(short_entry_overrides)
    return {
        "config_version": "bt_d6_strategy_backtest_config_v1",
        "strategy_identity": {
            "strategy_id": "pool_landscape_v1_candidate",
            "strategy_version": "bt_d23_test",
            "frozen_source_reference": "strategy_freeze/pool_landscape_v1_candidate/SOURCE_PROVENANCE.json",
            "research_mode": "depth2_density_research_v1",
        },
        "density": {
            "timeframe": "5m",
            "pool_detail": 1,
            "method": "exact_unique_origin",
            "min_unique_origins": 2,
            "density_source": "fixture",
        },
        "short_entry": se,
        "risk": {"sl_above_upper_top_pct": 0.2, "max_concurrent_positions": 1},
        "take_profit": {
            "selection_mode": "next_eligible_band",
            "min_distance_pct": 0.5,
            "distance_reference": "actual_fill_to_lower_upper_edge",
            "dense_cluster_semantics_approved": False,
            "tp_pool_quality_gate_enabled": False,
        },
        "pnl": {
            "initial_equity_usdt": 10000.0,
            "sizing_mode": "fixed_notional_usdt",
            "position_notional_usdt": 1000.0,
            "risk_fraction_pct": None,
            "leverage": 1.0,
            "trading_fee_model": {"model": "roundtrip_pct_of_notional", "roundtrip_pct": 0.11},
            "slippage_model": {"model": "none", "entry_slippage_pct": 0.0, "exit_slippage_pct": 0.0},
            "funding_model": {"model": "none", "funding_rate_bps_per_8h": 0.0},
            "intrabar_ambiguity_policy": "no_trade_ambiguous",
            "end_of_test_open_position_policy": "exclude_from_realized_pnl",
        },
        "backtest": {
            "symbol": "DOGEUSDT",
            "symbols": ["DOGEUSDT"],
            "start_utc": "2026-09-11T12:00:00Z",
            "end_utc": "2026-09-11T12:15:00Z",
            "end_utc_exclusive": True,
            "execution_timeframe": "5m",
            "outcome_blind_signals": True,
            "deterministic_seed": 1,
        },
    }


def _fixture_snapshots():
    """Two bars, one upper pool; second bar repeats same band (dedup)."""
    band = {
        "lower_price": 0.084,
        "upper_price": 0.0841,
        "side": "upper",
        "origin_ids": "o1|o2",
    }
    band_high = {
        "lower_price": 0.085,
        "upper_price": 0.0852,
        "side": "upper",
        "origin_ids": "o3|o4",
    }
    lower_tp = {
        "lower_price": 0.082,
        "upper_price": 0.0825,
        "side": "lower",
        "origin_ids": "l1|l2",
    }
    snap = {
        "pipeline_line_id": "fixture",
        "config_hash": "abc",
        "upper_bands": [band, band_high],
        "lower_bands": [lower_tp],
    }
    return {
        "2026-09-11T12:00:00Z": snap,
        "2026-09-11T12:05:00Z": snap,
        "2026-09-11T12:10:00Z": snap,
    }


@pytest.fixture
def patch_scanner(monkeypatch):
    from density_bridge_v1 import FixtureDensitySessionV1

    snaps = _fixture_snapshots()
    prices = {
        t: {"o": 0.083, "h": 0.0835, "l": 0.0829, "c": 0.083}
        for t in snaps
    }

    def fake_session(cfg):
        return FixtureDensitySessionV1(snaps)

    monkeypatch.setattr("setup_scanner_v1.create_density_session", fake_session)
    monkeypatch.setattr("setup_scanner_v1.load_prices_from_ssot_window", lambda *a, **k: prices)


def test_dedup_suppresses_unchanged_repeat():
    tr = SetupDedupTrackerV1()
    fp = SetupFingerprint(0.084, 0.0841)
    d1 = tr.evaluate("o1|o2", fp)
    assert d1.emit and d1.reason == "FIRST_STRUCTURAL_ELIGIBILITY"
    tr.commit("o1|o2", fp)
    d2 = tr.evaluate("o1|o2", fp)
    assert not d2.emit and d2.reason == "UNCHANGED_ORIGIN_SET"


def test_entry_lower_edge_and_offset():
    se = {"entry_price_reference": "upper_pool_lower_edge", "entry_offset_pct": 0.1}
    assert compute_short_entry_level(0.1, 0.101, se) == pytest.approx(0.0999)
    sl = short_sl(0.101, 0.2)
    assert sl == pytest.approx(0.101 * 1.002)


def test_scanner_fixture_no_pnl_fields(patch_scanner):
    cfg = _minimal_config()
    cfg["density"] = {
        "timeframe": "5m",
        "pool_detail": 1,
        "method": "exact_unique_origin",
        "min_unique_origins": 2,
        "density_source": "validated_cache",
        "validated_cache": {"bundle_path": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"},
    }
    result = scan_pool_first_setups(cfg, run_id="test_fixture")
    assert len(result.setups) >= 1
    for s in result.setups:
        assert s["status"] == "PLANNED"
        assert "pnl" not in s
        assert "fill" not in json.dumps(s).lower() or "fill" not in s
    assert len(result.setups) >= 1
    assert any(e.get("event") == "DEDUP_SUPPRESSED" for e in result.suppressed_events)
    doc = {"setups": result.setups, "run_id": "t", "symbol": "DOGEUSDT", "config_hash": "x"}
    bundle = setups_document_to_overlay_bundle(doc)
    assert bundle["records"][0]["metadata"]["not_executed"] is True


def test_chart_adapter_planned_label():
    doc = {
        "run_id": "r",
        "symbol": "DOGEUSDT",
        "config_hash": "h",
        "setups": [
            {
                "setup_id": "s1",
                "decision_utc": "2026-09-11T12:00:00Z",
                "planned_entry": 1.0,
                "planned_sl": 1.1,
                "planned_tp": 0.9,
            }
        ],
    }
    bundle = setups_document_to_overlay_bundle(doc)
    assert bundle["records"][0]["status_label"] == "PLANNED SHORT — NOT EXECUTED"
