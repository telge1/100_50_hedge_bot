"""Tests for momentum TP path / drawdown / intrabar audit."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.momentum_forward_audit import build_signal_rows
from research.regime_scanner.momentum_tp_path_audit import (
    RESOLUTION_ADVERSE_FIRST,
    RESOLUTION_MISSING_1M,
    RESOLUTION_TP_FIRST,
    RESOLUTION_UNRESOLVED_1M,
    compute_execution_rates,
    compute_signal_tp_path,
    drawdown_bucket,
    resolve_same_candle_with_1m,
    run_tp_path_audit,
)


def _c(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_long_short_path_mirror() -> None:
    long_f = [_c("t1", 100, 100.40, 99.80, 100.2)]
    short_f = [_c("t1", 100, 100.20, 99.60, 99.8)]
    long_p = compute_signal_tp_path(
        side="long", reference_close=100.0, future_candles=long_f, horizon=1, tp_pct=0.25
    )
    short_p = compute_signal_tp_path(
        side="short", reference_close=100.0, future_candles=short_f, horizon=1, tp_pct=0.25
    )
    assert long_p["tp_hit"] is True and short_p["tp_hit"] is True
    assert abs(long_p["mae_including_tp_candle_pct"] - 0.20) < 1e-12
    assert abs(short_p["mae_including_tp_candle_pct"] - 0.20) < 1e-12


def test_mae_before_excludes_tp_candle() -> None:
    future = [
        _c("t1", 100, 100.10, 99.40, 99.8),  # adverse 0.60
        _c("t2", 99.8, 100.50, 99.70, 100.2),  # hit; adverse 0.30 on hit candle
    ]
    out = compute_signal_tp_path(
        side="long", reference_close=100.0, future_candles=future, horizon=2, tp_pct=0.25
    )
    assert out["tp_hit"] is True
    assert abs(out["mae_before_tp_pct"] - 0.60) < 1e-12
    assert abs(out["mae_including_tp_candle_pct"] - 0.60) < 1e-12  # max(0.60, 0.30)


def test_mae_including_tp_candle_higher_when_hit_worse() -> None:
    future = [
        _c("t1", 100, 100.10, 99.80, 100.0),  # adverse 0.20
        _c("t2", 100, 100.40, 99.40, 100.2),  # hit + adverse 0.60
    ]
    out = compute_signal_tp_path(
        side="long", reference_close=100.0, future_candles=future, horizon=2, tp_pct=0.25
    )
    assert abs(out["mae_before_tp_pct"] - 0.20) < 1e-12
    assert abs(out["mae_including_tp_candle_pct"] - 0.60) < 1e-12


def test_drawdown_bucket_boundaries() -> None:
    assert drawdown_bucket(0.0) == "0.00-0.25"
    assert drawdown_bucket(0.25) == "0.00-0.25"
    assert drawdown_bucket(0.2500001) == "0.25-0.50"
    assert drawdown_bucket(0.50) == "0.25-0.50"
    assert drawdown_bucket(0.75) == "0.50-0.75"
    assert drawdown_bucket(1.00) == "0.75-1.00"
    assert drawdown_bucket(1.50) == "1.00-1.50"
    assert drawdown_bucket(1.5000001) == ">1.50"


def test_signal_without_tp_uses_path_mae() -> None:
    future = [_c(f"t{i}", 100, 100.10, 99.50, 99.9) for i in range(12)]
    out = compute_signal_tp_path(
        side="long", reference_close=100.0, future_candles=future, horizon=12, tp_pct=0.25
    )
    assert out["tp_hit"] is False
    assert out["mae_before_tp_pct"] is None
    assert abs(out["path_mae_pct"] - 0.50) < 1e-12


def test_later_tp_at_24_or_48() -> None:
    # 12 candles weak, then strong move
    future = [_c(f"t{i}", 100, 100.10, 99.9, 100.0) for i in range(30)]
    future[15] = _c("t15", 100, 100.40, 99.9, 100.2)  # age 15 → within 24
    p12 = compute_signal_tp_path(
        side="long", reference_close=100.0, future_candles=future, horizon=12, tp_pct=0.25
    )
    p24 = compute_signal_tp_path(
        side="long", reference_close=100.0, future_candles=future, horizon=24, tp_pct=0.25
    )
    assert p12["tp_hit"] is False
    assert p24["tp_hit"] is True
    assert p24["first_hit_age"] == 15


def test_1m_tp_first() -> None:
    hit_5m = _c("2026-03-01T00:00:00+00:00", 100, 100.40, 99.50, 100.1)
    bars_1m = pd.DataFrame(
        [
            _c("2026-03-01T00:00:00+00:00", 100, 100.30, 99.90, 100.2),  # TP first
            _c("2026-03-01T00:01:00+00:00", 100.2, 100.4, 99.50, 99.8),  # later adverse
        ]
    )
    bars_1m["timestamp"] = pd.to_datetime(bars_1m["timestamp"], utc=True)
    out = resolve_same_candle_with_1m(
        side="long",
        reference_close=100.0,
        tp_pct=0.25,
        hit_candle_5m=hit_5m,
        candles_1m=bars_1m,
    )
    assert out["resolution"] == RESOLUTION_TP_FIRST


def test_1m_adverse_first() -> None:
    hit_5m = _c("2026-03-01T00:00:00+00:00", 100, 100.40, 99.50, 100.1)
    bars_1m = pd.DataFrame(
        [
            _c("2026-03-01T00:00:00+00:00", 100, 100.10, 99.50, 99.7),  # adverse extreme first
            _c("2026-03-01T00:01:00+00:00", 99.7, 100.40, 99.7, 100.3),  # then TP
        ]
    )
    bars_1m["timestamp"] = pd.to_datetime(bars_1m["timestamp"], utc=True)
    out = resolve_same_candle_with_1m(
        side="long",
        reference_close=100.0,
        tp_pct=0.25,
        hit_candle_5m=hit_5m,
        candles_1m=bars_1m,
    )
    assert out["resolution"] == RESOLUTION_ADVERSE_FIRST


def test_1m_same_bar_unresolved() -> None:
    hit_5m = _c("2026-03-01T00:00:00+00:00", 100, 100.40, 99.50, 100.1)
    bars_1m = pd.DataFrame(
        [_c("2026-03-01T00:00:00+00:00", 100, 100.40, 99.50, 100.0)]  # both in one 1m
    )
    bars_1m["timestamp"] = pd.to_datetime(bars_1m["timestamp"], utc=True)
    out = resolve_same_candle_with_1m(
        side="long",
        reference_close=100.0,
        tp_pct=0.25,
        hit_candle_5m=hit_5m,
        candles_1m=bars_1m,
    )
    assert out["resolution"] == RESOLUTION_UNRESOLVED_1M


def test_missing_1m_data() -> None:
    hit_5m = _c("2026-03-01T00:00:00+00:00", 100, 100.40, 99.50, 100.1)
    out = resolve_same_candle_with_1m(
        side="long",
        reference_close=100.0,
        tp_pct=0.25,
        hit_candle_5m=hit_5m,
        candles_1m=None,
    )
    assert out["resolution"] == RESOLUTION_MISSING_1M


def test_confirmation_age_zero_preserved() -> None:
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
    from research.regime_scanner.momentum_tp_hit_audit import signal_groups

    assert "confirmed_age0" in signal_groups(rows[0])


def test_no_artificial_intrabar_order_without_1m() -> None:
    """Ambiguous 5m hit must not invent tp_first without 1m."""
    rows = [
        {
            "evaluable": True,
            "tp_hit_12": True,
            "same_candle_ambiguous": True,
            "intrabar_resolution": RESOLUTION_MISSING_1M,
            "groups": ["momentum_confirmed"],
        },
        {
            "evaluable": True,
            "tp_hit_12": True,
            "same_candle_ambiguous": False,
            "intrabar_resolution": "not_ambiguous",
            "groups": ["momentum_confirmed"],
        },
    ]
    rates = compute_execution_rates(rows)
    assert rates["optimistic_hits"] == 2
    assert rates["conservative_hits"] == 1  # only unambiguous
    assert rates["missing_1m_data"] == 1
    # resolved_only excludes missing from denominator: 1 hit / 1 resolved = 1.0
    assert abs(rates["resolved_only_hit_rate"] - 1.0) < 1e-12


def test_end_to_end_missing_1m_audit() -> None:
    frame = pd.DataFrame(
        [_c(f"2026-03-01T00:{i:02d}:00+00:00", 100, 100.1 + i * 0.01, 99.5, 100.0) for i in range(20)]
    )
    # Force a clear TP on first future candle with adverse (ambiguous)
    frame.loc[1, "high"] = 100.40
    frame.loc[1, "low"] = 99.50
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
    payload = run_tp_path_audit(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
        candles=frame,
        symbol="APTUSDT",
    )
    assert payload["audit_summary"]["one_m_data_status"] == "missing_1m_data"
    row = payload["signal_tp_paths"][0]
    assert row["confirmation_age"] == 0
    assert row["same_candle_ambiguous"] is True
    assert row["intrabar_resolution"] == RESOLUTION_MISSING_1M
