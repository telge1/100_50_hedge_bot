#!/usr/bin/env python3
"""
Phase 8 read-only: 4m baseline + 10% boundary trigger simulation, 5m/6m extension value.
Output: phase8_dynamic_extension_results.json
"""

from __future__ import annotations

import calendar
import json
import resource
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
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
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    candle_sequence_fingerprint,
    engine_input_hash,
    engine_input_document,
    config_hash,
)
from research_charts.service import _timeframe_seconds

SYMBOL = "TUTUSDT"
TF = "5m"
PD = 10
DATA_EARLIEST = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
BASELINE_M = 4
EXT1_M = 5
EXT2_M = 6
TRIGGER_PCT = 10.0
BANDS = (2.0, 5.0, 10.0)

PERIOD_START = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc)
CLOSE_STRIDE_BARS = 24  # 2h on 5m (ressourcenschonend)


def subtract_months(dt: datetime, months: int) -> datetime:
    y, m = dt.year, dt.month
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d, hour=dt.hour, minute=dt.minute, second=dt.second)


def enumerate_closes(start: datetime, end: datetime, stride_bars: int) -> list[datetime]:
    step = _timeframe_seconds(TF) * stride_bars
    out: list[datetime] = []
    t = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    while t <= end:
        if subtract_months(t, EXT2_M) >= DATA_EARLIEST:
            out.append(t)
        t += timedelta(seconds=step)
    return out


def active_view(result) -> dict[str, Any]:
    active = [p for p in result.pools if p.active]
    by_id = {p.pool_id: p for p in active}
    return {
        "pool_ids": set(by_id),
        "by_id": by_id,
        "count": len(active),
        "upper": sum(1 for p in active if str(p.side).lower() == "upper"),
        "lower": sum(1 for p in active if str(p.side).lower() == "lower"),
    }


def run_months(months: int, decision_close: datetime) -> dict[str, Any]:
    from_close = subtract_months(decision_close, months)
    t0 = time.perf_counter()
    candles, meta = _load_candles_for_window(SYMBOL, TF, from_close, decision_close, PD)
    t_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    result, cfg = run_engine_on_candles(candles, PD)
    t_eng = time.perf_counter() - t1
    ru = resource.getrusage(resource.RUSAGE_SELF)
    view = active_view(result)
    price = float(candles[-1].close)
    doc = engine_input_document(
        cfg=cfg,
        symbol=SYMBOL,
        timeframe=TF,
        from_close=from_close,
        to_close=decision_close,
        candle_load_start=meta["candle_load_start"],
        candles=candles,
        warmup_bars=meta["warmup_bars"],
    )
    return {
        "months": months,
        "from_close": from_close.isoformat(),
        "candle_load_start": meta["candle_load_start"].isoformat(),
        "candles_5m": len(candles),
        "engine_input_hash": engine_input_hash(doc),
        "config_hash": config_hash(cfg),
        "prefix_candle_fingerprint": candle_sequence_fingerprint(candles),
        "load_seconds": round(t_load, 4),
        "engine_seconds": round(t_eng, 4),
        "total_seconds": round(t_load + t_eng, 4),
        "max_rss_mb": round(ru.ru_maxrss / 1024.0, 2),
        "close_price": price,
        "view": view,
    }


def highest_upper_top(view: dict[str, Any]) -> float | None:
    tops = [
        float(p.top_price)
        for p in view["by_id"].values()
        if str(p.side).lower() == "upper"
    ]
    return max(tops) if tops else None


def lowest_lower_bottom(view: dict[str, Any]) -> float | None:
    bots = [
        float(p.bottom_price)
        for p in view["by_id"].values()
        if str(p.side).lower() == "lower"
    ]
    return min(bots) if bots else None


def upper_trigger(price: float, view: dict[str, Any]) -> dict[str, Any]:
    """
    Rising / approach from below to highest active upper top (U_max).
    gap_pct = (U_max - P) / U_max * 100 when P <= U_max; trigger if gap <= 10% or P > U_max.
    """
    umax = highest_upper_top(view)
    if umax is None:
        return {"fired": False, "reason": "NO_BOUNDARY_POOL", "gap_pct": None, "boundary": None}
    if price > umax:
        return {"fired": True, "reason": "PRICE_ABOVE_BOUNDARY", "gap_pct": 0.0, "boundary": umax}
    gap = (umax - price) / umax * 100.0
    return {
        "fired": gap <= TRIGGER_PCT,
        "reason": "WITHIN_10PCT_FROM_BELOW" if gap <= TRIGGER_PCT else "OUTSIDE_THRESHOLD",
        "gap_pct": round(gap, 4),
        "boundary": umax,
    }


