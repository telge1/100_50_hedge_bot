"""Optional metadata backfill for legacy run rows (pools untouched)."""

from __future__ import annotations

from datetime import datetime, timezone

import clickhouse_connect

from ..clickhouse_config import load_clickhouse_config
from .batch import _load_candles_for_window, run_engine_on_candles
from .identity import (
    ENGINE_VERSION,
    LEGACY_RUN_OVERRIDES,
    config_hash,
    engine_input_document,
    engine_input_hash,
    effective_config_dict,
)
from .schema import TABLE_RUN, ensure_test_tables
import json


DEFAULT_RUN_ID = "de5f74f6-236f-4dc1-81df-94e15dec1b83"


def backfill_legacy_run_identity(run_id: str = DEFAULT_RUN_ID) -> dict:
    legacy = LEGACY_RUN_OVERRIDES.get(run_id)
    if not legacy:
        raise ValueError(f"no legacy override for run_id {run_id}")

    pd = int(legacy["pool_detail"])
    from_dt = datetime.fromisoformat(legacy["from_close"].replace("Z", "+00:00"))
    to_dt = datetime.fromisoformat(legacy["to_close"].replace("Z", "+00:00"))
    sym = legacy["symbol"]
    tf = legacy["timeframe"]

    candles, meta = _load_candles_for_window(sym, tf, from_dt, to_dt, pd)
    _, cfg = run_engine_on_candles(candles, pd)
    doc = engine_input_document(
        cfg=cfg,
        symbol=sym,
        timeframe=tf,
        from_close=meta["from_close"],
        to_close=meta["to_close"],
        candle_load_start=meta["candle_load_start"],
        candles=candles,
        warmup_bars=meta["warmup_bars"],
    )
    cfg_h = config_hash(cfg)
    input_h = engine_input_hash(doc)
    cfg_json = json.dumps(effective_config_dict(cfg), sort_keys=True, separators=(",", ":"))

    ensure_test_tables()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        client.command(
            f"""
            ALTER TABLE {db}.{TABLE_RUN}
            UPDATE
                from_close = {{from_close:DateTime64(3, 'UTC')}},
                to_close = {{to_close:DateTime64(3, 'UTC')}},
                engine_version = {{ev:String}},
                engine_input_hash = {{eih:String}},
                candle_fingerprint = {{cf:String}},
                config_hash = {{ch:String}},
                config_json = {{cj:String}}
            WHERE run_id = {{rid:UUID}}
            """,
            parameters={
                "from_close": meta["from_close"],
                "to_close": meta["to_close"],
                "ev": ENGINE_VERSION,
                "eih": input_h,
                "cf": doc["candle_fingerprint"],
                "ch": cfg_h,
                "cj": cfg_json,
                "rid": run_id,
            },
        )
    finally:
        client.close()

    return {
        "run_id": run_id,
        "config_hash": cfg_h,
        "engine_input_hash": input_h,
        "candle_fingerprint": doc["candle_fingerprint"],
    }
