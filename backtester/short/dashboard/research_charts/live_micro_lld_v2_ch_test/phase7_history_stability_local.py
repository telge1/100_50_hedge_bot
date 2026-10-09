#!/usr/bin/env python3
"""
Phase 7 local read-only analysis (no CH writes, no engine changes).
Output: phase7_history_stability_results.json alongside this file.
"""

from __future__ import annotations

import calendar
import json
import resource
import sys
import time
import tracemalloc
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.live_micro_lld_v2_ch_test.batch import (
    _load_candles_for_window,
    run_engine_on_candles,
)
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config

SYMBOL = "TUTUSDT"
TF = "5m"
PD = 10
DATA_EARLIEST = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
MONTHS = (1, 2, 3, 4, 5, 6)
BANDS = (2.0, 5.0, 10.0)
FIXED_FROM_CLOSE = datetime(2026, 4, 8, 11, 20, tzinfo=timezone.utc)
PILOT_T = datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc)

TEST_CLOSES = [
    datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 6, 15, 45, tzinfo=timezone.utc),
    datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc),
]

ROLLING_T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
ROLLING_STEPS = 12


def subtract_months(dt: datetime, months: int) -> datetime:
    y, m = dt.year, dt.month
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d, hour=dt.hour, minute=dt.minute, second=dt.second)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    if not u:
        return 1.0
    return len(a & b) / len(u)


@dataclass
class ActiveView:
    pool_ids: set[str]
    by_id: dict[str, Any]
    upper: int
    lower: int


def run_at_window(from_close: datetime, to_close: datetime) -> dict[str, Any]:
    tracemalloc.start()
    t0 = time.perf_counter()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    candles, meta = _load_candles_for_window(SYMBOL, TF, from_close, to_close, PD)
    t_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    result, cfg = run_engine_on_candles(candles, PD)
    t_engine = time.perf_counter() - t1
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    max_rss_mb = (ru1.ru_maxrss) / 1024.0  # linux kb

    active: list = [p for p in result.pools if p.active]
    by_id = {p.pool_id: p for p in active}
    ids = set(by_id)
    upper = sum(1 for p in active if str(p.side).lower() == "upper")
    lower = sum(1 for p in active if str(p.side).lower() == "lower")
    close_p = float(candles[-1].close) if candles else None

    packed = meta.get("packed") or {}
    rows_1m_est = len(packed.get("candles") or packed.get("rows") or [])

    return {
        "from_close": from_close.isoformat(),
        "to_close": to_close.isoformat(),
        "candle_load_start": meta["candle_load_start"].isoformat(),
        "warmup_bars": meta["warmup_bars"],
        "candles_5m": len(candles),
        "packed_payload_rows_est": rows_1m_est,
        "load_seconds": round(t_load, 4),
        "engine_seconds": round(t_engine, 4),
        "total_seconds": round(t_load + t_engine, 4),
        "peak_tracemalloc_bytes": peak,
        "max_rss_mb": round(max_rss_mb, 2),
        "known_pools": len(result.pools),
        "active_pools": len(active),
        "active_upper": upper,
        "active_lower": lower,
        "close_price": close_p,
        "view": ActiveView(ids, by_id, upper, lower),
    }


def compare_views(ref: ActiveView, other: ActiveView) -> dict[str, Any]:
    common = ref.pool_ids & other.pool_ids
    missing = sorted(ref.pool_ids - other.pool_ids)
    extra = sorted(other.pool_ids - ref.pool_ids)
    bound_drift = []
    for pid in sorted(common):
        r, o = ref.by_id[pid], other.by_id[pid]
        if abs(r.top_price - o.top_price) > 1e-9 or abs(r.bottom_price - o.bottom_price) > 1e-9:
            bound_drift.append(pid)
        if abs(r.strength - o.strength) > 1e-9:
            bound_drift.append(f"{pid}:strength")
    return {
        "jaccard": round(jaccard(ref.pool_ids, other.pool_ids), 4),
        "common": len(common),
        "missing_vs_ref": missing[:20],
        "missing_count": len(missing),
        "extra_vs_ref": extra[:20],
        "extra_count": len(extra),
        "bound_or_strength_drift_common": bound_drift[:10],
        "bound_drift_count": len(bound_drift),
    }