def lower_trigger(price: float, view: dict[str, Any]) -> dict[str, Any]:
    """
    Falling / approach from above to lowest active lower bottom (L_min).
    gap_pct = (P - L_min) / L_min * 100 when P >= L_min; trigger if gap <= 10% or P < L_min.
    """
    lmin = lowest_lower_bottom(view)
    if lmin is None:
        return {"fired": False, "reason": "NO_BOUNDARY_POOL", "gap_pct": None, "boundary": None}
    if price < lmin:
        return {"fired": True, "reason": "PRICE_BELOW_BOUNDARY", "gap_pct": 0.0, "boundary": lmin}
    gap = (price - lmin) / lmin * 100.0
    return {
        "fired": gap <= TRIGGER_PCT,
        "reason": "WITHIN_10PCT_FROM_ABOVE" if gap <= TRIGGER_PCT else "OUTSIDE_THRESHOLD",
        "gap_pct": round(gap, 4),
        "boundary": lmin,
    }


def near_ids(view: dict[str, Any], price: float, pct: float) -> set[str]:
    lo, hi = price * (1 - pct / 100), price * (1 + pct / 100)
    out: set[str] = set()
    for pid, p in view["by_id"].items():
        if p.bottom_price <= hi and p.top_price >= lo:
            out.add(pid)
    return out


def nearest_upper(view: dict[str, Any], price: float) -> dict[str, Any] | None:
    cands = [
        p
        for p in view["by_id"].values()
        if str(p.side).lower() == "upper" and float(p.bottom_price) >= price
    ]
    if not cands:
        return None
    p = min(cands, key=lambda x: float(x.bottom_price))
    return {"pool_id": p.pool_id, "top": float(p.top_price), "bottom": float(p.bottom_price)}


def nearest_lower(view: dict[str, Any], price: float) -> dict[str, Any] | None:
    cands = [
        p
        for p in view["by_id"].values()
        if str(p.side).lower() == "lower" and float(p.top_price) <= price
    ]
    if not cands:
        return None
    p = max(cands, key=lambda x: float(x.top_price))
    return {"pool_id": p.pool_id, "top": float(p.top_price), "bottom": float(p.bottom_price)}


def compare_extension(base: dict[str, Any], ext: dict[str, Any], price: float) -> dict[str, Any]:
    bv, ev = base["view"], ext["view"]
    added = sorted(ev["pool_ids"] - bv["pool_ids"])
    removed = sorted(bv["pool_ids"] - ev["pool_ids"])
    umax4 = highest_upper_top(bv)
    lmin4 = lowest_lower_bottom(bv)
    new_higher = []
    if umax4 is not None:
        for p in ev["by_id"].values():
            if str(p.side).lower() == "upper" and float(p.top_price) > umax4:
                new_higher.append(p.pool_id)
    new_lower = []
    if lmin4 is not None:
        for p in ev["by_id"].values():
            if str(p.side).lower() == "lower" and float(p.bottom_price) < lmin4:
                new_lower.append(p.pool_id)
    near = {}
    for band in BANDS:
        bset, eset = near_ids(bv, price, band), near_ids(ev, price, band)
        inter = bset & eset
        near[str(band)] = {
            "jaccard": round(len(inter) / len(bset | eset), 4) if (bset | eset) else 1.0,
            "added_near": len(eset - bset),
            "removed_near": len(bset - eset),
        }
    nu4, nu5 = nearest_upper(bv, price), nearest_upper(ev, price)
    nl4, nl5 = nearest_lower(bv, price), nearest_lower(ev, price)
    return {
        "added_active_ids": added[:30],
        "added_count": len(added),
        "removed_active_ids": removed[:30],
        "removed_count": len(removed),
        "new_higher_upper_pool_ids": new_higher[:20],
        "new_higher_upper_count": len(new_higher),
        "new_lower_lower_pool_ids": new_lower[:20],
        "new_lower_lower_count": len(new_lower),
        "NO_ADDITIONAL_OUTER_UPPER": len(new_higher) == 0,
        "NO_ADDITIONAL_OUTER_LOWER": len(new_lower) == 0,
        "NO_ADDITIONAL_ACTIVE_POOLS": len(added) == 0,
        "nearest_upper_same": (nu4 and nu5 and nu4["pool_id"] == nu5["pool_id"]),
        "nearest_lower_same": (nl4 and nl5 and nl4["pool_id"] == nl5["pool_id"]),
        "nearest_upper": {"base": nu4, "ext": nu5},
        "nearest_lower": {"base": nl4, "ext": nl5},
        "near_price": near,
        "engine_input_hash_changed": base["engine_input_hash"] != ext["engine_input_hash"],
    }


