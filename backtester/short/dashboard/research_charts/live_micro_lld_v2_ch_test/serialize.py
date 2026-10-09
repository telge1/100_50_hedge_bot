"""Full LiquidityPoolV2 export (includes fields omitted from to_dict())."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from data.models import ensure_utc
from indicators.live_micro_lld_v2.models import LiquidityPoolV2


def _norm_iso(v: datetime | str | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return ensure_utc(datetime.fromisoformat(v.replace("Z", "+00:00"))).isoformat()
    return ensure_utc(v).isoformat()


def _dt(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    return v


def pool_to_ch_row(
    pool: LiquidityPoolV2,
    *,
    run_id: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "symbol": pool.symbol,
        "timeframe": pool.timeframe,
        "pool_id": pool.pool_id,
        "pool_type": pool.pool_type,
        "side": pool.side,
        "source_timestamp": _dt(pool.source_timestamp),
        "created_timestamp": _dt(pool.created_timestamp),
        "known_at": _dt(pool.known_at),
        "top_price": float(pool.top_price),
        "bottom_price": float(pool.bottom_price),
        "strength": float(pool.strength),
        "strength_class": str(pool.strength_class),
        "active": 1 if pool.active else 0,
        "break_at": _dt(pool.break_at),
        "pivot_timestamp": _dt(pool.pivot_timestamp),
        "early_timing": pool.early_timing,
        "reclaim_level": pool.reclaim_level,
        "candidate_created_at": _dt(pool.candidate_created_at),
        "confirmed_at": _dt(pool.confirmed_at),
        "invalidated_at": _dt(pool.invalidated_at),
        "created_index": int(pool.created_index),
        "metadata_json": json.dumps(pool.metadata or {}, separators=(",", ":"), sort_keys=True),
    }


def pool_fingerprint(pool: LiquidityPoolV2) -> tuple:
    return (
        pool.pool_id,
        pool.pool_type,
        pool.side,
        round(float(pool.top_price), 8),
        round(float(pool.bottom_price), 8),
        round(float(pool.strength), 8),
        str(pool.strength_class),
        _norm_iso(pool.known_at),
        _norm_iso(pool.break_at),
        _norm_iso(pool.invalidated_at),
        bool(pool.active),
        _norm_iso(pool.pivot_timestamp),
        pool.early_timing,
        pool.reclaim_level,
        _norm_iso(pool.candidate_created_at),
        _norm_iso(pool.confirmed_at),
        int(pool.created_index),
    )


def row_fingerprint(row: dict[str, Any]) -> tuple:
    return (
        row["pool_id"],
        row["pool_type"],
        row["side"],
        round(float(row["top_price"]), 8),
        round(float(row["bottom_price"]), 8),
        round(float(row["strength"]), 8),
        str(row["strength_class"]),
        _norm_iso(row["known_at"]),
        _norm_iso(row.get("break_at")),
        _norm_iso(row.get("invalidated_at")),
        bool(int(row.get("active", 0))),
        _norm_iso(row.get("pivot_timestamp")),
        row.get("early_timing"),
        row.get("reclaim_level"),
        _norm_iso(row.get("candidate_created_at")),
        _norm_iso(row.get("confirmed_at")),
        int(row.get("created_index", 0)),
    )
