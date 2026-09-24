"""Constants and source stamps. No baseline/Pool-V1 mutation."""

from __future__ import annotations

from typing import Any

STRATEGY_ID = "EMA_POOL_TREND_FLIP_V1"
TEST_FIXTURE_ONLY = "TEST_FIXTURE_ONLY"

REASON_DUP = "DUPLICATE_SYMBOL_ENTRY_TIME"
REASON_TREND = "TREND_CONTEXT_NOT_CONFIRMED"
REASON_NO_SL = "NO_STRUCTURAL_PROTECTION_POOL"
REASON_EPISODE = "STOCHASTIC_EPISODE_ALREADY_TRADED"
REASON_NO_CANDLES = "NO_CANDLES"
REASON_TZ = "INVALID_TIMEZONE"

DECISION_FLIPPED = "FLIPPED"
DECISION_ALIGNED = "ALIGNED"
DECISION_BLOCKED = "BLOCKED"
DECISION_NO_TRADE = "NO_TRADE"


def clickhouse_candle_stamp() -> dict[str, Any]:
    return {
        "pool_candle_source": "clickhouse",
        "database": "signal_generator",
        "table": "candles_1m",
        "exchange": "bybit",
        "interval": "1m",
        "final": True,
        "is_closed": 1,
        "timezone": "UTC",
    }


def is_clickhouse_candle_source(manifest: dict[str, Any]) -> bool:
    src = str(manifest.get("pool_candle_source") or "").lower()
    if src != "clickhouse":
        ch = manifest.get("clickhouse") if isinstance(manifest.get("clickhouse"), dict) else {}
        src = str(ch.get("pool_candle_source") or ch.get("source") or "").lower()
    if src != "clickhouse":
        return False
    ch = manifest.get("clickhouse") if isinstance(manifest.get("clickhouse"), dict) else manifest
    return (
        str(ch.get("database") or "") == "signal_generator"
        and str(ch.get("table") or "") == "candles_1m"
        and str(ch.get("exchange") or "").lower() == "bybit"
        and str(ch.get("interval") or "") == "1m"
        and bool(ch.get("final") is True or ch.get("final") == True)
        and int(ch.get("is_closed") or 0) == 1
    )


def is_test_fixture_only(manifest: dict[str, Any]) -> bool:
    return bool(manifest.get("test_fixture_only")) or str(manifest.get("candle_source") or "") == TEST_FIXTURE_ONLY
