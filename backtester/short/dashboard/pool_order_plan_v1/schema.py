"""Reason codes and record helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

STRATEGY_ID = "POOL_ORDER_PLAN_V1"

REASON_INVALID_ENTRY = "INVALID_ENTRY"
REASON_NO_CANDLES = "NO_CANDLES_FOR_SYMBOL"
REASON_ENTRY_BEFORE = "ENTRY_BEFORE_HISTORY"
REASON_ENTRY_AFTER = "ENTRY_AFTER_HISTORY"
REASON_LAST_5M_INCOMPLETE = "LAST_5M_INCOMPLETE"
REASON_NO_TP1 = "NO_TP1_AT_ENTRY"
REASON_NO_SL = "NO_SL_AT_ENTRY"
REASON_DYNAMIC = "DYNAMIC_MODE_EXCLUDED"
REASON_PLANNER_ERROR = "PLANNER_ERROR"
REASON_TZ = "TZ_MISMATCH"
REASON_DUP = "IGNORED_DUPLICATE"
REASON_ARTIFACT = "ARTIFACT_MISSING"
REASON_FUTURE_BAR = "FUTURE_BAR_IN_FRAME"
REASON_BAD_SOURCE = "POOL_CANDLE_SOURCE_NOT_CLICKHOUSE"

POOL_CANDLE_SOURCE_CLICKHOUSE = "clickhouse"
TEST_FIXTURE_ONLY = "TEST_FIXTURE_ONLY"

SOURCE_INTERVAL = "1m"
POOL_INTERVAL = "5m"
AGGREGATION = "strict_contiguous_1m_to_5m"
POOL_ENGINE = "bigbeluga"

# interval=1m is the ClickHouse source interval (backward compatible), not the pool TF.
REQUIRED_CLICKHOUSE_STAMP = {
    "pool_candle_source": POOL_CANDLE_SOURCE_CLICKHOUSE,
    "database": "signal_generator",
    "table": "candles_1m",
    "exchange": "bybit",
    "interval": SOURCE_INTERVAL,
    "final": True,
    "is_closed": 1,
}


def clickhouse_candle_stamp() -> dict:
    return dict(REQUIRED_CLICKHOUSE_STAMP)


def pool_pipeline_stamp() -> dict:
    """Source vs pool timeframe. interval remains 1m for source compatibility."""
    return {
        "source_database": "signal_generator",
        "source_table": "candles_1m",
        "source_interval": SOURCE_INTERVAL,
        "aggregation": AGGREGATION,
        "pool_interval": POOL_INTERVAL,
        "pool_engine": POOL_ENGINE,
        "pool_lookback": 8,
        "pool_warmup_days": 14,
        "replay": False,
    }


def is_clickhouse_candle_source(manifest: dict | None) -> bool:
    if not isinstance(manifest, dict):
        return False
    for key, expected in REQUIRED_CLICKHOUSE_STAMP.items():
        if manifest.get(key) != expected:
            return False
    return True


def is_test_fixture_only(manifest: dict | None) -> bool:
    if not isinstance(manifest, dict):
        return False
    return (
        manifest.get("test_fixture_only") is True
        or manifest.get("pool_candle_source") == TEST_FIXTURE_ONLY
    )


def _parse_utc(open_ts: Any) -> datetime:
    """ISO/datetime UTC parse without pandas (dashboard venv has no pandas)."""
    if isinstance(open_ts, datetime):
        dt = open_ts
    else:
        text = str(open_ts).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def last_5m_close_from_open(open_ts: Any) -> datetime:
    return _parse_utc(open_ts) + timedelta(minutes=5)


def is_confirmed_5m_pool_run(manifest: dict | None) -> bool:
    """Recognize new stamps and the frozen ACE comparison run without rewriting it."""
    if not isinstance(manifest, dict):
        return False
    if manifest.get("pool_interval") == POOL_INTERVAL:
        return True
    if not manifest.get("single_pass"):
        return False
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    if counts.get("pool_engine_runs") != 1:
        return False
    timings = manifest.get("timings") if isinstance(manifest.get("timings"), dict) else {}
    symbols = timings.get("symbols") if isinstance(timings.get("symbols"), dict) else {}
    return any(
        isinstance(block, dict) and block.get("aggregate_1m_5m_s") is not None
        for block in symbols.values()
    )


STATUS_READY = "READY"
STATUS_NO_PLAN = "NO_PLAN"
STATUS_IGNORED = "IGNORED_DUPLICATE"

OUTCOME_OPEN = "OPEN"
OUTCOME_SL = "SL"
OUTCOME_TP1 = "TP1"
OUTCOME_TP1_TP2 = "TP1_TP2"
OUTCOME_TP1_SL = "TP1_SL"