def near_price_ids(view: ActiveView, price: float, pct: float) -> set[str]:
    lo = price * (1.0 - pct / 100.0)
    hi = price * (1.0 + pct / 100.0)
    out: set[str] = set()
    for pid, p in view.by_id.items():
        if p.bottom_price <= hi and p.top_price >= lo:
            out.add(pid)
    return out


def nearest_upper(view: ActiveView, price: float) -> dict[str, Any] | None:
    cands = [
        p
        for p in view.by_id.values()
        if str(p.side).lower() == "upper" and float(p.bottom_price) >= price
    ]
    if not cands:
        return None
    p = min(cands, key=lambda x: float(x.bottom_price))
    return {"pool_id": p.pool_id, "bottom": float(p.bottom_price), "top": float(p.top_price)}


def nearest_lower(view: ActiveView, price: float) -> dict[str, Any] | None:
    cands = [
        p
        for p in view.by_id.values()
        if str(p.side).lower() == "lower" and float(p.top_price) <= price
    ]
    if not cands:
        return None
    p = max(cands, key=lambda x: float(x.top_price))
    return {"pool_id": p.pool_id, "bottom": float(p.bottom_price), "top": float(p.top_price)}


def extremes(view: ActiveView) -> dict[str, Any]:
    if not view.by_id:
        return {"highest_top": None, "lowest_bottom": None}
    tops = [float(p.top_price) for p in view.by_id.values()]
    bots = [float(p.bottom_price) for p in view.by_id.values()]
    return {"highest_top": max(tops), "lowest_bottom": min(bots)}


def valid_months_at(T: datetime) -> list[int]:
    ok = []
    for m in MONTHS:
        fc = subtract_months(T, m)
        if fc >= DATA_EARLIEST:
            ok.append(m)
    return ok


def analyze_decision_close(T: datetime) -> dict[str, Any]:
    months_ok = valid_months_at(T)
    if 6 not in months_ok:
        return {"decision_close": T.isoformat(), "skipped": True, "reason": "6m history unavailable"}
    runs: dict[int, dict[str, Any]] = {}
    for m in MONTHS:
        if m not in months_ok:
            continue
        fc = subtract_months(T, m)
        runs[m] = run_at_window(fc, T)

    ref = runs[6]["view"]
    price = runs[6]["close_price"]
    comp: dict[str, Any] = {}
    for m in MONTHS:
        if m == 6 or m not in runs:
            continue
        comp[str(m)] = compare_views(ref, runs[m]["view"])

    near: dict[str, Any] = {}
    for pct in BANDS:
        ref_set = near_price_ids(ref, price, pct)
        near[str(pct)] = {}
        for m in MONTHS:
            if m == 6 or m not in runs:
                continue
            oset = near_price_ids(runs[m]["view"], price, pct)
            inter = ref_set & oset
            near[str(pct)][str(m)] = {
                "ref_count": len(ref_set),
                "other_count": len(oset),
                "common": len(inter),
                "jaccard": round(jaccard(ref_set, oset), 4),
                "missing_count": len(ref_set - oset),
                "extra_count": len(oset - ref_set),
            }

    nu_ref = nearest_upper(ref, price)
    nl_ref = nearest_lower(ref, price)
    nearest_cmp = {"upper": {}, "lower": {}}
    for m in MONTHS:
        if m == 6 or m not in runs:
            continue
        nu = nearest_upper(runs[m]["view"], price)
        nl = nearest_lower(runs[m]["view"], price)
        nearest_cmp["upper"][str(m)] = {
            "same_pool_id": (nu_ref and nu and nu_ref["pool_id"] == nu["pool_id"]),
            "ref": nu_ref,
            "other": nu,
        }
        nearest_cmp["lower"][str(m)] = {
            "same_pool_id": (nl_ref and nl and nl_ref["pool_id"] == nl["pool_id"]),
            "ref": nl_ref,
            "other": nl,
        }

    ex_ref = extremes(ref)
    ex_cmp = {}
    for m in MONTHS:
        if m == 6 or m not in runs:
            continue
        ex_o = extremes(runs[m]["view"])
        ex_cmp[str(m)] = {
            "highest_top_delta": abs((ex_ref["highest_top"] or 0) - (ex_o["highest_top"] or 0)),
            "lowest_bottom_delta": abs((ex_ref["lowest_bottom"] or 0) - (ex_o["lowest_bottom"] or 0)),
        }

    strip = {str(m): {k: v for k, v in runs[m].items() if k != "view"} for m in runs}
    return {
        "decision_close": T.isoformat(),
        "close_price": price,
        "months_available": months_ok,
        "runs": strip,
        "active_jaccard_vs_6m": {k: v["jaccard"] for k, v in comp.items()},
        "compare_vs_6m": comp,
        "near_price": near,
        "nearest_pools": nearest_cmp,
        "extremes_vs_6m": ex_cmp,
    }


