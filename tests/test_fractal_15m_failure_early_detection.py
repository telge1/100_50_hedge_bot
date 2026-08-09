"""Unit tests for early failure detection helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_early_detection.analysis import (
    decide_partial_eff,
    decide_primary,
    prediction_stats,
)
from orderbook_analyse.fractal_15m_failure_early_detection.snapshots import _eff_decay_label


def test_eff_decay_label() -> None:
    assert _eff_decay_label(0.05, 0.01) == "EFFICIENCY_DECAYING"
    assert _eff_decay_label(0.05, 0.05) == "EFFICIENCY_STABLE"
    assert _eff_decay_label(0.01, 0.05) == "EFFICIENCY_IMPROVING"


def test_prediction_stats() -> None:
    df = pd.DataFrame(
        {
            "is_later_failure": [True, True, False, False, True],
            "early_failure_candidate": [True, False, True, False, True],
        }
    )
    r = prediction_stats(df, direction="UP", offset=5)
    assert r["n_candidates"] == 3
    assert abs(r["precision"] - (2 / 3)) < 1e-9
    assert abs(r["recall"] - (2 / 3)) < 1e-9


def test_decide_primary_with_edge() -> None:
    pred = []
    fwd = []
    for d in ("UP", "DOWN"):
        pred.append(
            {
                "direction": d,
                "offset_min": 5,
                "n_candidates": 100,
                "lift": 1.3,
                "precision": 0.5,
                "failure_rate_non_candidate": 0.3,
            }
        )
        fwd.append(
            {
                "direction": d,
                "offset_min": 5,
                "slice": "early_candidate",
                "n": 100,
                "hit_rate_60m": 0.58,
                "median_dir_ret_60m": 0.1,
            }
        )
    assert decide_primary(pred, fwd) == "15M_FAILURE_DETECTABLE_EARLY_WITH_EDGE"
    assert decide_partial_eff(pred, []) == "PARTIAL_EFFICIENCY_IS_USEFUL_EARLY_WARNING"
