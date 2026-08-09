"""Smoke tests for tier assignment / resolve."""

from __future__ import annotations

import numpy as np

from orderbook_analyse.fractal_wave_fade_tier_tpsl.simulate import (
    assign_tier,
    prep_path_levels,
    resolve_tpsl,
)


def test_assign_tier() -> None:
    assert assign_tier("TREND_ALIGNED", "Q4") == "A"
    assert assign_tier("TREND_ALIGNED", "Q2") == "B"
    assert assign_tier("COUNTERTREND", "Q4") == "C"
    assert assign_tier("COUNTERTREND", "Q1") == "D"
    assert assign_tier("MIXED", "Q4") == "MIXED"


def test_resolve_sl_first_ambiguous() -> None:
    path = {
        "valid": True,
        "fav": np.array([0.5]),
        "adv": np.array([-0.5]),
        "raw": np.array([0.0]),
        "hold_min": np.array([1.0]),
    }
    prep_path_levels(path, (0.25,), (0.25,))
    sl = resolve_tpsl(path, tp_pct=0.25, sl_pct=0.25, policy="SL_FIRST")
    tp = resolve_tpsl(path, tp_pct=0.25, sl_pct=0.25, policy="TP_FIRST")
    assert sl["exit_type"] == "SL" and sl["ambiguous"] is True
    assert tp["exit_type"] == "TP" and tp["ambiguous"] is True
