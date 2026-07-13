"""Tests for deepest-drop + later-recovery audit."""

from __future__ import annotations

from research.regime_scanner.momentum_deepest_drop_recovery import (
    compute_deepest_drop_recovery,
)


def _c(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def _pad(future: list[dict], n: int = 96) -> list[dict]:
    out = list(future)
    last = out[-1]
    while len(out) < n:
        i = len(out)
        out.append(
            _c(
                f"p{i}",
                last["close"],
                last["close"],
                last["close"],
                last["close"],
            )
        )
    return out[:n]


def test_long_deepest_then_later_high() -> None:
    future = _pad(
        [
            _c("a", 100, 100.2, 99.0, 99.2),  # deepest 99
            _c("b", 99.2, 99.5, 99.1, 99.3),  # high before would be ignored if before deep
            _c("c", 99.3, 100.4, 99.2, 100.2),  # later high after deepest
        ]
    )
    out = compute_deepest_drop_recovery(
        side="long", signal_price=100.0, future_candles=future, horizon=96
    )
    assert out["evaluable"] is True
    assert abs(out["adverse_extreme_price"] - 99.0) < 1e-12
    assert abs(out["max_adverse_drop_pct"] - 1.0) < 1e-12
    assert abs(out["later_favorable_price"] - 100.4) < 1e-12
    assert abs(out["later_favorable_vs_signal_pct"] - 0.4) < 1e-12
    assert out["returned_to_signal"] is True
    assert out["reached_plus_025"] is True


def test_short_mirror() -> None:
    future = _pad(
        [
            _c("a", 100, 101.0, 99.9, 100.8),  # adverse high 101
            _c("b", 100.8, 100.9, 99.5, 99.6),  # later low 99.5
        ]
    )
    out = compute_deepest_drop_recovery(
        side="short", signal_price=100.0, future_candles=future, horizon=96
    )
    assert abs(out["adverse_extreme_price"] - 101.0) < 1e-12
    assert abs(out["max_adverse_drop_pct"] - 1.0) < 1e-12
    assert abs(out["later_favorable_price"] - 99.5) < 1e-12
    assert abs(out["later_favorable_vs_signal_pct"] - 0.5) < 1e-12


def test_high_before_low_does_not_count() -> None:
    # Early high 101, then deepest low 99, then modest recovery to 99.5
    future = _pad(
        [
            _c("a", 100, 101.0, 99.8, 100.5),  # high BEFORE deepest — must not count
            _c("b", 100.5, 100.6, 99.0, 99.2),  # deepest
            _c("c", 99.2, 99.5, 99.1, 99.4),  # later high only 99.5
        ]
    )
    out = compute_deepest_drop_recovery(
        side="long", signal_price=100.0, future_candles=future, horizon=96
    )
    assert abs(out["adverse_extreme_price"] - 99.0) < 1e-12
    assert abs(out["later_favorable_price"] - 99.5) < 1e-12
    assert out["later_favorable_price"] < 101.0


def test_high_after_low_counts() -> None:
    future = _pad(
        [
            _c("a", 100, 100.1, 99.0, 99.2),
            _c("b", 99.2, 100.8, 99.1, 100.5),
        ]
    )
    out = compute_deepest_drop_recovery(
        side="long", signal_price=100.0, future_candles=future, horizon=96
    )
    assert abs(out["later_favorable_price"] - 100.8) < 1e-12
    assert out["later_favorable_age"] == 1


def test_adverse_at_data_end() -> None:
    future = _pad([_c("a", 100, 100.1, 99.5, 99.6)])
    # Make last candle the deepest
    future[-1] = _c("end", 99.0, 99.1, 98.0, 98.5)
    # Also make sure earlier not deeper
    for i in range(95):
        future[i] = _c(f"t{i}", 100, 100.1, 99.5, 99.8)
    future[-1] = _c("end", 99.0, 99.1, 98.0, 98.5)
    out = compute_deepest_drop_recovery(
        side="long", signal_price=100.0, future_candles=future, horizon=96
    )
    assert out["adverse_extreme_age"] == 95
    assert out["no_future_recovery_data"] is True
    assert out["later_favorable_price"] is None
