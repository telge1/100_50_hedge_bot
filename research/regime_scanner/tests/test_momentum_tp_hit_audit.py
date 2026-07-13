"""Tests for momentum TP-hit audit (research-only)."""

from __future__ import annotations

from research.regime_scanner.momentum_forward_audit import build_signal_rows
from research.regime_scanner.momentum_tp_hit_audit import (
    PRIMARY_TP_PCT,
    compute_tp_hit_for_path,
    run_tp_hit_audit,
    signal_groups,
)
import pandas as pd


def _c(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_long_short_tp_mirror() -> None:
    # Long: high reaches +0.25% at 100.25
    long_future = [_c("t1", 100, 100.25, 99.9, 100.1)]
    short_future = [_c("t1", 100, 100.1, 99.75, 99.9)]  # low reaches -0.25%
    long_hit = compute_tp_hit_for_path(
        side="long",
        reference_close=100.0,
        future_candles=long_future,
        horizon=1,
        tp_pct=0.25,
    )
    short_hit = compute_tp_hit_for_path(
        side="short",
        reference_close=100.0,
        future_candles=short_future,
        horizon=1,
        tp_pct=0.25,
    )
    assert long_hit["tp_hit"] is True
    assert short_hit["tp_hit"] is True
    assert long_hit["first_hit_age"] == short_hit["first_hit_age"] == 0


def test_exact_025_counts_as_hit() -> None:
    future = [_c("t1", 100, 100.25, 100.0, 100.2)]  # fav exactly 0.25%
    out = compute_tp_hit_for_path(
        side="long",
        reference_close=100.0,
        future_candles=future,
        horizon=1,
        tp_pct=PRIMARY_TP_PCT,
    )
    assert out["evaluable"] is True
    assert out["tp_hit"] is True
    assert out["first_hit_age"] == 0


def test_first_hit_age() -> None:
    future = [
        _c("t1", 100, 100.10, 99.9, 100.0),  # 0.10% — no hit
        _c("t2", 100, 100.20, 99.9, 100.0),  # 0.20% — no hit
        _c("t3", 100, 100.30, 99.9, 100.1),  # 0.30% — hit age 2
    ]
    out = compute_tp_hit_for_path(
        side="long",
        reference_close=100.0,
        future_candles=future,
        horizon=3,
        tp_pct=0.25,
    )
    assert out["tp_hit"] is True
    assert out["first_hit_age"] == 2


def test_insufficient_horizon() -> None:
    future = [_c("t1", 100, 101, 99, 100)]
    out = compute_tp_hit_for_path(
        side="long",
        reference_close=100.0,
        future_candles=future,
        horizon=3,
        tp_pct=0.25,
    )
    assert out["evaluable"] is False
    assert out["reason"] == "INSUFFICIENT_FUTURE_CANDLES"
    assert out["tp_hit"] is False


def test_same_candle_ambiguous() -> None:
    # Hits +0.25% high and also trades below reference (adverse > 0)
    future = [_c("t1", 100, 100.40, 99.50, 100.2)]
    out = compute_tp_hit_for_path(
        side="long",
        reference_close=100.0,
        future_candles=future,
        horizon=1,
        tp_pct=0.25,
    )
    assert out["tp_hit"] is True
    assert out["same_candle_ambiguous"] is True
    assert out["mae_before_tp_pct"] == 0.0  # no prior candles


def test_mae_only_before_first_tp() -> None:
    future = [
        _c("t1", 100, 100.10, 99.40, 99.8),  # adverse 0.60%, no TP
        _c("t2", 99.8, 100.50, 99.70, 100.3),  # hit; adverse on hit candle ignored for MAE-before
    ]
    out = compute_tp_hit_for_path(
        side="long",
        reference_close=100.0,
        future_candles=future,
        horizon=2,
        tp_pct=0.25,
    )
    assert out["tp_hit"] is True
    assert out["first_hit_age"] == 1
    assert abs(out["mae_before_tp_pct"] - 0.60) < 1e-12


def test_confirmation_age_zero_preserved_in_groups() -> None:
    pa = [
        {
            "setup_id": "a",
            "side": "long",
            "pattern_type": "higher_low",
            "structure_break_timestamp": "2026-03-01T00:00:00+00:00",
            "warnings": [],
        }
    ]
    mom = [
        {
            "setup_id": "a",
            "confirmation_timestamp": "2026-03-01T00:00:00+00:00",
            "confidence": "high",
            "candles_after_price_action_confirmation": 0,
            "confirmation_type": "break_candle",
        }
    ]
    events = [{"setup_id": "a", "event": "momentum_confirmed", "reason": None}]
    rows = build_signal_rows(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
    )
    assert rows[0]["confirmation_age"] == 0
    groups = signal_groups(rows[0])
    assert "confirmed_age0" in groups
    assert "confirmed_high_age0" in groups
    assert "momentum_confirmed" in groups


def test_end_to_end_deterministic_small() -> None:
    frame = pd.DataFrame(
        [
            _c("2026-03-01T00:00:00+00:00", 100, 100.1, 99.9, 100.0),
            _c("2026-03-01T00:05:00+00:00", 100, 100.30, 99.8, 100.2),
            _c("2026-03-01T00:10:00+00:00", 100.2, 100.4, 100.0, 100.3),
        ]
    )
    pa = [
        {
            "setup_id": "a",
            "side": "long",
            "pattern_type": "higher_low",
            "structure_break_timestamp": "2026-03-01T00:00:00+00:00",
            "warnings": [],
        }
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
    events = [{"setup_id": "a", "event": "momentum_confirmed", "reason": None}]
    p1 = run_tp_hit_audit(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
        candles=frame,
        horizons=(1, 2),
        tp_thresholds=(0.25,),
    )
    p2 = run_tp_hit_audit(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
        candles=frame,
        horizons=(1, 2),
        tp_thresholds=(0.25,),
    )
    assert p1["audit_summary"]["signal_counts"] == p2["audit_summary"]["signal_counts"]
    g = next(
        r
        for r in p1["group_tp_summary"]
        if r["group"] == "momentum_confirmed" and r["horizon"] == 1
    )
    assert g["tp_hits"] == 1
    assert g["n_evaluable"] == 1