def main() -> int:
    closes = enumerate_closes(PERIOD_START, PERIOD_END, CLOSE_STRIDE_BARS)
    cache: dict[tuple[int, str], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    trigger_events: list[dict[str, Any]] = []

    print(f"Phase 8: {len(closes)} decision closes (stride={CLOSE_STRIDE_BARS} bars)...", flush=True)

    for i, T in enumerate(closes):
        key4 = (BASELINE_M, T.isoformat())
        if key4 not in cache:
            cache[key4] = run_months(BASELINE_M, T)
        b4 = cache[key4]
        view = b4["view"]
        price = b4["close_price"]
        ut = upper_trigger(price, view)
        lt = lower_trigger(price, view)
        any_trig = ut["fired"] or lt["fired"]

        row = {
            "decision_close": T.isoformat(),
            "close_price": price,
            "active_pools_4m": view["count"],
            "upper_active_4m": view["upper"],
            "lower_active_4m": view["lower"],
            "highest_upper_top": highest_upper_top(view),
            "lowest_lower_bottom": lowest_lower_bottom(view),
            "upper_trigger": ut,
            "lower_trigger": lt,
            "any_trigger": any_trig,
        }
        if any_trig:
            ext5 = cache.setdefault((EXT1_M, T.isoformat()), run_months(EXT1_M, T))
            ext6 = cache.setdefault((EXT2_M, T.isoformat()), run_months(EXT2_M, T))
            c5 = compare_extension(b4, ext5, price)
            c6 = compare_extension(b4, ext6, price)
            row["extension"] = {
                "5m": {
                    "timing": {
                        "total_seconds": ext5["total_seconds"],
                        "engine_seconds": ext5["engine_seconds"],
                        "candles_5m": ext5["candles_5m"],
                    },
                    "compare": c5,
                },
                "6m": {
                    "timing": ext6["total_seconds"],
                    "compare": c6,
                },
            }
            trigger_events.append(
                {
                    "decision_close": T.isoformat(),
                    "upper": ut["fired"],
                    "lower": lt["fired"],
                    "added_5m": c5["added_count"],
                    "added_6m": c6["added_count"],
                    "new_higher_5m": c5["new_higher_upper_count"],
                    "new_higher_6m": c6["new_higher_upper_count"],
                    "new_lower_5m": c5["new_lower_lower_count"],
                    "new_lower_6m": c6["new_lower_lower_count"],
                    "no_add_5m": c5["NO_ADDITIONAL_ACTIVE_POOLS"],
                    "no_add_6m": c6["NO_ADDITIONAL_ACTIVE_POOLS"],
                }
            )
        rows.append(row)
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(closes)}", flush=True)

    upper_count = sum(1 for r in rows if r["upper_trigger"]["fired"])
    lower_count = sum(1 for r in rows if r["lower_trigger"]["fired"])
    any_count = sum(1 for r in rows if r["any_trigger"])

    # consecutive trigger streaks
    streaks: list[int] = []
    cur = 0
    for r in rows:
        if r["any_trigger"]:
            cur += 1
        else:
            if cur:
                streaks.append(cur)
            cur = 0
    if cur:
        streaks.append(cur)

    ext5_times = [
        r["extension"]["5m"]["timing"]["total_seconds"]
        for r in rows
        if r.get("extension")
    ]
    ext6_times = [r["extension"]["6m"]["timing"] for r in rows if r.get("extension")]

    no_add_cases = [
        e for e in trigger_events if e["no_add_5m"] and e["no_add_6m"]
    ]
    no_outer_upper = [
        e
        for e in trigger_events
        if e["upper"] and e["new_higher_5m"] == 0 and e["new_higher_6m"] == 0
    ]

    # near price parity aggregate on triggered
    near_agg = {str(b): [] for b in BANDS}
    for r in rows:
        if not r.get("extension"):
            continue
        for b in BANDS:
            near_agg[str(b)].append(r["extension"]["5m"]["compare"]["near_price"][str(b)]["jaccard"])

    summary = {
        "HISTORY_BASELINE": "4M",
        "HISTORY_EXTENSION_1": "5M",
        "HISTORY_EXTENSION_2": "6M",
        "TRIGGER_THRESHOLD": "10%",
        "TRIGGER_DEFINITION": {
            "upper": "gap_pct=(U_max-P)/U_max*100, fire if P>U_max or gap<=10%",
            "lower": "gap_pct=(P-L_min)/L_min*100, fire if P<L_min or gap<=10%",
        },
        "TEST_PERIOD": f"{PERIOD_START.isoformat()} .. {PERIOD_END.isoformat()}",
        "CLOSE_STRIDE_BARS": CLOSE_STRIDE_BARS,
        "DECISION_CLOSES_TESTED": len(closes),
        "UPPER_TRIGGER_COUNT": upper_count,
        "LOWER_TRIGGER_COUNT": lower_count,
        "ANY_TRIGGER_COUNT": any_count,
        "TRIGGER_RATE": round(any_count / len(closes), 4) if closes else 0,
        "TRIGGER_TIMESTAMPS": [r["decision_close"] for r in rows if r["any_trigger"]],
        "MAX_CONSECUTIVE_TRIGGER_STREAK": max(streaks) if streaks else 0,
        "MEAN_CONSECUTIVE_TRIGGER_STREAK": round(statistics.mean(streaks), 2) if streaks else 0,
        "NO_ADDITIONAL_ACTIVE_POOLS_CASES": len(no_add_cases),
        "NO_OUTER_UPPER_ON_UPPER_TRIGGER": len(no_outer_upper),
        "EXTENSION_RUNTIME_5M_AVG": round(statistics.mean(ext5_times), 4) if ext5_times else None,
        "EXTENSION_RUNTIME_6M_AVG": round(statistics.mean(ext6_times), 4) if ext6_times else None,
        "EXTENSION_RUNTIME_5M_MAX": max(ext5_times) if ext5_times else None,
        "EXTENSION_RUNTIME_6M_MAX": max(ext6_times) if ext6_times else None,
        "BASELINE_4M_RUNTIME_AVG": round(
            statistics.mean(cache[(BASELINE_M, c.isoformat())]["total_seconds"] for c in closes),
            4,
        ),
        "NEAR_PRICE_PARITY_ON_TRIGGERS_AVG_JACCARD_5M": {
            k: round(statistics.mean(v), 4) if v else None for k, v in near_agg.items()
        },
        "HISTORICAL_STATE_VERSIONING_REQUIRED": "YES",
        "BACKTEST_CAUSALITY_RISK": (
            "LOW if extension only applies forward from trigger close with new engine_input_hash; "
            "HIGH if old published 4m states were retroactively replaced."
        ),
        "REPEATED_TRIGGER_BEHAVIOR": {
            "trigger_events": len(trigger_events),
            "events_with_zero_add_5m_and_6m": len(no_add_cases),
            "note": "Cooldown/hysteresis not implemented; frequent streaks suggest need in live design.",
        },
        "rows": rows,
        "trigger_events_summary": trigger_events,
    }

    # usefulness heuristic
    useful_5 = sum(
        1
        for e in trigger_events
        if e["new_higher_5m"] or e["new_lower_5m"] or (e["added_5m"] and not e["no_add_5m"])
    )
    useful_6 = sum(
        1
        for e in trigger_events
        if e["new_higher_6m"] or e["new_lower_6m"] or (e["added_6m"] and not e["no_add_6m"])
    )
    if any_count == 0:
        dyn = "NOT_CONCLUSIVE"
    elif useful_5 / any_count < 0.1:
        dyn = "NO"
    elif useful_5 / any_count > 0.3:
        dyn = "YES"
    else:
        dyn = "NOT_CONCLUSIVE"

    summary["DYNAMIC_EXTENSION_USEFUL"] = dyn
    summary["RECOMMENDED_TRIGGER_APPROACH"] = (
        "Keep 4m as published baseline; on 10% boundary approach, optionally compute 5m/6m in parallel "
        "and publish NEW state only if outer pools or near-price coverage improve; require new "
        "engine_input_hash; retain prior published state until validation completes."
    )
    summary["LIMITATIONS"] = (
        "Single symbol/TF/PD; hourly stride; CH history from 2026-01-01; Phase 7 showed 4m=6m active sets "
        "at many points — extension may rarely change outers."
    )

    out_path = Path(__file__).with_name("phase8_dynamic_extension_results.json")
    out_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"Wrote {out_path}")
    print(
        json.dumps(
            {k: summary[k] for k in summary if k != "rows" and k != "trigger_events_summary"},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
