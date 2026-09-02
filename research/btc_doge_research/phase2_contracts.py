"""Frozen contracts for the controlled Phase-2 one-day pilot."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .contracts import ALLOWED_SYMBOLS, stable_hash

PHASE2_CONTRACT_VERSION = "btc_doge_research_phase_2_pilot_v1"
PRODUCER_LINEAGE_CONTRACT = "research_producer_lineage_v1"
PHASE2_PROCESSOR_VERSION = "btc_doge_research_processor_v2"
PILOT_DAY = "2026-08-26"
PILOT_START = datetime(2026, 8, 26, tzinfo=timezone.utc)
PILOT_END = datetime(2026, 8, 27, tzinfo=timezone.utc)
BUILD_ID = stable_hash(
    {
        "contract": PHASE2_CONTRACT_VERSION,
        "day": PILOT_DAY,
        "symbols": sorted(ALLOWED_SYMBOLS),
    }
)
TICK_SIZE = {
    "BTCUSDT": Decimal("0.1"),
    "DOGEUSDT": Decimal("0.00001"),
}

ORDERING_AMBIGUOUS_BUCKETS = (
    ("BTCUSDT", "2026-08-25T12:00:14Z"),
    ("DOGEUSDT", "2026-08-25T12:00:14Z"),
    ("DOGEUSDT", "2026-08-28T15:00:06Z"),
    ("DOGEUSDT", "2026-08-28T15:00:12Z"),
)


def producer_lineage_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in sorted(ALLOWED_SYMBOLS):
        live = {
            "symbol": symbol,
            "producer_id": "BYBIT_OB200_LIVE_COLLECTOR_V3",
            "producer_type": "LIVE_WEBSOCKET_COLLECTOR",
            "source_path_or_table": (
                "data/orderbook_raw_shadow/ob200_v3/"
                f"{symbol}/YYYY/MM/DD/*.zst"
            ),
            "source_semantics": "RECEIVE_TIME_ASOF",
            "event_time_available": True,
            "receive_time_available": True,
            "reconstruction_clock": "LOCAL_WALL_CLOCK_BUCKET_FINALIZATION",
            "coverage_start": "2026-08-24T22:47:53.538955Z",
            "coverage_end": "2026-08-28T16:26:23Z",
            "terminal_reason": "queue_full",
            "coverage_complete": False,
            "transition_contract": "raw_ob200_event_time_eos_v1_from_2026-08-24T22:47:54Z",
            "contract_version": PRODUCER_LINEAGE_CONTRACT,
            "build_id": BUILD_ID,
        }
        if symbol == "DOGEUSDT":
            live["coverage_start"] = "2026-08-24T22:47:53.538998Z"
        live["source_fingerprint"] = stable_hash(live)
        rows.append(live)

        importer = {
            "symbol": symbol,
            "producer_id": "BYBIT_OB200_DAY_ZIP_IMPORTER_V3",
            "producer_type": "HISTORICAL_DAY_ZIP_IMPORTER",
            "source_path_or_table": "Bybit public day ZIP -> orderbook_v2.parser.parse_day_zip",
            "source_semantics": "EVENT_TIME_END_OF_SECOND",
            "event_time_available": True,
            "receive_time_available": False,
            "reconstruction_clock": "UTC_EVENT_TIME",
            "coverage_start": "2026-07-19T00:00:00Z",
            "coverage_end": "2026-08-18T00:00:00Z",
            "terminal_reason": "BOUNDED_IMPORT_WINDOW_END",
            "coverage_complete": True,
            "transition_contract": "SEPARATE_PRODUCER_NO_SILENT_OVERLAP_MERGE",
            "contract_version": PRODUCER_LINEAGE_CONTRACT,
            "build_id": BUILD_ID,
        }
        importer["source_fingerprint"] = stable_hash(importer)
        rows.append(importer)
    return rows


def pilot_contract() -> dict[str, Any]:
    return {
        "contract_version": PHASE2_CONTRACT_VERSION,
        "build_id": BUILD_ID,
        "database": "btc_doge_research",
        "symbols": sorted(ALLOWED_SYMBOLS),
        "pilot_start": PILOT_START,
        "pilot_end": PILOT_END,
        "selection": "first fully proven common candidate day",
        "source_semantics_version": "raw_ob200_event_time_eos_v1",
        "ob_reconstruction_clock": "EVENT_TIME_END_OF_SECOND",
        "profile_session_resolver": (
            "research.btc_ob_fight.volume_profile.profile_session_window"
        ),
        "tpo_contract": "tpo_profile_facts_v1",
        "volume_profile_contract": "volume_profile_facts_v1",
        "ordering_ambiguous_buckets": ORDERING_AMBIGUOUS_BUCKETS,
        "queue_full_terminal": "2026-08-28T16:26:23Z",
        "queue_full_crossed": False,
        "day_zip_live_mix": False,
    }
