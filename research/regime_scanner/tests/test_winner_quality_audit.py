"""Tests for Rule-D winner quality audit."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.winner_quality_audit import (
    build_group_comparison,
    build_summary,
    cycle_class,
    estimate_refills,
    extract_winner_quality_row,
    rule_d_group,
    run_winner_quality_audit,
    speed_class,
    summarize_coverage_cycles,
    write_outputs,
)


def test_rule_d_group_split() -> None:
    assert rule_d_group("transition") == "rule_d_blocked_winner"
    assert rule_d_group("bullish_trend_with_trend_weakness") == "rule_d_blocked_winner"
    assert rule_d_group("bearish_trend_with_trend_weakness") == "rule_d_allowed_winner"
    assert rule_d_group("neutral") == "rule_d_allowed_winner"


def test_speed_and_cycle_classes() -> None:
    assert speed_class(12) == "sehr_schnell"
    assert speed_class(13) == "schnell"
    assert speed_class(48) == "schnell"
    assert speed_class(49) == "mittel"
    assert speed_class(144) == "mittel"
    assert speed_class(145) == "langsam"
    assert speed_class(576) == "langsam"
    assert speed_class(577) == "sehr_langsam"
    assert cycle_class(0) == "0_cycles_direct_tp"
    assert cycle_class(1) == "1_cycle"
    assert cycle_class(3) == "3_cycles"
    assert cycle_class(4) == "4_plus_cycles"


def test_summarize_coverage_ignores_duplicate_final_exit_row() -> None:
    rows = [
        {
            "cycle_index": 1,
            "loss_purpose": "CYCLE_1_LONG_ADD",
            "cover_purpose": "CYCLE_1_SHORT_REDUCE",
            "loss_pnl": -0.1,
            "cover_pnl": 0.12,
            "loss_fill_timestamp": "2026-01-01T00:00:00+00:00",
            "cover_fill_timestamp": "2026-01-01T01:00:00+00:00",
        },
        {
            "cycle_index": 1,
            "loss_purpose": "SHORT_SL_EXIT",
            "cover_purpose": "CYCLE_1_SHORT_REDUCE",
            "loss_pnl": -0.5,
            "cover_pnl": 0.8,
            "loss_fill_timestamp": "2026-01-01T02:00:00+00:00",
            "cover_fill_timestamp": "2026-01-01T01:00:00+00:00",
        },
    ]
    summary = summarize_coverage_cycles(rows)
    assert summary["highest_cycle"] == 1
    assert summary["cycle_first_legs_filled"] == 1
    assert summary["cycle_second_legs_filled"] == 1
    assert summary["cycle_fills_total"] == 2


def test_estimate_refills() -> None:
    executed, count = estimate_refills(
        fills_count=4, cycle_first_legs=0, cycle_second_legs=0
    )
    assert executed is False
    assert count == 0
    executed, count = estimate_refills(
        fills_count=10, cycle_first_legs=1, cycle_second_legs=1
    )
    assert executed is True
    assert count >= 1


def test_extract_direct_tp_quality_row() -> None:
    regime_row = {
        "trade_id": "backtest_long_continuous_trade_0004",
        "combined_regime": "neutral",
        "start_index": 567,
    }
    run = {
        "trade_block_id": "backtest_long_continuous_trade_0004",
        "start_index": 567,
        "input_slice_start_index": 0,
        "start_time": "2026-01-06T22:00:00+00:00",
        "end_time": "2026-01-06T22:05:00+00:00",
        "candles_processed": 2,
        "overall_pnl": 0.25,
        "fills_count": 4,
        "max_drawdown_pct": 0.0,
        "base_notional_usdt": 100.0,
        "recovery_activated": False,
        "final_status": "closed",
        "exit_quality": "closed_ok",
        "last_fill": {"purpose": "SHORT_SL_EXIT"},
        "recovery_diagnostic_events": [],
    }
    row = extract_winner_quality_row(
        regime_row=regime_row,
        run=run,
        coverage={"audit_rows": []},
    )
    assert row["rule_d_group"] == "rule_d_allowed_winner"
    assert row["highest_cycle"] == 0
    assert row["closed_via_normal_tp"] is True
    assert row["speed_class"] == "sehr_schnell"
    assert row["cycle_class"] == "0_cycles_direct_tp"
    assert row["pnl_per_hour"] == pytest.approx(0.25 / (2 * 5 / 60))


def test_group_comparison_and_summary_stats() -> None:
    rows = [
        {
            "trade_id": "b1",
            "rule_d_group": "rule_d_blocked_winner",
            "combined_regime": "transition",
            "duration_candles": 200,
            "duration_hours": 200 * 5 / 60,
            "pnl": 0.2,
            "pnl_per_hour": 0.01,
            "highest_cycle": 4,
            "cycle_fills_total": 7,
            "refill_count": 0,
            "maximum_adverse_excursion": -0.4,
            "maximum_favorable_excursion": 0.3,
            "largest_unrealized_loss": -0.4,
            "speed_class": "langsam",
            "cycle_class": "4_plus_cycles",
            "closed_via_normal_tp": False,
            "multiple_cycles_required": True,
            "recovery_or_reload_active": False,
            "undesirable_slow_or_3plus_cycles": True,
        },
        {
            "trade_id": "a1",
            "rule_d_group": "rule_d_allowed_winner",
            "combined_regime": "neutral",
            "duration_candles": 2,
            "duration_hours": 2 * 5 / 60,
            "pnl": 0.3,
            "pnl_per_hour": 1.8,
            "highest_cycle": 0,
            "cycle_fills_total": 0,
            "refill_count": 0,
            "maximum_adverse_excursion": 0.0,
            "maximum_favorable_excursion": 0.3,
            "largest_unrealized_loss": 0.0,
            "speed_class": "sehr_schnell",
            "cycle_class": "0_cycles_direct_tp",
            "closed_via_normal_tp": True,
            "multiple_cycles_required": False,
            "recovery_or_reload_active": False,
            "undesirable_slow_or_3plus_cycles": False,
        },
    ]
    comparison = build_group_comparison(rows)
    assert any(
        r["rule_d_group"] == "rule_d_blocked_winner" and r["metric"] == "duration_candles"
        for r in comparison
    )
    summary = build_summary(rows)
    assert summary["blocked_count"] == 1
    assert summary["allowed_count"] == 1
    assert summary["answers"]["blocked_slower_than_allowed"] is True
    assert summary["answers"]["allowed_mostly_fast_direct_tp"] is True


def test_run_winner_quality_audit_synth(tmp_path: Path) -> None:
    regime_csv = tmp_path / "regime.csv"
    pd.DataFrame(
        [
            {
                "trade_id": "backtest_long_continuous_trade_0001",
                "combined_regime": "transition",
                "pnl": 0.2,
                "start_index": 10,
            },
            {
                "trade_id": "backtest_long_continuous_trade_0002",
                "combined_regime": "neutral",
                "pnl": 0.3,
                "start_index": 20,
            },
        ]
    ).to_csv(regime_csv, index=False)

    result_file = tmp_path / "results.json"
    result_file.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "trade_block_id": "backtest_long_continuous_trade_0001",
                        "final_status": "closed",
                        "overall_pnl": 0.2,
                        "start_index": 10,
                        "input_slice_start_index": 0,
                        "start_time": "2026-01-01T00:00:00+00:00",
                        "end_time": "2026-01-01T10:00:00+00:00",
                        "candles_processed": 120,
                        "fills_count": 8,
                        "max_drawdown_pct": 0.1,
                        "base_notional_usdt": 100.0,
                        "recovery_activated": False,
                        "exit_quality": "closed_ok",
                        "last_fill": {"purpose": "SHORT_SL_EXIT"},
                        "recovery_diagnostic_events": [],
                    },
                    {
                        "trade_block_id": "backtest_long_continuous_trade_0002",
                        "final_status": "closed",
                        "overall_pnl": 0.3,
                        "start_index": 20,
                        "input_slice_start_index": 0,
                        "start_time": "2026-01-02T00:00:00+00:00",
                        "end_time": "2026-01-02T00:10:00+00:00",
                        "candles_processed": 2,
                        "fills_count": 4,
                        "max_drawdown_pct": 0.0,
                        "base_notional_usdt": 100.0,
                        "recovery_activated": False,
                        "exit_quality": "closed_ok",
                        "last_fill": {"purpose": "SHORT_SL_EXIT"},
                        "recovery_diagnostic_events": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    coverage_dir = tmp_path / "coverage"
    coverage_dir.mkdir()
    (coverage_dir / "APTUSDT_long_continuous_trade_0001_conservative_live_pnl_coverage_audit.json").write_text(
        json.dumps(
            {
                "audit_rows": [
                    {
                        "cycle_index": 2,
                        "loss_purpose": "CYCLE_2_LONG_ADD",
                        "cover_purpose": "CYCLE_2_SHORT_REDUCE",
                        "loss_pnl": -0.05,
                        "cover_pnl": 0.08,
                        "loss_fill_timestamp": "2026-01-01T01:00:00+00:00",
                        "cover_fill_timestamp": "2026-01-01T02:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (coverage_dir / "APTUSDT_long_continuous_trade_0002_conservative_live_pnl_coverage_audit.json").write_text(
        json.dumps({"audit_rows": []}),
        encoding="utf-8",
    )

    payload = run_winner_quality_audit(
        regime_rows_csv=regime_csv,
        result_file=result_file,
        coverage_dir=coverage_dir,
    )
    assert payload["summary"]["blocked_count"] == 1
    assert payload["summary"]["allowed_count"] == 1
    assert payload["summary"]["error_count"] == 0
    out = write_outputs(payload, tmp_path / "out")
    assert out["rows"].exists()
    assert out["summary_json"].exists()
    assert out["readme"].exists()
    summary = json.loads(out["summary_json"].read_text(encoding="utf-8"))
    assert "NaN" not in out["summary_json"].read_text(encoding="utf-8")
    assert summary["blocked_count"] == 1


@pytest.mark.skipif(
    not Path(
        "research/backtests/results/regime_scanner_profitable_batch/"
        "profitable_regime_audit_rows.csv"
    ).exists()
    or not Path(
        "research/backtests/results/full_history_continuous_long_recovery/"
        "APTUSDT_original_hedge_5m_continuous_results.json"
    ).exists(),
    reason="real profitable batch artifacts missing",
)
def test_real_winner_quality_audit_counts() -> None:
    payload = run_winner_quality_audit(
        regime_rows_csv=(
            "research/backtests/results/regime_scanner_profitable_batch/"
            "profitable_regime_audit_rows.csv"
        ),
        result_file=(
            "research/backtests/results/full_history_continuous_long_recovery/"
            "APTUSDT_original_hedge_5m_continuous_results.json"
        ),
        coverage_dir="research/backtests/results/full_history_continuous_long_recovery",
    )
    assert payload["summary"]["trade_count_total"] == 79
    assert payload["summary"]["blocked_count"] == 44
    assert payload["summary"]["allowed_count"] == 35
    assert payload["summary"]["error_count"] == 0
    # Reproducible group assignment
    again = run_winner_quality_audit(
        regime_rows_csv=(
            "research/backtests/results/regime_scanner_profitable_batch/"
            "profitable_regime_audit_rows.csv"
        ),
        result_file=(
            "research/backtests/results/full_history_continuous_long_recovery/"
            "APTUSDT_original_hedge_5m_continuous_results.json"
        ),
        coverage_dir="research/backtests/results/full_history_continuous_long_recovery",
    )
    assert [r["trade_id"] for r in payload["rows"]] == [
        r["trade_id"] for r in again["rows"]
    ]
    assert [r["rule_d_group"] for r in payload["rows"]] == [
        r["rule_d_group"] for r in again["rows"]
    ]
