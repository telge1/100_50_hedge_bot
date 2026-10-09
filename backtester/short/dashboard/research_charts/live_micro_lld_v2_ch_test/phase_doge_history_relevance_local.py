#!/usr/bin/env python3
"""DOGEUSDT Phase 7/8 replication — history length & price relevance (read-only)."""

from __future__ import annotations

import calendar
import json
import resource
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import clickhouse_connect
from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.batch import (
    _load_candles_for_window,
    run_engine_on_candles,
)
from research_charts.service import _timeframe_seconds, load_candles

SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
DATA_EARLIEST = datetime(2025, 12, 11, 0, 0, tzinfo=timezone.utc)
MONTHS = (1, 2, 3, 4, 5, 6)
BANDS = (1.0, 2.0, 3.0, 5.0, 10.0)
DIST_BUCKETS = ((0, 1), (1, 2), (2, 3), (3, 5), (5, 10), (10, 1e9))
FIXED_FROM = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
PILOT_T = datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc)

TEST_CLOSES = [
    datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc),
]


def subtract_months(dt: datetime, months: int) -> datetime:
    y, m = dt.year, dt.month
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d, hour=dt.hour, minute=dt.minute, second=dt.second)


def jaccard(a: set[str], b: set[str]) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 1.0


@dataclass
class ActiveView:
    pool_ids: set[str]
    by_id: dict[str, Any]


def active_view(result) -> ActiveView:
    active = [p for p in result.pools if p.active]
    by_id = {p.pool_id: p for p in active}
    return ActiveView(set(by_id), by_id)


def run_months(months: int, T: datetime) -> dict[str, Any]:
    from_close = subtract_months(T, months)
    if from_close < DATA_EARLIEST:
        raise ValueError(f"from_close {from_close} before data earliest")
    tracemalloc.start()
    t0 = time.perf_counter()
    candles, meta = _load_candles_for_window(SYMBOL, TF, from_close, T, PD)
    t_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    result, _ = run_engine_on_candles(candles, PD)
    t_eng = time.perf_counter() - t1
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    view = active_view(result)
    price = float(candles[-1].close)
    tops = [float(p.top_price) for p in view.by_id.values()]
    bots = [float(p.bottom_price) for p in view.by_id.values()]
    return {
        "months": months,
        "from_close": from_close.isoformat(),
        "candle_load_start": meta["candle_load_start"].isoformat(),
        "candles_5m": len(candles),
        "load_seconds": round(t_load, 4),
        "engine_seconds": round(t_eng, 4),
        "total_seconds": round(t_load + t_eng, 4),
        "peak_tracemalloc_bytes": peak,
        "max_rss_mb": round(ru.ru_maxrss / 1024.0, 2),
        "known_pools": len(result.pools),
        "active_pools": len(view.by_id),
        "active_upper": sum(1 for p in view.by_id.values() if str(p.side).lower() == "upper"),
        "active_lower": sum(1 for p in view.by_id.values() if str(p.side).lower() == "lower"),
        "min_active_bottom": min(bots) if bots else None,
        "max_active_top": max(tops) if tops else None,
        "close_price": price,
        "view": view,
    }


def pools_inside_price(view: ActiveView, price: float) -> list[dict[str, Any]]:
    out = []
    for p in view.by_id.values():
        if float(p.bottom_price) <= price <= float(p.top_price):
            out.append(
                {
                    "pool_id": p.pool_id,
                    "side": str(p.side),
                    "pool_type": str(p.pool_type),
                    "top": float(p.top_price),
                    "bottom": float(p.bottom_price),
                }
            )
    return out


def nearest_upper_strict(view: ActiveView, price: float) -> dict[str, Any]:
    inside = [p for p in view.by_id.values() if str(p.side).lower() == "upper" and float(p.bottom_price) <= price <= float(p.top_price)]
    if inside:
        p = min(inside, key=lambda x: float(x.bottom_price))
        return {
            "kind": "PRICE_INSIDE_POOL",
            "pool_id": p.pool_id,
            "pool_type": str(p.pool_type),
            "top": float(p.top_price),
            "bottom": float(p.bottom_price),
            "strength": float(p.strength),
            "strength_class": str(p.strength_class),
            "distance_pct": 0.0,
            "known_at": p.known_at.isoformat() if p.known_at else None,
        }
    cands = [p for p in view.by_id.values() if str(p.side).lower() == "upper" and float(p.bottom_price) > price]
    if not cands:
        return {"kind": "NO_UPPER_POOL_ABOVE"}
    p = min(cands, key=lambda x: float(x.bottom_price))
    dist = (float(p.bottom_price) - price) / price * 100.0
    return {
        "kind": "NEAREST_ABOVE",
        "pool_id": p.pool_id,
        "pool_type": str(p.pool_type),
        "top": float(p.top_price),
        "bottom": float(p.bottom_price),
        "strength": float(p.strength),
        "strength_class": str(p.strength_class),
        "distance_pct": round(dist, 4),
        "known_at": p.known_at.isoformat() if p.known_at else None,
    }


