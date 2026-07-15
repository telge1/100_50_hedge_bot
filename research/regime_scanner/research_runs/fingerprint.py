"""Deterministic run fingerprint (fachlich identical runs → identical fingerprint)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from research.regime_scanner.research_runs.hashing import json_hash
from research.regime_scanner.research_runs.parameters import ResearchParameterSet
from research.regime_scanner.timeframes import ensure_utc_timestamp

# Documented fingerprint serialization (must remain stable):
# 1. Build dict with fixed top-level keys in alphabetical order (json.dumps sort_keys=True).
# 2. Timestamps: ISO-8601 UTC with offset (+00:00), via ensure_utc_timestamp().isoformat().
# 3. Floats in candle hashes are pre-hashed SHA256 hex strings (not raw floats).
# 4. Excluded: run_id, started_at, finished_at, duration, any runtime UUID.
# 5. code_version = git commit SHA when available, else scanner_version only.
# 6. parameters = full canonical ResearchParameterSet dict (see parameters.py).
# 7. candle_input_hashes: {5m, 15m, 30m} over [warmup_start, end_time) export slices.


def iso_utc(ts: datetime | object) -> str:
    return ensure_utc_timestamp(ts).isoformat()


def build_run_fingerprint(
    *,
    params: ResearchParameterSet,
    start_time: datetime,
    end_time: datetime,
    warmup_start: datetime,
    decision_time: datetime | None,
    code_version: str,
    candle_hash_5m: str,
    candle_hash_15m: str,
    candle_hash_30m: str,
) -> str:
    payload: dict[str, Any] = {
        "scanner_name": params.scanner_name,
        "scanner_version": params.scanner_version,
        "symbol": params.symbol,
        "exchange": params.exchange,
        "data_source": params.data_source,
        "start_time": iso_utc(start_time),
        "end_time": iso_utc(end_time),
        "warmup_start": iso_utc(warmup_start),
        "decision_time": None if decision_time is None else iso_utc(decision_time),
        "timeframes": list(params.timeframes),
        "history_candles": int(params.history_candles),
        "parameters": params.to_canonical_dict(),
        "code_version": code_version,
        "candle_input_hashes": {
            "5m": candle_hash_5m,
            "15m": candle_hash_15m,
            "30m": candle_hash_30m,
        },
    }
    return json_hash(payload)
