"""Smoke tests for generalization helpers."""

from __future__ import annotations

import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization.annotate import (
    assign_frozen_quartile,
    load_frozen_quantile_edges,
)
from orderbook_analyse.fractal_all_wave_fade_generalization.analysis import decide_primary


def test_frozen_edges_load() -> None:
    edges = load_frozen_quantile_edges()
    assert ("15m", "UP", "directional_efficiency") in edges
    q = assign_frozen_quartile(
        pd.Series([edges[("15m", "UP", "directional_efficiency")][0.25] - 1.0]),
        edges[("15m", "UP", "directional_efficiency")],
    )
    assert q.iloc[0] == "Q1"


def test_primary_generalizes() -> None:
    status = {
        "APTUSDT_OOS": {"coverage_status": "COVERAGE_INSUFFICIENT", "hypotheses": {"H1": "INSUFFICIENT"}},
        "DOGEUSDT": {"coverage_status": "OK", "hypotheses": {"H1": "PASS"}},
        "BTCUSDT": {"coverage_status": "OK", "hypotheses": {"H1": "PASS"}},
    }
    assert decide_primary(status) == "STOCH_WAVE_FADE_GENERALIZES"
