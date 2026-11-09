"""Strict V2 vs CH compare guards (config + engine input)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .batch import _load_candles_for_window
from .compare_guard_types import CompareVerdict
from .errors import AmbiguousRunError, ConfigNotAvailableError
from .identity import (
    ENGINE_VERSION,
    build_live_micro_lld_v2_config,
    config_hash,
    engine_input_document,
    engine_input_hash,
)
from .run_resolve import RunSelector


def _parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


def build_live_engine_input(
    symbol: str,
    timeframe: str,
    from_close: datetime,
    to_close: datetime,
    pool_detail: int,
) -> dict[str, Any]:
    candles, meta = _load_candles_for_window(
        symbol, timeframe, from_close, to_close, pool_detail
    )
    cfg = meta["config"]
    return engine_input_document(
        cfg=cfg,
        symbol=symbol,
        timeframe=timeframe,
        from_close=meta["from_close"],
        to_close=meta["to_close"],
        candle_load_start=meta["candle_load_start"],
        candles=candles,
        warmup_bars=meta["warmup_bars"],
    )


def evaluate_compare_guard(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    from_close: datetime | str,
    to_close: datetime | str,
    run_id: str | None = None,
) -> dict[str, Any]:
    from_dt = _parse_dt(from_close)
    to_dt = _parse_dt(to_close)
    pd = int(pool_detail)
    sym = str(symbol).upper()
    tf = str(timeframe)

    live_cfg = build_live_micro_lld_v2_config(pd)
    live_hash = config_hash(live_cfg)

    try:
        run_meta = RunSelector().resolve_run(
            sym, tf, pd, run_id=run_id, require_config_hash=live_hash
        )
    except ConfigNotAvailableError as exc:
        return {
            "status": CompareVerdict.CONFIG_NOT_AVAILABLE,
            "reason": str(exc),
            "details": exc.details,
        }
    except AmbiguousRunError as exc:
        return {
            "status": CompareVerdict.CONFIG_NOT_AVAILABLE,
            "reason": str(exc),
            "details": exc.details,
            "code": "AMBIGUOUS_RUN",
        }

    if str(run_meta.get("engine_version") or ENGINE_VERSION) != ENGINE_VERSION:
        return {
            "status": CompareVerdict.CONFIG_MISMATCH,
            "reason": "engine_version mismatch",
            "live": ENGINE_VERSION,
            "ch": run_meta.get("engine_version"),
        }

    if str(run_meta.get("config_hash")) != live_hash:
        return {
            "status": CompareVerdict.CONFIG_MISMATCH,
            "reason": "config_hash mismatch",
            "live_config_hash": live_hash,
            "ch_config_hash": run_meta.get("config_hash"),
        }

    live_doc = build_live_engine_input(sym, tf, from_dt, to_dt, pd)
    live_input_hash = engine_input_hash(live_doc)

    ch_input_hash = str(run_meta.get("engine_input_hash") or "")
    if not ch_input_hash:
        return {
            "status": CompareVerdict.NOT_VERIFIED,
            "reason": "CH run missing engine_input_hash (legacy run)",
            "live_engine_input_hash": live_input_hash,
        }

    if ch_input_hash == live_input_hash:
        return {
            "status": CompareVerdict.PASS,
            "run_id": str(run_meta.get("run_id")),
            "config_hash": live_hash,
            "engine_input_hash": live_input_hash,
            "engine_input": live_doc,
            "ch_run": {
                "run_id": str(run_meta.get("run_id")),
                "pool_detail": int(run_meta.get("pool_detail")),
                "candle_load_start": str(run_meta.get("candle_load_start")),
                "decision_close": str(run_meta.get("decision_close")),
            },
        }

    # Stored run used fixed batch window; compare only when live request matches stored batch input.
    run_from = run_meta.get("from_close")
    run_to = run_meta.get("to_close") or run_meta.get("decision_close")
    if run_from and run_to:
        rf, rt = _parse_dt(run_from), _parse_dt(run_to)
        if int(from_dt.timestamp()) != int(rf.timestamp()) or int(to_dt.timestamp()) != int(rt.timestamp()):
            return {
                "status": CompareVerdict.INPUT_MISMATCH,
                "reason": "from_close/to_close differ from stored CH batch engine input",
                "live": {"from_close": live_doc["from_close"], "to_close": live_doc["to_close"]},
                "ch": {"from_close": rf.isoformat(), "to_close": rt.isoformat()},
            }

    if ch_input_hash != live_input_hash:
        return {
            "status": CompareVerdict.INPUT_MISMATCH,
            "reason": "engine_input_hash mismatch",
            "live_engine_input_hash": live_input_hash,
            "ch_engine_input_hash": ch_input_hash,
            "live_candle_fingerprint": live_doc.get("candle_fingerprint"),
            "ch_candle_fingerprint": run_meta.get("candle_fingerprint"),
        }

    return {
        "status": CompareVerdict.INPUT_MISMATCH,
        "reason": "engine_input_hash mismatch after window check",
        "live_engine_input_hash": live_input_hash,
        "ch_engine_input_hash": ch_input_hash,
    }