def nearest_lower_strict(view: ActiveView, price: float) -> dict[str, Any]:
    inside = [p for p in view.by_id.values() if str(p.side).lower() == "lower" and float(p.bottom_price) <= price <= float(p.top_price)]
    if inside:
        p = max(inside, key=lambda x: float(x.top_price))
        return {
            "kind": "PRICE_INSIDE_POOL",
            "pool_id": p.pool_id,
            "pool_type": str(p.pool_type),
            "top": float(p.top_price),
            "bottom": float(p.bottom_price),
            "strength": float(p.strength),
            "strength_class": str(p.strength_class),
            "distance_pct": 0.0,
            "known_at": p.known_at.isoformat() if p.known_at else None,
        }
    cands = [p for p in view.by_id.values() if str(p.side).lower() == "lower" and float(p.top_price) < price]
    if not cands:
        return {"kind": "NO_LOWER_POOL_BELOW"}
    p = max(cands, key=lambda x: float(x.top_price))
    dist = (price - float(p.top_price)) / price * 100.0
    return {
        "kind": "NEAREST_BELOW",
        "pool_id": p.pool_id,
        "pool_type": str(p.pool_type),
        "top": float(p.top_price),
        "bottom": float(p.bottom_price),
        "strength": float(p.strength),
        "strength_class": str(p.strength_class),
        "distance_pct": round(dist, 4),
        "known_at": p.known_at.isoformat() if p.known_at else None,
    }


def bucket_distance(pct: float | None) -> str:
    if pct is None:
        return "none"
    for lo, hi in DIST_BUCKETS:
        if lo <= pct < hi:
            if hi > 100:
                return "over_10pct"
            return f"{lo}_{hi}pct"
    return "over_10pct"


def near_ids(view: ActiveView, price: float, pct: float) -> set[str]:
    lo, hi = price * (1 - pct / 100), price * (1 + pct / 100)
    return {
        pid
        for pid, p in view.by_id.items()
        if p.bottom_price <= hi and p.top_price >= lo
    }


def any_pool_in_band(view: ActiveView, price: float, pct: float) -> bool:
    return bool(near_ids(view, price, pct))


def compare_active(ref: ActiveView, other: ActiveView) -> dict[str, Any]:
    common = ref.pool_ids & other.pool_ids
    drift = []
    for pid in common:
        r, o = ref.by_id[pid], other.by_id[pid]
        if abs(r.top_price - o.top_price) > 1e-9 or abs(r.bottom_price - o.bottom_price) > 1e-9:
            drift.append(pid)
        if abs(r.strength - o.strength) > 1e-9:
            drift.append(f"{pid}:strength")
    return {
        "jaccard": round(jaccard(ref.pool_ids, other.pool_ids), 4),
        "missing_count": len(ref.pool_ids - other.pool_ids),
        "extra_count": len(other.pool_ids - ref.pool_ids),
        "bound_drift_count": len(drift),
    }


def ch_coverage() -> dict[str, Any]:
    cfg = load_clickhouse_config()
    c = clickhouse_connect.get_client(**cfg.connect_kwargs())
    row = c.query(
        f"SELECT min(open_time), max(open_time), count() FROM {cfg.database}.candles_1m WHERE symbol={{s:String}}",
        parameters={"s": SYMBOL},
    ).result_rows[0]
    c.close()
    return {
        "earliest_1m": row[0].isoformat(),
        "latest_1m": row[1].isoformat(),
        "count_1m": int(row[2]),
        "earliest_utc": DATA_EARLIEST.isoformat(),
        "earliest_T_with_full_6m_history": subtract_months(
            DATA_EARLIEST, -6
        ).isoformat()
        if DATA_EARLIEST.month + 6 <= 12
        else datetime(2026, 6, 11, tzinfo=timezone.utc).isoformat(),
    }


