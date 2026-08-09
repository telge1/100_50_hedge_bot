"""Unit tests for regime / entry helpers."""

from __future__ import annotations

import pandas as pd

from orderbook_analyse.fractal_direction_and_entry import TF_PREFIX
from orderbook_analyse.fractal_direction_and_entry.analysis import decide_direction, decide_entry
from orderbook_analyse.fractal_direction_and_entry.regime import (
    bull_controlled,
    classify_direction_state,
)


def _tf_row(**overrides):
    base = {
        "direction": "UP",
        "signed_price_move_pct": 0.5,
        "directional_efficiency": 0.1,
        "price_move_pct": 0.5,
        "rsi_end": 60.0,
        "rsi_delta": 1.0,
        "rsi_end_gt_50": True,
        "rsi_end_lt_50": False,
        "price_vs_ema20_end": "ABOVE",
        "ema9_vs_ema20_end": "BULL",
        "end_available_at": pd.Timestamp("2024-01-01", tz="UTC"),
    }
    base.update(overrides)
    return base


def _frame_for_tfs(tf_map: dict[str, dict]) -> pd.DataFrame:
    row = {}
    for tf, vals in tf_map.items():
        p = TF_PREFIX[tf]
        for k, v in vals.items():
            row[f"{p}_{k}"] = v
    # fill missing tfs with neutral/na
    for tf, p in TF_PREFIX.items():
        if tf in tf_map:
            continue
        row[f"{p}_direction"] = "UP"
        row[f"{p}_signed_price_move_pct"] = 0.0
        row[f"{p}_directional_efficiency"] = 0.0
        row[f"{p}_price_move_pct"] = 0.0
        row[f"{p}_rsi_end"] = 50.0
        row[f"{p}_rsi_delta"] = 0.0
        row[f"{p}_rsi_end_gt_50"] = False
        row[f"{p}_rsi_end_lt_50"] = False
        row[f"{p}_price_vs_ema20_end"] = "AT"
        row[f"{p}_ema9_vs_ema20_end"] = "FLAT"
        row[f"{p}_end_available_at"] = pd.NaT
    return pd.DataFrame([row])


def test_bull_controlled_up_efficient() -> None:
    df = _frame_for_tfs({"4h": _tf_row()})
    assert bool(bull_controlled(df, "4h").iloc[0])


def test_strong_bull_classification() -> None:
    bull = _tf_row()
    df = _frame_for_tfs(
        {
            "1d": bull,
            "4h": bull,
            "1h": bull,
            "1w": bull,
        }
    )
    state = classify_direction_state(df).iloc[0]
    assert state == "STRONG_BULL"


def test_decide_direction_robust() -> None:
    state_rows = [
        {
            "state": "BULL",
            "role": "directional",
            "n": 1000,
            "hit_rate_60m": 0.56,
            "median_dir_ret_60m": 0.1,
            "hit_rate_120m": 0.54,
        },
        {
            "state": "STRONG_BULL",
            "role": "directional",
            "n": 200,
            "hit_rate_60m": 0.58,
            "median_dir_ret_60m": 0.15,
            "hit_rate_120m": 0.55,
        },
        {
            "state": "BEAR",
            "role": "directional",
            "n": 1000,
            "hit_rate_60m": 0.55,
            "median_dir_ret_60m": 0.08,
            "hit_rate_120m": 0.53,
        },
        {
            "state": "STRONG_BEAR",
            "role": "directional",
            "n": 200,
            "hit_rate_60m": 0.57,
            "median_dir_ret_60m": 0.12,
            "hit_rate_120m": 0.54,
        },
    ]
    monthly = []
    for m in range(1, 13):
        for st in ("BULL", "BEAR"):
            monthly.append(
                {
                    "month": f"2024-{m:02d}",
                    "state": st,
                    "small_sample": False,
                    "hit_rate_60m": 0.55,
                }
            )
    blocks = []
    for b in ("H1_first_half", "H2_second_half"):
        for st in ("BULL", "BEAR"):
            blocks.append(
                {
                    "block": b,
                    "state": st,
                    "n": 500,
                    "hit_rate_60m": 0.55,
                    "median_dir_ret_60m": 0.1,
                }
            )
    assert decide_direction(state_rows, monthly, blocks) == "MTF_DIRECTIONAL_BIAS_ROBUST"


def test_decide_entry_has_edge() -> None:
    rows = []
    for side in ("LONG", "SHORT"):
        rows.append(
            {
                "side": side,
                "slice": "entry",
                "n": 80,
                "hit_rate_60m": 0.62,
                "median_dir_ret_60m": 0.25,
                "median_dir_ret_60m_net_fee": 0.14,
            }
        )
        rows.append(
            {
                "side": side,
                "slice": "regime_all",
                "n": 5000,
                "hit_rate_60m": 0.52,
                "median_dir_ret_60m": 0.05,
            }
        )
        rows.append(
            {
                "side": side,
                "slice": "cw_fail_no_realign",
                "n": 200,
                "hit_rate_60m": 0.50,
                "median_dir_ret_60m": 0.02,
            }
        )
        rows.append(
            {
                "side": side,
                "slice": "realign_no_htf",
                "n": 200,
                "hit_rate_60m": 0.48,
                "median_dir_ret_60m": 0.0,
            }
        )
    assert decide_entry(rows) == "FRACTAL_REALIGN_ENTRY_HAS_EDGE"
