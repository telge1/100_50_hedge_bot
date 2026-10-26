"""BT-D26 dynamic selector tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D23 = ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1"
BT_D26 = ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1"
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
for p in (str(BT_D6), str(BT_D7), str(BT_D23), str(BT_D26), str(PHASE4D)):
    if p not in sys.path:
        sys.path.insert(0, p)

from candidate_identity_v1 import resolve_tracked_upper_band  # noqa: E402
from candidate_state_v1 import (  # noqa: E402
    STATE_CANDIDATE_SELECTED,
    STATE_CANDIDATE_STILL_VALID,
    STATE_NEXT_CANDIDATE_SELECTED,
    DynamicCandidateStateV1,
)
from chart_adapter_v1 import setups_document_to_overlay_bundle  # noqa: E402
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from dynamic_selector_v1 import scan_dynamic_upper_pool_setups  # noqa: E402
from gap_detection_v1 import has_upper_large_gap_above_pool  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper  # noqa: E402


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
            "strategy_version": "bt_d26_test",
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
    band_low = {
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
        "upper_bands": [band_low, band_high],
        "lower_bands": [lower_tp],
    }
    return {
        "2026-09-11T12:00:00Z": snap,
        "2026-09-11T12:05:00Z": snap,
        "2026-09-11T12:10:00Z": snap,
    }


@pytest.fixture
def patch_selector(monkeypatch):
    from density_bridge_v1 import FixtureDensitySessionV1

    snaps = _fixture_snapshots()
    prices = {t: {"o": 0.083, "h": 0.0835, "l": 0.0829, "c": 0.083} for t in snaps}

    monkeypatch.setattr("dynamic_selector_v1.create_density_session", lambda cfg: FixtureDensitySessionV1(snaps))
    monkeypatch.setattr("dynamic_selector_v1.load_prices_from_ssot_window", lambda *a, **k: prices)


def test_at_most_one_setup_emit_per_bar(patch_selector):
    cfg = _minimal_config()
    cfg["density"] = {
        "timeframe": "5m",
        "pool_detail": 1,
        "method": "exact_unique_origin",
        "min_unique_origins": 2,
        "density_source": "validated_cache",
        "validated_cache": {"bundle_path": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"},
    }
    result = scan_dynamic_upper_pool_setups(cfg, run_id="t_one_per_bar")
    by_bar: dict[str, int] = {}
    for s in result.setups:
        by_bar[s["decision_utc"]] = by_bar.get(s["decision_utc"], 0) + 1
    assert all(c <= 1 for c in by_bar.values())
    assert len(result.setups) == 1
    assert result.setups[0]["pool_lower"] == 0.084


def test_still_valid_suppresses_repeat(patch_selector):
    cfg = _minimal_config()
    cfg["density"] = {
        "timeframe": "5m",
        "pool_detail": 1,
        "method": "exact_unique_origin",
        "min_unique_origins": 2,
        "density_source": "validated_cache",
        "validated_cache": {"bundle_path": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"},
    }
    result = scan_dynamic_upper_pool_setups(cfg, run_id="t_still")
    states = [t["state"] for t in result.candidate_transitions]
    assert STATE_CANDIDATE_SELECTED in states
    assert states.count(STATE_CANDIDATE_STILL_VALID) >= 2


def test_gap_gate_used():
    uppers = [
        {"lower_price": 0.1, "upper_price": 0.101, "side": "upper", "origin_ids": "a|b"},
        {"lower_price": 0.105, "upper_price": 0.106, "side": "upper", "origin_ids": "c|d"},
    ]
    assert has_upper_large_gap_above_pool(uppers, 0.101, 0.5) is True


def test_identity_origin_set_merge():
    cfg = _minimal_config()
    snap = {
        "upper_bands": [
            {"lower_price": 0.084, "upper_price": 0.0842, "origin_ids": "o1|o2|o3"},
        ]
    }
    res = resolve_tracked_upper_band(
        cfg,
        snap,
        pool_lo=0.084,
        pool_hi=0.0841,
        origin_ids="o1|o2",
    )
    assert res.ok
    assert res.band is not None
    assert res.band.pool_upper == 0.0842


def test_chart_overlay_compatible():
    doc = {
        "contract_version": "bt_d26_dynamic_upper_pool_selector_v1",
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
                "selection_reason": "FIRST",
            }
        ],
    }
    bundle = setups_document_to_overlay_bundle(doc)
    assert bundle["records"][0]["metadata"]["not_executed"] is True


def test_entry_offset_via_normalized_config():
    import sys
    from pathlib import Path

    bt6 = Path(__file__).resolve().parents[3] / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
    if str(bt6) not in sys.path:
        sys.path.insert(0, str(bt6))
    from config_yaml_v1 import load_yaml_and_validate

    cfg = load_yaml_and_validate(
        Path(__file__).resolve().parents[3] / "configs/pilot_bt_d26_entry_offset_0p1_24h.yaml"
    )
    se = cfg["short_entry"]
    assert se["entry_price_reference"] == "upper_pool_lower_edge"
    assert se["entry_offset_pct"] == 0.1
    entry = compute_short_entry_level(0.08681, 0.086838, se)
    assert entry == pytest.approx(0.08681 * 0.999)


def test_state_machine_switch_lower_pool():
    cfg = _minimal_config()
    sm = DynamicCandidateStateV1()
    from candidate_identity_v1 import ResolvedUpperBand

    a = ResolvedUpperBand(0.085, 0.0852, "o3|o4", "o3|o4", "0.085|0.0852")
    b = ResolvedUpperBand(0.084, 0.0841, "o1|o2", "o1|o2", "0.084|0.0841")
    snap = {"upper_bands": []}
    t1 = sm.process_bar(cfg, decision_utc="t1", snapshot=snap, selected=a)
    assert t1.state == STATE_CANDIDATE_SELECTED
    t2 = sm.process_bar(cfg, decision_utc="t2", snapshot=snap, selected=b)
    assert t2.state == STATE_NEXT_CANDIDATE_SELECTED
    assert t2.emit_setup
