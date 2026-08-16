"""Unit tests for dashboard signal feed query (SignalRepository.query_signals)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from signal_generator.bybit.live.control_api import _signal_row_to_api
from signal_generator.db.signals import SignalRepository


def _dt(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def test_query_signals_builds_filters_and_newest_first():
    client = MagicMock()
    client.database = "signal_generator"

    count_result = MagicMock()
    count_result.result_rows = [(3,)]

    row_cols = [
        "signal_id",
        "generated_at",
        "candle_open_time",
        "candle_close_time",
        "symbol",
        "timeframe",
        "direction",
        "signal_type",
        "signal_price",
        "stoch_k",
        "stoch_d",
        "wave_state",
        "tier_a",
        "tier_a_context",
        "rank_score",
        "selected",
        "selection_reason",
        "trend_15m",
        "trend_30m",
        "trend_1h",
        "trend_4h",
        "signal_bias",
        "traded",
        "trade_id",
        "generator_version",
        "strategy_version",
        "metadata",
        "ingested_at",
    ]
    close_new = _dt(2026, 8, 10, 16, 30)
    close_old = _dt(2026, 8, 10, 14, 30)
    gen_late = _dt(2026, 8, 10, 16, 30, 4)
    data_result = MagicMock()
    data_result.column_names = row_cols
    data_result.result_rows = [
        (
            "id-new",
            gen_late,
            _dt(2026, 8, 10, 16, 15),
            close_new,
            "APTUSDT",
            "15m",
            "LONG",
            "WAVE_FADE",
            0.59,
            12.3,
            15.8,
            "DOWN",
            1,
            "{}",
            1.0,
            0,
            "",
            "UP",
            "UP",
            "UP",
            "UP",
            "LONG",
            0,
            "",
            "g1",
            "s1",
            "{}",
            gen_late,
        ),
        (
            "id-catchup",
            gen_late,
            _dt(2026, 8, 10, 14, 15),
            close_old,
            "DOGEUSDT",
            "15m",
            "SHORT",
            "WAVE_FADE",
            0.1,
            80.0,
            75.0,
            "UP",
            0,
            "{}",
            0.5,
            0,
            "",
            "DOWN",
            "DOWN",
            "DOWN",
            "DOWN",
            "SHORT",
            0,
            "",
            "g1",
            "s1",
            "{}",
            gen_late,
        ),
    ]
    client.query.side_effect = [count_result, data_result]

    repo = SignalRepository(client)
    rows, total = repo.query_signals(
        start=_dt(2026, 8, 9),
        end=_dt(2026, 8, 11),
        symbols=["APTUSDT", "DOGEUSDT"],
        timeframe="15m",
        direction="LONG",
        tier_a=True,
        selected=False,
        limit=50,
        offset=0,
    )
    assert total == 3
    assert len(rows) == 2
    assert rows[0]["signal_id"] == "id-new"
    assert rows[1]["candle_close_time"] == close_old
    assert rows[1]["generated_at"] == gen_late

    count_sql = client.query.call_args_list[0].args[0]
    data_sql = client.query.call_args_list[1].args[0]
    assert "FINAL" in count_sql
    assert "FINAL" in data_sql
    assert "candle_close_time DESC" in data_sql
    params = client.query.call_args_list[1].kwargs["parameters"]
    assert params["direction"] == "LONG"
    assert params["tier_a"] == 1
    assert params["selected"] == 0
    assert params["timeframe"] == "15m"
    assert "APTUSDT" in params["symbols"]


def test_query_signals_rejects_bad_time_field():
    repo = SignalRepository(MagicMock())
    with pytest.raises(ValueError):
        repo.query_signals(
            start=_dt(2026, 1, 1),
            end=_dt(2026, 1, 2),
            time_field="not_a_field",
        )


def test_signal_row_to_api_preserves_catchup_timestamps():
    close = _dt(2026, 8, 10, 14, 30)
    gen = _dt(2026, 8, 10, 16, 30, 4)
    out = _signal_row_to_api(
        {
            "signal_id": "abc",
            "symbol": "APTUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "signal_type": "WAVE_FADE",
            "signal_price": 0.5926,
            "candle_open_time": _dt(2026, 8, 10, 14, 15),
            "candle_close_time": close,
            "generated_at": gen,
            "tier_a": True,
            "tier_a_context": "ctx",
            "stoch_k": 12.3,
            "stoch_d": 15.8,
            "wave_state": "DOWN",
            "rank_score": 1.2,
            "selected": False,
            "selection_reason": "",
            "trend_15m": "UP",
            "trend_30m": "UP",
            "trend_1h": "UP",
            "trend_4h": "FLAT",
            "signal_bias": "LONG",
            "traded": False,
            "trade_id": "",
            "generator_version": "g",
            "strategy_version": "s",
            "metadata": {},
        }
    )
    assert out["signal_id"] == "abc"
    assert out["tier_a"] is True
    assert out["candle_close_time"].startswith("2026-08-10T14:30:00")
    assert out["generated_at"].startswith("2026-08-10T16:30:04")
    assert out["direction"] == "LONG"
    assert out["marker"] == "▲"
    assert out["signal_price"] == 0.5926
    assert isinstance(out["signal_price"], float)
