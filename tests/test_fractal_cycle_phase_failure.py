"""Unit tests for cycle-phase failure helpers."""

from __future__ import annotations

import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure.analysis import (
    decide_cycle_phase,
    decide_signal,
)
from orderbook_analyse.fractal_cycle_phase_failure.events import local_failure_mask
from orderbook_analyse.fractal_cycle_phase_failure.phase import cycle_phase_from_wave


def test_cycle_phase_mapping() -> None:
    df = pd.DataFrame(
        {
            "direction": ["UP", "UP", "UP", "DOWN", "DOWN", "DOWN"],
            "stoch_zone_end": ["LOW", "MID", "HIGH", "HIGH", "MID", "LOW"],
        }
    )
    phases = cycle_phase_from_wave(df).tolist()
    assert phases == ["LOW_UP", "MID_UP", "HIGH_UP", "HIGH_DOWN", "MID_DOWN", "LOW_DOWN"]


def test_local_failure_mask() -> None:
    df = pd.DataFrame(
        {
            "direction": ["UP", "UP", "DOWN", "DOWN"],
            "signed_price_move_pct": [-0.1, 0.5, 0.4, 0.4],
            "directional_efficiency": [0.1, 0.1, 0.1, -0.1],
            "inefficient_flag": [False, False, False, False],
        }
    )
    fu, fd = local_failure_mask(df)
    assert list(fu) == [True, False, False, False]
    assert list(fd) == [False, False, False, True]


def test_decide_cycle_phase_conditions() -> None:
    early_late = [
        {
            "failure_type": "FAILED_UP_WAVE",
            "tf": "1d",
            "bucket": "LATE_UP",
            "n": 100,
            "hit_rate_60m": 0.60,
            "median_dir_ret_60m": 0.2,
        },
        {
            "failure_type": "FAILED_UP_WAVE",
            "tf": "1d",
            "bucket": "EARLY_UP",
            "n": 100,
            "hit_rate_60m": 0.50,
            "median_dir_ret_60m": 0.0,
        },
        {
            "failure_type": "FAILED_DOWN_WAVE",
            "tf": "1d",
            "bucket": "LATE_DOWN",
            "n": 100,
            "hit_rate_60m": 0.58,
            "median_dir_ret_60m": 0.15,
        },
        {
            "failure_type": "FAILED_DOWN_WAVE",
            "tf": "1d",
            "bucket": "EARLY_DOWN",
            "n": 100,
            "hit_rate_60m": 0.50,
            "median_dir_ret_60m": 0.0,
        },
    ]
    assert decide_cycle_phase(early_late, []) == "CYCLE_PHASE_CONDITIONS_FAILURE_DIRECTION"


def test_decide_signal_no_edge() -> None:
    base = [
        {
            "failure_type": "FAILED_UP_WAVE",
            "slice": "ALL",
            "n": 100,
            "hit_rate_60m": 0.45,
            "median_dir_ret_60m": -0.1,
        },
        {
            "failure_type": "FAILED_DOWN_WAVE",
            "slice": "ALL",
            "n": 100,
            "hit_rate_60m": 0.46,
            "median_dir_ret_60m": -0.05,
        },
    ]
    assert decide_signal(base, []) == "15M_FAILURE_PHASE_SIGNAL_NO_EDGE"
