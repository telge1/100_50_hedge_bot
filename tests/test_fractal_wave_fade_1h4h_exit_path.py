"""Unit tests for scale-out fee + SL_FIRST path order."""

from __future__ import annotations

import numpy as np

from orderbook_analyse.fractal_wave_fade_1h4h_exit_path import FEE_PCT
from orderbook_analyse.fractal_wave_fade_1h4h_exit_path.simulate import (
    simulate_scaleout,
    simulate_single_tpsl,
    target_before_adverse,
)


def _path(fav, adv, raw=None):
    fav = np.asarray(fav, dtype=float)
    adv = np.asarray(adv, dtype=float)
    raw = np.asarray(raw if raw is not None else fav * 0.5, dtype=float)
    hold = np.arange(1, len(fav) + 1, dtype=float)
    return {
        "valid": True,
        "fav": fav,
        "adv": adv,
        "raw": raw,
        "hold_min": hold,
        "symbol": "TEST",
        "timeframe": "1h",
        "side": "LONG",
        "entry_time": None,
    }


def test_fee_full_single_equals_011() -> None:
    p = _path([2.0], [-0.1], raw=[2.0])
    s = simulate_single_tpsl(p, tp_pct=2.0, sl_pct=1.5)
    assert s["exit_type"] == "TP"
    assert abs(s["net"] - (2.0 - FEE_PCT)) < 1e-9


def test_fee_partial_sums_to_011() -> None:
    # bar reaches +3%: both 50% legs fill at 1 and 3
    p = _path([3.0], [-0.1], raw=[3.0])
    s = simulate_scaleout(
        p,
        legs=((0.5, 1.0), (0.5, 3.0)),
        sl_pct=1.5,
        be_after_first_tp=False,
    )
    expected_gross = 0.5 * 1.0 + 0.5 * 3.0
    expected_net = expected_gross - FEE_PCT  # 0.5*0.11 + 0.5*0.11
    assert abs(s["gross"] - expected_gross) < 1e-9
    assert abs(s["net"] - expected_net) < 1e-9


def test_sl_first_same_bar() -> None:
    p = _path([2.0], [-2.0], raw=[0.0])
    s = simulate_single_tpsl(p, tp_pct=2.0, sl_pct=2.0, policy="SL_FIRST")
    assert s["exit_type"] == "SL"
    assert target_before_adverse(p, 2.0, 2.0) is False


def test_be_after_tp1() -> None:
    # TP1 at bar0 (+1%), then adverse to 0 on bar1 -> BE rest
    p = _path([1.0, 0.5], [-0.2, 0.0], raw=[0.8, 0.2])
    s = simulate_scaleout(
        p,
        legs=((0.5, 1.0), (0.5, 3.0)),
        sl_pct=1.5,
        be_after_first_tp=True,
    )
    # 50% at +1, 50% at BE 0
    assert abs(s["gross"] - 0.5) < 1e-9
    assert "BE" in s["exit_type"]
