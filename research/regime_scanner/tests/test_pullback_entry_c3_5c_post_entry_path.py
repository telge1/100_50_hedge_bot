"""Tests for post-entry path checkpoints and early-failure candidates."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.c35c_signal_store.path_build import compute_checkpoints_for_signal
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import signed_return_pct
from research.regime_scanner.pullback_entry_c3_5c_multicoin_early_failure_audit import (
    rule_fires,
    simulate_early_exit_row,
)


def _synthetic_frame(n: int = 20, *, side: str = "long") -> pd.DataFrame:
    """Causal 15m-like frame with structure/EMA columns."""
    rng = np.random.default_rng(42)
    ts0 = pd.Timestamp("2026-03-01T00:00:00+00:00")
    rows = []
    px = 100.0
    for i in range(n):
        o = px
        # first bars drift adverse for long, then recover
        if side == "long":
            c = o - 0.4 if i < 3 else o + 0.3
        else:
            c = o + 0.4 if i < 3 else o - 0.3
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        rows.append(
            {
                "timestamp": ts0 + pd.Timedelta(minutes=15 * i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000 + i,
                "atr_14": 1.0,
                "ema_9": c + (0.1 if side == "long" and i == 0 else -0.2),
                "ema_20": c,
                "adx": 20.0,
                "plus_di": 15.0 if side == "long" else 25.0,
                "minus_di": 25.0 if side == "long" else 15.0,
                "major_direction": 1 if side == "long" else -1,
                "protected_high": 101.0,
                "protected_low": 99.0,
                "arm_edge_internal_bear": i == 1 and side == "long",
                "arm_edge_internal_bull": i == 1 and side == "short",
                "internal_bos_down": False,
                "internal_bos_up": False,
                "arm_edge_choch_bear": False,
                "arm_edge_choch_bull": False,
                "choch_side": None,
            }
        )
        px = c
    return pd.DataFrame(rows)


def test_checkpoint_semantics_fill_is_bar0_cp1():
    frame = _synthetic_frame()
    fill_i = 5
    signal = {
        "id": 1,
        "run_id": "run-a",
        "direction": "long",
        "entry_price": float(frame.iloc[fill_i]["open"]),
        "entry_time": frame.iloc[fill_i]["timestamp"],
        "setup_id": 7,
        "metadata_json": {"trigger_bar": fill_i - 1},
    }
    outcome = {"bars_held": 10, "exit_reason": "TP"}
    trig = {"breakout_level": float(frame.iloc[fill_i]["open"]) - 0.05, "atr": 1.0}
    fill_f = {"protected_low": 99.0, "atr": 1.0}
    rows = compute_checkpoints_for_signal(
        signal=signal,
        outcome=outcome,
        trigger_feat=trig,
        fill_feat=fill_f,
        frame=frame,
        checkpoints=(1, 2, 3, 4),
    )
    assert len(rows) == 4
    assert all(r["availability"] == "ok" for r in rows)
    assert rows[0]["bars_since_fill"] == 0
    assert rows[0]["checkpoint_bar"] == 1
    assert rows[3]["bars_since_fill"] == 3
    # no future leakage: MFE at CP1 uses only fill bar
    assert rows[0]["mfe_so_far_pct"] is not None
    assert rows[1]["mfe_so_far_pct"] >= rows[0]["mfe_so_far_pct"] - 1e-12


def test_prior_exit_blocks_later_checkpoints():
    frame = _synthetic_frame()
    fill_i = 2
    signal = {
        "id": 2,
        "run_id": "run-a",
        "direction": "long",
        "entry_price": float(frame.iloc[fill_i]["open"]),
        "entry_time": frame.iloc[fill_i]["timestamp"],
        "setup_id": 1,
        "metadata_json": {},
    }
    outcome = {"bars_held": 0, "exit_reason": "same_bar_conservative_sl"}
    rows = compute_checkpoints_for_signal(
        signal=signal,
        outcome=outcome,
        trigger_feat={"breakout_level": 100.0},
        fill_feat={},
        frame=frame,
        checkpoints=(1, 2, 3, 4),
    )
    assert rows[0]["availability"] == "ok"
    assert rows[1]["availability"] == "not_available_due_to_prior_exit"
    assert rows[2]["availability"] == "not_available_due_to_prior_exit"


def test_breakout_lost_long_short_mirror():
    frame = _synthetic_frame(side="long")
    fill_i = 0  # adverse closes for i<3
    brk = float(frame.iloc[fill_i]["open"]) + 0.01
    signal = {
        "id": 3,
        "run_id": "r",
        "direction": "long",
        "entry_price": float(frame.iloc[fill_i]["open"]),
        "entry_time": frame.iloc[fill_i]["timestamp"],
        "setup_id": 1,
        "metadata_json": {},
    }
    rows = compute_checkpoints_for_signal(
        signal=signal,
        outcome={"bars_held": 8, "exit_reason": "SL"},
        trigger_feat={"breakout_level": brk},
        fill_feat={},
        frame=frame,
        checkpoints=(1, 2),
    )
    assert rows[0]["availability"] == "ok"
    assert int(rows[0]["breakout_level_lost"]) == 1
    assert int(rows[1]["breakout_level_lost"]) == 1

    frame_s = _synthetic_frame(side="short")
    brk_s = float(frame_s.iloc[fill_i]["open"]) - 0.01
    signal_s = {
        "id": 4,
        "run_id": "r",
        "direction": "short",
        "entry_price": float(frame_s.iloc[fill_i]["open"]),
        "entry_time": frame_s.iloc[fill_i]["timestamp"],
        "setup_id": 2,
        "metadata_json": {},
    }
    rows_s = compute_checkpoints_for_signal(
        signal=signal_s,
        outcome={"bars_held": 8, "exit_reason": "SL"},
        trigger_feat={"breakout_level": brk_s},
        fill_feat={},
        frame=frame_s,
        checkpoints=(1, 2),
    )
    assert int(rows_s[0]["breakout_level_lost"]) == 1
    assert int(rows_s[1]["breakout_level_lost"]) == 1


def test_micro_counter_bos_sticky():
    frame = _synthetic_frame(side="long")
    fill_i = 0
    # edge on bar offset 1 → visible from CP2
    signal = {
        "id": 5,
        "run_id": "r",
        "direction": "long",
        "entry_price": float(frame.iloc[fill_i]["open"]),
        "entry_time": frame.iloc[fill_i]["timestamp"],
        "setup_id": 1,
        "metadata_json": {},
    }
    rows = compute_checkpoints_for_signal(
        signal=signal,
        outcome={"bars_held": 10, "exit_reason": "TP"},
        trigger_feat={"breakout_level": 99.5},
        fill_feat={},
        frame=frame,
        checkpoints=(1, 2),
    )
    assert int(rows[0]["micro_counter_bos"]) == 0
    assert int(rows[1]["micro_counter_bos"]) == 1


def test_early_exit_next_open_no_backdate():
    entry = 100.0
    # CP1 → next open is bar1 open
    next_open = 99.0
    sim = simulate_early_exit_row(
        side=1,
        entry=entry,
        baseline_net=-2.0 - 0.20,
        baseline_reason="SL",
        bars_held=10,
        checkpoint_bar=1,
        feature_json={
            "still_open_after_checkpoint": True,
            "next_open_price": next_open,
        },
        cost_pct=0.20,
    )
    assert sim["early_exit_applied"] is True
    expected_gross = signed_return_pct(1, entry, next_open)
    assert math.isclose(sim["early_exit_gross_pnl_pct"], expected_gross)
    assert math.isclose(sim["early_exit_net_pnl_pct"], expected_gross - 0.20)
    # already exited → no early exit
    sim2 = simulate_early_exit_row(
        side=1,
        entry=entry,
        baseline_net=-2.2,
        baseline_reason="SL",
        bars_held=0,
        checkpoint_bar=2,
        feature_json={"still_open_after_checkpoint": False, "next_open_price": 99.0},
    )
    assert sim2["early_exit_applied"] is False
    assert sim2["skip_reason"] == "already_exited"


def test_rule_candidates_f1_f6():
    row = pd.Series(
        {
            "mfe_so_far_pct": 0.0,
            "mae_so_far_pct": -0.60,
            "breakout_level_lost": 1,
            "breakout_level_reclaimed": 0,
            "micro_counter_bos": 1,
            "micro_counter_choch": 0,
            "ema9_20_lost": 1,
        }
    )
    assert rule_fires("F1_no_mfe", row)
    assert rule_fires("F2_breakout_lost", row)
    assert rule_fires("F3_counter_micro_bos", row)
    assert rule_fires("F4_ema_alignment_lost", row)
    assert rule_fires("F5_mae_le_0_50", row)
    assert rule_fires("F6_no_mfe_and_breakout_lost", row)
    assert rule_fires("F6_breakout_lost_and_counter_bos", row)
    assert rule_fires("F6_no_mfe_and_mae_le_0_50", row)
    assert rule_fires("F6_ema_lost_and_breakout_lost", row)
    row2 = row.copy()
    row2["mfe_so_far_pct"] = 0.5
    assert not rule_fires("F1_no_mfe", row2)


def test_data_end_missing_checkpoint():
    frame = _synthetic_frame(n=6)
    fill_i = 4  # only bars 4,5 available → CP1,CP2 ok; CP3,CP4 data_end
    signal = {
        "id": 9,
        "run_id": "r",
        "direction": "long",
        "entry_price": float(frame.iloc[fill_i]["open"]),
        "entry_time": frame.iloc[fill_i]["timestamp"],
        "setup_id": 1,
        "metadata_json": {},
    }
    rows = compute_checkpoints_for_signal(
        signal=signal,
        outcome={"bars_held": 20, "exit_reason": "time_exit"},
        trigger_feat={"breakout_level": 100.0},
        fill_feat={},
        frame=frame,
        checkpoints=(1, 2, 3, 4),
    )
    assert rows[0]["availability"] == "ok"
    assert rows[1]["availability"] == "ok"
    assert rows[2]["availability"] == "not_available_data_end"
    assert rows[3]["availability"] == "not_available_data_end"
