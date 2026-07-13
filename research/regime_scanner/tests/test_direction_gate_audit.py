"""Tests for direction gate audit helpers (no full March data dependency)."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.direction_gate_audit import classify_long_quality, stability_for_variant


def test_classify_long_quality_weak_and_good() -> None:
    assert (
        classify_long_quality(
            {"reached_plus_025": False, "max_adverse_drop_pct": 2.5, "returned_to_signal": False}
        )
        == "weak"
    )
    assert (
        classify_long_quality(
            {"reached_plus_025": True, "max_adverse_drop_pct": 0.2, "returned_to_signal": True, "mfe_pct": 0.4}
        )
        == "good"
    )


def test_stability_for_variant_empty() -> None:
    assert stability_for_variant(pd.DataFrame()) == {}


def test_stability_counts_runs() -> None:
    g = pd.DataFrame(
        {
            "bar_close_time": pd.date_range("2026-03-06", periods=6, freq="15min", tz="UTC"),
            "direction_gate_state": [
                "neutral",
                "strong_bearish",
                "strong_bearish",
                "neutral",
                "strong_bearish",
                "strong_bearish",
            ],
        }
    )
    m = stability_for_variant(g)
    assert m["n_bearish_runs"] == 2
    assert m["n_state_changes"] >= 2
