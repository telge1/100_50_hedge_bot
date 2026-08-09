"""Unit tests for all-wave fade helpers."""

from __future__ import annotations

import pandas as pd

from orderbook_analyse.fractal_all_wave_fade.analysis import (
    decide_failure_filter,
    decide_overall,
    decide_tf,
)
from orderbook_analyse.fractal_all_wave_fade.events import load_all_waves
from orderbook_analyse.fractal_cycle_phase_failure.events import local_failure_mask


def test_load_all_waves_has_fade_sides() -> None:
    w = load_all_waves("15m")
    assert len(w) > 1000
    assert set(w["side"].unique()) <= {"LONG", "SHORT"}
    up = w[w["direction"] == "UP"]
    assert (up["side"] == "SHORT").all()
    fail_up, fail_dn = local_failure_mask(w)
    assert int((fail_up | fail_dn).sum()) == int(w["is_failed"].sum())


def test_decide_tf_has_edge() -> None:
    ranking = [
        {
            "wave_group": "ALL",
            "side": "COMBINED",
            "n": 1000,
            "hit": 0.58,
            "median_return": 0.2,
            "net_after_fee": 0.09,
            "monthly_positive_share": 0.8,
        },
        {
            "wave_group": "ALL",
            "side": "LONG",
            "n": 500,
            "hit": 0.55,
            "median_return": 0.15,
            "net_after_fee": 0.04,
        },
        {
            "wave_group": "ALL",
            "side": "SHORT",
            "n": 500,
            "hit": 0.56,
            "median_return": 0.18,
            "net_after_fee": 0.07,
        },
    ]
    assert decide_tf(ranking, [], "15m") == "ALL_WAVE_FADE_HAS_EDGE"


def test_decide_failure_hurts() -> None:
    rows = []
    for tf, h in (("5m", 30), ("15m", 60), ("30m", 120)):
        rows.append(
            {
                "timeframe": tf,
                "side": "COMBINED",
                "wave_group": "FAILED",
                f"median_dir_ret_{h}m": 0.05,
                f"hit_rate_{h}m": 0.52,
            }
        )
        rows.append(
            {
                "timeframe": tf,
                "side": "COMBINED",
                "wave_group": "NON_FAILED",
                f"median_dir_ret_{h}m": 0.20,
                f"hit_rate_{h}m": 0.60,
            }
        )
    assert decide_failure_filter(rows) == "FAILURE_FILTER_HURTS_EDGE"


def test_decide_overall_general() -> None:
    d = {tf: "ALL_WAVE_FADE_HAS_EDGE" for tf in ("5m", "15m", "30m", "1h", "4h")}
    assert decide_overall(d) == "STOCH_WAVE_END_FADE_IS_GENERAL_SIGNAL"
