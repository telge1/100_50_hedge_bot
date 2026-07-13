"""Tests for March-6 07:30-anchor early-downtrend audit helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.early_downtrend_audit import (
    CHECKPOINT_TIMES,
    VISUAL_ANCHOR,
    block_effect,
    checkpoint_rows,
)

PIPELINE = Path(
    "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
)


def test_checkpoints_include_required_times() -> None:
    assert "07:30" in CHECKPOINT_TIMES
    assert "07:15" in CHECKPOINT_TIMES
    assert "09:30" in CHECKPOINT_TIMES
    assert VISUAL_ANCHOR.hour == 7 and VISUAL_ANCHOR.minute == 30


def test_fixed_block_marked_posthoc_only() -> None:
    events = {
        "setups": pd.DataFrame(
            {
                "setup_id": ["a"],
                "setup_activation_timestamp": [pd.Timestamp("2026-03-06T08:00:00+00:00")],
            }
        ),
        "pa": pd.DataFrame(columns=["setup_id", "structure_break_timestamp"]),
        "mom": pd.DataFrame(columns=["setup_id", "confirmation_timestamp"]),
    }
    row = block_effect(
        label="fixed_0730",
        block_from=VISUAL_ANCHOR,
        events=events,
        outcomes=pd.DataFrame(),
        kind="fixed_clock_posthoc_only",
    )
    assert row["kind"] == "fixed_clock_posthoc_only"
    assert row["long_setups_blocked"] == 1


def test_checkpoint_rows_missing_candle_flag() -> None:
    tl = pd.DataFrame(
        {
            "decision_time": [pd.Timestamp("2026-03-06T07:30:00+00:00")],
            "close": [1.0],
            "ema_9": [0.99],
            "ema_20": [0.98],
            "ema9_slope": [0.1],
            "ema20_slope": [0.1],
            "di_spread": [5.0],
            "adx": [20.0],
            "last_swing_high": [None],
            "last_swing_low": [None],
            "hl_broken": [False],
            "swing_low_broken": [False],
            "lower_high_confirmed": [False],
            "consecutive_lower_closes": [0],
            "neg_impulse": [False],
            "impulse_atr": [None],
            "regime_15m": ["x"],
            "state": ["neutral"],
            "bearish_warning": [False],
            "early_bearish_trend": [False],
            "confirmed_bearish_trend": [False],
            "active_criteria": [[]],
            "would_block_long": [False],
        }
    )
    rows = checkpoint_rows(tl, "D1")
    by = {r["checkpoint"]: r for r in rows}
    assert by["07:30"]["candle_closed_available"] is True
    assert by["07:15"]["candle_closed_available"] is False


@pytest.mark.skipif(not PIPELINE.exists(), reason="march pipeline missing")
def test_audit_outputs_exist_after_run(tmp_path: Path) -> None:
    from research.regime_scanner.early_downtrend_audit import build_arg_parser, run_audit

    args = build_arg_parser().parse_args(
        ["--output-dir", str(tmp_path), "--pipeline-dir", str(PIPELINE)]
    )
    summary = run_audit(args)
    assert summary["status"] == "ok"
    assert (tmp_path / "early_downtrend_0715_0930_timeline.csv").exists()
    assert (tmp_path / "early_downtrend_fixed_time_control.csv").exists()
    assert (tmp_path / "early_downtrend_dynamic_vs_fixed.csv").exists()
    assert (tmp_path / "early_downtrend_long_events_after_0730.csv").exists()
    # Safety: no trading rule equality of the four times
    four = summary["answers"]["four_time_concepts"]
    assert four["do_not_equate"] is True
    assert four["visual_optimal_block_start"].startswith("2026-03-06T07:30")
