"""Unit tests for TP/SL helpers."""

from __future__ import annotations

import numpy as np

from orderbook_analyse.fractal_15m_failure_tpsl.analysis import decide_primary, decide_sl
from orderbook_analyse.fractal_15m_failure_tpsl.simulate import resolve_on_path


def test_resolve_sl_first_ambiguous() -> None:
    path = {
        "valid": True,
        "fav": np.array([0.05, 0.30]),
        "adv": np.array([-0.02, -0.30]),
        "raw_close": np.array([0.01, 0.0]),
        "hold_min": np.array([1.0, 2.0]),
    }
    # bar1 hits both tp=0.25 and sl=0.25
    path["fav"] = np.array([0.30])
    path["adv"] = np.array([-0.30])
    path["raw_close"] = np.array([0.0])
    path["hold_min"] = np.array([1.0])
    sl = resolve_on_path(path, tp_pct=0.25, sl_pct=0.25, policy="SL_FIRST")
    tp = resolve_on_path(path, tp_pct=0.25, sl_pct=0.25, policy="TP_FIRST")
    assert sl["exit_type"] == "SL" and sl["ambiguous"] is True
    assert tp["exit_type"] == "TP" and tp["ambiguous"] is True


def test_decide_not_profitable() -> None:
    rows = [
        {
            "n": 100,
            "mean_net_return": -0.05,
            "profit_factor": 0.8,
            "net_total_return": -5,
            "tp_pct": 0.2,
            "sl_pct": 0.3,
        }
    ]
    assert decide_primary(rows) == "FIXED_TPSL_NOT_PROFITABLE"
    assert decide_sl(rows) == "FIXED_SL_TOO_DESTRUCTIVE"
