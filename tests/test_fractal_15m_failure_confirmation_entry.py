"""Unit tests for confirmation-entry helpers."""

from __future__ import annotations

import numpy as np

from orderbook_analyse.fractal_15m_failure_confirmation_entry.analysis import (
    decide_primary,
    decide_pullback,
)
from orderbook_analyse.fractal_15m_failure_confirmation_entry.events import first_open_after


def test_first_open_after_strict() -> None:
    times = np.array(
        [
            np.datetime64("2024-01-01T10:00:00"),
            np.datetime64("2024-01-01T10:01:00"),
            np.datetime64("2024-01-01T10:02:00"),
        ],
        dtype="datetime64[ns]",
    )
    opens = np.array([1.0, 2.0, 3.0])
    # decision exactly at 10:00 -> first STRICTLY after is 10:01
    i, px, t = first_open_after(times, opens, np.datetime64("2024-01-01T10:00:00"))
    assert i == 1 and px == 2.0


def test_decide_immediate_best() -> None:
    decay = []
    for d, med, hit in (
        (0, 0.25, 0.60),
        (1, 0.20, 0.58),
        (2, 0.18, 0.57),
        (3, 0.15, 0.56),
        (5, 0.10, 0.54),
        (10, 0.05, 0.52),
        (15, 0.02, 0.51),
        (30, -0.05, 0.48),
    ):
        decay.append(
            {
                "side": "COMBINED",
                "delay_min": d,
                "n": 1000,
                "median_dir_ret_60m": med,
                "hit_rate_60m": hit,
            }
        )
    wait = [
        {
            "side": "COMBINED",
            "strategy": "A_immediate",
            "n": 1000,
            "hit_rate_60m": 0.60,
            "median_dir_ret_60m": 0.25,
            "fill_rate": 1.0,
        },
        {
            "side": "COMBINED",
            "strategy": "B_wait_1m_realign",
            "n": 800,
            "hit_rate_60m": 0.55,
            "median_dir_ret_60m": 0.15,
            "fill_rate": 0.9,
        },
    ]
    assert decide_primary(decay, wait) == "IMMEDIATE_FAILURE_CONFIRMATION_ENTRY_BEST"


def test_decide_pullback_no_clear() -> None:
    rows = [
        {
            "side": "COMBINED",
            "bucket": "IMMEDIATE_T0",
            "median_dir_ret_60m": 0.2,
            "fill_rate": 1.0,
            "opportunity_adjusted_med60": 0.2,
        },
        {
            "side": "COMBINED",
            "bucket": "10_20",
            "median_dir_ret_60m": 0.21,
            "fill_rate": 0.3,
            "opportunity_adjusted_med60": 0.06,
        },
    ]
    assert decide_pullback(rows) == "PULLBACK_ENTRY_NO_CLEAR_VALUE"
