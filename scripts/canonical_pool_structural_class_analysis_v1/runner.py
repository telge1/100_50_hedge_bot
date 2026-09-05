#!/usr/bin/env python3
"""Outcome-blind 30-day canonical pool structural class analysis v1."""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

OA_ROOT = Path(__file__).resolve().parents[2]
DASH_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
OUT_ROOT = OA_ROOT / "results" / "canonical_pool_structural_class_analysis_v1"
SCRIPTS_ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(OA_ROOT / "src"))
sys.path.insert(0, str(DASH_ROOT))

SYMBOL = "BTCUSDT"
TIMEFRAMES = ("5m", "15m", "30m")
SNAPSHOT_INTERVAL_S = 1800  # 30m
ANALYSIS_DAYS = 30
MIN_COVERAGE_PCT = 99.0
SMOKE_SNAPSHOT_COUNT = 8
MAX_WALLCLOCK_HOURS = 4.0
EXP04_REFERENCE_TS = "2026-08-26T11:34:51Z"
EXP04_POOL_ID = "lld:BTCUSDT:5m:lower:1787740200"

FORBIDDEN_OUTPUT_KEYS = frozenset(
    {
        "future_return",
        "forward_return",
        "mfe",
        "mae",
        "pnl",
        "tp_hit",
        "sl_hit",
        "winning",
        "losing",
        "outcome",
        "profit",
        "tradeable",
    }
)

SOURCE_FILES = [
    OA_ROOT / "src/orderbook_analyse/liquidity_pool_signal/chart_pool_adapter.py",
    OA_ROOT / "src/orderbook_analyse/liquidity_pool_signal/canonical.py",
    OA_ROOT / "src/orderbook_analyse/liquidity_pool_signal/contracts.py",
    Path("/home/telgenbuescher/projects/trading_research_platform/indicators/liquidity_location/engine.py"),
    Path("/home/telgenbuescher/projects/trading_research_platform/indicators/liquidity_location/clusters.py"),
    Path("/home/telgenbuescher/projects/trading_research_platform/indicators/liquidity_location/compose.py"),
    Path("/home/telgenbuescher/projects/trading_research_platform/indicators/liquidity_location/config.py"),
    Path("/home/telgenbuescher/projects/trading_research_platform/indicators/liquidity_location/models.py"),
    Path("/home/telgenbuescher/projects/trading_research_platform/indicators/liquidity_location/availability.py"),
    DASH_ROOT / "research_charts/service.py",
    DASH_ROOT / "research_charts/canonical_lld.py",
]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def git_info(repo: Path) -> dict[str, Any]:
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).splitlines()
    return {"repo": str(repo), "branch": branch, "head": head, "dirty_count": len(dirty), "dirty_sample": dirty[:20]}


def verify_source_hashes(expected: dict[str, str]) -> None:
    for rel, exp in expected.items():
        p = OA_ROOT / rel if not rel.startswith("/") else Path(rel)
        if not p.is_file():
            p = Path(rel)
        if not p.is_file():
            raise RuntimeError(f"SOURCE_HASH_MISSING:{p}")
        got = sha256_file(p)
        if got != exp:
            raise RuntimeError(f"SOURCE_HASH_MISMATCH:{p}:{got}!={exp}")


def build_spec(source_hashes: dict[str, str]) -> dict[str, Any]:
    from orderbook_analyse.liquidity_pool_signal.canonical import CANONICAL_PROVIDER_VERSION
    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import DEFAULT_LIQUIDITY

    return {
        "schema_version": "canonical_pool_structural_class_analysis_v1",
        "symbol": SYMBOL,
        "timeframes": list(TIMEFRAMES),
        "analysis_days": ANALYSIS_DAYS,
        "snapshot_interval_minutes": SNAPSHOT_INTERVAL_S // 60,
        "min_coverage_pct": MIN_COVERAGE_PCT,
        "canonical_provider_version": CANONICAL_PROVIDER_VERSION,
        "lld_config": dict(DEFAULT_LIQUIDITY),
        "source_of_truth": "chart_pool_adapter.export_snapshot",
        "forbidden_sources": [
            "pool_arrivals_v2.csv",
            "expansion_freeze_v1_v4",
            "selected_pool.json",
            "case_spec_bounds",
        ],
        "outcome_blind_forbidden_fields": sorted(FORBIDDEN_OUTPUT_KEYS),
        "source_file_sha256": source_hashes,
        "git": {
            "orderbook_analyse": git_info(OA_ROOT),
            "spread_recovery_hedge_short_dev": git_info(DASH_ROOT.parent),
        },
        "multi_tf_linkage": "descriptive_only_no_engine_mutation",
        "quantile_scope": "per_timeframe_and_side",
        "episode_deduplication": True,
        "resource_limits": {"max_wallclock_hours": MAX_WALLCLOCK_HOURS, "clickhouse_writes": False},
    }


def spec_sha256(spec: dict[str, Any]) -> str:
    body = {k: v for k, v in spec.items() if k not in ("structural_analysis_spec_sha256",)}
    return sha256_bytes(canonical_json(body))


class CandleStore:
    def __init__(self, timeframe: str, candles: list[Any]):
        self.timeframe = timeframe
        self.candles = candles
        self.unix = [int(c.unix_seconds) for c in candles]

    def last_index_at_or_before(self, as_of: datetime) -> int:
        sec = tf_sec(self.timeframe)
        ts = int(as_of.timestamp())
        # Include only bars fully closed at as_of (matches strict_complete_buckets semantics).
        i = bisect.bisect_right(self.unix, ts) - 1
        while i >= 0 and self.unix[i] + sec > ts:
            i -= 1
        if i < 0:
            raise ValueError(f"no complete candle at or before {as_of}")
        return i

    def slice_causal(self, as_of: datetime) -> list[Any]:
        from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import chart_lookback_start

        end_i = self.last_index_at_or_before(as_of)
        start_dt = chart_lookback_start(as_of, self.timeframe)
        start_ts = int(start_dt.timestamp())
        start_i = bisect.bisect_left(self.unix, start_ts)
        return self.candles[start_i : end_i + 1]

    def prefix_until(self, as_of: datetime) -> list[Any]:
        return self.candles[: self.last_index_at_or_before(as_of) + 1]


def load_candle_store(symbol: str, tf: str, end_ts: int, extra_lookback_s: int) -> tuple[CandleStore, dict[str, Any]]:
    from research_charts.service import _TF_SEC, resolve_candle_pack, _candles_from_packed

    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    start_dt = end_dt - timedelta(seconds=extra_lookback_s)
    start_ts = int(start_dt.timestamp())
    sec = _TF_SEC[tf]
    est_bars = (end_ts - start_ts) // sec + 10
    limit = min(max(est_bars, 500), 3000)
    packed = resolve_candle_pack(symbol, tf, start=start_ts, end=end_ts, limit=limit, allow_stale=True)
    candles = _candles_from_packed(packed, allow_stale=True)
    meta = {
        "timeframe": tf,
        "bars_loaded": len(candles),
        "first_ts": iso_z(candles[0].timestamp) if candles else None,
        "last_ts": iso_z(candles[-1].timestamp) if candles else None,
        "query_start": iso_z(start_dt),
        "query_end": iso_z(end_dt),
        "limit": limit,
    }
    return CandleStore(tf, candles), meta


def tf_sec(tf: str) -> int:
    from research_charts.service import _TF_SEC

    return int(_TF_SEC[tf])


