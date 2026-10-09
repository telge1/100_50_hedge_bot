"""Deterministic snapshot and active-set fingerprints."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Sequence


def _sorted_active_payload(pool_ids: Sequence[str], sides: Sequence[str]) -> str:
    pairs = sorted(zip(pool_ids, sides), key=lambda x: (x[0], x[1]))
    return json.dumps(pairs, separators=(",", ":"))


def active_state_fingerprint(pool_ids: Sequence[str], sides: Sequence[str]) -> str:
    if len(pool_ids) != len(sides):
        raise ValueError("pool_ids and sides length mismatch")
    raw = _sorted_active_payload(pool_ids, sides)
    return hashlib.sha256(raw.encode()).hexdigest()


def deterministic_snapshot_id(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    config_hash: str,
    candle_load_start: datetime,
    decision_close: datetime,
    prefix_candle_fingerprint: str,
    engine_version: str,
) -> str:
    payload = {
        "symbol": str(symbol).upper(),
        "timeframe": str(timeframe),
        "pool_detail": int(pool_detail),
        "config_hash": str(config_hash),
        "candle_load_start": candle_load_start.isoformat(),
        "decision_close": decision_close.isoformat(),
        "prefix_candle_fingerprint": str(prefix_candle_fingerprint),
        "engine_version": str(engine_version),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
