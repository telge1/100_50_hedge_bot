"""Unit tests for multi-TF failure helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure.events import local_failure_mask
from orderbook_analyse.fractal_failure_multitimeframe.analysis import decide_overall
from orderbook_analyse.fractal_failure_multitimeframe.outcomes import resolve_entries


def test_local_failure_mask_semantics() -> None:
    df = pd.DataFrame(
        {
            "direction": ["UP", "UP", "DOWN", "DOWN"],
            "signed_price_move_pct": [-0.1, 0.5, -0.1, 0.5],
            "directional_efficiency": [0.1, 0.2, 0.1, -0.1],
            "inefficient_flag": [False, False, False, False],
        }
    )
    up, dn = local_failure_mask(df)
    assert bool(up.iloc[0]) is True  # signed<=0
    assert bool(up.iloc[1]) is False
    assert bool(dn.iloc[2]) is True  # signed<=0
    assert bool(dn.iloc[3]) is True  # eff<=0


def test_resolve_entries_strictly_after() -> None:
    open_times = np.array(
        ["2024-01-01T00:00", "2024-01-01T00:01", "2024-01-01T00:02"],
        dtype="datetime64[m]",
    ).astype("datetime64[ns]")
    opens = np.array([1.0, 2.0, 3.0])
    ev = pd.DataFrame(
        {
            "confirmation_available_at": [pd.Timestamp("2024-01-01T00:00", tz="UTC")],
            "side": ["LONG"],
        }
    )
    out = resolve_entries(ev, open_times, opens, delay_min=0)
    assert int(out.iloc[0]["entry_i"]) == 1
    assert float(out.iloc[0]["entry_price"]) == 2.0


def test_decide_overall_only_15m() -> None:
    d = {
        "5m": "FAILURE_SIGNAL_NO_EDGE",
        "15m": "FAILURE_SIGNAL_HAS_EDGE",
        "30m": "FAILURE_SIGNAL_NO_EDGE",
        "1h": "FAILURE_SIGNAL_NO_EDGE",
        "4h": "FAILURE_SIGNAL_NO_EDGE",
    }
    ranking = [
        {
            "timeframe": "15m",
            "failure_type": "ALL",
            "n": 100,
            "hit_rate": 0.58,
            "median_dir_ret": 0.1,
        }
    ]
    assert decide_overall(d, ranking) == "WAVE_FAILURE_ONLY_WORKS_ON_15M"


def test_decide_overall_generalizes() -> None:
    d = {tf: "FAILURE_SIGNAL_CONTEXT_DEPENDENT" for tf in ("5m", "15m", "30m", "1h", "4h")}
    ranking = [
        {
            "timeframe": tf,
            "failure_type": "ALL",
            "n": 100,
            "hit_rate": 0.55,
            "median_dir_ret": 0.05,
        }
        for tf in ("5m", "15m", "30m", "1h", "4h")
    ]
    assert decide_overall(d, ranking) == "WAVE_FAILURE_GENERALIZES_ACROSS_TIMEFRAMES"
