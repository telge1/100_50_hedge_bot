"""Universal historical remaining adapter. Publications stay read-only.

A missing original publication stops the call. This module does not run the V2 engine.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from data.timeframes import SUPPORTED_TIMEFRAMES, timeframe_seconds

from ..identity import build_live_micro_lld_v2_config
from ..phase11u_b.adapter import (
    MISSING_PUBLICATION,
    AdapterStop,
    CandleCache,
    load_historical_remaining,
    resolve_publication,
)
from ..phase11u_c.coverage import BASE_PRICE_HASH, BASE_REVISION_HASH, BASE_SNAPSHOT_ID
from .matrix import EXPECTED_COMPOSITE_ID, SYMBOL, verify_composite

MISSING_ORIGINAL_PUBLICATION = "MISSING_ORIGINAL_PUBLICATION"
UNSUPPORTED_SYMBOL = "UNSUPPORTED_SYMBOL"


def _reject_mixed(pool_id: str, timeframe: str) -> None:
    token = f":{timeframe}:"
    if token not in str(pool_id):
        raise AdapterStop("MIXED_TIMEFRAME", f"pool {pool_id} is not a {timeframe} publication")


def get_remaining_heatmap(
    source,
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    decision_close: datetime,
    immutable_input_id: str,
    research_root: Path,
    cache: CandleCache | None = None,
) -> dict:
    sym = str(symbol or "").upper().strip()
    tf = str(timeframe or "").strip()
    if sym != SYMBOL:
        raise AdapterStop(UNSUPPORTED_SYMBOL, f"unsupported symbol {sym}")
    if tf not in SUPPORTED_TIMEFRAMES or tf == "1m":
        raise AdapterStop("INVALID_REQUEST", f"unsupported timeframe {tf}")
    if int(pool_detail) < 1 or int(pool_detail) > 10:
        raise AdapterStop("INVALID_REQUEST", "pool_detail must be 1..10")
    try:
        timeframe_seconds(tf)
        cfg = build_live_micro_lld_v2_config(int(pool_detail))
    except ValueError as exc:
        raise AdapterStop("INVALID_REQUEST", str(exc)) from exc
    if int(cfg.pool_detail) != int(pool_detail):
        raise AdapterStop("MIXED_POOL_DETAIL", "preset pool detail does not match the request")
    if str(immutable_input_id) != EXPECTED_COMPOSITE_ID:
        raise AdapterStop("INPUT_IDENTITY_MISMATCH", "composite id is not the official Phase 11U-D input")
    identity = verify_composite(research_root)
    if not identity["matches_expected"]:
        raise AdapterStop("INPUT_IDENTITY_MISMATCH", "recomputed composite id does not match the manifests")
    try:
        hits = source.find(symbol=sym, timeframe=tf, pool_detail=int(pool_detail), decision_close=decision_close)
        resolve_publication(hits)
    except AdapterStop as exc:
        if exc.status == MISSING_PUBLICATION:
            raise AdapterStop(MISSING_ORIGINAL_PUBLICATION, exc.detail) from exc
        raise
    result = load_historical_remaining(
        source,
        symbol=sym,
        timeframe=tf,
        pool_detail=int(pool_detail),
        decision_close=decision_close,
        immutable_candle_snapshot_id=BASE_SNAPSHOT_ID,
        price_content_hash=BASE_PRICE_HASH,
        revision_metadata_hash=BASE_REVISION_HASH,
        snapshot_root=research_root,
        cache=cache,
    )
    for pool in result["active"]:
        _reject_mixed(pool["pool_id"], tf)
    result["immutable_input_id"] = identity["composite_id"]
    result["preset_swing_len"] = int(cfg.swing_len)
    result["preset_volume_lookback"] = int(cfg.volume_lookback)
    result["clickhouse_candle_reads"] = 0
    result["engine_executed"] = False
    payload = json.dumps(
        {
            "symbol": result["symbol"],
            "timeframe": result["timeframe"],
            "pool_detail": result["pool_detail"],
            "decision_close": result["decision_close"],
            "published_snapshot_id": result["published_snapshot_id"],
            "immutable_input_id": result["immutable_input_id"],
            "active": result["active"],
            "remaining": result["remaining"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    result["payload_id"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return result