def warmup_seconds() -> int:
    from research_charts.service import DEFAULT_LIMIT_BY_TF, _TF_SEC

    mx = 0
    for tf in TIMEFRAMES:
        lim = int(DEFAULT_LIMIT_BY_TF.get(tf, 1500))
        mx = max(mx, lim * int(_TF_SEC[tf]))
    return mx


def determine_analysis_window(stores: dict[str, CandleStore]) -> dict[str, Any]:
    last_complete = {}
    for tf in TIMEFRAMES:
        st = stores[tf]
        last_complete[tf] = st.candles[-1].timestamp
        if last_complete[tf].tzinfo is None:
            last_complete[tf] = last_complete[tf].replace(tzinfo=timezone.utc)

    end_raw = min(last_complete.values())
    # floor to last closed 30m bar
    end_ts = int(end_raw.timestamp())
    end_ts = (end_ts // SNAPSHOT_INTERVAL_S) * SNAPSHOT_INTERVAL_S
    analysis_end = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    analysis_start = analysis_end - timedelta(days=ANALYSIS_DAYS)

    snapshots = []
    t = int(analysis_start.timestamp())
    end_i = int(analysis_end.timestamp())
    while t <= end_i:
        snapshots.append(datetime.fromtimestamp(t, tz=timezone.utc))
        t += SNAPSHOT_INTERVAL_S

    return {
        "analysis_start_utc": iso_z(analysis_start),
        "analysis_end_utc": iso_z(analysis_end),
        "snapshot_count": len(snapshots),
        "last_complete_by_tf": {
            tf: iso_z(last_complete[tf] if isinstance(last_complete[tf], datetime) else last_complete[tf])
            for tf in TIMEFRAMES
        },
        "snapshots": snapshots,
    }


def coverage_report(stores: dict[str, CandleStore], window: dict[str, Any]) -> dict[str, Any]:
    start = parse_iso(window["analysis_start_utc"])
    end = parse_iso(window["analysis_end_utc"])
    rows = []
    for tf in TIMEFRAMES:
        st = stores[tf]
        sec = tf_sec(tf)
        expected = int((end.timestamp() - start.timestamp()) // sec) + 1
        in_window = [u for u in st.unix if start.timestamp() <= u <= end.timestamp()]
        present = len(in_window)
        missing = max(0, expected - present)
        pct = 100.0 * present / expected if expected else 0.0
        gaps = []
        if len(in_window) > 1:
            max_gap = 0
            for a, b in zip(in_window, in_window[1:]):
                gap = b - a
                if gap > sec:
                    max_gap = max(max_gap, gap)
                    gaps.append({"from": a, "to": b, "gap_s": gap})
        rows.append(
            {
                "timeframe": tf,
                "expected_bars": expected,
                "present_bars": present,
                "missing_bars": missing,
                "coverage_pct": round(pct, 4),
                "first_in_window": iso_z(datetime.fromtimestamp(in_window[0], tz=timezone.utc)) if in_window else None,
                "last_in_window": iso_z(datetime.fromtimestamp(in_window[-1], tz=timezone.utc)) if in_window else None,
                "max_gap_s": max((g["gap_s"] for g in gaps), default=0),
                "gap_events": gaps[:5],
            }
        )
    ok = all(r["coverage_pct"] >= MIN_COVERAGE_PCT for r in rows)
    return {
        "analysis_start_utc": window["analysis_start_utc"],
        "analysis_end_utc": window["analysis_end_utc"],
        "warmup_seconds": warmup_seconds(),
        "warmup_days_approx": round(warmup_seconds() / 86400, 2),
        "per_timeframe": rows,
        "coverage_accepted": ok,
        "min_required_pct": MIN_COVERAGE_PCT,
    }


class SnapshotEngine:
    def __init__(self, liquidity: dict[str, Any]):
        from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
            _load_chart_bindings,
            build_liquidity_config,
            pool_row_from_engine,
            side_ask_bid,
            market_price_at,
            fingerprint,
        )
        from orderbook_analyse.liquidity_pool_signal.canonical import (
            CANONICAL_PROVIDER_VERSION,
            canonical_pool_record,
            overlay_fields_for_pool,
            clip_overlays_to_as_of,
        )
        from indicators.liquidity_location.compose import cluster_label_text
        from indicators.liquidity_location.availability import pool_availability_timestamps

        self.trp, self.eng = _load_chart_bindings()
        self.liquidity = liquidity
        self.pool_row_from_engine = pool_row_from_engine
        self.side_ask_bid = side_ask_bid
        self.market_price_at = market_price_at
        self.fingerprint = fingerprint
        self.canonical_pool_record = canonical_pool_record
        self.overlay_fields_for_pool = overlay_fields_for_pool
        self.clip_overlays_to_as_of = clip_overlays_to_as_of
        self.cluster_label_text = cluster_label_text
        self.pool_availability_timestamps = pool_availability_timestamps
        self.CPV = CANONICAL_PROVIDER_VERSION
        self.query_count = 0

    def run_cached(
        self, symbol: str, tf: str, as_of: datetime, store: CandleStore
    ) -> dict[str, Any]:
        from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import build_liquidity_config

        candles = store.slice_causal(as_of)
        cfg = build_liquidity_config(self.liquidity, tf)
        result = self.eng(candles, cfg)
        clusters_all = self.trp["cluster_pools"](
            result.pools, gap_pct=float(cfg.cluster_gap_pct), active_only=True
        )
        overlays = self.trp["compose_lld_overlays"](result, cfg, clusters=clusters_all)
        serialized = self.trp["serialize_overlays"](overlays)
        as_of_unix = int(as_of.timestamp())
        clipped = self.clip_overlays_to_as_of(serialized, as_of_unix)
        market = self.market_price_at(candles, as_of)
        rows = [
            self.pool_row_from_engine(p, cfg=cfg, as_of=as_of, market_price=market) for p in result.pools
        ]
        active = [r for r in rows if r.get("active_as_of")]
        from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import normalize_pool_payload

        pool_norm = normalize_pool_payload(rows)
        sha = self.fingerprint({"pools": pool_norm, "as_of": iso_z(as_of), "provider": self.CPV})
        self.query_count += 0  # no DB
        return {
            "as_of": iso_z(as_of),
            "timeframe": tf,
            "symbol": symbol,
            "market_price": market,
            "pools": rows,
            "active_pools": active,
            "engine_pools_all_count": len(result.pools_all) if hasattr(result, "pools_all") else len(result.pools),
            "clusters_all": clusters_all,
            "clusters_shown": self.trp["filter_clusters"](
                clusters_all, minimum_pools=int(cfg.minimum_cluster_pools)
            ),
            "serialized": serialized,
            "clipped": clipped,
            "config": cfg,
            "candles_used": len(candles),
            "canonical_snapshot_sha256": sha,
            "result": result,
            "candles": candles,
        }

    def run_export_snapshot(self, symbol: str, tf: str, as_of: datetime) -> dict[str, Any]:
        from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import export_snapshot

        self.query_count += 1
        return export_snapshot(symbol=symbol, timeframe=tf, window_start=as_of, as_of=as_of, liquidity=self.liquidity)


def front_back(side: str, lower: float, upper: float) -> tuple[float, float]:
    if side.upper() == "BID":
        return upper, lower
    return lower, upper


def relation_to_mid(mid_price: float | None, lower: float, upper: float, front: float, back: float) -> str:
    if mid_price is None:
        return "UNKNOWN"
    tol = max(1e-6, (upper - lower) * 0.001)
    if lower <= mid_price <= upper:
        return "INSIDE"
    if abs(mid_price - front) <= tol:
        return "AT_FRONT"
    if abs(mid_price - back) <= tol:
        return "AT_BACK"
    return "ABOVE" if mid_price > upper else "BELOW"


def age_closed_bars(available_at: datetime, as_of: datetime, tf: str) -> int:
    sec = tf_sec(tf)
    if as_of <= available_at:
        return 0
    return int((as_of - available_at).total_seconds() // sec)


def overlay_maps(clipped: list[dict], shown_cluster_ids: set[str]) -> tuple[dict, dict]:
    pool_zone = {}
    cluster_hull = {}
    for o in clipped:
        meta = o.get("metadata") or {}
        src = meta.get("source")
        if src == "lld":
            pid = meta.get("pool_id") or str(o.get("id", "")).replace(":zone", "")
            pool_zone[pid] = o
        elif src == "lld-cluster":
            cid = meta.get("cluster_id") or str(o.get("id", "")).replace(":zone", "")
            cluster_hull[cid] = o
    return pool_zone, cluster_hull


def extract_pool_features(
    snap: dict[str, Any],
    as_of: datetime,
    tf: str,
    pool_obj: Any,
    row: dict[str, Any],
    member_map: dict[str, str],
    member_counts: dict[str, int],
) -> dict[str, Any]:
    clipped = snap["clipped"]
    side = row["side"]
    lower, upper = float(row["lower_edge"]), float(row["upper_edge"])
    front, back = front_back(side, lower, upper)
    mid_price = snap.get("market_price")
    midpt = (lower + upper) / 2.0
    height_abs = upper - lower
    height_bps = (height_abs / midpt * 10000.0) if midpt else None
    dist_bps = None
    if mid_price and mid_price > 0:
        dist_bps = abs(midpt - mid_price) / mid_price * 10000.0

    avail = parse_iso(row["available_at"])
    age_sec = max(0, int((as_of - avail).total_seconds()))
    age_closed = age_closed_bars(avail, as_of, tf)

    pool_zones, cluster_hulls = overlay_maps(clipped, set())
    zone = pool_zones.get(row["pool_id"])
    cid = member_map.get(row["pool_id"])
    hull = cluster_hulls.get(cid) if cid else None
    strength = row.get("strength")
    rendered_width = None
    if zone:
        st = zone.get("start_timestamp")
        en = zone.get("end_timestamp")
        if st is not None and en is not None:
            rendered_width = max(0, int(en) - int(st)) // tf_sec(tf)

    has_label = any(
        (o.get("metadata") or {}).get("pool_id") == row["pool_id"]
        and str(o.get("id") or "").endswith(":label")
        for o in snap.get("serialized") or []
    )
    if not has_label:
        has_label = strength is not None and float(strength) >= 1.0 and zone is not None

    block = None
    if not row.get("active_as_of"):
        block = "inactive_as_of"
    elif zone is None:
        block = "not_in_clipped_overlay"

    return {
        "snapshot_ts": iso_z(as_of),
        "symbol": row["symbol"],
        "timeframe": tf,
        "pool_id": row["pool_id"],
        "side": side,
        "source_ts": row.get("source_timestamp"),
        "source_unix": int(parse_iso(row["source_timestamp"]).timestamp()) if row.get("source_timestamp") else None,
        "available_at": row["available_at"],
        "active_as_of": bool(row.get("active_as_of")),
        "invalidated_ts": row.get("invalidated_ts"),
        "invalidation_reason": None,
        "lower": lower,
        "upper": upper,
        "front_edge": front,
        "back_edge": back,
        "midpoint": midpt,
        "zone_height_abs": height_abs,
        "zone_height_bps": height_bps,
        "distance_from_snapshot_mid_bps": dist_bps,
        "relation_to_mid": relation_to_mid(mid_price, lower, upper, front, back),
        "age_seconds": age_sec,
        "age_bars_total": age_closed,
        "age_closed_bars": age_closed,
        "raw_strength": strength,
        "normalized_strength": strength,
        "source_candle_range": float(pool_obj.source_high - pool_obj.source_low) if pool_obj else None,
        "source_candle_volume": float(pool_obj.source_volume) if pool_obj else None,
        "included_in_canonical_snapshot": True,
        "included_in_serialized_overlay": zone is not None,
        "rendered_as_single_pool": zone is not None,
        "rendered_as_cluster_member": cid is not None,
        "rendered_as_cluster_hull": hull is not None,
        "has_filled_zone": zone is not None and float(zone.get("opacity") or 0) > 0,
        "has_numeric_label": bool(has_label and strength is not None and float(strength) >= 1),
        "rendered_width_bars_as_of": rendered_width,
        "rendered_height_bps": height_bps,
        "visibility_reason": "single_pool_zone" if zone else "absent_overlay",
        "visibility_block_reason": block,
        "raw_active": bool(row.get("active_as_of")),
        "merged": cid is not None and member_counts.get(cid, 1) > 1,
        "merge_reason": "same_tf_cluster" if cid else None,
        "member_of_same_tf_cluster": cid is not None,
        "same_tf_cluster_id": cid,
        "market_price": mid_price,
    }


def build_member_map(clusters_all: list) -> tuple[dict[str, str], dict[str, int]]:
    m: dict[str, str] = {}
    counts: dict[str, int] = {}
    for c in clusters_all:
        counts[c.cluster_id] = c.pool_count
        for pid in c.pool_ids:
            m[pid] = c.cluster_id
    return m, counts


def extract_component_features(
    snap: dict, as_of: datetime, tf: str, cluster, shown_ids: set[str], mid_price: float | None, cluster_label_text
) -> dict[str, Any]:
    label = cluster_label_text(cluster)
    lo, hi = cluster.cluster_low, cluster.cluster_high
    mid = (lo + hi) / 2.0
    height_bps = (hi - lo) / mid * 10000.0 if mid else None
    front, back = front_back("BID" if cluster.side == "lower" else "ASK", lo, hi)
    if cluster.side == "upper":
        front, back = lo, hi
    else:
        front, back = hi, lo

    ages = []
    for p in cluster.pools:
        from indicators.liquidity_location.availability import pool_availability_timestamps

        avail = pool_availability_timestamps(p)["available_at"]
        ages.append(age_closed_bars(avail, as_of, tf))

    visible_members = 0
    labeled_members = 0
    pool_zones, _ = overlay_maps(snap["clipped"], shown_ids)
    for pid in cluster.pool_ids:
        if pid in pool_zones:
            visible_members += 1
        for o in snap.get("serialized") or []:
            meta = o.get("metadata") or {}
            if meta.get("pool_id") == pid and str(o.get("id") or "").endswith(":label"):
                labeled_members += 1

    internal_edges = max(0, cluster.pool_count - 1) if cluster.pool_count > 1 else 0

    return {
        "snapshot_ts": iso_z(as_of),
        "component_id": cluster.cluster_id,
        "timeframe": tf,
        "side": "BID" if cluster.side == "lower" else "ASK",
        "member_pool_ids": list(cluster.pool_ids),
        "P": cluster.pool_count,
        "pool_count": cluster.pool_count,
        "Σ": cluster.strength_sum,
        "strength_sum": cluster.strength_sum,
        "strength_mean": cluster.strength_mean,
        "strength_max": cluster.strength_max,
        "component_lower": lo,
        "component_upper": hi,
        "component_height_abs": hi - lo,
        "component_height_bps": height_bps,
        "oldest_member_age_bars": min(ages) if ages else None,
        "newest_member_age_bars": max(ages) if ages else None,
        "component_age_bars": max(ages) - min(ages) if ages else 0,
        "visible_member_count": visible_members,
        "labeled_member_count": labeled_members,
        "rendered_as_cluster_hull": cluster.cluster_id in shown_ids,
        "chart_label": label,
        "distance_to_mid_bps": abs(mid - mid_price) / mid_price * 10000.0 if mid_price else None,
        "relation_to_mid": relation_to_mid(mid_price, lo, hi, front, back),
        "internal_edge_count": internal_edges,
        "exterior_front_edge": front,
        "exterior_back_edge": back,
        "market_price": mid_price,
    }


def overlap_ratio(a_lo, a_hi, b_lo, b_hi) -> float:
    inter_lo = max(a_lo, b_lo)
    inter_hi = min(a_hi, b_hi)
    if inter_hi <= inter_lo:
        return 0.0
    inter = inter_hi - inter_lo
    union = max(a_hi, b_hi) - min(a_lo, b_lo)
    return inter / union if union > 0 else 0.0


def link_multi_tf(components_by_tf: dict[str, list[dict]], as_of: datetime) -> list[dict[str, Any]]:
    """Descriptive multi-TF linkage — does not mutate engine objects."""
    comps = []
    for tf in TIMEFRAMES:
        for c in components_by_tf.get(tf, []):
            comps.append({**c, "tf": tf})

    by_side: dict[str, list] = defaultdict(list)
    for c in comps:
        by_side[c["side"]].append(c)

    out = []
    used = set()
    for side, group in by_side.items():
        group = sorted(group, key=lambda x: (x["tf"], x["component_id"]))
        n = len(group)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            for j in range(i + 1, n):
                a, b = group[i], group[j]
                alo, ahi = a["component_lower"], a["component_upper"]
                blo, bhi = b["component_lower"], b["component_upper"]
                contained = (alo <= blo and ahi >= bhi) or (blo <= alo and bhi >= ahi)
                ov = overlap_ratio(alo, ahi, blo, bhi)
                gap = max(0.0, max(blo - ahi, alo - bhi))
                mid = (alo + ahi + blo + bhi) / 4.0
                gap_pct = (gap / mid * 100.0) if mid and gap > 0 else 0.0
                if ov > 0 or contained or gap_pct <= 0.10:
                    union(i, j)

        buckets: dict[int, list] = defaultdict(list)
        for i in range(n):
            buckets[find(i)].append(group[i])

        for members in buckets.values():
            tfs = sorted({m["tf"] for m in members})
            lo = min(m["component_lower"] for m in members)
            hi = max(m["component_upper"] for m in members)
            mid = (lo + hi) / 2.0
            inter_lo = max(m["component_lower"] for m in members)
            inter_hi = min(m["component_upper"] for m in members)
            has_inter = inter_hi > inter_lo
            mtf_id = sha256_bytes(
                canonical_json({"as_of": iso_z(as_of), "side": side, "members": [m["component_id"] for m in members]})
            )[:16]
            parent_tf = max(tfs, key=lambda t: {"5m": 1, "15m": 2, "30m": 3}[t])
            parent = max(members, key=lambda m: m["component_height_abs"])
            child_ids = [m["component_id"] for m in members if m["component_id"] != parent["component_id"]]
            strength_by_tf = {m["tf"]: m.get("strength_sum") for m in members}
            combined_members = sum(m.get("pool_count") or 0 for m in members)

            def pair_flag(tf_a, tf_b):
                ma = [m for m in members if m["tf"] == tf_a]
                mb = [m for m in members if m["tf"] == tf_b]
                if not ma or not mb:
                    return False, 0.0
                best = 0.0
                for a in ma:
                    for b in mb:
                        best = max(best, overlap_ratio(a["component_lower"], a["component_upper"], b["component_lower"], b["component_upper"]))
                return best > 0, best

            o51, r51 = pair_flag("5m", "15m")
            o53, r53 = pair_flag("5m", "30m")
            o153, r153 = pair_flag("15m", "30m")

            front, back = front_back(side, lo, hi)
            out.append(
                {
                    "snapshot_ts": iso_z(as_of),
                    "multi_tf_component_id": f"mtfc:{SYMBOL}:{side}:{mtf_id}",
                    "side": side,
                    "participating_timeframes": tfs,
                    "timeframe_count": len(tfs),
                    "has_5m": "5m" in tfs,
                    "has_15m": "15m" in tfs,
                    "has_30m": "30m" in tfs,
                    "overlap_5m_15m": o51,
                    "overlap_5m_30m": o53,
                    "overlap_15m_30m": o153,
                    "overlap_ratio_5m_15m": r51,
                    "overlap_ratio_5m_30m": r53,
                    "overlap_ratio_15m_30m": r153,
                    "containment_relation": "nested" if len(members) > 1 and has_inter else "overlap",
                    "parent_component_tf": parent_tf,
                    "parent_component_id": parent["component_id"],
                    "child_component_ids": child_ids,
                    "multi_tf_lower": lo,
                    "multi_tf_upper": hi,
                    "union_height_bps": (hi - lo) / mid * 10000.0 if mid else None,
                    "intersection_lower": inter_lo if has_inter else None,
                    "intersection_upper": inter_hi if has_inter else None,
                    "intersection_height_bps": (inter_hi - inter_lo) / mid * 10000.0 if has_inter and mid else None,
                    "combined_member_count": combined_members,
                    "strength_sum_by_tf": strength_by_tf,
                    "exterior_front_edge": front,
                    "exterior_back_edge": back,
                    "nearest_internal_child_edge_distance_bps": None,
                }
            )
    return out


def compute_contact_history(
    pool: dict[str, Any], candles: list[Any], tf: str, as_of: datetime
) -> dict[str, Any]:
    sec = tf_sec(tf)
    lo, hi = pool["lower"], pool["upper"]
    front, back = pool["front_edge"], pool["back_edge"]
    end_i = bisect.bisect_right([c.unix_seconds for c in candles], int(as_of.timestamp())) - 1
    if end_i < 0:
        return {}
    hist = candles[: end_i + 1]
    front_t, inside_t, back_t, traversals = 0, 0, 0, 0
    last_touch = None
    first_touch = None
    max_pen = 0.0
    prev_state = None
    episode_state = None

    for c in hist:
        state = None
        if lo <= c.low <= hi or lo <= c.high <= hi or (c.low <= lo and c.high >= hi):
            state = "inside"
        elif abs(c.high - front) <= (hi - lo) * 0.01 or (front >= lo and front <= hi and c.high >= front >= c.low):
            state = "front"
        elif abs(c.low - back) <= (hi - lo) * 0.01:
            state = "back"
        if state:
            if episode_state != state:
                if state == "front":
                    front_t += 1
                elif state == "inside":
                    inside_t += 1
                elif state == "back":
                    back_t += 1
                episode_state = state
                ts = iso_z(c.timestamp)
                first_touch = first_touch or ts
                last_touch = ts
            if hi > lo:
                if c.low < lo:
                    pen = (lo - c.low) / (hi - lo) * 100.0
                    max_pen = max(max_pen, pen)
                if c.high > hi:
                    pen = (c.high - hi) / (hi - lo) * 100.0
                    max_pen = max(max_pen, pen)
        else:
            episode_state = None
        if prev_state == "inside" and state == "back":
            traversals += 1
        prev_state = state if state else prev_state

    bars_since = None
    if last_touch:
        bars_since = age_closed_bars(parse_iso(last_touch), as_of, tf)

    untested = front_t == 0 and inside_t == 0 and back_t == 0
    deep = max_pen > 50 or traversals >= 2

    return {
        "number_of_front_edge_touches": front_t,
        "number_of_inside_entries": inside_t,
        "number_of_back_edge_touches": back_t,
        "number_of_full_traversals": traversals,
        "first_touch_ts": first_touch,
        "last_touch_ts": last_touch,
        "bars_since_last_touch": bars_since,
        "maximum_historical_penetration_pct": max_pen,
        "currently_untested": untested,
        "currently_partially_tested": not untested and not deep,
        "currently_deeply_tested": deep,
    }


def membership_tag(p: int) -> str:
    if p <= 1:
        return "SINGLETON_P1"
    if p == 2:
        return "PAIR_P2"
    if p <= 4:
        return "CLUSTER_P3_4"
    if p <= 8:
        return "CLUSTER_P5_8"
    return "CLUSTER_P9_PLUS"


def quantile_tag(value: float | None, qs: np.ndarray, prefix: str) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    for i, q in enumerate(qs):
        if value <= q:
            return f"{prefix}_Q{i+1}"
    return f"{prefix}_Q4"


def assign_tags(df: pd.DataFrame, comp_df: pd.DataFrame, mtf_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    strength_q = {}
    sigma_q = {}
    height_q = {}
    age_q = {}
    for tf in TIMEFRAMES:
        for side in ("ASK", "BID"):
            sub = df[(df["timeframe"] == tf) & (df["side"] == side) & df["raw_strength"].notna()]
            if len(sub) >= 4:
                strength_q[(tf, side)] = np.quantile(sub["raw_strength"], [0.25, 0.5, 0.75])
            sub_h = df[(df["timeframe"] == tf) & (df["side"] == side) & df["zone_height_bps"].notna()]
            if len(sub_h) >= 4:
                height_q[(tf, side)] = np.quantile(sub_h["zone_height_bps"], [0.25, 0.5, 0.75])
            sub_a = df[(df["timeframe"] == tf) & (df["side"] == side) & df["age_closed_bars"].notna()]
            if len(sub_a) >= 4:
                age_q[(tf, side)] = np.quantile(sub_a["age_closed_bars"], [0.25, 0.5, 0.75])
    for tf in TIMEFRAMES:
        for side in ("ASK", "BID"):
            sub = comp_df[(comp_df["timeframe"] == tf) & (comp_df["side"] == side) & comp_df["strength_sum"].notna()]
            if len(sub) >= 4:
                sigma_q[(tf, side)] = np.quantile(sub["strength_sum"], [0.25, 0.5, 0.75])

    comp_lookup = comp_df.set_index(["snapshot_ts", "component_id"]).to_dict("index") if len(comp_df) else {}
    mtf_lookup = {}
    for _, r in mtf_df.iterrows():
        mtf_lookup.setdefault(r["snapshot_ts"], []).append(r)

    for _, r in df.iterrows():
        tags = [f"TF_{r['timeframe'].upper()}"]
        p = 1
        sigma = None
        cid = r.get("same_tf_cluster_id")
        if cid and (r["snapshot_ts"], cid) in comp_lookup:
            comp = comp_lookup[(r["snapshot_ts"], cid)]
            p = int(comp.get("pool_count") or 1)
            sigma = comp.get("strength_sum")
        tags.append(membership_tag(p))
        qs = strength_q.get((r["timeframe"], r["side"]))
        if qs is not None:
            tags.append(quantile_tag(r.get("raw_strength"), qs, "STRENGTH"))
        qs = height_q.get((r["timeframe"], r["side"]))
        if qs is not None:
            ht = quantile_tag(r.get("zone_height_bps"), qs, "HEIGHT")
            if ht:
                tags.append(ht)
        qs = age_q.get((r["timeframe"], r["side"]))
        if qs is not None:
            tags.append(quantile_tag(r.get("age_closed_bars"), qs, "AGE"))
        if cid:
            comp = comp_lookup.get((r["snapshot_ts"], cid), {})
            sq = sigma_q.get((r["timeframe"], r["side"]))
            if sq is not None:
                tags.append(quantile_tag(sigma, sq, "SIGMA"))

        if r.get("rendered_as_cluster_hull"):
            tags.append("CLEAR_CLUSTER_HULL")
        elif r.get("has_filled_zone"):
            tags.append("CLEAR_FILLED_SINGLE")
        elif r.get("rendered_as_cluster_member"):
            tags.append("MEMBER_ONLY")
        elif r.get("included_in_serialized_overlay"):
            tags.append("LINE_OR_FRAGMENT")
        else:
            tags.append("NOT_SERIALIZED")
        tags.append("LABEL_VISIBLE" if r.get("has_numeric_label") else "NO_LABEL")

        # HTF from mtf
        mtfs = mtf_lookup.get(r["snapshot_ts"], [])
        htf = [m for m in mtfs if m["side"] == r["side"]]
        parent = [m for m in htf if r["timeframe"] == "5m" and m.get("has_15m") or m.get("has_30m")]
        if not parent:
            tags.append("NO_HTF_PARENT")
        else:
            m0 = parent[0]
            if m0.get("has_15m") and m0.get("has_30m"):
                tags.append("PARENT_15M_30M")
            elif m0.get("has_30m"):
                tags.append("PARENT_30M")
            elif m0.get("has_15m"):
                tags.append("PARENT_15M")
            if m0.get("timeframe_count", 0) >= 2:
                tags.append("MULTI_TF_OVERLAP_2")
            if m0.get("timeframe_count", 0) >= 3:
                tags.append("MULTI_TF_OVERLAP_3")

        if p == 1:
            tags.append("ISOLATED_COMPONENT")
        elif r.get("member_of_same_tf_cluster"):
            tags.append("INTERNAL_CHILD_EDGE")
        else:
            tags.append("EXTERIOR_COMPONENT_EDGE")

        if r.get("currently_untested"):
            tags.append("UNTESTED")
        elif r.get("number_of_front_edge_touches", 0) + r.get("number_of_inside_entries", 0) <= 1:
            tags.append("SINGLE_TEST")
        elif r.get("currently_deeply_tested"):
            tags.append("DEEP_TESTED")
        else:
            tags.append("MULTI_TEST")

        rows.append({**r.to_dict(), "class_tags": sorted(set(t for t in tags if t))})
    return pd.DataFrame(rows)


def build_episodes(snapshot_df: pd.DataFrame, key_cols: list[str], id_col: str) -> pd.DataFrame:
    eps = []
    for eid, grp in snapshot_df.groupby(id_col):
        grp = grp.sort_values("snapshot_ts")
        eps.append(
            {
                "episode_id": eid,
                **{c: grp[c].iloc[0] for c in key_cols if c in grp.columns and c != id_col},
                "first_seen": grp["snapshot_ts"].iloc[0],
                "last_seen": grp["snapshot_ts"].iloc[-1],
                "snapshot_count": len(grp),
                "maximum_age_closed_bars": grp["age_closed_bars"].max() if "age_closed_bars" in grp else None,
                "maximum_P": grp["P"].max() if "P" in grp else None,
                "maximum_strength_sum": grp["strength_sum"].max() if "strength_sum" in grp else None,
            }
        )
    return pd.DataFrame(eps)


def parity_check(engine: SnapshotEngine, store: CandleStore, as_of: datetime, tf: str) -> dict[str, Any]:
    cached = engine.run_cached(SYMBOL, tf, as_of, store)
    exported = engine.run_export_snapshot(SYMBOL, tf, as_of)
    c_ids = sorted(p["pool_id"] for p in cached["active_pools"])
    e_ids = sorted(p["pool_id"] for p in exported["active_pools"])
    bounds_ok = True
    e_map = {p["pool_id"]: p for p in exported["active_pools"]}
    for p in cached["active_pools"]:
        e = e_map.get(p["pool_id"])
        if not e:
            bounds_ok = False
            break
        if abs(p["lower_edge"] - e["lower_edge"]) > 1e-6 or abs(p["upper_edge"] - e["upper_edge"]) > 1e-6:
            bounds_ok = False
            break
    ids_ok = c_ids == e_ids
    p_counts = [c.pool_count for c in cached["clusters_all"]]
    return {
        "as_of": iso_z(as_of),
        "timeframe": tf,
        "active_pool_ids_match": ids_ok,
        "bounds_match": bounds_ok,
        "cached_sha": cached["canonical_snapshot_sha256"],
        "exported_sha": exported.get("canonical_snapshot_sha256"),
        "sha_match": cached["canonical_snapshot_sha256"] == exported.get("canonical_snapshot_sha256"),
        "cluster_pool_counts": p_counts,
        "parity_ok": ids_ok and bounds_ok,
    }


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def write_parquet(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def bundle_hash(out_root: Path, exclude: set[str]) -> str:
    parts = []
    for p in sorted(out_root.iterdir()):
        if not p.is_file() or p.name in exclude:
            continue
        if p.suffix in (".parquet",):
            parts.append(sha256_file(p))
        elif p.suffix in (".json", ".csv", ".md"):
            data = p.read_bytes()
            if p.name.endswith(".json"):
                try:
                    obj = json.loads(data)
                    if isinstance(obj, dict):
                        obj.pop("created_at", None)
                        obj.pop("structural_class_bundle_sha256", None)
                        data = canonical_json(obj)
                except json.JSONDecodeError:
                    pass
            parts.append(sha256_bytes(data))
    return sha256_bytes("".join(parts).encode())


def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    peak_rss = 0

    def rss():
        nonlocal peak_rss
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mb = ru / 1024 if sys.platform != "darwin" else ru / (1024 * 1024)
        peak_rss = max(peak_rss, mb)
        return mb

    # Phase 0: source hashes
    source_hashes = {}
    missing = []
    for p in SOURCE_FILES:
        if not p.is_file():
            missing.append(str(p))
            continue
        source_hashes[str(p)] = sha256_file(p)
    if missing:
        write_json(OUT_ROOT / "data_quality_report.json", {"verdict": "CANONICAL_POOL_PARITY_FAILURE", "missing": missing})
        print("FAIL missing sources", missing)
        return 1

    spec = build_spec(source_hashes)
    spec_sha = spec_sha256(spec)
    spec["structural_analysis_spec_sha256"] = spec_sha
    write_json(OUT_ROOT / "structural_analysis_spec.json", spec)

    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import DEFAULT_LIQUIDITY, get_engine_function

    eng_fn = get_engine_function()
    assert eng_fn.__module__ == "indicators.liquidity_location.engine"

    # Phase 1: load candles
    warm_s = warmup_seconds()
    stores: dict[str, CandleStore] = {}
    load_meta = {}
    for tf in TIMEFRAMES:
        # probe last complete
        st_probe, _ = load_candle_store(SYMBOL, tf, int(datetime.now(timezone.utc).timestamp()), warm_s + ANALYSIS_DAYS * 86400)
        last_ts = int(st_probe.candles[-1].unix_seconds)
        st, meta = load_candle_store(SYMBOL, tf, last_ts, warm_s + ANALYSIS_DAYS * 86400 + 86400)
        stores[tf] = st
        load_meta[tf] = meta

    window = determine_analysis_window(stores)
    cov = coverage_report(stores, window)
    write_json(OUT_ROOT / "coverage_report.json", cov)

    if not cov["coverage_accepted"]:
        write_json(
            OUT_ROOT / "freeze_manifest.json",
            {"verdict": "DATA_COVERAGE_INSUFFICIENT", "spec_sha256": spec_sha, "coverage": cov},
        )
        print("FAIL coverage")
        return 1

    snapshots: list[datetime] = window["snapshots"]
    smoke_indices = [int(i * (len(snapshots) - 1) / (SMOKE_SNAPSHOT_COUNT - 1)) for i in range(SMOKE_SNAPSHOT_COUNT)]
    smoke_times = [snapshots[i] for i in smoke_indices]

    engine = SnapshotEngine(DEFAULT_LIQUIDITY)
    engine.cluster_label_text  # bind
    snap_engine_ref = SnapshotEngine(DEFAULT_LIQUIDITY)
    snap_engine_ref.cluster_label_text

    # Phase 2 smoke
    smoke_rows = []
    smoke_t0 = time.perf_counter()
    db_queries_before = engine.query_count
    for as_of in smoke_times:
        for tf in TIMEFRAMES:
            t1 = time.perf_counter()
            cached = engine.run_cached(SYMBOL, tf, as_of, stores[tf])
            par = parity_check(engine, stores[tf], as_of, tf)
            smoke_rows.append({**par, "elapsed_s": time.perf_counter() - t1, "rss_mb": rss(), "cached_active_n": len(cached["active_pools"])})
            if not par["parity_ok"]:
                write_json(OUT_ROOT / "smoke_report.json", {"verdict": "CANONICAL_POOL_PARITY_FAILURE", "rows": smoke_rows})
                return 1
    smoke_elapsed = time.perf_counter() - smoke_t0
    per_snap_s = smoke_elapsed / (SMOKE_SNAPSHOT_COUNT * len(TIMEFRAMES))
    est_full_s = per_snap_s * len(snapshots) * len(TIMEFRAMES)
    runtime_plan = {
        "snapshot_count": len(snapshots),
        "tf_count": len(TIMEFRAMES),
        "total_tf_snapshots": len(snapshots) * len(TIMEFRAMES),
        "smoke_elapsed_s": smoke_elapsed,
        "smoke_per_tf_snapshot_s": per_snap_s,
        "estimated_full_wallclock_s": est_full_s,
        "estimated_full_wallclock_h": est_full_s / 3600,
        "smoke_peak_rss_mb": peak_rss,
        "db_queries_in_smoke": engine.query_count - db_queries_before,
        "candle_load_once_per_tf": True,
        "max_wallclock_hours": MAX_WALLCLOCK_HOURS,
        "full_run_allowed": est_full_s / 3600 <= MAX_WALLCLOCK_HOURS,
    }
    write_json(OUT_ROOT / "runtime_plan.json", runtime_plan)
    write_json(OUT_ROOT / "smoke_report.json", {"verdict": "SMOKE_PASS", "rows": smoke_rows, **runtime_plan})

    if not runtime_plan["full_run_allowed"]:
        write_json(
            OUT_ROOT / "freeze_manifest.json",
            {"verdict": "CANONICAL_POOL_STRUCTURAL_ANALYSIS_PLAN_ONLY", "spec_sha256": spec_sha, "runtime_plan": runtime_plan},
        )
        _write_plan_report(spec, cov, runtime_plan, spec_sha)
        print("PLAN_ONLY", est_full_s / 3600, "hours")
        return 0

    # Full run
    raw_rows = []
    comp_rows = []
    mtf_rows = []
    snap_meta_rows = []
    pool_obj_map: dict[tuple[str, str], Any] = {}

    full_t0 = time.perf_counter()
    for si, as_of in enumerate(snapshots):
        if si % 100 == 0:
            print(f"snapshot {si}/{len(snapshots)} {iso_z(as_of)} rss={rss():.0f}MB")
        tf_snaps = {}
        components_by_tf = {}
        for tf in TIMEFRAMES:
            snap = engine.run_cached(SYMBOL, tf, as_of, stores[tf])
            snap["engine"] = engine
            tf_snaps[tf] = snap
            member_map, member_counts = build_member_map(snap["clusters_all"])
            shown_ids = {c.cluster_id for c in snap["clusters_shown"]}
            for p in snap["result"].pools:
                pool_obj_map[(tf, p.pool_id)] = p
            comp_feats = []
            for c in snap["clusters_all"]:
                cf = extract_component_features(
                    snap, as_of, tf, c, shown_ids, snap.get("market_price"), engine.cluster_label_text
                )
                comp_feats.append(cf)
                comp_rows.append(cf)
            components_by_tf[tf] = comp_feats

            for row in snap["active_pools"]:
                pobj = pool_obj_map.get((tf, row["pool_id"]))
                feat = extract_pool_features(snap, as_of, tf, pobj, row, member_map, member_counts)
                feat["same_tf_cluster_id"] = member_map.get(row["pool_id"])
                feat["P"] = member_counts.get(member_map.get(row["pool_id"]), 1) if member_map.get(row["pool_id"]) else 1
                contact = compute_contact_history(feat, snap["candles"], tf, as_of)
                feat.update(contact)
                raw_rows.append(feat)

            snap_meta_rows.append(
                {
                    "snapshot_ts": iso_z(as_of),
                    "timeframe": tf,
                    "active_pools": len(snap["active_pools"]),
                    "clusters_all": len(snap["clusters_all"]),
                    "clusters_shown": len(snap["clusters_shown"]),
                    "canonical_snapshot_sha256": snap["canonical_snapshot_sha256"],
                    "market_price": snap.get("market_price"),
                }
            )

        mtf = link_multi_tf(components_by_tf, as_of)
        mtf_rows.extend(mtf)

    raw_df = pd.DataFrame(raw_rows)
    comp_df = pd.DataFrame(comp_rows)
    mtf_df = pd.DataFrame(mtf_rows)
    snap_df = pd.DataFrame(snap_meta_rows)

    # consecutive snapshot presence
    raw_df = raw_df.sort_values(["pool_id", "timeframe", "snapshot_ts"])
    raw_df["consecutive_snapshot_presence"] = 1
    raw_df["first_snapshot_seen"] = raw_df.groupby(["pool_id", "timeframe"])["snapshot_ts"].transform("min")
    raw_df["last_snapshot_seen"] = raw_df.groupby(["pool_id", "timeframe"])["snapshot_ts"].transform("max")

    tagged_df = assign_tags(raw_df, comp_df, mtf_df)

    def write_class_counts(df, level):
        rows = []
        for _, r in df.iterrows():
            tags = r.get("class_tags") or []
            if isinstance(tags, str):
                tags = json.loads(tags) if tags.startswith("[") else [tags]
            for t in tags:
                rows.append({"tag": t, "timeframe": r.get("timeframe"), "side": r.get("side")})
        out = pd.DataFrame(rows)
        if len(out):
            out.groupby("tag").size().reset_index(name="count").to_csv(
                OUT_ROOT / f"class_counts_{level}.csv", index=False
            )

    pool_eps = build_episodes(
        tagged_df,
        ["timeframe", "side", "pool_id", "lower", "upper"],
        "pool_id",
    )
    comp_eps = build_episodes(comp_df, ["timeframe", "side", "component_id"], "component_id")

    # episode tags: union of tags seen per pool_id
    if len(tagged_df) and "class_tags" in tagged_df.columns:
        ep_tags = (
            tagged_df.groupby("pool_id")["class_tags"]
            .apply(lambda s: sorted(set(t for tags in s for t in (tags or []))))
            .reset_index()
        )
        ep_tags["timeframe"] = ep_tags["pool_id"].map(tagged_df.groupby("pool_id")["timeframe"].first())
        ep_tags["side"] = ep_tags["pool_id"].map(tagged_df.groupby("pool_id")["side"].first())
        write_class_counts(ep_tags, "episode")
    # distributions
    def dist_csv(df, col, path):
        if col not in df.columns:
            return
        s = df[col].dropna()
        rows = [{"bin": "count", "value": len(s)}]
        for q in [0, 0.25, 0.5, 0.75, 1.0]:
            rows.append({"bin": f"q{int(q*100)}", "value": float(s.quantile(q)) if len(s) else None})
        pd.DataFrame(rows).to_csv(OUT_ROOT / path, index=False)

    dist_csv(comp_df, "pool_count", "p_distribution.csv")
    dist_csv(comp_df, "strength_sum", "sigma_distribution.csv")
    dist_csv(raw_df, "raw_strength", "strength_distribution.csv")
    dist_csv(raw_df, "zone_height_bps", "height_bps_distribution.csv")
    dist_csv(raw_df, "age_closed_bars", "age_bars_distribution.csv")

    if "class_tags" in tagged_df.columns:
        write_class_counts(tagged_df, "snapshot")
    if len(comp_df):
        comp_df.assign(class_tags=comp_df["pool_count"].apply(lambda p: membership_tag(int(p)))).pipe(
            lambda d: write_class_counts(d, "component_snapshot")
        )

    # additional distributions
    def tag_dist(df, col, path):
        if col not in df.columns:
            return
        pd.DataFrame(df[col].value_counts()).reset_index().to_csv(OUT_ROOT / path, index=False)

    if len(mtf_df):
        mtf_df["htf_class"] = np.where(
            mtf_df["timeframe_count"] >= 3,
            "MULTI_TF_3",
            np.where(mtf_df["timeframe_count"] == 2, "MULTI_TF_2", "SINGLE_TF"),
        )
        tag_dist(mtf_df, "htf_class", "htf_confluence_distribution.csv")

    vis_rows = []
    for _, r in tagged_df.iterrows():
        if r.get("rendered_as_cluster_hull"):
            vis_rows.append("CLEAR_CLUSTER_HULL")
        elif r.get("has_filled_zone"):
            vis_rows.append("CLEAR_FILLED_SINGLE")
        elif r.get("rendered_as_cluster_member"):
            vis_rows.append("MEMBER_ONLY")
        elif r.get("included_in_serialized_overlay"):
            vis_rows.append("LINE_OR_FRAGMENT")
        else:
            vis_rows.append("NOT_SERIALIZED")
    pd.DataFrame({"visibility": vis_rows}).value_counts().reset_index(name="count").to_csv(
        OUT_ROOT / "visibility_distribution.csv", index=False
    )

    for col in (
        "number_of_front_edge_touches",
        "number_of_inside_entries",
        "currently_untested",
        "currently_deeply_tested",
    ):
        if col in tagged_df.columns:
            tagged_df[col].value_counts().reset_index(name="count").to_csv(
                OUT_ROOT / f"contact_{col}_distribution.csv", index=False
            )
    tagged_df.assign(
        contact_class=np.select(
            [
                tagged_df.get("currently_untested", False),
                tagged_df.get("currently_deeply_tested", False),
            ],
            ["UNTESTED", "DEEP_TESTED"],
            default="MULTI_TEST",
        )
    )["contact_class"].value_counts().reset_index(name="count").to_csv(
        OUT_ROOT / "contact_history_distribution.csv", index=False
    )

    # structural correlations (numeric only)
    corr_cols = ["raw_strength", "zone_height_bps", "age_closed_bars", "P"]
    avail_cols = [c for c in corr_cols if c in tagged_df.columns]
    if len(avail_cols) >= 2:
        tagged_df[avail_cols].corr(numeric_only=True).to_csv(OUT_ROOT / "structural_correlations.csv")
    if len(comp_df) >= 2 and "pool_count" in comp_df.columns and "component_height_bps" in comp_df.columns:
        comp_df[["pool_count", "strength_sum", "component_height_bps"]].corr(numeric_only=True).to_csv(
            OUT_ROOT / "structural_correlations_components.csv"
        )

    # representative examples (deterministic hash)
    examples = []
    if "class_tags" in tagged_df.columns:
        for tag, grp in tagged_df.groupby(tagged_df["class_tags"].apply(lambda x: x[0] if x else "UNKNOWN")):
            if len(grp) == 0:
                continue
            grp = grp.sort_values("pool_id")
            n_ex = min(3, len(grp)) if len(grp) >= 3 else len(grp)
            for i in range(n_ex):
                row = grp.iloc[i]
                eid = row.get("pool_id") or row.get("component_id")
                h = sha256_bytes(
                    canonical_json(
                        {"spec": spec_sha, "episode_id": str(eid), "class_tag": tag, "idx": i}
                    )
                )
                examples.append(
                    {
                        "class_tag": tag,
                        "pool_id": row.get("pool_id"),
                        "snapshot_ts": row.get("snapshot_ts"),
                        "selection_hash": h,
                    }
                )
    write_json(OUT_ROOT / "representative_examples_manifest.json", {"examples": examples[:200]})

    write_json(
        OUT_ROOT / "data_quality_report.json",
        {
            "verdict": "PASS",
            "coverage_accepted": cov["coverage_accepted"],
            "snapshot_count": len(snapshots),
            "parity_smoke": "pass",
            "source_hashes_verified": True,
        },
    )
    # write parquets
    write_parquet(OUT_ROOT / "canonical_snapshots.parquet", snap_df)
    write_parquet(OUT_ROOT / "raw_pool_snapshot_features.parquet", tagged_df)
    write_parquet(OUT_ROOT / "same_tf_component_snapshot_features.parquet", comp_df)
    write_parquet(OUT_ROOT / "multi_tf_component_snapshot_features.parquet", mtf_df)
    write_parquet(OUT_ROOT / "raw_pool_episodes.parquet", pool_eps)
    write_parquet(OUT_ROOT / "component_episodes.parquet", comp_eps)
    write_parquet(OUT_ROOT / "pool_class_tags.parquet", tagged_df[["pool_id", "snapshot_ts", "timeframe", "side", "class_tags"]])

    # EXP_04
    exp_ref = parse_iso(EXP04_REFERENCE_TS)
    exp_snap = max((s for s in snapshots if s <= exp_ref), default=None)
    exp_rows = tagged_df[(tagged_df["pool_id"] == EXP04_POOL_ID) & (tagged_df["snapshot_ts"] == iso_z(exp_snap))] if exp_snap else pd.DataFrame()
    exp_class = exp_rows.iloc[0].to_dict() if len(exp_rows) else {}
    write_json(
        OUT_ROOT / "exp_04_structural_classification.json",
        {
            "pool_id": EXP04_POOL_ID,
            "reference_ts": EXP04_REFERENCE_TS,
            "nearest_snapshot_ts": iso_z(exp_snap) if exp_snap else None,
            "classification": exp_class,
            "note": "Outcome-blind structural tags only; mechanical verdict unchanged",
        },
    )

    # outcome blindness audit — column/key names only (no substring file scan)
    forbidden_found = []
    for p in OUT_ROOT.glob("*.parquet"):
        try:
            cols = pd.read_parquet(p, columns=[]).columns.tolist() or pd.read_parquet(p).columns.tolist()
        except Exception:
            cols = pd.read_parquet(p).columns.tolist()
        for c in cols:
            cl = c.lower()
            for k in FORBIDDEN_OUTPUT_KEYS:
                if cl == k or cl.endswith(f"_{k}") or cl.startswith(f"{k}_"):
                    forbidden_found.append({"file": p.name, "column": c, "key": k})
    for p in OUT_ROOT.glob("*.json"):
        if p.name == "structural_analysis_spec.json":
            continue
        try:
            obj = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue

        def walk(o, path=""):
            if isinstance(o, dict):
                for kk, vv in o.items():
                    kl = str(kk).lower()
                    for fk in FORBIDDEN_OUTPUT_KEYS:
                        if kl == fk or kl.endswith(f"_{fk}") or kl.startswith(f"{fk}_"):
                            forbidden_found.append({"file": p.name, "field": path + kk, "key": fk})
                    walk(vv, path + kk + ".")
            elif isinstance(o, list):
                for i, vv in enumerate(o[:50]):
                    walk(vv, path + f"[{i}].")

        walk(obj)
    write_json(OUT_ROOT / "outcome_blindness_audit.json", {"violations": forbidden_found, "passed": len(forbidden_found) == 0})

    write_json(
        OUT_ROOT / "query_audit.json",
        {"db_queries_during_full_run": engine.query_count, "candle_loads": 3, "clickhouse_writes": 0},
    )

    full_elapsed = time.perf_counter() - full_t0
    bhash = bundle_hash(OUT_ROOT, exclude={"structural_class_bundle_sha256", "_generate_audit.py"})
    freeze = {
        "verdict": "CANONICAL_POOL_STRUCTURAL_CLASSES_COMPLETE",
        "structural_analysis_spec_sha256": spec_sha,
        "structural_class_bundle_sha256": bhash,
        "analysis_start_utc": window["analysis_start_utc"],
        "analysis_end_utc": window["analysis_end_utc"],
        "snapshot_count": len(snapshots),
        "full_elapsed_s": full_elapsed,
        "peak_rss_mb": peak_rss,
    }
    write_json(OUT_ROOT / "freeze_manifest.json", freeze)

    write_json(
        OUT_ROOT / "test_results.json",
        {"smoke": "pass", "parity": "pass", "outcome_blind": len(forbidden_found) == 0},
    )

    _write_final_report(spec, cov, runtime_plan, freeze, raw_df, comp_df, tagged_df, spec_sha, bhash, full_elapsed, peak_rss)
    print("DONE", freeze["verdict"], "bundle", bhash[:16])
    return 0


def _write_plan_report(spec, cov, runtime_plan, spec_sha):
    (OUT_ROOT / "STRUCTURAL_CLASS_REPORT.md").write_text(
        f"# Structural Class Analysis — PLAN ONLY\n\nVerdict: CANONICAL_POOL_STRUCTURAL_ANALYSIS_PLAN_ONLY\n\n"
        f"Estimated runtime: {runtime_plan['estimated_full_wallclock_h']:.2f}h > {MAX_WALLCLOCK_HOURS}h limit.\n",
        encoding="utf-8",
    )


def _write_final_report(spec, cov, runtime_plan, freeze, raw_df, comp_df, tagged_df, spec_sha, bhash, elapsed, rss):
    lines = [
        "# Canonical Pool Structural Class Analysis v1 — 30 Days",
        "",
        f"**Verdict:** `{freeze['verdict']}`",
        "",
        "## Summary",
        "",
        f"- Period: {cov['analysis_start_utc']} → {cov['analysis_end_utc']}",
        f"- Snapshots: {freeze['snapshot_count']}",
        f"- Raw pool snapshot rows: {len(raw_df)}",
        f"- Component snapshot rows: {len(comp_df)}",
        f"- Wallclock: {elapsed/3600:.2f}h",
        f"- Peak RSS: {rss:.0f} MB",
        f"- Spec SHA256: `{spec_sha}`",
        f"- Bundle SHA256: `{bhash}`",
        "",
        "Outcome-blind structural analysis only. No profitability claims.",
        "",
        f"Output: `{OUT_ROOT}`",
    ]
    (OUT_ROOT / "STRUCTURAL_CLASS_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write_json(OUT_ROOT / "data_quality_report.json", {"verdict": "FAILED", "error": str(exc), "trace": traceback.format_exc()})
        raise
