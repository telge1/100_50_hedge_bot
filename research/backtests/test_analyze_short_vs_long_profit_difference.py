"""Regression tests for short vs long profit difference analysis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.backtests.analyze_short_vs_long_profit_difference import (
    DEFAULT_SOURCE_DIR,
    SHORT_RECOVERY_PURPOSE,
    build_direction_neutrality_audit,
    build_effective_config_comparison,
    classify_exit_path,
    decompose_profit_difference,
    estimate_additional_short_trades_if_long_duration_distribution,
    run_full_analysis,
)
from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.independent_continuous_long_short_analysis import summarize_direction_runs

SOURCE_DIR = DEFAULT_SOURCE_DIR
OUTPUT_DIR = Path("research/backtests/results/short_vs_long_profit_difference_analysis_test")


@pytest.fixture(scope="module")
def analysis_summary() -> dict:
    if not (SOURCE_DIR / "long_continuous_results.json").is_file():
        pytest.skip("independent continuous results missing")
    return run_full_analysis(
        source_dir=SOURCE_DIR,
        output_dir=OUTPUT_DIR,
        skip_neutral_control=True,
    )


def test_effective_config_resolution_long_short():
    long_cfg = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT").config
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT").config
    assert long_cfg.strategy_class == "FixedCycleHedgeStrategy"
    assert short_cfg.strategy_class == "ShortFixedCycleHedgeStrategy"
    assert long_cfg.base_notional_usdt == 100.0
    assert short_cfg.base_notional_usdt == 50.0
    assert long_cfg.hedge_ratio_short == 0.5
    assert short_cfg.hedge_ratio_short == 2.0
    rows = build_effective_config_comparison()
    primary_rows = [r for r in rows if r["parameter"] == "primary_notional_usdt"]
    assert primary_rows
    assert primary_rows[0]["long_value"] == pytest.approx(100.0)
    assert primary_rows[0]["short_value"] == pytest.approx(100.0)


def test_direction_neutral_cycle_purpose_mirror():
    rows = build_direction_neutrality_audit()
    assert len(rows) == 4
    assert all(row["correct"] for row in rows)
    assert rows[3]["short_first_leg"] == SHORT_RECOVERY_PURPOSE


def test_classify_exit_path_initial_only():
    path = classify_exit_path(
        {"final_status": "closed", "recovery_activated": False},
        fills=[{"purpose": "INITIAL_LONG_ENTRY"}, {"purpose": "LONG_TP_EXIT"}],
    )
    assert path == "initial_exit_only"


def test_duration_counterfactual_estimate():
    long_runs = json.loads((SOURCE_DIR / "long_continuous_results.json").read_text())["runs"]
    short_runs = json.loads((SOURCE_DIR / "short_continuous_results.json").read_text())["runs"]
    estimate = estimate_additional_short_trades_if_long_duration_distribution(
        long_runs, short_runs, total_history_candles=52569
    )
    assert estimate["actual_short_trades"] == 117
    assert estimate["actual_long_trades"] == 226
    assert estimate["method_mean"]["additional_trades_vs_actual"] > 50


def test_pnl_decomposition_matches_known_totals(analysis_summary: dict):
    long_summary = analysis_summary["long_summary"]
    short_summary = analysis_summary["short_summary"]
    assert int(long_summary["trades_started"]) == 226
    assert int(short_summary["trades_started"]) == 117
    assert float(long_summary["gross_profit"]) == pytest.approx(70.37, rel=0.02)
    assert float(short_summary["gross_profit"]) == pytest.approx(24.28, rel=0.02)
    decomp = analysis_summary["profit_decomposition"]
    assert decomp["gross_profit_difference"] > 40.0


def test_longest_short_trade_analysis(analysis_summary: dict):
    longest = analysis_summary["longest_short_trade"]
    assert int(longest["trade_number"]) == 80
    assert int(longest["candles_processed"]) == 29089
    assert longest["recovery_reference_purpose_reached"] is False
    assert int(longest["max_cycle_stage_reached"]) == 1


def test_analysis_outputs_exist(analysis_summary: dict):
    output = Path(analysis_summary["output_dir"])
    required = [
        "effective_long_short_config_comparison.csv",
        "long_short_duration_distribution.csv",
        "long_short_longest_trades.csv",
        "long_short_trade_pnl_distribution.csv",
        "long_short_exit_path_comparison.csv",
        "longest_short_trade_analysis.json",
        "analysis_summary.json",
        "REPORT.md",
    ]
    for name in required:
        assert (output / name).is_file(), name


def test_existing_independent_results_unchanged():
    long_runs = json.loads((SOURCE_DIR / "long_continuous_results.json").read_text())["runs"]
    short_runs = json.loads((SOURCE_DIR / "short_continuous_results.json").read_text())["runs"]
    assert len(long_runs) == 226
    assert len(short_runs) == 117
    assert summarize_direction_runs(long_runs, direction="long")["net_realized_pnl"] == pytest.approx(29.60, rel=0.02)


def test_no_live_config_files_modified():
    live_paths = [
        Path("live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json"),
        Path("live_bots/short_hedge_bot/short_bot_1/config/fixed_cycle_config.json"),
    ]
    for path in live_paths:
        assert path.is_file()
