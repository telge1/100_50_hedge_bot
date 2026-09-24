from __future__ import annotations

import json
from pathlib import Path

from ema_pool_trend_flip_v1.research_feed import MISSING_MESSAGE, missing_response, research_signals_response
from ema_pool_trend_flip_v1.schema import STRATEGY_ID


def test_missing_artifact_no_baseline_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_POOL_TREND_FLIP_RESEARCH_REGISTRY", str(tmp_path / "missing.json"))
    payload = research_signals_response(symbol="ACEUSDT")
    assert payload["success"] is True
    assert payload["feed_ready"] is False
    assert payload["signals"] == []
    assert payload["message"] == MISSING_MESSAGE
    assert "wave_fade" not in json.dumps(payload)


def test_missing_response_contract():
    m = missing_response()
    assert m["feed_ready"] is False
    assert m["collector_called"] is False
    assert m["strategy_id"] == STRATEGY_ID


def test_feed_reads_only_artifact(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    manifest = {
        "run_id": "t1",
        "strategy_id": STRATEGY_ID,
        "complete": True,
        "productive": True,
        "test_fixture_only": False,
        "pool_candle_source": "clickhouse",
        "clickhouse": {
            "pool_candle_source": "clickhouse",
            "database": "signal_generator",
            "table": "candles_1m",
            "exchange": "bybit",
            "interval": "1m",
            "final": True,
            "is_closed": 1,
        },
        "planner": {"pin_ok": True, "commit": "c6c960a82e9a0c538dbe24b03f481893e722072f"},
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "trades.jsonl").write_text(
        json.dumps(
            {
                "signal_id": "s1",
                "symbol": "ACEUSDT",
                "variant": "EMA_POOL_TREND_FLIP_V1_STATIC",
                "decision": "ALIGNED",
                "executed_direction": "LONG",
                "original_direction": "LONG",
                "entry_time": "2026-08-12T16:00:00Z",
                "entry_price": 1.0,
                "outcome": "OPEN",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "blocked_signals.jsonl").write_text("", encoding="utf-8")
    (run / "ignored_duplicates.jsonl").write_text("", encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"STATIC": {"wins": 0}}), encoding="utf-8")
    reg = {
        "strategy_id": STRATEGY_ID,
        "research_only": True,
        "live_trading": False,
        "artifact_dir": str(run),
        "planner_version": "c6c960a82e9a0c538dbe24b03f481893e722072f",
        "symbol": "ACEUSDT",
    }
    reg_path = tmp_path / "reg.json"
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setenv("EMA_POOL_TREND_FLIP_RESEARCH_REGISTRY", str(reg_path))
    payload = research_signals_response(symbol="ACEUSDT")
    assert payload["feed_ready"] is True
    assert payload["signals"][0]["signal_id"] == "s1"
    empty = research_signals_response(symbol="HYPEUSDT")
    assert empty["signals"] == []


def test_incomplete_not_ready(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    manifest = {
        "run_id": "bad",
        "strategy_id": STRATEGY_ID,
        "complete": False,
        "pool_candle_source": "clickhouse",
        "clickhouse": {
            "database": "signal_generator",
            "table": "candles_1m",
            "exchange": "bybit",
            "interval": "1m",
            "final": True,
            "is_closed": 1,
        },
        "planner": {"pin_ok": True, "commit": "c6c960a82e9a0c538dbe24b03f481893e722072f"},
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    reg = {
        "strategy_id": STRATEGY_ID,
        "research_only": True,
        "live_trading": False,
        "artifact_dir": str(run),
        "planner_version": "c6c960a82e9a0c538dbe24b03f481893e722072f",
    }
    p = tmp_path / "reg.json"
    p.write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setenv("EMA_POOL_TREND_FLIP_RESEARCH_REGISTRY", str(p))
    payload = research_signals_response()
    assert payload["feed_ready"] is False
