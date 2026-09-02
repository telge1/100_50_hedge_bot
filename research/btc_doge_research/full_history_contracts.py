"""Frozen contracts for resumable BTC/DOGE full-history backfill."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .contracts import ALLOWED_SYMBOLS, stable_hash
from .phase2_contracts import ORDERING_AMBIGUOUS_BUCKETS, PHASE2_CONTRACT_VERSION

FULL_HISTORY_CONTRACT_VERSION = "btc_doge_research_full_history_v1"
FULL_HISTORY_BUILD_ID = stable_hash(
    {
        "contract": FULL_HISTORY_CONTRACT_VERSION,
        "processor": "btc_doge_research_processor_v2",
        "symbols": sorted(ALLOWED_SYMBOLS),
    }
)

LIVE_PRODUCER_ID = "BYBIT_OB200_LIVE_COLLECTOR_V3"
LIVE_RAW_FROM = datetime(2026, 8, 24, 22, 47, 54, tzinfo=timezone.utc)
LIVE_TERMINAL = datetime(2026, 8, 28, 16, 26, 23, tzinfo=timezone.utc)
LIVE_TERMINAL_REASON = "queue_full"
DAY_ZIP_PRODUCER_ID = "BYBIT_OB200_DAY_ZIP_IMPORTER_V3"
DAY_ZIP_END = datetime(2026, 8, 18, tzinfo=timezone.utc)

OB_SEMANTICS = "raw_ob200_event_time_eos_v1"
OB_RECONSTRUCTION_CLOCK = "EVENT_TIME_END_OF_SECOND"
EXPECTED_OI = 17280
EXPECTED_CANDLES = 1440
EXPECTED_OB_SECONDS = 86400
EXPECTED_OB_FILES = 24
MIN_DISK_RESERVE_GIB = 20
STORAGE_SAFETY_FACTOR = 1.35
PILOT_COMPRESSED_BYTES = 104_694_680


def day_build_id(symbol: str, day: datetime) -> str:
    return stable_hash(
        {
            "contract": FULL_HISTORY_CONTRACT_VERSION,
            "symbol": symbol,
            "day": day.strftime("%Y-%m-%d"),
            "build": FULL_HISTORY_BUILD_ID,
        }
    )


def day_batch_id(symbol: str, day: datetime) -> str:
    return f"full_history:{symbol}:{day:%Y%m%d}:{FULL_HISTORY_BUILD_ID[:16]}"


def pilot_batch_id() -> str:
    from .phase2_runner import BATCH_ID

    return BATCH_ID


def ordering_ambiguous_for_day(symbol: str, day: datetime) -> list[str]:
    day_str = day.strftime("%Y-%m-%d")
    return [
        ts
        for sym, ts in ORDERING_AMBIGUOUS_BUCKETS
        if sym == symbol and ts.startswith(day_str)
    ]


def full_history_contract() -> dict[str, Any]:
    return {
        "contract_version": FULL_HISTORY_CONTRACT_VERSION,
        "build_id": FULL_HISTORY_BUILD_ID,
        "database": "btc_doge_research",
        "symbols": sorted(ALLOWED_SYMBOLS),
        "live_producer_id": LIVE_PRODUCER_ID,
        "live_raw_from": LIVE_RAW_FROM.isoformat().replace("+00:00", "Z"),
        "live_terminal": LIVE_TERMINAL.isoformat().replace("+00:00", "Z"),
        "live_terminal_reason": LIVE_TERMINAL_REASON,
        "day_zip_producer_id": DAY_ZIP_PRODUCER_ID,
        "day_zip_end": DAY_ZIP_END.isoformat().replace("+00:00", "Z"),
        "ob_semantics": OB_SEMANTICS,
        "phase2_contract": PHASE2_CONTRACT_VERSION,
        "ordering_ambiguous_preserved": True,
        "silent_producer_mix_forbidden": True,
    }
