from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from research.stoch_fade_evaluation.engine import evaluate_tier_a_signals, generation_key
from research.stoch_fade_evaluation.full_1m_scan import evaluate_signal_no_be50_full_1m
from research.stoch_fade_evaluation.guards import assert_no_writers_or_be50_eval_path
from research.stoch_fade_evaluation.identity import frozen_outcome_identity


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_identity_pins_full_1m_engine_and_sg_hold_untouched():
    ident = frozen_outcome_identity()
    assert ident["signal_strategy_version"] == "wave_fade_frozen_f16ae32"
    assert ident["exit_policy"] == "NO_BE50"
    assert ident["uses_be50_exit_for_evaluation"] is False
    assert ident["outcome_engine"] == "evaluate_signal_no_be50_full_1m"
    assert ident["max_hold_applied"] is False
    assert ident["sg_no_be50_engine_unchanged"].endswith("evaluate_signal_no_be50")
    assert ident["sg_hold_end_i_still_present"].endswith("hold_end_i")


def test_guards_reject_be50_eval_import():
    assert_no_writers_or_be50_eval_path()


def test_bnb_regression_tp_after_old_max_hold():
    et = datetime(2026, 1, 1, 7, 31, tzinfo=timezone.utc)
    bars = []
    t = et
    # 24h of no-touch then TP at 08:08 next day
    while t < datetime(2026, 1, 2, 8, 8, tzinfo=timezone.utc):
        bars.append(_bar(t, 859.0, 860.0, 858.0, 859.0))
        t += timedelta(minutes=1)
    bars.append(_bar(datetime(2026, 1, 2, 8, 8, tzinfo=timezone.utc), 859.0, 867.59, 858.0, 867.0))
    pin = datetime(2026, 8, 11, 11, 16, tzinfo=timezone.utc)
    bars.append(_bar(pin, 900.0, 901.0, 899.0, 900.0))
    sig = {
        "signal_id": "60725130-15fc-5e7d-8b8f-8f0e9ab42ad9",
        "symbol": "BNBUSDT",
        "timeframe": "15m",
        "direction": "LONG",
        "entry_time": "2026-01-01T07:31:00Z",
        "entry_price": 859.0,
        "tp_price": 867.59,
        "sl_price": 850.41,
        "tier_a": True,
        "strategy_version": "wave_fade_frozen_f16ae32",
        "candle_open_time": "2026-01-01T07:30:00Z",
        "signal_type": "wave_fade",
    }
    api = evaluate_signal_no_be50_full_1m(sig, _frame(bars), candle_data_to=pin)
    assert api["result"] == "WIN"
    assert api["exit_reason"] == "TP"
    assert api["exit_time"] == "2026-01-02T08:08:00Z"
    assert api["duration_seconds"] == 37 * 60 + 24 * 3600
    old_hold = et + timedelta(hours=24)
    exit_ts = datetime(2026, 1, 2, 8, 8, tzinfo=timezone.utc)
    assert exit_ts - old_hold == timedelta(minutes=37)


def test_sl_after_old_max_hold():
    et = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    bars = []
    t = et
    while t < et + timedelta(hours=25):
        bars.append(_bar(t, 100.0, 100.5, 99.6, 100.0))
        t += timedelta(minutes=1)
    sl_ts = et + timedelta(hours=25)
    bars.append(_bar(sl_ts, 100.0, 100.2, 98.0, 98.5))
    pin = sl_ts + timedelta(days=1)
    bars.append(_bar(pin, 99.0, 99.1, 98.9, 99.0))
    sig = {
        "signal_id": "sl-after",
        "direction": "LONG",
        "entry_time": "2026-01-01T00:00:00Z",
        "entry_price": 100.0,
        "tp_price": 102.0,
        "sl_price": 98.5,
    }
    api = evaluate_signal_no_be50_full_1m(sig, _frame(bars), candle_data_to=pin)
    assert api["result"] == "LOSS"
    assert api["exit_reason"] == "SL"
    assert api["exit_time"] == "2026-01-02T01:00:00Z"


def test_true_end_of_history_open():
    et = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    pin = datetime(2026, 8, 11, 10, 5, tzinfo=timezone.utc)
    bars = [_bar(et + timedelta(minutes=i), 100.0, 100.2, 99.8, 100.0) for i in range(6)]
    sig = {
        "direction": "LONG",
        "entry_time": "2026-08-11T10:00:00Z",
        "entry_price": 100.0,
        "tp_price": 110.0,
        "sl_price": 90.0,
    }
    api = evaluate_signal_no_be50_full_1m(sig, _frame(bars), candle_data_to=pin)
    assert api["result"] == "OPEN"
    assert api["exit_time"] is None
    assert api["exit_reason"] == "END_OF_HISTORY"
    assert api["duration_seconds"] == 5 * 60


def test_same_bar_sl_first():
    et = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    bars = [_bar(et, 100.0, 102.0, 98.0, 101.0)]
    sig = {
        "direction": "LONG",
        "entry_time": "2026-01-01T00:00:00Z",
        "entry_price": 100.0,
        "tp_price": 101.5,
        "sl_price": 98.5,
    }
    api = evaluate_signal_no_be50_full_1m(sig, _frame(bars), candle_data_to=et)
    assert api["result"] == "LOSS"
    assert api["exit_reason"] == "SL"
    assert api["ambiguity_flag"] == "AMBIGUOUS_INTRABAR"


