"""Phase 5S batch prefill for PD1 landscape window (read-only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from research_charts.historical_lmv2_pd_contract_v1 import (
    HistoricalPdConfig,
    membership_slot_minutes,
)
from research_charts.historical_pool_state_batch_reader_v1 import (
    build_episode_walk_batch_cache,
    phase5s_batch_prefill_direct_lookup_enabled,
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


def _batch_pub_catalog(
    batch,
    *,
    client_db: str,
    hcfg: HistoricalPdConfig,
    dc: datetime,
) -> tuple[dict, list[dict]]:
    if phase5s_batch_prefill_direct_lookup_enabled():
        pub = batch.peek_publication(hcfg.config_hash, dc)
        cat = batch.peek_catalog(client_db, pub["snapshot_id"], dc)
    else:
        pub = batch.publication(hcfg.config_hash, dc)
        cat = batch.catalog(client_db, pub["snapshot_id"], dc)
    return pub, cat


def _prefill_member_cache_phase5s_legacy(
    cache: dict[str, list[dict]],
    closes: list[datetime],
    *,
    client: Any,
    database: str,
    hcfg: HistoricalPdConfig,
) -> Phase5sPrefillResult:
    batch = build_episode_walk_batch_cache(
        client,
        database,
        hcfg.pipeline_line_id,
        hcfg.config_hash,
        closes,
    )
    filled = 0
    for dc in closes:
        pub, catalog = _batch_pub_catalog(
            batch, client_db=database, hcfg=hcfg, dc=dc
        )
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


def _prefill_member_cache_phase5s_with_reuse(
    cache: dict[str, list[dict]],
    closes: list[datetime],
    *,
    client: Any,
    database: str,
    hcfg: HistoricalPdConfig,
) -> Phase5sPrefillResult:
    """MC4R-C1: load only missing closes; cross-window store when flag ON."""
    from .phase5s_member_prefill_reuse_v1 import store_member_row, try_reuse_member_row

    missing: list[datetime] = []
    reused = 0
    for dc in closes:
        iso = _iso(dc)
        if iso in cache:
            if not _request_cache_row_valid(cache[iso], hcfg, iso):
                missing.append(dc)
            else:
                reused += 1
            continue
        recycled = try_reuse_member_row(hcfg, iso)
        if recycled is not None:
            cache[iso] = recycled
            reused += 1
        else:
            missing.append(dc)
    from .phase5s_member_prefill_reuse_v1 import member_prefill_low_copy_enabled, reuse_cache_stats

    stats: dict[str, Any] = {
        "decision_closes": len(closes),
        "closes_reused": reused,
        "closes_missing": len(missing),
        "member_prefill_reuse_active": True,
        "member_prefill_low_copy_active": member_prefill_low_copy_enabled(),
        "clickhouse_query_count": 0,
    }
    filled = 0
    if missing:
        batch = build_episode_walk_batch_cache(
            client,
            database,
            hcfg.pipeline_line_id,
            hcfg.config_hash,
            missing,
        )
        stats.update(dict(batch.stats))
        stats["clickhouse_query_count"] = batch.stats.get("clickhouse_query_count", 0)
        for dc in missing:
            pub, catalog = _batch_pub_catalog(
                batch, client_db=database, hcfg=hcfg, dc=dc
            )
            active = _active_members_from_catalog(
                catalog, pub["snapshot_id"], pipeline_line_id=hcfg.pipeline_line_id
            )
            rows = [_canonical_active_member(m) for m in active]
            iso = _iso(dc)
            cache[iso] = rows
            store_member_row(hcfg, iso, rows)
            filled += 1
    stats["cache_keys_filled"] = filled
    stats["reuse_store_stats"] = reuse_cache_stats()
    return Phase5sPrefillResult(
        stats=stats,
        decision_closes=len(closes),
        cache_keys_filled=filled,
    )


def _request_cache_row_valid(members: list[dict], hcfg: HistoricalPdConfig, dc_iso: str) -> bool:
    if not members:
        return True
    for row in members:
        if str(row.get("pipeline_line_id") or "") != hcfg.pipeline_line_id:
            return False
        if row.get("snapshot_id") in (None, ""):
            return False
        dc = row.get("decision_close")
        if dc is not None and str(dc) != dc_iso:
            return False
    return True


def prefill_member_cache_phase5s(
    cache: dict[str, list[dict]],
    context_from: datetime,
    vis_to: datetime,
    *,
    client: Any,
    database: str,
    hcfg: HistoricalPdConfig,
) -> Phase5sPrefillResult | None:
    """
    One-shot Phase 5S batch load for all decision closes in [context_from, vis_to].
    Fills `cache` so `_load_members` avoids load_published / _load_catalog per close.
    """
    from research_charts.historical_pool_state_batch_reader_v1 import historical_lmv2_batch_reader_enabled

    if not historical_lmv2_batch_reader_enabled():
        return None
    from .phase5s_member_prefill_reuse_v1 import member_prefill_reuse_enabled

    slot_minutes = membership_slot_minutes(hcfg.timeframe)
    closes = unique_decision_closes_between(context_from, vis_to, slot_minutes=slot_minutes)
    if not closes:
        return None
    if member_prefill_reuse_enabled():
        return _prefill_member_cache_phase5s_with_reuse(
            cache, closes, client=client, database=database, hcfg=hcfg
        )
    return _prefill_member_cache_phase5s_legacy(
        cache, closes, client=client, database=database, hcfg=hcfg
    )
