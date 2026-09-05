"""Pool loading and causal filtering (closed confirmation bar v2)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Iterable

import pandas as pd

from orderbook_analyse.cluster_sweep_research.cluster_adapter import (
    active_clusters_as_of,
    run_lld_pools,
)
from orderbook_analyse.liquidity_location_causal.availability import pool_lifecycle_status, pool_time_fields
from orderbook_analyse.liquidity_location_causal.prefix import (
    build_tf_from_closed_1m_prefix,
    candles_1m_closed_until,
    utc_naive,
)
from orderbook_analyse.liquidity_location_pool_lifecycle.causality import engine_side_to_bid_ask

from .config import MIN_COMPONENT_COUNT, MIN_POOL_STRENGTH, TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO
from .models import PoolRecord, _utc_naive


class PoolLifecycle(str, Enum):
    NOT_YET_KNOWN = "NOT_YET_KNOWN"
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    DATA_QUALITY_ERROR = "DATA_QUALITY_ERROR"
    SNAPSHOT_INCONSISTENT = "SNAPSHOT_INCONSISTENT"


def _utc(ts: Any) -> datetime:
    return _utc_naive(pd.Timestamp(ts).to_pydatetime())


def pool_from_engine(pool: Any) -> PoolRecord:
    side = engine_side_to_bid_ask(pool.side)
    lower = float(pool.bottom_price)
    upper = float(pool.top_price)
    strength = None if pool.strength is None else float(pool.strength)
    times = pool_time_fields(pool)
    available = times["available_at"]
    return PoolRecord(
        pool_id=str(pool.pool_id),
        symbol=str(pool.symbol),
        timeframe=str(pool.timeframe),
        side=side,
        lower_edge=lower,
        upper_edge=upper,
        midpoint=(lower + upper) / 2.0,
        component_count=1,
        strength=strength,
        known_at=available,
        available_at=available,
        invalidated_at=None if pool.invalidated_timestamp is None else _utc(pool.invalidated_timestamp),
        source_timestamp=_utc(times["source_timestamp"]),
        source_bar_start=_utc(times["source_bar_start"]),
        source_bar_end=_utc(times["source_bar_end"]),
        confirmation_bar_start=_utc(times["confirmation_bar_start"]),
        confirmation_bar_end=_utc(times["confirmation_bar_end"]),
        max_feature_timestamp=_utc(times["max_feature_timestamp"]),
    )


def cluster_as_pool(cluster: Any, *, symbol: str, timeframe: str) -> PoolRecord:
    side = engine_side_to_bid_ask(getattr(cluster, "side", "lower"))
    lower = float(getattr(cluster, "cluster_low", None) or getattr(cluster, "low"))
    upper = float(getattr(cluster, "cluster_high", None) or getattr(cluster, "high"))
    members = getattr(cluster, "pool_ids", None) or getattr(cluster, "member_pool_ids", None) or []
    n = len(members) if members else int(getattr(cluster, "pool_count", 0) or 1)
    strength = getattr(cluster, "cluster_strength", None) or getattr(cluster, "strength_max", None) or getattr(cluster, "strength", 0)
    known = getattr(cluster, "newest_created_timestamp", None) or getattr(cluster, "newest_created", None)
    return PoolRecord(
        pool_id=str(getattr(cluster, "cluster_id", None) or f"cluster:{symbol}:{timeframe}:{side}:{lower}:{upper}"),
        symbol=symbol,
        timeframe=timeframe,
        side=side,
        lower_edge=lower,
        upper_edge=upper,
        midpoint=(lower + upper) / 2.0,
        component_count=max(1, n),
        strength=None if strength is None else float(strength),
        known_at=_utc(known),
        available_at=_utc(known),
        invalidated_at=None,
        source_timestamp=_utc(getattr(cluster, "oldest_source_timestamp", known) or known),
    )


def _run_lld_causal(
    candles_by_tf: dict[str, pd.DataFrame],
    *,
    symbol: str,
    as_of: datetime,
    timeframes: Iterable[str],
) -> dict[str, list[Any]]:
    """Closed 1m prefix → TF aggregate → LLD pools_all per timeframe."""
    df_1m = candles_by_tf.get("1m")
    if df_1m is None or df_1m.empty:
        # Fallback: treat supplied TF frames as already causal (tests/mocks).
        out: dict[str, list[Any]] = {}
        for tf in timeframes:
            df = candles_by_tf.get(tf)
            if df is None or df.empty:
                out[tf] = []
                continue
            hist = df[pd.to_datetime(df["open_time"]) <= utc_naive(as_of)].copy()
            lld = run_lld_pools(hist, symbol=symbol, timeframe=tf)
            out[tf] = list(lld.pools or [])
        return out

    prefix_1m = candles_1m_closed_until(df_1m, as_of)
    by_tf = build_tf_from_closed_1m_prefix(prefix_1m, timeframes)
    out = {}
    for tf in timeframes:
        df_tf = by_tf.get(tf)
        if df_tf is None or df_tf.empty:
            out[tf] = []
            continue
        lld = run_lld_pools(df_tf, symbol=symbol, timeframe=tf)
        out[tf] = list(lld.pools or [])
    return out


def load_engine_pools_at(
    candles_by_tf: dict[str, pd.DataFrame],
    *,
    symbol: str,
    as_of: datetime,
    timeframes: Iterable[str] = (TF_MACRO, TF_LIQUIDITY, TF_ENTRY_POOL),
) -> dict[str, list[Any]]:
    """All engine pools from causal prefix (includes invalidated)."""
    return _run_lld_causal(candles_by_tf, symbol=symbol, as_of=as_of, timeframes=timeframes)


def load_pools_at(
    candles_by_tf: dict[str, pd.DataFrame],
    *,
    symbol: str,
    as_of: datetime,
    timeframes: Iterable[str] = (TF_MACRO, TF_LIQUIDITY, TF_ENTRY_POOL),
    minimum_cluster_pools: int = MIN_COMPONENT_COUNT,
    causal_closed_prefix: bool = True,
) -> dict[str, list[PoolRecord]]:
    """Causal pool snapshot: only pools with available_at <= as_of and still active."""
    del causal_closed_prefix  # always causal in v2
    out: dict[str, list[PoolRecord]] = {}
    engine_by_tf = _run_lld_causal(candles_by_tf, symbol=symbol, as_of=as_of, timeframes=timeframes)
    as_of_naive = utc_naive(as_of)
    for tf in timeframes:
        pools = engine_by_tf.get(tf) or []
        clusters = active_clusters_as_of(pools, as_of, gap_pct=0.10, minimum_pools=minimum_cluster_pools)
        rows: list[PoolRecord] = []
        seen: set[str] = set()
        for c in clusters:
            pr = cluster_as_pool(c, symbol=symbol, timeframe=tf)
            if pr.is_active_at(as_of) and pr.component_count >= minimum_cluster_pools:
                if pr.strength is None or pr.strength >= MIN_POOL_STRENGTH:
                    rows.append(pr)
                    seen.add(pr.pool_id)
        for p in pools:
            if not getattr(p, "active", True):
                continue
            pr = pool_from_engine(p)
            if pr.pool_id in seen:
                continue
            if pr.is_active_at(as_of):
                rows.append(pr)
                seen.add(pr.pool_id)
        out[tf] = rows
    return out


def resolve_pool_lifecycle(
    pool_id: str,
    candles_by_tf: dict[str, pd.DataFrame],
    *,
    symbol: str,
    as_of: datetime,
    timeframes: Iterable[str] = (TF_MACRO, TF_LIQUIDITY, TF_ENTRY_POOL),
    inject_records: dict[str, list[PoolRecord]] | None = None,
) -> tuple[str, PoolRecord | None]:
    """Explicit lifecycle for an individual or cluster pool id at as_of."""
    engine_by_tf = _run_lld_causal(candles_by_tf, symbol=symbol, as_of=as_of, timeframes=timeframes)
    for tf in timeframes:
        for p in engine_by_tf.get(tf) or []:
            if str(p.pool_id) != pool_id:
                continue
            pr = pool_from_engine(p)
            status = pool_lifecycle_status(p, as_of)
            return status, pr
    if inject_records:
        for ps in inject_records.values():
            for pr in ps:
                if pr.pool_id == pool_id:
                    return pr.status_at(as_of), pr
    snap = load_pools_at(candles_by_tf, symbol=symbol, as_of=as_of, timeframes=timeframes)
    for ps in snap.values():
        for pr in ps:
            if pr.pool_id == pool_id:
                return pr.status_at(as_of), pr
    return PoolLifecycle.DATA_QUALITY_ERROR.value, None


def pools_known_before_approach(pool: PoolRecord, approach_at: datetime) -> bool:
    return _utc(pool.available_at) <= _utc(approach_at)


def pool_known_at_or_before(pool: PoolRecord, as_of: datetime) -> bool:
    return _utc(pool.available_at) <= _utc(as_of)


def pool_valid_at(pool: PoolRecord, as_of: datetime) -> bool:
    if not pool_known_at_or_before(pool, as_of):
        return False
    if pool.invalidated_at is not None and _utc(pool.invalidated_at) <= _utc(as_of):
        return False
    return True


def eligible_target_pools(pools: list[PoolRecord], as_of: datetime) -> list[PoolRecord]:
    """Causal target snapshot: available_at <= as_of and still valid at as_of."""
    return [p for p in pools if pool_valid_at(p, as_of)]


def find_pool_in_snapshot(
    pools: dict[str, list[PoolRecord]] | list[PoolRecord],
    pool_id: str,
) -> PoolRecord | None:
    """Lookup pool by id in the current causal snapshot (active pools only)."""
    if isinstance(pools, dict):
        seq: list[PoolRecord] = [p for ps in pools.values() for p in ps]
    else:
        seq = list(pools)
    for p in seq:
        if p.pool_id == pool_id:
            return p
    return None


def pool_present_in_snapshot(
    pools: dict[str, list[PoolRecord]] | list[PoolRecord],
    pool_id: str,
) -> bool:
    """True iff pool_id is causally ACTIVE in the as-of snapshot."""
    return find_pool_in_snapshot(pools, pool_id) is not None


def pool_explicitly_invalidated(
    pool_id: str,
    candles_by_tf: dict[str, pd.DataFrame],
    *,
    symbol: str,
    as_of: datetime,
    timeframes: Iterable[str] = (TF_MACRO, TF_LIQUIDITY, TF_ENTRY_POOL),
) -> bool:
    status, _ = resolve_pool_lifecycle(
        pool_id, candles_by_tf, symbol=symbol, as_of=as_of, timeframes=timeframes
    )
    return status == PoolLifecycle.INVALIDATED.value