def test_frozen_cutoff_ignores_later_touch():
    et = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    pin = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100.0, 100.2, 99.8, 100.0),
        _bar(et + timedelta(minutes=1), 100.0, 100.2, 99.8, 100.0),
        _bar(et + timedelta(minutes=2), 100.0, 100.1, 99.9, 100.0),
        _bar(et + timedelta(minutes=3), 100.0, 110.0, 99.0, 105.0),
    ]
    sig = {
        "direction": "LONG",
        "entry_time": "2026-01-01T00:00:00Z",
        "entry_price": 100.0,
        "tp_price": 105.0,
        "sl_price": 90.0,
    }
    api = evaluate_signal_no_be50_full_1m(sig, _frame(bars), candle_data_to=pin)
    assert api["result"] == "OPEN"
    assert api["exit_time"] is None


def test_entry_bar_must_match_exactly():
    et = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    bars = [_bar(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), 100.0, 110.0, 90.0, 100.0)]
    sig = {
        "direction": "LONG",
        "entry_time": "2026-01-01T00:01:00Z",
        "entry_price": 100.0,
        "tp_price": 105.0,
        "sl_price": 95.0,
    }
    api = evaluate_signal_no_be50_full_1m(sig, _frame(bars), candle_data_to=et)
    assert api["result"] == "OPEN"
    assert api["ambiguity_flag"] in {"ENTRY_BAR_MISSING", "NO_CANDLES_AFTER_ENTRY"}


def test_sg_no_be50_still_uses_hold_end():
    from signal_generator.pipeline.outcome_eval import simulate_no_be50_counterfactual
    import inspect

    src = inspect.getsource(simulate_no_be50_counterfactual)
    assert "hold_end_i" in src


def test_portfolio_slot_reuse_after_corrected_win():
    from research.stoch_fade_portfolio_backtest.simulate import simulate_portfolio

    rows = [
        {
            "signal_id": "open-old",
            "symbol": "BNBUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "entry_time": "2026-01-01T07:31:00Z",
            "entry_price": 859.0,
            "outcome": "WIN",
            "exit_time": "2026-01-02T08:08:00Z",
            "pnl_pct_gross": 1.0,
            "duration_seconds": 88800,
            "exit_reason": "TP",
        },
        {
            "signal_id": "next",
            "symbol": "ETHUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "entry_time": "2026-01-02T08:09:00Z",
            "entry_price": 1.0,
            "outcome": "WIN",
            "exit_time": "2026-01-02T09:00:00Z",
            "pnl_pct_gross": 1.0,
            "duration_seconds": 3060,
            "exit_reason": "TP",
        },
        {
            "signal_id": "eoh",
            "symbol": "TRXUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "entry_time": "2026-01-01T08:00:00Z",
            "entry_price": 1.0,
            "outcome": "OPEN",
            "exit_time": None,
            "pnl_pct_gross": None,
            "duration_seconds": 100,
            "exit_reason": "END_OF_HISTORY",
        },
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=2, notional=100)
    ids = [t["signal_id"] for t in sim.accepted]
    assert "open-old" in ids and "next" in ids
    assert any(t["signal_id"] == "eoh" for t in sim.open_at_end)


def _signals():
    return [
        {
            "signal_id": "a",
            "symbol": "AAVEUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "signal_type": "wave_fade",
            "tier_a": True,
            "strategy_version": "wave_fade_frozen_f16ae32",
            "candle_open_time": "2026-08-01T02:00:00Z",
            "entry_time": "2026-08-01T02:00:00Z",
            "entry_price": 10.0,
            "tp_price": 10.1,
            "sl_price": 9.9,
        },
        {
            "signal_id": "b",
            "symbol": "AAVEUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "signal_type": "wave_fade",
            "tier_a": True,
            "strategy_version": "wave_fade_frozen_f16ae32",
            "candle_open_time": "2026-08-01T02:00:00Z",
            "entry_time": "2026-08-01T02:00:00Z",
            "entry_price": 10.0,
            "tp_price": 10.1,
            "sl_price": 9.9,
        },
        {
            "signal_id": "raw",
            "symbol": "AAVEUSDT",
            "tier_a": False,
            "strategy_version": "wave_fade_frozen_f16ae32",
            "candle_open_time": "2026-08-01T02:00:00Z",
        },
    ]


def test_tier_a_only_independent_no_dedup():
    frame = pd.DataFrame(
        {
            "timestamp": [datetime(2026, 8, 1, 2, tzinfo=timezone.utc)],
            "open": [10.0],
            "high": [10.2],
            "low": [9.95],
            "close": [10.05],
        }
    )
    rows, summary, _ident = evaluate_tier_a_signals(
        _signals(),
        frame,
        evaluation_id="e1",
        source_job_id="j1",
        candle_data_to=datetime(2026, 8, 1, 2, tzinfo=timezone.utc),
    )
    assert [r["signal_id"] for r in rows] == ["a", "b"]
    assert generation_key(_signals()[0]) == generation_key(_signals()[1])
    assert summary["execution_dedup_applied"] is False
    assert summary["max_hold_applied"] is False
    assert all(r["outcome"] == "WIN" for r in rows)


def test_be_result_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "research.stoch_fade_evaluation.engine.evaluate_signal_no_be50_full_1m",
        lambda *a, **k: {"result": "BE / LOSS", "display_result": "BE / LOSS", "be50_activated": False},
    )
    frame = pd.DataFrame(
        {
            "timestamp": [datetime(2026, 8, 1, 2, tzinfo=timezone.utc)],
            "open": [10.0],
            "high": [10.2],
            "low": [9.8],
            "close": [10.05],
        }
    )
    with pytest.raises(RuntimeError, match="NO_BE50_RESULT_VIOLATION"):
        evaluate_tier_a_signals(_signals()[:1], frame, evaluation_id="e1", source_job_id="j1")
