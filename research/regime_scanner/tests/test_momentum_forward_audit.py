"""Tests for momentum forward-outcome audit (research-only)."""

from __future__ import annotations

import json

import pandas as pd

from research.regime_scanner.momentum_forward_audit import (
    COHORT_MOMENTUM_CONFIRMED,
    aggregate_group,
    build_group_summaries,
    build_signal_rows,
    compute_forward_path_metrics,
    directional_close_return_pct,
    evaluate_signal_horizons,
    excursion_pcts_for_candle,
    run_forward_audit,
    _candle_maps,
)


def _candle(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_long_short_directional_return_mirror() -> None:
    long_r = directional_close_return_pct(
        side="long", reference_close=100.0, future_close=110.0
    )
    short_r = directional_close_return_pct(
        side="short", reference_close=100.0, future_close=90.0
    )
    assert abs(long_r - 10.0) < 1e-12
    assert abs(short_r - 10.0) < 1e-12
    assert directional_close_return_pct(
        side="long", reference_close=100.0, future_close=90.0
    ) == -directional_close_return_pct(
        side="short", reference_close=100.0, future_close=90.0
    )


def test_mfe_mae_long_short_mirror() -> None:
    lf, la = excursion_pcts_for_candle(
        side="long", reference_close=100.0, high=105.0, low=97.0
    )
    sf, sa = excursion_pcts_for_candle(
        side="short", reference_close=100.0, high=105.0, low=97.0
    )
    assert abs(lf - 5.0) < 1e-12
    assert abs(la - 3.0) < 1e-12
    assert abs(sf - 3.0) < 1e-12
    assert abs(sa - 5.0) < 1e-12


def test_forward_path_mfe_mae_and_order() -> None:
    future = [
        _candle("2026-03-01T00:05:00+00:00", 100, 104, 99.5, 103),
        _candle("2026-03-01T00:10:00+00:00", 103, 103.5, 96, 97),
    ]
    out = compute_forward_path_metrics(
        side="long", reference_close=100.0, future_candles=future, horizon=2
    )
    assert out["evaluable"] is True
    assert abs(out["mfe_pct"] - 4.0) < 1e-12
    assert abs(out["mae_pct"] - 4.0) < 1e-12
    assert out["mfe_before_mae"] is True
    assert abs(out["directional_close_return_pct"] - (-3.0)) < 1e-12


def test_insufficient_future_at_end() -> None:
    future = [_candle("t1", 1, 2, 0.5, 1.5)]
    out = compute_forward_path_metrics(
        side="long", reference_close=1.0, future_candles=future, horizon=3
    )
    assert out["evaluable"] is False
    assert out["reason"] == "INSUFFICIENT_FUTURE_CANDLES"


def test_invalid_ohlc_in_window() -> None:
    future = [
        _candle("t1", 100, 101, 99, 100.5),
        {
            "timestamp": "t2",
            "open": float("nan"),
            "high": 102,
            "low": 98,
            "close": 101,
            "volume": 1,
        },
    ]
    out = compute_forward_path_metrics(
        side="long", reference_close=100.0, future_candles=future, horizon=2
    )
    assert out["evaluable"] is False
    assert out["reason"] == "INVALID_OHLC_IN_FORWARD_WINDOW"
    assert out["invalid_ohlc_count"] == 1


def test_measurement_pa_vs_momentum_basis() -> None:
    frame = pd.DataFrame(
        [
            _candle("2026-03-01T00:00:00+00:00", 99, 101, 98, 100),
            _candle("2026-03-01T00:05:00+00:00", 100, 103, 99.5, 102),
            _candle("2026-03-01T00:10:00+00:00", 102, 106, 101, 105),
        ]
    )
    signal = {
        "setup_id": "s1",
        "side": "long",
        "pattern_type": "higher_low",
        "cohort": COHORT_MOMENTUM_CONFIRMED,
        "in_not_confirmed_combo": False,
        "pa_structure_break_timestamp": "2026-03-01T00:00:00+00:00",
        "momentum_confirmation_timestamp": "2026-03-01T00:05:00+00:00",
        "momentum_confidence": "high",
        "confirmation_age": 1,
        "confirmation_type": "candle_1",
        "regime_5m": "bullish_trend",
        "regime_15m": "transition",
        "regime_30m": "transition",
        "combined_regime": "bullish_trend",
    }
    _, ts_to_i, candles = _candle_maps(frame)
    pa_rows = evaluate_signal_horizons(
        signal,
        candles=candles,
        ts_to_i=ts_to_i,
        horizons=[1],
        measurement_basis="pa_candle",
    )
    mom_rows = evaluate_signal_horizons(
        signal,
        candles=candles,
        ts_to_i=ts_to_i,
        horizons=[1],
        measurement_basis="momentum_candle",
    )
    assert pa_rows[0]["evaluable"] is True
    assert mom_rows[0]["evaluable"] is True
    assert abs(pa_rows[0]["directional_close_return_pct"] - 2.0) < 1e-12
    assert abs(mom_rows[0]["directional_close_return_pct"] - (3.0 / 102.0 * 100.0)) < 1e-12
    assert abs(mom_rows[0]["mfe_pct"] - (4.0 / 102.0 * 100.0)) < 1e-12


def test_no_future_data_before_measurement() -> None:
    frame_candles = [
        _candle("2026-03-01T00:00:00+00:00", 100, 120, 99, 100),
        _candle("2026-03-01T00:05:00+00:00", 100, 101, 99.5, 100.5),
    ]
    out = compute_forward_path_metrics(
        side="long",
        reference_close=100.0,
        future_candles=frame_candles[1:],
        horizon=1,
    )
    assert abs(out["mfe_pct"] - 1.0) < 1e-12


def test_build_signal_rows_cohorts() -> None:
    pa = [
        {
            "setup_id": "a",
            "side": "long",
            "pattern_type": "higher_low",
            "structure_break_timestamp": "2026-03-01T00:00:00+00:00",
            "warnings": [],
        },
        {
            "setup_id": "b",
            "side": "short",
            "pattern_type": "lower_high",
            "structure_break_timestamp": "2026-03-01T01:00:00+00:00",
            "warnings": [],
        },
        {
            "setup_id": "c",
            "side": "long",
            "pattern_type": "failed_breakdown",
            "structure_break_timestamp": "2026-03-01T02:00:00+00:00",
            "warnings": [],
        },
    ]
    events = [
        {"setup_id": "a", "event": "momentum_confirmed", "reason": None},
        {"setup_id": "b", "event": "invalidated", "reason": "CLOSE_BEYOND_STRUCTURE_LEVEL"},
        {"setup_id": "c", "event": "expired", "reason": "MOMENTUM_WINDOW_EXPIRED"},
    ]
    mom = [
        {
            "setup_id": "a",
            "confirmation_timestamp": "2026-03-01T00:00:00+00:00",
            "confidence": "medium",
            "candles_after_price_action_confirmation": 0,
            "confirmation_type": "break_candle",
        }
    ]
    rows = build_signal_rows(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
    )
    by_id = {r["setup_id"]: r for r in rows}
    assert by_id["a"]["cohort"] == "momentum_confirmed"
    assert by_id["b"]["cohort"] == "momentum_invalidated"
    assert by_id["c"]["cohort"] == "momentum_expired"
    assert by_id["b"]["in_not_confirmed_combo"] is True


def test_confirmation_age_zero_is_preserved() -> None:
    """Age 0 must not be treated as missing via falsy `or` fallbacks."""
    pa = [
        {
            "setup_id": "age0",
            "side": "long",
            "pattern_type": "higher_low",
            "structure_break_timestamp": "2026-03-01T00:00:00+00:00",
            "warnings": [],
        }
    ]
    events = [{"setup_id": "age0", "event": "momentum_confirmed", "reason": None}]
    mom = [
        {
            "setup_id": "age0",
            "confirmation_timestamp": "2026-03-01T00:00:00+00:00",
            "confidence": "medium",
            "candles_after_price_action_confirmation": 0,
            "confirmation_type": "break_candle",
        }
    ]
    rows = build_signal_rows(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
    )
    assert len(rows) == 1
    result = rows[0]
    assert result["confirmation_age"] == 0
    assert result["confirmation_age"] is not None


def test_aggregate_empty_group() -> None:
    out = aggregate_group([], group_keys={"cohort": "x", "horizon": 1})
    assert out["n_evaluable"] == 0
    assert out["mfe_median"] is None


def test_group_summary_and_deterministic_run() -> None:
    frame = pd.DataFrame(
        [
            _candle("2026-03-01T00:00:00+00:00", 100, 101, 99, 100),
            _candle("2026-03-01T00:05:00+00:00", 100, 103, 99, 102),
            _candle("2026-03-01T00:10:00+00:00", 102, 104, 101, 103),
            _candle("2026-03-01T00:15:00+00:00", 103, 105, 102, 104),
            _candle("2026-03-01T00:20:00+00:00", 104, 106, 103, 105),
        ]
    )
    pa = [
        {
            "setup_id": "a",
            "side": "long",
            "pattern_type": "higher_low",
            "structure_break_timestamp": "2026-03-01T00:00:00+00:00",
            "regime_5m": "bullish_trend",
            "regime_15m": "transition",
            "regime_30m": "transition",
            "combined_regime": "bullish_trend",
            "warnings": [],
        }
    ]
    mom = [
        {
            "setup_id": "a",
            "confirmation_timestamp": "2026-03-01T00:00:00+00:00",
            "confirming_candle_timestamp": "2026-03-01T00:00:00+00:00",
            "confidence": "high",
            "candles_after_price_action_confirmation": 0,
            "confirmation_type": "break_candle",
        }
    ]
    events = [{"setup_id": "a", "event": "momentum_confirmed", "reason": None}]
    p1 = run_forward_audit(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
        candles=frame,
        horizons=(1, 3),
    )
    p2 = run_forward_audit(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
        candles=frame,
        horizons=(1, 3),
    )
    assert json.dumps(p1["signal_forward_outcomes"], sort_keys=True) == json.dumps(
        p2["signal_forward_outcomes"], sort_keys=True
    )
    groups = build_group_summaries(p1["signal_forward_outcomes"])
    assert any(g["n_evaluable"] >= 1 for g in groups)
    assert p1["audit_summary"]["signal_counts_by_cohort"]["momentum_confirmed"] == 1
