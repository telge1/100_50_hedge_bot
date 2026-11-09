"""Bounded historical batch: candles → run_live_micro_lld_v2 → isolated CH test tables."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

from ..clickhouse_config import load_clickhouse_config
from ..service import _candles_from_packed, _timeframe_seconds, load_candles
from ..trp_import import ensure_trp_on_path
from .identity import (
    ENGINE_VERSION,
    build_live_micro_lld_v2_config,
    config_hash,
    engine_input_document,
    engine_input_hash,
    effective_config_dict,
)
from .schema import TABLE_POOL, TABLE_RUN, ensure_test_tables
from .serialize import pool_to_ch_row

ENGINE_LABEL = "run_live_micro_lld_v2"


def _load_candles_for_window(
    symbol: str,
    timeframe: str,
    from_close: datetime,
    to_close: datetime,
    pool_detail: int,
    *,
    candle_limit: int = 50000,
) -> tuple[list, dict[str, Any]]:
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.as_of import filter_candles_closed_at_or_before

    sym = str(symbol).strip().upper()
    tf = str(timeframe).strip()
    from_dt = from_close.astimezone(timezone.utc)
    to_dt = to_close.astimezone(timezone.utc)
    if from_dt > to_dt:
        raise ValueError("from_close must be <= to_close")

    cfg = build_live_micro_lld_v2_config(int(pool_detail))
    warmup_bars = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
    tf_sec = _timeframe_seconds(tf)
    from_unix = int(from_dt.timestamp())
    to_unix = int(to_dt.timestamp())
    load_start = max(0, from_unix - warmup_bars * tf_sec)

    packed = load_candles(sym, tf, start=load_start, end=to_unix, limit=candle_limit)
    raw = _candles_from_packed(packed)
    candles = filter_candles_closed_at_or_before(raw, to_dt, tf)
    meta = {
        "symbol": sym,
        "timeframe": tf,
        "pool_detail": int(pool_detail),
        "config": cfg,
        "candle_load_start": datetime.fromtimestamp(load_start, tz=timezone.utc),
        "from_close": from_dt,
        "to_close": to_dt,
        "packed": packed,
        "warmup_bars": warmup_bars,
    }
    return candles, meta


def run_engine_on_candles(candles: list, pool_detail: int):
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.engine import run_live_micro_lld_v2

    cfg = build_live_micro_lld_v2_config(int(pool_detail))
    if not candles:
        raise ValueError("no candles")
    return run_live_micro_lld_v2(candles, cfg), cfg


def run_bounded_batch(
    symbol: str,
    timeframe: str,
    from_close: datetime,
    to_close: datetime,
    *,
    pool_detail: int = 10,
    persist: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run engine on [from_close, to_close] and optionally persist to CH test tables."""
    pd = int(pool_detail)
    if pd < 1 or pd > 10:
        raise ValueError("pool_detail must be 1..10")

    candles, meta = _load_candles_for_window(
        symbol, timeframe, from_close, to_close, pd
    )
    result, cfg = run_engine_on_candles(candles, pd)
    pools = list(result.pools)

    input_doc = engine_input_document(
        cfg=cfg,
        symbol=meta["symbol"],
        timeframe=meta["timeframe"],
        from_close=meta["from_close"],
        to_close=meta["to_close"],
        candle_load_start=meta["candle_load_start"],
        candles=candles,
        warmup_bars=meta["warmup_bars"],
    )
    cfg_h = config_hash(cfg)
    input_h = engine_input_hash(input_doc)
    cfg_json = json.dumps(effective_config_dict(cfg), sort_keys=True, separators=(",", ":"))

    packed = meta["packed"]
    row_count = len(packed.get("rows") or packed.get("candles") or [])
    if not row_count and candles:
        row_count = len(candles)
    data_complete = 1
    if row_count >= 50000:
        data_complete = 0

    rid = run_id or str(uuid.uuid4())
    out: dict[str, Any] = {
        "run_id": rid,
        "symbol": meta["symbol"],
        "timeframe": meta["timeframe"],
        "pool_detail": pd,
        "candle_load_start": meta["candle_load_start"].isoformat(),
        "candle_first_open": candles[0].timestamp.isoformat(),
        "candle_last_open": candles[-1].timestamp.isoformat(),
        "decision_close": meta["to_close"].isoformat(),
        "from_close": meta["from_close"].isoformat(),
        "to_close": meta["to_close"].isoformat(),
        "candle_count": len(candles),
        "pool_count": len(pools),
        "data_complete": data_complete,
        "config_hash": cfg_h,
        "engine_input_hash": input_h,
        "engine_input": input_doc,
        "pools": pools,
        "engine_result": result,
    }

    if not persist:
        return out

    ensure_test_tables()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        run_row = [
            (
                uuid.UUID(rid),
                meta["symbol"],
                meta["timeframe"],
                pd,
                ENGINE_LABEL,
                meta["candle_load_start"],
                candles[0].timestamp,
                candles[-1].timestamp,
                meta["to_close"],
                len(candles),
                len(pools),
                data_complete,
                cfg_h,
                meta["from_close"],
                meta["to_close"],
                ENGINE_VERSION,
                input_h,
                input_doc["candle_fingerprint"],
                cfg_json,
            )
        ]
        client.insert(
            f"{db}.{TABLE_RUN}",
            run_row,
            column_names=[
                "run_id",
                "symbol",
                "timeframe",
                "pool_detail",
                "engine_label",
                "candle_load_start",
                "candle_first_open",
                "candle_last_open",
                "decision_close",
                "candle_count",
                "pool_count",
                "data_complete",
                "config_hash",
                "from_close",
                "to_close",
                "engine_version",
                "engine_input_hash",
                "candle_fingerprint",
                "config_json",
            ],
        )
        pool_rows = [pool_to_ch_row(p, run_id=rid) for p in pools]
        if pool_rows:
            uid = uuid.UUID(rid)
            for r in pool_rows:
                r["run_id"] = uid
            client.insert(
                f"{db}.{TABLE_POOL}",
                [tuple(r[c] for c in _POOL_COLS) for r in pool_rows],
                column_names=_POOL_COLS,
            )
    finally:
        client.close()

    out.pop("pools", None)
    out.pop("engine_result", None)
    return out


_POOL_COLS = [
    "run_id",
    "symbol",
    "timeframe",
    "pool_id",
    "pool_type",
    "side",
    "source_timestamp",
    "created_timestamp",
    "known_at",
    "top_price",
    "bottom_price",
    "strength",
    "strength_class",
    "active",
    "break_at",
    "pivot_timestamp",
    "early_timing",
    "reclaim_level",
    "candidate_created_at",
    "confirmed_at",
    "invalidated_at",
    "created_index",
    "metadata_json",
]
