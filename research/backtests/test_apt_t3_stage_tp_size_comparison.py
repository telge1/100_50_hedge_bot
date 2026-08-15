"""Tests for APTUSDT T3 stage-TP size comparison audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.backtests.apt_t3_stage_tp_size_comparison import (
    APT_TRADE3_START_INDEX,
    MIN_NOTIONAL_USDT,
    PROTECTED,
    assert_output_dir_safe,
    classify_cycle4_split_outcome,
    parse_sizes,
    run_apt_t3_at_size,
    stage_attempt_rows_for_cycle,
)
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.long_add_multistart_metrics import analyze_trade
from research.backtests.apt_baseline_blocker_root_cause import check_baseline_parity
from research.backtests.run_apt_t3_stage_tp_size_comparison import run_audit


@pytest.fixture(scope="module")
def apt_candles():
    return normalize_candles("APTUSDT", load_candles_for_symbol("APTUSDT", limit=50000))


def test_parse_sizes() -> None:
    sizes = parse_sizes("100:50,1000:500")
    assert sizes[0] == ("S100", 100.0, 50.0)
    assert sizes[1] == ("S1000", 1000.0, 500.0)


def test_protected_dirs() -> None:
    assert any("current_baseline" in str(p) for p in PROTECTED)


def test_assert_refuses_protected() -> None:
    baseline = next(p for p in PROTECTED if "current_baseline" in str(p))
    with pytest.raises(RuntimeError, match="protected"):
        assert_output_dir_safe(baseline)


def test_s100_parity(apt_candles) -> None:
    result = run_apt_t3_at_size(
        candles=apt_candles, start_index=APT_TRADE3_START_INDEX, base_notional_usdt=100.0
    )
    analysis = analyze_trade(
        result,
        variant="S100",
        long_add_pct=0.5,
        target_profit_usdt=0.015,
        window_candles=apt_candles[APT_TRADE3_START_INDEX:],
        valid=True,
        skip_reason="ok",
    )
    parity = check_baseline_parity(coin="APTUSDT", trade_id=3, result=result, analysis=analysis)
    assert parity["ok"] is True


def test_classify_rejected_attempt() -> None:
    result = BacktestResult(symbol="APTUSDT", direction="long")
    result.intent_log = [
        {
            "purpose": "CYCLE_4_SHORT_REDUCE",
            "qty": 3.0,
            "trigger_price": 1.66,
            "metadata_excerpt": {
                "fallback_to_single_second_leg": True,
                "split_fallback_reason": "stage_below_min_notional",
            },
        }
    ]
    attempts = [
        {
            "cycle": 4,
            "accepted": 0,
            "rejected": 1,
            "rejection_reason": "stage_below_min_notional",
            "full_second_leg_fallback_used": 1,
        }
    ]
    out = classify_cycle4_split_outcome(attempts, result)
    assert out["outcome"] == "attempted_rejected_min_notional"


def test_min_notional_constant() -> None:
    assert MIN_NOTIONAL_USDT == 5.0


def test_full_runner(tmp_path: Path, apt_candles) -> None:
    out = tmp_path / "apt_t3_stage"
    payload = run_audit(
        coin="APTUSDT",
        trade_id=3,
        sizes_spec="100:50,1000:500",
        output_dir=out,
        candle_limit=50000,
        start_index=APT_TRADE3_START_INDEX,
    )
    assert payload["ok"] is True
    required = [
        "REPORT.md",
        "code_path_map.md",
        "size_comparison_summary.csv",
        "cycle4_stage_attempts.csv",
        "cycle4_stage_fills.csv",
        "exit_after_each_stage.csv",
        "coverage_after_each_stage.csv",
        "event_timeline_100_50.csv",
        "event_timeline_1000_500.csv",
        "cycle_snapshots_100_50.csv",
        "cycle_snapshots_1000_500.csv",
        "bounce_reachability_comparison.json",
        "parity_and_guards.json",
    ]
    for name in required:
        assert (out / name).exists(), name
    guards = json.loads((out / "parity_and_guards.json").read_text())
    assert guards["s100_parity_ok"] is True
    assert guards["invalid_partial_all_zero"] is True