def verify_full_load(T: datetime, months: int = 6) -> dict[str, Any]:
    fc = subtract_months(T, months)
    end = int(T.timestamp())
    start = int(fc.timestamp()) - 400 * 300  # warmup slack
    p = load_candles(SYMBOL, TF, start=start, end=end)
    n = len(p.get("candles") or [])
    run = run_months(months, T)
    return {
        "decision_close": T.isoformat(),
        "from_close": fc.isoformat(),
        "loaded_5m_payload_count": n,
        "engine_candles_5m": run["candles_5m"],
        "truncation_suspect": n < run["candles_5m"] * 0.9,
    }


def analyze_T(T: datetime, cache: dict) -> dict[str, Any]:
    if subtract_months(T, 6) < DATA_EARLIEST:
        return {"decision_close": T.isoformat(), "skipped": True, "reason": "6m before DATA_EARLIEST"}
    runs = {}
    for m in MONTHS:
        key = (m, T.isoformat())
        if key not in cache:
            cache[key] = run_months(m, T)
        runs[m] = cache[key]
    ref = runs[6]["view"]
    price = runs[6]["close_price"]
    comp = {str(m): compare_active(ref, runs[m]["view"]) for m in MONTHS if m != 6}
    near = {}
    for b in BANDS:
        ref_set = near_ids(ref, price, b)
        near[str(b)] = {}
        for m in MONTHS:
            if m == 6:
                continue
            oset = near_ids(runs[m]["view"], price, b)
            near[str(b)][str(m)] = {
                "ref_count": len(ref_set),
                "other_count": len(oset),
                "jaccard": round(jaccard(ref_set, oset), 4),
                "missing": len(ref_set - oset),
                "extra": len(oset - ref_set),
            }
    nearest = {}
    for m in MONTHS:
        v = runs[m]["view"]
        p = runs[m]["close_price"]
        nearest[str(m)] = {
            "upper": nearest_upper_strict(v, p),
            "lower": nearest_lower_strict(v, p),
            "inside_pools": pools_inside_price(v, p),
        }
    ext = {}
    for m in (4, 5):
        v4, v6 = runs[4]["view"], runs[6]["view"]
        vm = runs[m]["view"]
        added_near_5 = near_ids(vm, price, 5.0) - near_ids(v4, price, 5.0)
        added_far = (vm.pool_ids - v4.pool_ids) - added_near_5
        ext[str(m)] = {
            "added_active_vs_4m": len(vm.pool_ids - v4.pool_ids),
            "removed_active_vs_4m": len(v4.pool_ids - vm.pool_ids),
            "added_near_5pct_vs_4m": len(added_near_5),
            "added_only_far_vs_4m": len(added_far),
        }
    return {
        "decision_close": T.isoformat(),
        "close_price": price,
        "runs_summary": {
            str(m): {k: runs[m][k] for k in runs[m] if k != "view"} for m in MONTHS
        },
        "active_jaccard_vs_6m": {k: comp[k]["jaccard"] for k in comp},
        "compare_vs_6m": comp,
        "near_price": near,
        "nearest_by_history": nearest,
        "extension_vs_4m": ext,
        "no_pool_within_3pct_4m": not any_pool_in_band(runs[4]["view"], price, 3.0),
        "no_pool_within_5pct_4m": not any_pool_in_band(runs[4]["view"], price, 5.0),
        "no_pool_within_3pct_6m": not any_pool_in_band(ref, price, 3.0),
    }


def rolling_study(cache: dict) -> dict[str, Any]:
    T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    step = _timeframe_seconds(TF) * 12
    rows = []
    for i in range(12):
        T = T0 + timedelta(seconds=step * i)
        if subtract_months(T, 6) < DATA_EARLIEST:
            continue
        r4 = cache.setdefault((4, T.isoformat()), run_months(4, T))
        r6 = cache.setdefault((6, T.isoformat()), run_months(6, T))
        keyf = ("fix", T.isoformat())
        if keyf not in cache:
            candles, meta = _load_candles_for_window(SYMBOL, TF, FIXED_FROM, T, PD)
            result, _ = run_engine_on_candles(candles, PD)
            cache[keyf] = {"view": active_view(result), "close_price": float(candles[-1].close)}
        vf = cache[keyf]["view"]
        price = r4["close_price"]
        rows.append(
            {
                "decision_close": T.isoformat(),
                "roll4_vs_roll6_jaccard": round(jaccard(r4["view"].pool_ids, r6["view"].pool_ids), 4),
                "fixed_vs_roll4_jaccard": round(jaccard(vf.pool_ids, r4["view"].pool_ids), 4),
                "near5_jaccard_4_6": round(
                    jaccard(near_ids(r4["view"], price, 5), near_ids(r6["view"], price, 5)), 4
                ),
            }
        )
    return {"fixed_from": FIXED_FROM.isoformat(), "steps": rows}


