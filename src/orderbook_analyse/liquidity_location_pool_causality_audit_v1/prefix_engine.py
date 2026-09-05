"""Causal prefix helpers: bar_end semantics and snapshot hashing."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import aggregate_timeframe
from orderbook_analyse.cluster_sweep_research.cluster_adapter import (
    active_clusters_as_of,
    run_lld_pools,
)
from orderbook_analyse.liquidity_location_causal.availability import pool_time_fields

from .config import TF_MINUTES, TIMEFRAMES


def utc_naive(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def bar_end_1m(open_time: Any) -> pd.Timestamp:
    return utc_naive(open_time) + pd.Timedelta(minutes=1)


def candles_1m_until(df_1m: pd.DataFrame, as_of: Any) -> pd.DataFrame:
    """Keep only fully closed 1m bars: bar_end <= as_of."""
    if df_1m is None or df_1m.empty:
        return df_1m.iloc[0:0].copy() if df_1m is not None else pd.DataFrame()
    T = utc_naive(as_of)
    ot = pd.to_datetime(df_1m["open_time"])
    if getattr(ot.dt, "tz", None) is not None:
        ot = ot.dt.tz_convert("UTC").dt.tz_localize(None)
    return df_1m.loc[ot + pd.Timedelta(minutes=1) <= T].copy()


def build_tf_from_prefix(df_1m_prefix: pd.DataFrame, timeframes: Iterable[str] = TIMEFRAMES) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {"1m": df_1m_prefix}
    for tf in timeframes:
        out[tf] = aggregate_timeframe(df_1m_prefix, tf)
    return out


def build_tf_full_then_filter(
    df_1m_full: pd.DataFrame,
    as_of: Any,
    timeframes: Iterable[str] = TIMEFRAMES,
) -> dict[str, pd.DataFrame]:
    """Scanner-style path: aggregate full range once, filter open_time <= as_of.

    This can include HTF bars whose OHLC used 1m data after as_of.
    """
    T = utc_naive(as_of)
    out: dict[str, pd.DataFrame] = {}
    full_1m = df_1m_full
    out["1m"] = candles_1m_until(full_1m, as_of)
    for tf in timeframes:
        full_tf = aggregate_timeframe(full_1m, tf)
        ot = pd.to_datetime(full_tf["open_time"])
        if getattr(ot.dt, "tz", None) is not None:
            ot = ot.dt.tz_convert("UTC").dt.tz_localize(None)
        out[tf] = full_tf.loc[ot <= T].copy()
    return out


def tf_minutes(tf: str) -> int:
    return int(TF_MINUTES[tf])


def confirmation_bar_end(created_timestamp: Any, timeframe: str) -> pd.Timestamp:
    """Earliest causal availability: close of confirmation TF bar."""
    return utc_naive(created_timestamp) + pd.Timedelta(minutes=tf_minutes(timeframe))


def pool_birth_fields(pool: Any) -> dict[str, Any]:
    inv = pool.invalidated_timestamp
    times = pool_time_fields(pool)
    available = times["available_at"]
    return {
        "pool_id": str(pool.pool_id),
        "symbol": str(pool.symbol),
        "timeframe": str(pool.timeframe),
        "side": str(pool.side),
        "known_at": utc_naive(available).isoformat(),
        "available_at": utc_naive(available).isoformat(),
        "source_timestamp": utc_naive(times["source_timestamp"]).isoformat(),
        "source_bar_start": utc_naive(times["source_bar_start"]).isoformat(),
        "source_bar_end": utc_naive(times["source_bar_end"]).isoformat(),
        "confirmation_bar_start": utc_naive(times["confirmation_bar_start"]).isoformat(),
        "confirmation_bar_end": utc_naive(times["confirmation_bar_end"]).isoformat(),
        "lower_edge_at_birth": float(pool.bottom_price),
        "upper_edge_at_birth": float(pool.top_price),
        "midpoint_at_birth": (float(pool.bottom_price) + float(pool.top_price)) / 2.0,
        "component_count_at_birth": 1,
        "strength_at_birth": None if pool.strength is None else float(pool.strength),
        "creation_reason": "swing_confirm_pine_parity",
        "active": bool(pool.active),
        "invalidated_at": None if inv is None else utc_naive(inv).isoformat(),
        "max_feature_timestamp": utc_naive(times["max_feature_timestamp"]).isoformat(),
    }


def birth_hash(fields: dict[str, Any]) -> str:
    keys = [
        "pool_id",
        "symbol",
        "timeframe",
        "side",
        "known_at",
        "source_timestamp",
        "source_bar_start",
        "source_bar_end",
        "lower_edge_at_birth",
        "upper_edge_at_birth",
        "midpoint_at_birth",
        "component_count_at_birth",
        "strength_at_birth",
        "creation_reason",
    ]
    payload = {k: fields.get(k) for k in keys}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def snapshot_row(pool: Any, *, as_of: Any, mode: str) -> dict[str, Any]:
    fields = pool_birth_fields(pool)
    earliest = utc_naive(fields["confirmation_bar_end"])
    T = utc_naive(as_of)
    known = utc_naive(fields["known_at"])
    lookahead_safe = bool(earliest <= T) and bool(known <= earliest)
    known_at_claims_early = known < earliest
    fields.update(
        {
            "as_of": T.isoformat(),
            "mode": mode,
            "earliest_possible_known_at": earliest.isoformat(),
            "known_at_claims_early": known_at_claims_early,
            "lookahead_safe": lookahead_safe and not known_at_claims_early,
            "birth_hash": birth_hash(fields),
            "present": True,
        }
    )
    return fields


def run_pools_for_tf(df_tf: pd.DataFrame, *, symbol: str, timeframe: str) -> list[Any]:
    if df_tf is None or df_tf.empty:
        return []
    lld = run_lld_pools(df_tf, symbol=symbol, timeframe=timeframe)
    return list(lld.pools or [])


def active_pools_as_of(pools: list[Any], as_of: Any) -> list[Any]:
    """Pools available and not yet invalidated at as_of."""
    T = utc_naive(as_of)
    out = []
    for p in pools:
        times = pool_time_fields(p)
        if utc_naive(times["available_at"]) > T:
            continue
        inv = p.invalidated_timestamp
        if inv is not None and utc_naive(inv) <= T:
            continue
        out.append(p)
    return out


def cluster_snapshot_rows(
    pools: list[Any],
    *,
    symbol: str,
    timeframe: str,
    as_of: Any,
) -> list[dict[str, Any]]:
    T = utc_naive(as_of)
    # Use timezone-aware for TRP cluster API
    t_aware = T.to_pydatetime().replace(tzinfo=timezone.utc)
    clusters = active_clusters_as_of(pools, t_aware, gap_pct=0.10, minimum_pools=1)
    rows = []
    for c in clusters:
        members = list(c.pool_ids)
        rows.append(
            {
                "cluster_id": c.cluster_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "side": c.side,
                "as_of": T.isoformat(),
                "lower_edge": float(c.low),
                "upper_edge": float(c.high),
                "component_count": int(c.pool_count),
                "strength_max": None if c.strength_max is None else float(c.strength_max),
                "newest_created": utc_naive(c.newest_created).isoformat(),
                "oldest_created": utc_naive(c.oldest_created).isoformat(),
                "members": "|".join(sorted(members)),
                "member_hash": hashlib.sha256("|".join(sorted(members)).encode()).hexdigest()[:16],
            }
        )
    return rows


def snapshot_hash(pool_ids: Iterable[str], birth_hashes: Iterable[str]) -> str:
    raw = json.dumps(
        {"ids": sorted(pool_ids), "births": sorted(birth_hashes)},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def incremental_append_and_run(
    df_1m_so_far: pd.DataFrame,
    new_1m_rows: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
) -> list[Any]:
    """Batch-equivalent incremental: append closed 1m then re-aggregate TF and run."""
    if df_1m_so_far is None or df_1m_so_far.empty:
        combined = new_1m_rows.copy()
    else:
        combined = pd.concat([df_1m_so_far, new_1m_rows], ignore_index=True)
        combined = combined.drop_duplicates(subset=["open_time"]).sort_values("open_time")
    tf_df = aggregate_timeframe(combined, timeframe)
    return run_pools_for_tf(tf_df, symbol=symbol, timeframe=timeframe), combined
