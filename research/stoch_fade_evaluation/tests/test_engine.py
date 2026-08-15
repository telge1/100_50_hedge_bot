from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from research.stoch_fade_evaluation.engine import evaluate_tier_a_signals, generation_key
from research.stoch_fade_evaluation.guards import assert_no_writers_or_be50_eval_path
from research.stoch_fade_evaluation.identity import frozen_outcome_identity


class _View:
    def __init__(self, result: str, pnl: float):
        self.result = result
        self.display_result = result
        self._pnl = pnl

    def as_api(self):
        return {
            "display_result": self.result,
            "exit_reason": "TP" if self.result == "WIN" else "SL",
            "exit_time": "2026-08-01T03:00:00Z",
            "exit_price": 10.1,
            "pnl_pct": self._pnl,
            "duration_seconds": 3600,
            "be50_activated": False,
            "be50_activated_at": None,
            "entry_time": "2026-08-01T02:00:00Z",
            "entry_price": 10.0,
            "ambiguity_flag": None,
        }


def test_identity_pins_no_be50_engine():
    ident = frozen_outcome_identity()
    assert ident["signal_strategy_version"] == "wave_fade_frozen_f16ae32"
    assert ident["exit_policy"] == "NO_BE50"
    assert ident["uses_be50_exit_for_evaluation"] is False
    assert ident["outcome_engine"] == "evaluate_signal_no_be50"
    assert ident["scan_exit"].endswith("scan_exit_sl_first")


def test_guards_reject_be50_eval_import():
    assert_no_writers_or_be50_eval_path()


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


def test_tier_a_only_independent_no_dedup(monkeypatch):
    calls = []

    def fake_no_be50(signal, frame, as_of=None):
        calls.append(signal["signal_id"])
        return _View("WIN", 1.0)

    monkeypatch.setattr(
        "signal_generator.pipeline.outcome_eval.evaluate_signal_no_be50",
        fake_no_be50,
    )
    monkeypatch.setattr(
        "signal_generator.pipeline.outcome_eval.summarize_trade_views",
        lambda views: {"signals": len(views), "wins": len(views), "losses": 0, "open": 0},
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
    rows, summary, _ident = evaluate_tier_a_signals(
        _signals(),
        frame,
        evaluation_id="e1",
        source_job_id="j1",
    )
    assert calls == ["a", "b"]
    assert len(rows) == 2
    assert generation_key(_signals()[0]) == generation_key(_signals()[1])
    assert summary["execution_dedup_applied"] is False
    assert summary["exit_policy"] == "NO_BE50"
    assert all(r["outcome"] in ("WIN", "LOSS", "OPEN") for r in rows)
    assert all(r["be_activated"] is False for r in rows)


def test_be_result_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "signal_generator.pipeline.outcome_eval.evaluate_signal_no_be50",
        lambda signal, frame, as_of=None: _View("BE / LOSS", 0.0),
    )
    monkeypatch.setattr(
        "signal_generator.pipeline.outcome_eval.summarize_trade_views",
        lambda views: {},
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