def rolling_study() -> dict[str, Any]:
    from datetime import timedelta

    from research_charts.service import _timeframe_seconds

    step = _timeframe_seconds(TF)
    closes = [ROLLING_T0 + timedelta(seconds=step * i) for i in range(ROLLING_STEPS)]
    rows = []
    for T in closes:
        if 6 not in valid_months_at(T):
            continue
        fixed = run_at_window(FIXED_FROM_CLOSE, T)
        r3 = run_at_window(subtract_months(T, 3), T)
        r4 = run_at_window(subtract_months(T, 4), T)
        r6 = run_at_window(subtract_months(T, 6), T)
        price = fixed["close_price"]
        rows.append(
            {
                "decision_close": T.isoformat(),
                "fixed_active": fixed["active_pools"],
                "roll3_jaccard_vs_fixed": round(jaccard(fixed["view"].pool_ids, r3["view"].pool_ids), 4),
                "roll4_jaccard_vs_fixed": round(jaccard(fixed["view"].pool_ids, r4["view"].pool_ids), 4),
                "roll6_jaccard_vs_fixed": round(jaccard(fixed["view"].pool_ids, r6["view"].pool_ids), 4),
                "fixed_missing_in_roll3": len(fixed["view"].pool_ids - r3["view"].pool_ids),
                "nearest_upper_fixed_vs_roll3": nearest_upper(fixed["view"], price),
                "nearest_upper_roll3": nearest_upper(r3["view"], price),
            }
        )
    return {"fixed_from_close": FIXED_FROM_CLOSE.isoformat(), "steps": rows}


def main() -> int:
    print("Phase 7 pilot...", flush=True)
    pilot_runs = {}
    for m in MONTHS:
        fc = subtract_months(PILOT_T, m)
        if fc < DATA_EARLIEST:
            continue
        pilot_runs[m] = run_at_window(fc, PILOT_T)
    pilot = {m: {k: v for k, v in pilot_runs[m].items() if k != "view"} for m in pilot_runs}

    print("Phase 7 multi-point...", flush=True)
    multi = [analyze_decision_close(T) for T in TEST_CLOSES]

    print("Phase 7 rolling...", flush=True)
    rolling = rolling_study()

    # Aggregate recommendation inputs
    jacc_by_m: dict[int, list[float]] = {m: [] for m in MONTHS if m != 6}
    near5: dict[int, list[float]] = {m: [] for m in MONTHS if m != 6}
    for row in multi:
        if row.get("skipped"):
            continue
        for m, j in row.get("active_jaccard_vs_6m", {}).items():
            jacc_by_m[int(m)].append(j)
        for m in jacc_by_m:
            near5[m].append(row["near_price"]["5.0"][str(m)]["jaccard"])

    avg_jacc = {str(m): round(sum(v) / len(v), 4) if v else None for m, v in jacc_by_m.items()}
    avg_near5 = {str(m): round(sum(v) / len(v), 4) if v else None for m, v in near5.items()}

    out = {
        "SYMBOL": SYMBOL,
        "TIMEFRAME": TF,
        "PD": PD,
        "AVAILABLE_HISTORY": f"{DATA_EARLIEST.isoformat()} .. (CH candles_1m max ~2026-10-08)",
        "PILOT_DECISION_CLOSE": PILOT_T.isoformat(),
        "HISTORY_LENGTHS_TESTED": list(MONTHS),
        "pilot": pilot,
        "multi_point": multi,
        "rolling_window": rolling,
        "aggregate_avg_jaccard_vs_6m": avg_jacc,
        "aggregate_avg_near_5pct_jaccard_vs_6m": avg_near5,
    }
    out_path = Path(__file__).with_name("phase7_history_stability_results.json")
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
