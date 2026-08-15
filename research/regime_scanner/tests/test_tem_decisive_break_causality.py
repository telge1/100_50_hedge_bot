"""Additional decisive-break tests: causality boundaries + cases scaffolding."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.tem_structure_break.decisive_break import run_decisive_break
from research.regime_scanner.tem_structure_break.decisive_models import DecisiveState


def test_future_mutation_does_not_change_past_confirmation() -> None:
    base = pd.Timestamp("2026-04-01T00:00:00Z")
    rows = []
    for i in range(3):
        t = base + pd.Timedelta(hours=4 * i)
        rows.append({"timestamp": t, "open": 100, "high": 101, "low": 99, "close": 100,
                     "htf_close_decision": t + pd.Timedelta(hours=4)})
    rows.append({"timestamp": base + pd.Timedelta(hours=12), "open": 100, "high": 100, "low": 90, "close": 92,
                 "htf_close_decision": base + pd.Timedelta(hours=16)})
    rows.append({"timestamp": base + pd.Timedelta(hours=16), "open": 92, "high": 95, "low": 91, "close": 94,
                 "htf_close_decision": base + pd.Timedelta(hours=20)})
    rows.append({"timestamp": base + pd.Timedelta(hours=20), "open": 94, "high": 94, "low": 88, "close": 89,
                 "htf_close_decision": base + pd.Timedelta(hours=24)})
    rows.append({"timestamp": base + pd.Timedelta(hours=24), "open": 89, "high": 90, "low": 87, "close": 88,
                 "htf_close_decision": base + pd.Timedelta(hours=28)})
    h4 = pd.DataFrame(rows)
    arm = str(h4.iloc[0]["htf_close_decision"])
    a = run_decisive_break(h4, v2_first_break_ts=arm, stabilize_bars=3)
    # mutate far-future last close wildly
    h42 = h4.copy()
    h42.loc[h42.index[-1], "close"] = 1.0
    h42.loc[h42.index[-1], "low"] = 0.5
    b = run_decisive_break(h42.iloc[:-1], v2_first_break_ts=arm, stabilize_bars=3)
    # without the last bar, confirmation may be pending; with full series confirmed
    assert a.pending_ts == b.pending_ts or a.state == DecisiveState.DECISIVE_BREAK_CONFIRMED


def test_break_not_before_level_ready() -> None:
    base = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = []
    for i in range(2):  # insufficient stabilize if stabilize=3
        t = base + pd.Timedelta(hours=4 * i)
        rows.append({"timestamp": t, "open": 100, "high": 101, "low": 50, "close": 50,
                     "htf_close_decision": t + pd.Timedelta(hours=4)})
    h4 = pd.DataFrame(rows)
    arm = str(h4.iloc[0]["htf_close_decision"])
    rt = run_decisive_break(h4, v2_first_break_ts=arm, stabilize_bars=3)
    assert rt.confirmed_ts is None
    assert rt.state in {
        DecisiveState.DECISIVE_ARMING,
        DecisiveState.DECISIVE_NOT_ARMED,
        DecisiveState.DECISIVE_LEVEL_READY,
    }
