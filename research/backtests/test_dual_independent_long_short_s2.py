"""Unit tests for dual independent Long/Short S2 research audit."""

from __future__ import annotations

from types import SimpleNamespace

from research.backtests.dual_independent_long_short_s2 import (
    build_combined_equity_curve,
    build_s2_freeze_config,
    check_d0_s2_parity,
    opener_classification_ok,
    shared_initial_entry_row,
    summarize_side_trades,
    validate_independent_reentry_offsets,
)
from research.backtests.inventory_mtm_freeze import is_new_cycle_open_purpose
from research.backtests.safe_cycle_boundary_freeze import is_direction_aware_cycle_opener


def test_s2_config_stop_after_cycle_1() -> None:
    cfg = build_s2_freeze_config()
    assert cfg.safe_cycle_boundary is True
    assert cfg.stop_after_cycle == 1
    assert cfg.safe_boundary_arm_mode == "stop_after_cycle"
    assert cfg.use_mtm_trigger is False


def test_shared_initial_entry_parity() -> None:
    candles = [SimpleNamespace(close=1.23, timestamp=SimpleNamespace(isoformat=lambda: "t0"))]
    long_first = {"start_index": 0, "entry_price": 1.23}
    short_first = {"start_index": 0, "entry_price": 1.229}
    row = shared_initial_entry_row(
        coin="APTUSDT", candles=candles, long_first=long_first, short_first=short_first
    )
    assert row["start_parity_ok"] == 1
    assert row["shared_initial_entry_index"] == 0
    assert row["shared_initial_mark_price"] == 1.23


def test_independent_reentry_no_same_candle() -> None:
    rows = [
        {"trade_number": 1, "end_index": 10, "start_index": 0},
        {"trade_number": 2, "end_index": 20, "start_index": 11},
    ]
    assert validate_independent_reentry_offsets(rows) is True
    bad = [
        {"trade_number": 1, "end_index": 10, "start_index": 0},
        {"trade_number": 2, "end_index": 20, "start_index": 10},
    ]
    assert validate_independent_reentry_offsets(bad) is False


def test_short_opener_is_short_reduce_not_short_add() -> None:
    assert is_direction_aware_cycle_opener("CYCLE_2_SHORT_REDUCE", primary_side="short") is True
    assert is_direction_aware_cycle_opener("CYCLE_2_SHORT_ADD", primary_side="short") is False
    assert is_new_cycle_open_purpose("CYCLE_2_SHORT_ADD", primary_side="short") is True  # legacy bug
    rows = opener_classification_ok()
    by = {(r["primary_side"], r["purpose"]): r for r in rows}
    assert by[("short", "CYCLE_2_SHORT_REDUCE")]["is_opener"] == 1
    assert by[("short", "CYCLE_2_SHORT_ADD")]["is_opener"] == 0
    assert by[("long", "CYCLE_2_SHORT_REDUCE")]["is_second_leg"] == 1
    assert by[("long", "CYCLE_2_SHORT_REDUCE")]["is_opener"] == 0


def test_combined_equity_is_sum_of_sides() -> None:
    candles = [SimpleNamespace(close=100.0, timestamp=None) for _ in range(5)]
    long_rows = [
        {
            "trade_number": 1,
            "start_index": 0,
            "end_index": 2,
            "is_blocker": 0,
            "closed_pnl_usdt": 1.0,
            "fill_log": [
                {
                    "candle_index": 0,
                    "long_qty_after": 1.0,
                    "short_qty_after": 0.5,
                    "long_avg_after": 100.0,
                    "short_avg_after": 100.0,
                    "closed_pnl": 0.0,
                    "confirmed_closed_pnl": 0.0,
                },
                {
                    "candle_index": 2,
                    "long_qty_after": 0.0,
                    "short_qty_after": 0.0,
                    "long_avg_after": 0.0,
                    "short_avg_after": 0.0,
                    "closed_pnl": 1.0,
                    "confirmed_closed_pnl": 1.0,
                },
            ],
        }
    ]
    short_rows = [
        {
            "trade_number": 1,
            "start_index": 0,
            "end_index": 4,
            "is_blocker": 0,
            "closed_pnl_usdt": 0.5,
            "fill_log": [
                {
                    "candle_index": 0,
                    "long_qty_after": 0.25,
                    "short_qty_after": 0.5,
                    "long_avg_after": 100.0,
                    "short_avg_after": 100.0,
                    "closed_pnl": 0.0,
                    "confirmed_closed_pnl": 0.0,
                },
                {
                    "candle_index": 4,
                    "long_qty_after": 0.0,
                    "short_qty_after": 0.0,
                    "long_avg_after": 0.0,
                    "short_avg_after": 0.0,
                    "closed_pnl": 0.5,
                    "confirmed_closed_pnl": 0.5,
                },
            ],
        }
    ]
    rows, summary = build_combined_equity_curve(
        coin="TEST", candles=candles, long_rows=long_rows, short_rows=short_rows
    )
    for row in rows:
        assert abs(
            row["combined_equity"] - (row["long_total_equity"] + row["short_total_equity"])
        ) < 1e-9
    assert summary["margin_competition_simulated"] is False


def test_summarize_invalid_partial_zero() -> None:
    rows = [
        {
            "is_blocker": 0,
            "closed_pnl_usdt": 0.2,
            "mtm_pnl": 0.2,
            "duration_candles": 10,
            "max_cycle": 1,
            "invalid_partial_cycle": 0,
            "undercoverage": 0,
            "final_open_mtm_usdt": 0.0,
        }
    ]
    summary = summarize_side_trades(rows, side="long")
    assert summary["invalid_partial_cycle_count"] == 0


def test_d0_parity_helper_structure() -> None:
    summary = {
        "trades_started": 264,
        "trades_closed": 261,
        "open_blocker_count": 3,
        "total_series_mtm_usdt": 42.06951304045645,
        "invalid_partial_cycle_count": 0,
    }
    parity = check_d0_s2_parity(summary)
    assert parity["ok"] is True


def test_no_runtime_bot_files_in_this_module_tree() -> None:
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    porcelain = subprocess.check_output(
        ["git", "status", "--porcelain", "fixed_cycle_hedge_bot"],
        cwd=root,
        text=True,
    )
    assert porcelain.strip() == ""
