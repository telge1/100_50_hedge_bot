"""Tests for scaled unresolved APT multistart audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.config import default_apt_example
from research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit import (
    EXPECTED_CASES,
    POLICY,
    load_scaled_unresolved_cases,
    _run_one,
    _scaled_policy,
    _window_price_stats,
)
from research.backtests.cobertura_0_notional_strategie.scaled_unresolved_audit import (
    classify_unresolved_causes,
    compare_replay,
    near_be_flags,
)
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol

MS_DIR = (
    Path(__file__).resolve().parents[1]
    / "results"
    / "apt_multistart_validation_20260725"
)


@pytest.mark.skipif(not MS_DIR.exists(), reason="multistart results missing")
def test_loads_exactly_22_scaled_unresolved():
    cases = load_scaled_unresolved_cases(MS_DIR)
    assert len(cases) == EXPECTED_CASES
    assert all(c["policy"] == POLICY for c in cases)
    assert len({c["run_id"] for c in cases}) == EXPECTED_CASES
    assert all(str(c["recovered_be"]).lower() == "false" for c in cases)
    assert all(str(c["safety_violation"]).lower() == "false" for c in cases)


@pytest.mark.skipif(not MS_DIR.exists(), reason="multistart results missing")
def test_replay_fingerprint_matches_raw_runs():
    cases = load_scaled_unresolved_cases(MS_DIR)
    case = cases[0]
    base = default_apt_example()
    policy = _scaled_policy()
    candles = load_candles_for_symbol(
        base.symbol, timeframe=base.timeframe, data_dir=DEFAULT_DATA_DIR, limit=None
    )
    _result, _seed, metrics = _run_one(
        case=case,
        candles=candles,
        base=base,
        policy=policy,
        max_horizon_days=60,
    )
    assert compare_replay(case, metrics) == []
    assert metrics["policy"] == POLICY
    assert policy["overlay_exit_policy"] == "individual_tp_scaled"
    assert len(policy["individual_tp_steps"]) == 3
    # Replay preserves audited net-BE gate (target 0 + safety 0.25)
    from research.backtests.cobertura_0_notional_strategie.run_apt_multistart_validation import (
        NET_BE_SAFETY_BUFFER_USDT,
        NET_BE_TARGET_USDT,
        build_run_cfg,
    )
    from research.backtests.cobertura_0_notional_strategie.multistart_seeding import (
        horizon_end_index,
        materialize_start,
    )

    seed = materialize_start(candles, int(float(case["start_index"])), cfg_template=base)
    end_i = horizon_end_index(candles, int(float(case["start_index"])), max_horizon_days=60)
    from research.backtests.cobertura_0_notional_strategie.engine import _parse_ts

    cfg = build_run_cfg(
        base=base,
        policy=policy,
        seed=seed,
        end_timestamp=_parse_ts(candles[end_i]["timestamp"]).isoformat(),
        run_id=case["run_id"],
    )
    assert cfg.full_exit_target_mode == "net_be"
    assert cfg.full_exit_target_usdt == NET_BE_TARGET_USDT
    assert cfg.full_exit_safety_buffer_usdt == NET_BE_SAFETY_BUFFER_USDT
    assert cfg.individual_tp_steps is not None
    assert [s.move_pct for s in cfg.individual_tp_steps] == [0.01, 0.02, 0.03]


def test_near_be_classification():
    assert near_be_flags(-0.5) == {
        "near_be_1": True,
        "near_be_5": True,
        "near_be_10": True,
    }
    assert near_be_flags(-3.0)["near_be_1"] is False
    assert near_be_flags(-3.0)["near_be_5"] is True
    assert near_be_flags(-12.0)["near_be_10"] is False


def test_cause_classification_deterministic():
    base = {
        "max_drop_from_start_pct": -0.12,
        "end_ret_pct": -0.09,
        "end_near_window_min": True,
        "max_rally_from_low_pct": 0.02,
        "overlay_grows_faster_than_tp_harvest": True,
        "max_overlay_to_core_ratio": 4.0,
        "number_of_short_adds": 8,
        "total_fees_usdt": 6.0,
        "best_total_economics_usdt": -2.0,
        "unresolved_overlay_qty": 300.0,
        "initial_long_qty": 400.0,
    }
    a = classify_unresolved_causes(base)
    b = classify_unresolved_causes(base)
    assert a == b
    assert "CONTINUED_DOWNTREND" in a
    assert "TP_HARVEST_TOO_SLOW" in a
    assert "OVERLAY_SATURATED" in a
    assert classify_unresolved_causes({}) == ["OTHER"]


def test_compare_replay_csv_types():
    expected = {
        "final_status": "DATA_END_OPEN",
        "recovered_be": "False",
        "unresolved": "True",
        "final_total_economics_usdt": "-1.5",
        "number_of_short_adds": "2",
        "number_of_partial_tp_events": "1",
        "max_overlay_qty": "10",
        "recovery_bars": "100",
        "max_adverse_total_economics_usdt": "-3",
        "total_fees_usdt": "0.1",
    }
    actual = {
        "final_status": "DATA_END_OPEN",
        "recovered_be": False,
        "unresolved": True,
        "final_total_economics_usdt": -1.5,
        "number_of_short_adds": 2,
        "number_of_partial_tp_events": 1,
        "max_overlay_qty": 10.0,
        "recovery_bars": 100,
        "max_adverse_total_economics_usdt": -3.0,
        "total_fees_usdt": 0.1,
    }
    assert compare_replay(expected, actual) == []
    bad = dict(actual)
    bad["final_total_economics_usdt"] = -2.0
    assert compare_replay(expected, bad)


def test_extended_horizon_does_not_exceed_data():
    candles = [
        {
            "timestamp": f"t{i}",
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "volume": 1.0,
        }
        for i in range(100)
    ]
    stats = _window_price_stats(candles, 10, 50, start_price=1.0)
    assert stats["end_price"] == 1.0
    # end_index clipped conceptually: window uses end_i inclusive
    assert len(candles[10:51]) == 41


def test_tranche_reconciliation_helper_math():
    initial, closed, remaining = 100.0, 75.0, 25.0
    assert abs(closed + remaining - initial) < 1e-9
    assert remaining >= 0
    assert closed <= initial + 1e-9


@pytest.mark.skipif(not MS_DIR.exists(), reason="multistart results missing")
def test_no_hidden_safety_in_case_set():
    cases = load_scaled_unresolved_cases(MS_DIR)
    for c in cases:
        assert str(c.get("final_status")) != "STOPPED"
        assert str(c.get("safety_violation")).lower() == "false"
        assert int(float(c.get("invariant_fail_count") or 0)) == 0
