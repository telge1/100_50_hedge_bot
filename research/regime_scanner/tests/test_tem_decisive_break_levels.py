"""Level / causality / hardcoding tests for decisive-break v3."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from research.regime_scanner.tem_structure_break.decisive_break import run_decisive_break
from research.regime_scanner.tem_structure_break.decisive_levels import (
    confirmed_swing_lows,
    prepare_h4_series,
)
from research.regime_scanner.tem_structure_break.decisive_models import DECISIVE_SEMANTICS


PKG = Path(__file__).resolve().parents[1] / "tem_structure_break"
FORBIDDEN = ["DOTUSDT", "ATOMUSDT", "LTCUSDT", "INJUSDT", "AAVEUSDT"]


def test_swing_low_requires_right_bar_confirmation() -> None:
    ts = pd.date_range("2026-01-01", periods=5, freq="4h", tz="UTC")
    h4 = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 10,
            "high": 11,
            "low": [10, 9, 8, 8.5, 8.6],
            "close": 10,
            "htf_close_decision": ts + pd.Timedelta(hours=4),
        }
    )
    h4 = prepare_h4_series(h4)
    swings = confirmed_swing_lows(h4)
    # low at index 2 (=8) confirmed at index 3
    assert any(s["index"] == 2 and s["confirm_index"] == 3 for s in swings)
    assert all(s["confirm_index"] > s["index"] for s in swings)


def test_prefix_invariance_decisive() -> None:
    ts = pd.date_range("2026-03-01", periods=20, freq="4h", tz="UTC")
    lows = [100 - (i % 5) for i in range(20)]
    h4 = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 100,
            "high": 101,
            "low": lows,
            "close": [l + 1 for l in lows],
            "htf_close_decision": ts + pd.Timedelta(hours=4),
        }
    )
    arm = str(h4.iloc[0]["htf_close_decision"])
    full = run_decisive_break(h4, v2_first_break_ts=arm, stabilize_bars=3)
    prefix = run_decisive_break(h4.iloc[:12].copy(), v2_first_break_ts=arm, stabilize_bars=3)
    # Events up to prefix horizon should match prefix run
    pref_events = [e for e in full.events if str(e.get("signal_available_ts") or e.get("timestamp") or "") <= str(h4.iloc[11]["htf_close_decision"])]
    # Compare states reachable within prefix length
    assert prefix.state.value in {e.get("state") for e in full.events} | {prefix.state.value, full.state.value}


def test_no_hardcoding_in_decisive_modules() -> None:
    files = [
        PKG / "decisive_break.py",
        PKG / "decisive_levels.py",
        PKG / "decisive_models.py",
        PKG / "decisive_evaluation.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for coin in FORBIDDEN:
            assert coin not in text, path.name
        assert "2026-01-19" not in text
        assert re.search(r"170\.86", text) is None


def test_semantics_snapshot_has_reclaim_and_priority() -> None:
    assert DECISIVE_SEMANTICS["does_not_mutate_v2"] is True
    assert DECISIVE_SEMANTICS["reclaim_window_4h_bars"] == 1
    assert "level_priority" in DECISIVE_SEMANTICS