def main() -> int:
    cov = ch_coverage()
    print("Coverage", cov)
    verify = verify_full_load(PILOT_T, 6)
    if verify.get("truncation_suspect"):
        print("STOP: truncation suspected", verify)
        return 2

    cache: dict = {}
    pilot = {}
    for m in MONTHS:
        pilot[m] = {k: v for k, v in run_months(m, PILOT_T).items() if k != "view"}

    multi = [analyze_T(T, cache) for T in TEST_CLOSES]
    rolling = rolling_study(cache)

    # distributions from 4m nearest at each multi point
    upper_buckets: dict[str, int] = {}
    lower_buckets: dict[str, int] = {}
    inside_cases = 0
    no3_4 = no5_4 = 0
    for row in multi:
        if row.get("skipped"):
            continue
        n4 = row["nearest_by_history"]["4"]
        u, l = n4["upper"], n4["lower"]
        if u.get("kind") == "PRICE_INSIDE_POOL" or l.get("kind") == "PRICE_INSIDE_POOL":
            inside_cases += 1
        ub = bucket_distance(u.get("distance_pct") if u.get("kind") == "NEAREST_ABOVE" else None)
        lb = bucket_distance(l.get("distance_pct") if l.get("kind") == "NEAREST_BELOW" else None)
        upper_buckets[ub] = upper_buckets.get(ub, 0) + 1
        lower_buckets[lb] = lower_buckets.get(lb, 0) + 1
        if row["no_pool_within_3pct_4m"]:
            no3_4 += 1
        if row["no_pool_within_5pct_4m"]:
            no5_4 += 1

    jacc_agg = {str(m): [] for m in MONTHS if m != 6}
    near_agg = {str(b): {str(m): [] for m in MONTHS if m != 6} for b in BANDS}
    for row in multi:
        if row.get("skipped"):
            continue
        for m in jacc_agg:
            jacc_agg[m].append(row["active_jaccard_vs_6m"][m])
        for b in BANDS:
            for m in MONTHS:
                if m == 6:
                    continue
                near_agg[str(b)][str(m)].append(row["near_price"][str(b)][str(m)]["jaccard"])

    avg_j = {m: round(statistics.mean(v), 4) for m, v in jacc_agg.items() if v}
    avg_near = {
        str(b): {m: round(statistics.mean(v), 4) for m, v in near_agg[str(b)].items() if v}
        for b in BANDS
    }

    j4 = avg_j.get("4", 0)
    rec = "4M"
    conf = "MEDIUM"
    if j4 >= 0.99:
        rec = "4M (full active parity to 6M in sample)"
        conf = "MEDIUM"
    elif avg_j.get("3", 0) >= 0.98:
        rec = "3M minimum; 4M preferred"
        conf = "MEDIUM"
    else:
        rec = "4M-6M; NOT_CONCLUSIVE on shorter"
        conf = "LOW"

    out = {
        "SYMBOL": SYMBOL,
        "DOGE_CANDLE_COVERAGE": cov,
        "LOAD_VERIFICATION_PILOT_6M": verify,
        "PILOT_DECISION_CLOSE": PILOT_T.isoformat(),
        "pilot": pilot,
        "DECISION_CLOSES_TESTED": [r["decision_close"] for r in multi if not r.get("skipped")],
        "multi_point": multi,
        "rolling_window": rolling,
        "aggregate_active_jaccard_vs_6m": avg_j,
        "aggregate_near_price_jaccard_vs_6m": avg_near,
        "FULL_ACTIVE_PARITY_4M_VS_6M": avg_j.get("4"),
        "NEAREST_UPPER_DISTANCE_DISTRIBUTION_4M": upper_buckets,
        "NEAREST_LOWER_DISTANCE_DISTRIBUTION_4M": lower_buckets,
        "PRICE_INSIDE_POOL_CASES": inside_cases,
        "NO_POOL_WITHIN_3PCT_4M_count": no3_4,
        "NO_POOL_WITHIN_5PCT_4M_count": no5_4,
        "RECOMMENDED_HISTORY_DOGE_5M_PD10": rec,
        "RECOMMENDATION_CONFIDENCE": conf,
        "LIMITATIONS": "CH 1m only from 2025-12-11; single symbol/PD; ~8 test closes; no loader cap when start/end set.",
    }
    path = Path(__file__).with_name("phase_doge_history_relevance_results.json")
    path.write_text(json.dumps(out, indent=2, default=str))
    print("Wrote", path)
    print(json.dumps({k: out[k] for k in out if k not in ("multi_point", "pilot")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
