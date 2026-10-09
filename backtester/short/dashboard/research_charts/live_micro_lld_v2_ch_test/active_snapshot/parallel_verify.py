"""Serializable verification records for parallel worker POC (Phase 11E)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from indicators.live_micro_lld_v2.models import LiquidityPoolV2

from ..identity import candle_sequence_fingerprint
from ..serialize import pool_fingerprint
from .extract import ActiveStateSnapshot


def _pool_row(p: LiquidityPoolV2) -> dict[str, Any]:
    return {
        "pool_id": p.pool_id,
        "side": str(p.side).lower(),
        "top_price": float(p.top_price),
        "bottom_price": float(p.bottom_price),
        "strength": float(p.strength),
        "strength_class": str(p.strength_class),
        "known_at": p.known_at.astimezone(timezone.utc).isoformat(),
        "break_at": p.break_at.astimezone(timezone.utc).isoformat() if p.break_at else None,
        "invalidated_at": (
            p.invalidated_at.astimezone(timezone.utc).isoformat() if p.invalidated_at else None
        ),
        "candidate_created_at": (
            p.candidate_created_at.astimezone(timezone.utc).isoformat()
            if p.candidate_created_at
            else None
        ),
        "confirmed_at": (
            p.confirmed_at.astimezone(timezone.utc).isoformat() if p.confirmed_at else None
        ),
        "reclaim_level": float(p.reclaim_level) if p.reclaim_level is not None else None,
        "created_index": int(p.created_index),
        "row_fingerprint": "|".join(str(x) for x in pool_fingerprint(p)),
    }


def active_pools_record(result, decision_close: datetime) -> dict[str, dict[str, Any]]:
    dc = decision_close.astimezone(timezone.utc)
    out: dict[str, dict[str, Any]] = {}
    for p in result.pools:
        if not p.active:
            continue
        if p.known_at.astimezone(timezone.utc) > dc:
            continue
        if p.break_at is not None:
            continue
        out[p.pool_id] = _pool_row(p)
    return out


def verification_record(
    snap: ActiveStateSnapshot,
    candles: Sequence,
    result,
    decision_close: datetime,
) -> dict[str, Any]:
    dc = decision_close.astimezone(timezone.utc)
    return {
        "decision_close": dc.isoformat(),
        "engine_input_hash": snap.engine_input_hash,
        "active_state_fingerprint": snap.active_state_fingerprint,
        "prefix_candle_fingerprint": snap.prefix_candle_fingerprint,
        "candle_count": len(candles),
        "candle_sequence_fingerprint": candle_sequence_fingerprint(candles),
        "candle_load_start": snap.candle_load_start.astimezone(timezone.utc).isoformat(),
        "history_from_close": snap.history_from_close.isoformat(),
        "active_pool_ids": list(snap.active_pool_ids),
        "active_sides": list(snap.active_sides),
        "pools": active_pools_record(result, dc),
    }


def compare_records(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    *,
    check_candles: bool = True,
) -> list[str]:
    errs: list[str] = []
    dc = reference.get("decision_close")
    if reference.get("decision_close") != candidate.get("decision_close"):
        return ["decision_close mismatch"]
    keys = (
        "engine_input_hash",
        "active_state_fingerprint",
        "prefix_candle_fingerprint",
        "candle_count",
        "candle_load_start",
        "history_from_close",
    )
    for k in keys:
        if reference.get(k) != candidate.get(k):
            errs.append(f"{k}@{dc}")
    if check_candles and reference.get("candle_sequence_fingerprint") != candidate.get(
        "candle_sequence_fingerprint"
    ):
        errs.append(f"candle_fp@{dc}")
    if reference.get("active_pool_ids") != candidate.get("active_pool_ids"):
        errs.append(f"active_ids@{dc}")
    if reference.get("active_sides") != candidate.get("active_sides"):
        errs.append(f"active_sides@{dc}")
    ref_p = reference.get("pools") or {}
    cand_p = candidate.get("pools") or {}
    if set(ref_p) != set(cand_p):
        errs.append(f"pool_set@{dc}")
    for pid in sorted(set(ref_p) & set(cand_p)):
        for fk in ref_p[pid]:
            if ref_p[pid].get(fk) != cand_p[pid].get(fk):
                errs.append(f"pool.{fk}.{pid}@{dc}")
                break
    return errs


def compare_records_strict(
    reference: dict[str, Any],
    candidate: dict[str, Any],
) -> list[str]:
    return compare_records(reference, candidate, check_candles=True)
