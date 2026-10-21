"""Phase 5S batch prefill for PD1 landscape window (read-only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from research_charts.historical_lmv2_pd_contract_v1 import (
    membership_slot_minutes,
    resolve_historical_pd_config,
)
from research_charts.historical_pool_state_batch_reader_v1 import (
    build_episode_walk_batch_cache,
    historical_lmv2_batch_reader_enabled,
)
from research_charts.historical_pool_state_v1 import _active_members_from_catalog

PD1_POOL_DETAIL = 1


def _iso(dt: datetime) -> str:
    return dt.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(dt: datetime) -> datetime:
    from datetime import timezone

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _causal_decision_close(t: datetime, slot_minutes: int) -> datetime:
    t = _utc(t)
    if slot_minutes >= 60:
        total_min = t.hour * 60 + t.minute
        aligned = (total_min // slot_minutes) * slot_minutes
        return t.replace(hour=aligned // 60, minute=aligned % 60, second=0, microsecond=0)
    slot = (t.minute // slot_minutes) * slot_minutes
    return t.replace(minute=slot, second=0, microsecond=0)


def _canonical_active_member(row: dict) -> dict:
    lower = row.get("lower_bound")
    if lower is None:
        lower = row.get("lower")
    upper = row.get("upper_bound")
    if upper is None:
        upper = row.get("upper")
    return {
        "pool_id": row.get("pool_id"),
        "pipeline_line_id": row.get("pipeline_line_id"),
        "snapshot_id": row.get("snapshot_id"),
        "side": row.get("side"),
        "pool_type": row.get("pool_type"),
        "lower_bound": lower,
        "upper_bound": upper,
        "strength": row.get("strength"),
        "strength_class": row.get("strength_class"),
        "known_at": row.get("known_at"),
        "break_at": row.get("break_at"),
        "confirmed_at": row.get("confirmed_at"),
        "invalidated_at": row.get("invalidated_at"),
        "decision_close": row.get("decision_close"),
        "active_membership": row.get("active_membership"),
        "removal_cause": row.get("removal_cause"),
    }


def unique_decision_closes_between(
    start: datetime, end: datetime, *, slot_minutes: int = 15
) -> list[datetime]:
    """Decision closes on publication grid covering [start, end] inclusive."""
    out: list[datetime] = []
    t = _causal_decision_close(start, slot_minutes)
    end_dc = _causal_decision_close(end, slot_minutes)
    while t <= end_dc:
        out.append(t)
        t += timedelta(minutes=slot_minutes)
    return out


@dataclass
class Phase5sPrefillResult:
    stats: dict[str, Any]
    decision_closes: int
    cache_keys_filled: int


def prefill_member_cache_phase5s(
    cache: dict[str, list[dict]],
    context_from: datetime,
    vis_to: datetime,
    *,
    client: Any,
    database: str,
    pool_detail: int = PD1_POOL_DETAIL,
    timeframe: str = "15m",
) -> Phase5sPrefillResult | None:
    """
    One-shot Phase 5S batch load for all decision closes in [context_from, vis_to].
    Fills `cache` so `_load_members` avoids load_published / _load_catalog per close.
    """
    if not historical_lmv2_batch_reader_enabled():
        return None
    hcfg = resolve_historical_pd_config(pool_detail, timeframe=timeframe)
    slot_minutes = membership_slot_minutes(hcfg.timeframe)
    closes = unique_decision_closes_between(context_from, vis_to, slot_minutes=slot_minutes)
    if not closes:
        return None
    batch = build_episode_walk_batch_cache(
        client,
        database,
        hcfg.pipeline_line_id,
        hcfg.config_hash,
        closes,
    )
    filled = 0
    for dc in closes:
        pub = batch.publication(hcfg.config_hash, dc)
        catalog = batch.catalog(database, pub["snapshot_id"], dc)
        active = _active_members_from_catalog(
            catalog, pub["snapshot_id"], pipeline_line_id=hcfg.pipeline_line_id
        )
        cache[_iso(dc)] = [_canonical_active_member(m) for m in active]
        filled += 1
    return Phase5sPrefillResult(
        stats=dict(batch.stats),
        decision_closes=len(closes),
        cache_keys_filled=filled,
    )
