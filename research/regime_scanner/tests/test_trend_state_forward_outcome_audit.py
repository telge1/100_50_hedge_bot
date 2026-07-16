"""Tests for Phase C2 trend-state forward outcome audit."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.trend_audit_shared_replay import (
    build_shared_structure_timeline,
    load_or_build_shared_context,
    reset_audit_counters,
)
import research.regime_scanner.trend_audit_shared_replay as shared_replay_mod
from research.regime_scanner import swings as swings_mod
from research.regime_scanner.trend_robustness_audit import load_analysis_frame
from research.regime_scanner.trend_state_forward_outcome_audit import (
    build_price_arrays,
    compute_horizon_outcome,
    enrich_events,
    fast_reversal_flags,
    replay_variant_naive,
    replay_variant_optimized,
    run_audit,
    state_duration_until_next_change,
)


def _arrays_from_closes(closes: list[float]) -> dict:
    arr = np.asarray(closes, dtype=float)
    return {"close": arr, "high": arr + 1.0, "low": arr - 1.0, "n_bars": len(arr)}


def test_topping_falling_market_direction_hit() -> None:
    # Reference 100; after topping, price falls → short directional positive
    closes = [100.0] + [99.0 - i * 0.5 for i in range(10)]
    arrays = _arrays_from_closes(closes)
    out = compute_horizon_outcome(
        bar_index=0,
        horizon=6,
        reference_close=100.0,
        side="short",
        arrays=arrays,
    )
    assert out["evaluable"] is True
    assert out["direction_hit"] is True
    assert out["directional_close_return_pct"] > 0.0
    assert out["raw_close_return_pct"] < 0.0


def test_topping_rising_market_direction_miss() -> None:
    closes = [100.0] + [100.0 + i * 0.5 for i in range(1, 12)]
    arrays = _arrays_from_closes(closes)
    out = compute_horizon_outcome(
        bar_index=0,
        horizon=6,
        reference_close=100.0,
        side="short",
        arrays=arrays,
    )
    assert out["evaluable"] is True
    assert out["direction_hit"] is False
    assert out["directional_close_return_pct"] < 0.0


def test_bottoming_rising_market_direction_hit() -> None:
    closes = [50.0] + [50.0 + i for i in range(1, 12)]
    arrays = _arrays_from_closes(closes)
    out = compute_horizon_outcome(
        bar_index=0,
        horizon=6,
        reference_close=50.0,
        side="long",
        arrays=arrays,
    )
    assert out["evaluable"] is True
    assert out["direction_hit"] is True
    assert out["directional_close_return_pct"] > 0.0


def test_bottoming_falling_market_direction_miss() -> None:
    closes = [50.0] + [50.0 - i for i in range(1, 12)]
    arrays = _arrays_from_closes(closes)
    out = compute_horizon_outcome(
        bar_index=0,
        horizon=6,
        reference_close=50.0,
        side="long",
        arrays=arrays,
    )
    assert out["evaluable"] is True
    assert out["direction_hit"] is False
    assert out["directional_close_return_pct"] < 0.0


def test_directional_return_signs() -> None:
    arrays = _arrays_from_closes([100.0, 110.0, 90.0])
    long_up = compute_horizon_outcome(
        bar_index=0, horizon=1, reference_close=100.0, side="long", arrays=arrays
    )
    short_up = compute_horizon_outcome(
        bar_index=0, horizon=1, reference_close=100.0, side="short", arrays=arrays
    )
    assert long_up["directional_close_return_pct"] == 10.0
    assert short_up["directional_close_return_pct"] == -10.0


def test_mfe_mae_and_timing() -> None:
    # Long: high peaks early, low dips later
    closes = [100.0, 105.0, 104.0, 97.0, 98.0]
    highs = [100.0, 108.0, 105.0, 100.0, 99.0]
    lows = [99.0, 104.0, 103.0, 95.0, 97.0]
    arrays = {
        "close": np.asarray(closes, dtype=float),
        "high": np.asarray(highs, dtype=float),
        "low": np.asarray(lows, dtype=float),
        "n_bars": 5,
    }
    out = compute_horizon_outcome(
        bar_index=0, horizon=4, reference_close=100.0, side="long", arrays=arrays
    )
    assert out["evaluable"] is True
    assert abs(out["mfe_pct"] - 8.0) < 1e-9
    assert abs(out["mae_pct"] - 5.0) < 1e-9
    assert out["mfe_peak_offset"] == 1
    assert out["mae_peak_offset"] == 3
    assert out["bars_to_first_positive_directional"] == 1


def test_incomplete_horizon_at_data_end() -> None:
    arrays = _arrays_from_closes([100.0, 101.0, 102.0])
    out = compute_horizon_outcome(
        bar_index=1, horizon=5, reference_close=101.0, side="long", arrays=arrays
    )
    assert out["evaluable"] is False
    assert out["reason"] == "INSUFFICIENT_FUTURE_CANDLES"
    assert out["direction_hit"] is None


def test_fast_reversal_to_previous_and_opposite() -> None:
    states = ["a", "topping", "x", "a", "bottoming", "topping"]
    flags = fast_reversal_flags(
        event_bar=1,
        previous_state="a",
        new_state="topping",
        state_by_bar=states,
    )
    assert flags["reversal_to_previous_within_3"] is True
    assert flags["reversal_to_opposite_within_6"] is True
    assert flags["fast_reversal_within_3"] is True


def test_state_duration_until_next_change() -> None:
    states = ["topping", "topping", "topping", "neutral", "neutral"]
    assert state_duration_until_next_change(0, "topping", states) == 3
    assert state_duration_until_next_change(3, "neutral", states) is None


def test_enrich_events_incomplete_counted() -> None:
    replay = {
        "events": [
            {
                "bar_index": 1,
                "new_state": "topping",
                "previous_state": "bullish_weakening",
                "entry_close": 100.0,
                "timestamp": "2026-03-01T00:05:00+00:00",
                "variant": "C1_B_loose",
                "mode": "loose",
                "trigger_reasons": "test",
            }
        ],
        "state_by_bar": ["bullish_weakening", "topping", "topping"],
    }
    arrays = _arrays_from_closes([99.0, 100.0, 101.0])
    rows = enrich_events(
        symbol="TEST",
        replay_result=replay,
        arrays=arrays,
        horizons=(3,),
    )
    assert rows[0]["h3_evaluable"] is False
    assert rows[0]["h3_reason"] == "INSUFFICIENT_FUTURE_CANDLES"


@pytest.mark.parametrize("mode", ["loose", "strict"])
def test_optimized_replay_matches_naive(mode: str) -> None:
    try:
        frame = load_analysis_frame(
            "APTUSDT",
            load_start="2026-02-20",
            load_end="2026-03-15",
            max_bars=2500,
        )
    except Exception as exc:
        pytest.skip(f"APTUSDT candles unavailable: {exc}")

    a0 = pd.Timestamp("2026-03-01", tz="UTC")
    a1 = pd.Timestamp("2026-03-12", tz="UTC")
    targets = frozenset(
        {"topping", "bottoming", "bullish_weakening", "bearish_weakening"}
    )
    variant = "C1_B_loose" if mode == "loose" else "C1_C_strict"

    reset_audit_counters()
    naive = replay_variant_naive(
        frame,
        mode=mode,  # type: ignore[arg-type]
        variant_name=variant,
        analyze_start=a0,
        analyze_end=a1,
        targets=targets,
    )

    reset_audit_counters()
    shared = build_shared_structure_timeline(frame)
    opt = replay_variant_optimized(
        frame,
        mode=mode,  # type: ignore[arg-type]
        variant_name=variant,
        analyze_start=a0,
        analyze_end=a1,
        shared=shared,
        targets=targets,
    )
    assert naive["events"] == opt["events"]


def test_no_update_market_structure_in_variant_loop() -> None:
    try:
        frame = load_analysis_frame(
            "APTUSDT",
            load_start="2026-02-20",
            load_end="2026-03-10",
            max_bars=1200,
        )
    except Exception as exc:
        pytest.skip(f"APTUSDT candles unavailable: {exc}")

    import research.regime_scanner.trend_structure as ts_mod

    calls = {"n": 0}
    orig = ts_mod.update_market_structure

    def _count(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return orig(*args, **kwargs)

    reset_audit_counters()
    shared = build_shared_structure_timeline(frame)
    calls["n"] = 0

    with mock.patch.object(ts_mod, "update_market_structure", side_effect=_count):
        replay_variant_optimized(
            frame,
            mode="loose",
            variant_name="C1_B_loose",
            analyze_start=pd.Timestamp("2026-03-01", tz="UTC"),
            analyze_end=pd.Timestamp("2026-03-08", tz="UTC"),
            shared=shared,
            targets=frozenset({"topping", "bottoming"}),
        )
    assert calls["n"] == 0


def test_no_filter_pivots_as_of_in_variant_loop() -> None:
    try:
        frame = load_analysis_frame(
            "APTUSDT",
            load_start="2026-02-20",
            load_end="2026-03-10",
            max_bars=1200,
        )
    except Exception as exc:
        pytest.skip(f"APTUSDT candles unavailable: {exc}")

    reset_audit_counters()
    swings_mod.FILTER_PIVOTS_AS_OF_CALLS = 0
    shared = build_shared_structure_timeline(frame)
    build_calls = swings_mod.FILTER_PIVOTS_AS_OF_CALLS

    swings_mod.FILTER_PIVOTS_AS_OF_CALLS = 0
    replay_variant_optimized(
        frame,
        mode="strict",
        variant_name="C1_C_strict",
        analyze_start=pd.Timestamp("2026-03-01", tz="UTC"),
        analyze_end=pd.Timestamp("2026-03-08", tz="UTC"),
        shared=shared,
        targets=frozenset({"topping", "bottoming"}),
    )
    assert build_calls == 0
    assert swings_mod.FILTER_PIVOTS_AS_OF_CALLS == 0


def test_deterministic_summary(tmp_path: Path) -> None:
    try:
        frame = load_analysis_frame(
            "APTUSDT",
            load_start="2026-02-20",
            load_end="2026-03-15",
            max_bars=2000,
        )
    except Exception as exc:
        pytest.skip(f"APTUSDT candles unavailable: {exc}")

    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    s1 = run_audit(
        symbol="APTUSDT",
        output_dir=out1,
        load_start="2026-02-20",
        load_end="2026-03-15",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        horizons=(3, 6, 12),
    )
    s2 = run_audit(
        symbol="APTUSDT",
        output_dir=out2,
        load_start="2026-02-20",
        load_end="2026-03-15",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        horizons=(3, 6, 12),
    )
    assert s1["deterministic_hash"] == s2["deterministic_hash"]
    blob1 = (out1 / "summary.json").read_text(encoding="utf-8")
    blob2 = (out2 / "summary.json").read_text(encoding="utf-8")
    assert json.loads(blob1)["deterministic_hash"] == json.loads(blob2)["deterministic_hash"]


def test_shared_context_disk_cache_reuse(tmp_path: Path) -> None:
    try:
        frame = load_analysis_frame("APTUSDT", load_start="2026-02-20", load_end="2026-03-08", max_bars=800)
    except Exception as exc:
        pytest.skip(f"APTUSDT candles unavailable: {exc}")

    reset_audit_counters()
    ctx1 = load_or_build_shared_context(frame, cache_dir=tmp_path)
    assert shared_replay_mod.SHARED_STRUCTURE_PASS_COUNT == 1
    reset_audit_counters()
    ctx2 = load_or_build_shared_context(frame, cache_dir=tmp_path)
    assert shared_replay_mod.SHARED_STRUCTURE_PASS_COUNT == 0
    assert ctx1.cache_key == ctx2.cache_key


def test_build_price_arrays_from_frame() -> None:
    frame = pd.DataFrame(
        {
            "close": [1.0, 2.0],
            "high": [1.1, 2.1],
            "low": [0.9, 1.9],
        }
    )
    arrays = build_price_arrays(frame)
    assert arrays["n_bars"] == 2
    assert arrays["close"][1] == 2.0
