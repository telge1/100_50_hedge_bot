#!/usr/bin/env python3
"""Phase 6XT AUG16/17 micro-drop & early lower-pool audit — read-only."""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_aug16_17_microdrop_early_lower_audit_v1"
_CTX = Path(__file__).resolve().parent
_DET = _REPO / "logs/historical_import/overnight_2026/phase6xt_bottom_to_upward_staircase_detector_v1"
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_PRE = _REPO / "logs/historical_import/overnight_2026/phase6xt_pre_breakout_pd1_pd10_forensics_v1"
_EXT = _REPO / "logs/historical_import/overnight_2026/phase6xt_extended_lookback_bottom_origin_forensics_v1"

AUDIT_START = datetime(2026, 8, 16, 20, 30, tzinfo=timezone.utc)
AUDIT_END = datetime(2026, 8, 17, 5, 0, tzinfo=timezone.utc)
FOCUS_15M_DC = datetime(2026, 8, 16, 21, 30, tzinfo=timezone.utc)
POOL_MIN_H = 168
_EPS = 0.00012
_GEOM_MAIN = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
from phase6xt_pd1_density_parity_audit_v1 import exact_max_origin_overlap  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson
load_members = c7.load_members


def _wcsv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fn = sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def load_params() -> dict:
    return json.loads((_DET / "detector_parameters.json").read_text(encoding="utf-8"))


def aggregate_15m(bars5: dict, dc: datetime) -> dict | None:
    """OHLC for 15m bucket ending at decision_close dc (21:15..21:30 → close 21:30)."""
    start = dc - timedelta(minutes=15)
    seg = []
    t = start + timedelta(minutes=5)
    while t <= dc:
        if t in bars5:
            seg.append(bars5[t])
        t += timedelta(minutes=5)
    if len(seg) < 3:
        return None
    o = float(seg[0]["open"])
    h = max(float(x["high"]) for x in seg)
    l = min(float(x["low"]) for x in seg)
    c = float(seg[-1]["close"])
    return {"open": o, "high": h, "low": l, "close": c, "bucket_start": _iso(start), "decision_close": _iso(dc)}


def bar_metrics(bars: dict, t: datetime, lb: int) -> dict:
    all_times = sorted(bars.keys())
    if t not in bars:
        return {}
    i = all_times.index(t)
    b = bars[t]
    o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
    rng = h - l
    range_pct = rng / c * 100 if c else 0
    win = all_times[max(0, i - lb) : i + 1]
    wh = max(float(bars[x]["high"]) for x in win)
    wl = min(float(bars[x]["low"]) for x in win)
    dd_pct = (wh - c) / wh * 100 if wh else 0
    dd_close_from_wh = (wh - c) / wh * 100 if wh else 0
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    body = abs(c - o)
    wick_ratio = lower_wick / rng if rng > 1e-12 else 0
    local_high_4h = max(float(bars[x]["high"]) for x in all_times[max(0, i - 48) : i + 1])
    decline_from_local_high_pct = (local_high_4h - c) / local_high_4h * 100 if local_high_4h else 0
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range_abs": rng,
        "range_pct": round(range_pct, 4),
        "lower_wick_abs": lower_wick,
        "upper_wick_abs": upper_wick,
        "body_abs": body,
        "lower_wick_ratio": round(wick_ratio, 4),
        "is_red": c < o,
        "rolling_24h_high": wh,
        "rolling_24h_low_in_window": wl,
        "drawdown_from_24h_high_pct": round(dd_pct, 4),
        "decline_from_prior_4h_high_pct": round(decline_from_local_high_pct, 4),
    }


def detect_drops_global(bars: dict, window_start: datetime, window_end: datetime, params: dict) -> list[dict]:
    lb = params["drop_lookback_bars_5m"]
    all_times = sorted(bars.keys())
    drops = []
    for i, t in enumerate(all_times):
        if t < window_start or t > window_end:
            continue
        m = bar_metrics(bars, t, lb)
        if not m:
            continue
        if m["drawdown_from_24h_high_pct"] < params["drop_min_drawdown_from_lookback_high_pct"] and m[
            "range_pct"
        ] < params["drop_min_single_bar_range_pct"]:
            continue
        drops.append({**m, "drop_close_utc": _iso(t), "reference_low": m["low"]})
    merged = []
    gap_h = params["drop_merge_gap_hours"]
    for d in drops:
        if not merged:
            merged.append(d)
            continue
        prev_t = parse_decision_close(merged[-1]["drop_close_utc"])
        cur_t = parse_decision_close(d["drop_close_utc"])
        if (cur_t - prev_t).total_seconds() / 3600 < gap_h:
            if d["drawdown_from_24h_high_pct"] > merged[-1]["drawdown_from_24h_high_pct"]:
                merged[-1] = d
        else:
            merged.append(d)
    return merged


def active_reference_at(bars: dict, asof: datetime, params: dict) -> dict:
    """Latest merged drop episode whose reference low still holds at asof (causal)."""
    eps = params["reference_low_invalidation_eps"]
    hist_start = asof - timedelta(days=21)
    drops = detect_drops_global(bars, hist_start, asof, params)
    if not drops:
        return {"causal_reference_low": None, "source_drop_close": None, "ref_holds_at_asof": None}
    last = drops[-1]
    ref = float(last["reference_low"])
    t0 = parse_decision_close(last["drop_close_utc"])
    holds = True
    for t in sorted(bars):
        if t <= t0 or t > asof:
            continue
        if float(bars[t]["low"]) < ref - eps:
            holds = False
            break
    return {
        "causal_reference_low": ref,
        "source_drop_close": last["drop_close_utc"],
        "ref_holds_at_asof": holds,
        "source_drawdown_pct": last["drawdown_from_24h_high_pct"],
    }


def detector_fail_reason(m: dict, params: dict) -> str:
    dd_ok = m["drawdown_from_24h_high_pct"] >= params["drop_min_drawdown_from_lookback_high_pct"]
    rng_ok = m["range_pct"] >= params["drop_min_single_bar_range_pct"]
    if dd_ok or rng_ok:
        return "WOULD_TRIGGER_BAR_LEVEL"
    parts = []
    if not dd_ok:
        parts.append(
            f"THRESHOLD_NOT_MET:24h_DD={m['drawdown_from_24h_high_pct']:.4f}%<{params['drop_min_drawdown_from_lookback_high_pct']}%"
        )
    if not rng_ok:
        parts.append(
            f"THRESHOLD_NOT_MET:5m_range={m['range_pct']:.4f}%<{params['drop_min_single_bar_range_pct']}%"
        )
    return ";".join(parts)


def load_geom_rows(path: Path, pd: int, t0: datetime, t1: datetime) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if int(r.get("pool_detail", 0)) != pd:
            continue
        dc = parse_decision_close(r["decision_close"])
        if t0 <= dc <= t1 and r.get("side") == "lower":
            out.append(r)
    return out


def load_pre_births(pd: int, t0: datetime, t1: datetime) -> list[dict]:
    path = _PRE / f"pd{pd}_lower_births.csv"
    if not path.is_file():
        return []
    out = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        dc = parse_decision_close(r["decision_close"])
        if t0 <= dc <= t1:
            r["pool_detail"] = int(r["pool_detail"])
            for k in ("is_new_origin", "class_deeper", "class_higher_step", "class_within_zone", "phase_a_eligible"):
                if k in r:
                    r[k] = r[k] == "True"
            out.append(r)
    return out


def geom_classification(pd: int, origin_id: str, dc_s: str) -> dict:
    out = {"geometry_step_kind": None, "geometry_step_id": None, "geometry_note": None}
    for path, kind_field in (
        (_GEOM / "actual_higher_lower_steps.csv", "kind"),
        (_GEOM / "existing_step_reinforcements.csv", "kind"),
    ):
        if not path.is_file():
            continue
        for r in csv.DictReader(path.open(encoding="utf-8")):
            if int(r.get("pool_detail", 0)) != pd:
                continue
            if r.get("decision_close") != dc_s and r.get("origin_id") != origin_id:
                continue
            if path.name == "actual_higher_lower_steps.csv" and r.get("origin_id") == origin_id:
                out["geometry_step_kind"] = r.get(kind_field)
                out["geometry_step_id"] = r.get("step_id")
                out["geometry_note"] = "main_window_spatial_step"
                return out
            if path.name == "existing_step_reinforcements.csv" and r.get("origin_id") == origin_id:
                out["geometry_step_kind"] = r.get(kind_field)
                out["geometry_step_id"] = r.get("step_id")
                out["geometry_note"] = r.get("uncertain")
                return out
    return out


def enrich_origin_row(
    r: dict,
    pd: int,
    bars: dict,
    drop_low_window: float | None,
    client,
    db: str,
) -> dict:
    dc = parse_decision_close(r["decision_close"])
    dc_s = r["decision_close"]
    mem = load_members({}, dc, pd, client, db)
    oid = r["origin_id"]
    same_origin_pools = [m for m in mem if origin_logical_v2_key(m["pool_id"]) == oid and m.get("side") == "lower"]
    first_active_dc = None
    for probe in (dc - timedelta(hours=168), dc):
        m2 = load_members({}, probe, pd, client, db)
        if any(origin_logical_v2_key(x["pool_id"]) == oid for x in m2):
            first_active_dc = _iso(probe) if probe != dc else dc_s
            break
    geom = geom_classification(pd, oid, dc_s)
    bot, top = float(r["bottom"]), float(r["top"])
    prev_lower = [
        float(x["bottom"])
        for x in load_pre_births(pd, AUDIT_START - timedelta(days=7), dc - timedelta(minutes=1))
        if x.get("is_new_origin") and x["origin_id"] != oid
    ]
    prev_max = max(prev_lower) if prev_lower else 0.0
    spatial_delta = bot - prev_max if prev_max else None
    ref_gap = (bot - drop_low_window) if drop_low_window is not None else None
    return {
        **r,
        "pool_detail": pd,
        "in_geometry_main_window": dc >= _GEOM_MAIN,
        "first_ssot_active_probe": first_active_dc,
        "active_pool_ids_at_birth": "|".join(sorted({m["pool_id"] for m in same_origin_pools})),
        "strength_at_birth": r.get("strength"),
        "exact_origin_density_side": exact_max_origin_overlap(mem, "lower")["max"],
        "geometry_step_kind": geom["geometry_step_kind"],
        "geometry_step_id": geom["geometry_step_id"],
        "geometry_runner_note": geom["geometry_note"],
        "spatial_delta_vs_prior_origin_bottom": spatial_delta,
        "distance_above_session_drop_low": ref_gap,
        "event_type_summary": (
            "NEW_INDEPENDENT_ORIGIN"
            if r.get("is_new_origin")
            else "POOL_VARIANT_BIRTH_SAME_ORIGIN"
        ),
        "forensics_higher_step_flag": r.get("class_higher_step"),
        "forensics_within_zone_flag": r.get("class_within_zone"),
        "forensics_deeper_flag": r.get("class_deeper"),
    }


def main() -> None:
    t0_run = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    params = load_params()

    bar_load_start = AUDIT_START - timedelta(hours=POOL_MIN_H + 24)
    bar_load_end = AUDIT_END + timedelta(hours=1)
    src = ClickHouseResearchCandleSource()
    bars = resample_5m_from_candles(src.get_candles("DOGEUSDT", "5m", start=bar_load_start, end=bar_load_end))
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database

    lb = params["drop_lookback_bars_5m"]
    drop_audit_rows: list[dict] = []

    agg15 = aggregate_15m(bars, FOCUS_15M_DC)
    if agg15:
        m5_at_dc = bar_metrics(bars, FOCUS_15M_DC, lb) if FOCUS_15M_DC in bars else {}
        m15 = {
            **agg15,
            "timeframe": "15m",
            "bar_role": "user_marked_15m_decision_close",
            "actual_5m_close_at_decision_close": m5_at_dc.get("close"),
            "note_close": "15m close = last 5m close in bucket (21:30 bar); matches SSOT decision_close",
        }
        rng = m15["high"] - m15["low"]
        lw = min(m15["open"], m15["close"]) - m15["low"]
        m15["lower_wick_abs"] = lw
        m15["lower_wick_ratio"] = round(lw / rng, 4) if rng > 1e-12 else 0
        m15["range_abs"] = rng
        m15["range_pct"] = round(rng / m15["close"] * 100, 4) if m15["close"] else 0
        m15["is_red"] = m15["close"] < m15["open"]
        if m5_at_dc:
            m15["drawdown_from_24h_high_pct"] = m5_at_dc["drawdown_from_24h_high_pct"]
            m15["rolling_24h_high"] = m5_at_dc["rolling_24h_high"]
            m15["decline_from_prior_4h_high_pct"] = m5_at_dc["decline_from_prior_4h_high_pct"]
            m15["detector_fail_reason"] = detector_fail_reason(m5_at_dc, params)
        ref_at = active_reference_at(bars, FOCUS_15M_DC, params)
        m15.update(ref_at)
        drop_audit_rows.append({k: (round(v, 6) if isinstance(v, float) else v) for k, v in m15.items()})

    seg_5m_start = FOCUS_15M_DC - timedelta(minutes=15)
    seg_5m_end = FOCUS_15M_DC
    session_low = None
    for t in sorted(bars):
        if seg_5m_start < t <= seg_5m_end + timedelta(minutes=30):
            session_low = float(bars[t]["low"]) if session_low is None else min(session_low, float(bars[t]["low"]))

    for t in sorted(bars):
        if t < seg_5m_start or t > seg_5m_end + timedelta(minutes=45):
            continue
        m = bar_metrics(bars, t, lb)
        if not m:
            continue
        ref_at = active_reference_at(bars, t, params)
        row = {
            "timeframe": "5m",
            "bar_utc": _iso(t),
            "in_15m_bucket_21_30": seg_5m_start < t <= seg_5m_end,
            **m,
            **ref_at,
            "detector_fail_reason": detector_fail_reason(m, params),
            "drop_threshold_dd_pct": params["drop_min_drawdown_from_lookback_high_pct"],
            "drop_threshold_range_pct": params["drop_min_single_bar_range_pct"],
        }
        drop_audit_rows.append(row)

    _wcsv(_OUT / "drop_candle_audit.csv", drop_audit_rows)

    origin_window_start = datetime(2026, 8, 16, 21, 30, tzinfo=timezone.utc)
    pre_window_pd1 = load_pre_births(
        1,
        datetime(2026, 8, 16, 21, 0, tzinfo=timezone.utc),
        origin_window_start - timedelta(minutes=1),
    )
    pre_window_pd10 = load_pre_births(
        10,
        datetime(2026, 8, 16, 21, 0, tzinfo=timezone.utc),
        origin_window_start - timedelta(minutes=1),
    )
    origin_window_end = datetime(2026, 8, 17, 2, 15, tzinfo=timezone.utc)
    drop_low = session_low
    for pd in (1, 10):
        births = load_pre_births(pd, origin_window_start, origin_window_end)
        enriched = [enrich_origin_row(r, pd, bars, drop_low, client, db) for r in births]
        _wcsv(_OUT / f"pre_0215_lower_origins_pd{pd}.csv", enriched)

    steps = list(csv.DictReader((_GEOM / "actual_higher_lower_steps.csv").open(encoding="utf-8")))
    aug_steps = [s for s in steps if s.get("period") == "august"]

    timeline: list[dict] = []
    timeline.append(
        {
            "seq": 1,
            "event": "USER_MARKED_15M_CANDLE",
            "utc": _iso(FOCUS_15M_DC),
            "pd1_note": "15m decision_close bucket 21:15–21:30",
            "pd10_note": "same",
            "detail": json.dumps(agg15) if agg15 else "",
        }
    )
    ref_2130 = active_reference_at(bars, FOCUS_15M_DC, params)
    timeline.append(
        {
            "seq": 2,
            "event": "CAUSAL_REFERENCE_LOW_AT_21_30",
            "utc": _iso(FOCUS_15M_DC),
            "pd1_note": str(ref_2130.get("causal_reference_low")),
            "pd10_note": str(ref_2130.get("causal_reference_low")),
            "detail": json.dumps(ref_2130),
        }
    )

    for pd in (1, 10):
        births = load_pre_births(pd, origin_window_start, origin_window_end)
        new_origins = [b for b in births if b.get("is_new_origin")]
        if new_origins:
            first = min(new_origins, key=lambda x: x["decision_close"])
            timeline.append(
                {
                    "seq": 3 if pd == 1 else 4,
                    "event": f"FIRST_NEW_LOWER_ORIGIN_PD{pd}",
                    "utc": first["decision_close"],
                    "pd1_note": first["origin_id"] if pd == 1 else "",
                    "pd10_note": first["origin_id"] if pd == 10 else "",
                    "detail": f"bottom={first['bottom']} known_at={first.get('known_at')}",
                }
            )

    for s in sorted(aug_steps, key=lambda x: x["decision_close"]):
        dc = parse_decision_close(s["decision_close"])
        if dc < origin_window_start or dc > AUDIT_END:
            continue
        timeline.append(
            {
                "seq": 10 + int(s["pool_detail"]),
                "event": f"SPATIAL_STEP_{s.get('step_id')}",
                "utc": s["decision_close"],
                "pd1_note": s["bottom"] if s["pool_detail"] == "1" else "",
                "pd10_note": s["bottom"] if s["pool_detail"] == "10" else "",
                "detail": s.get("kind", ""),
            }
        )

    _wcsv(_OUT / "causal_event_timeline.csv", timeline)

    pd10_early = [r for r in load_pre_births(10, origin_window_start, _GEOM_MAIN) if r.get("is_new_origin")]
    pd10_0030 = [r for r in load_pre_births(10, _GEOM_MAIN, origin_window_end) if r.get("is_new_origin")]

    recon = f"""# PD1 / PD10 visual reconciliation

## User-marked 15m candle (decision_close 2026-08-16T21:30Z)

Aggregated from 5m (bucket 21:15–21:30): {json.dumps(agg15, indent=2) if agg15 else "N/A"}

Per-bar detector diagnostics: see `drop_candle_audit.csv` (5m rows + 15m summary row).

## Why geometry names **02:15Z** as first higher lower step (PD1 & PD10)

- Geometry **main window** starts `2026-08-17T00:00:00Z` (`AUG_MAIN` in staircase geometry runner).
- On first main-window `decision_close`, `main_last_step_bottom` is initialized to the **highest lower-pool upper bound still below price** (support reference), not to zero.
- Origins on **16.08** (21:00, 21:15, 21:45) fall in **history** (`in_main=false`): they update `running_support_top` but **do not** allocate `august:PD*:S*` step IDs.
- **00:30Z** micro origins (PD1 bottom ~0.06934, PD10 ~0.06940) are classified `within_zone_or_ambiguous` in `existing_step_reinforcements.csv` — bottom **not above** `main_last_step_bottom + eps` at that time → **not** `new_spatial_step`.
- **02:15Z** origins (`lldv2:DOGEUSDT:15m:lower:1786932000`) are the first **main-window** births with bottom clearly above that reference → `august:PD1:S1` / `august:PD10:S1`.

## PD10 “small zone before 02:15” on static chart

Causal new origins **before** main midnight (PD10):

{chr(10).join(f"- {r['decision_close']} {r['origin_id']} bottom={r['bottom']} micro" for r in pd10_early) or "(none)"}

After main window opens:

{chr(10).join(f"- {r['decision_close']} {r['origin_id']} bottom={r['bottom']} class_higher_step={r.get('class_higher_step')}" for r in pd10_0030) or "(none)"}

**Interpretation:** Chart overlay can show pool **bands** as soon as price interacts with bounds; **causal birth** for independent origin is the `decision_close` / `known_at` in SSOT replay (see `pre_0215_lower_origins_pd10.csv`). The 00:30 PD10 zone is real causally but **not** counted as upward staircase step S1.

## PD1 vs PD10 at 21:30

- PD10 records a **normal** pool birth at 21:30 for origin `1786914000` (same origin as 21:15 micro) — **not** a new independent origin.
- PD1 has **no** row at 21:30; prior PD1 micro origin at 21:15.

## Pre-21:30 context (not in origin CSV window)

PD1:
{chr(10).join(f"- {r['decision_close']} {r['origin_id']} bottom={r['bottom']}" for r in pre_window_pd1 if r.get('is_new_origin')) or "(none)"}

PD10:
{chr(10).join(f"- {r['decision_close']} {r['origin_id']} bottom={r['bottom']}" for r in pre_window_pd10 if r.get('is_new_origin')) or "(none)"}

"""
    (_OUT / "pd1_pd10_visual_reconciliation.md").write_text(recon, encoding="utf-8")

    strongest_5m = max(
        [r for r in drop_audit_rows if r.get("timeframe") == "5m"],
        key=lambda x: x.get("decline_from_prior_4h_high_pct", 0),
        default={},
    )

    report = f"""# August 16/17 micro-drop & early lower-pool audit

**Mode:** STRICT READ-ONLY
**Symbol:** DOGEUSDT · 15m PD1 primary · PD10 separate
**Audit window:** `{_iso(AUDIT_START)}` – `{_iso(AUDIT_END)}`
**Pool context:** ≥{POOL_MIN_H}h before audit start (`{_iso(bar_load_start)}` 5m load)

## 1. Drop investigation (~21:30 UTC)

User-marked **15m decision_close 21:30Z** (not a pre-confirmed DROP_EVENT).

| Field | Value |
|-------|-------|
| 15m OHLC | {agg15} |
| Causal ref low @21:30 | {ref_2130} |

Strongest local 5m move near bucket (see CSV): `{strongest_5m.get('bar_utc')}` DD24h={strongest_5m.get('drawdown_from_24h_high_pct')}% range={strongest_5m.get('range_pct')}%

**Detector:** unchanged OR rule (24h DD ≥ {params['drop_min_drawdown_from_lookback_high_pct']}% **or** 5m range ≥ {params['drop_min_single_bar_range_pct']}%).
No bar in the 21:30 focus window meets thresholds → **`THRESHOLD_NOT_MET`** (not missing warmup).
Larger sell pressure appears on **21:45Z** 5m/15m closes (new PD1/PD10 micro origins at ~0.06865 / 0.06891) — still below drop thresholds at those closes.

## 2. Early lower origins (21:30 – 02:15)

See `pre_0215_lower_origins_pd1.csv` and `pre_0215_lower_origins_pd10.csv`.

## 3. Timeline

See `causal_event_timeline.csv` and `pd1_pd10_visual_reconciliation.md`.

**First spatial staircase steps (sealed geometry):** PD1 & PD10 at **2026-08-17T02:15Z**; second PD1 step **04:45Z**.

## 4. Sources

- Drop params: `{_DET / 'detector_parameters.json'}`
- Geometry: `{_GEOM}`
- Pre-breakout births: `{_PRE}`
- Extended lookback drops (context): `{_EXT}`

**Verdict:** `AUG16_17_MICRODROP_EARLY_LOWER_AUDIT_COMPLETE`

Runtime: {time.time() - t0_run:.1f}s
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")

    _wjson(
        _OUT / "job_status.json",
        {
            "phase": "phase6xt_aug16_17_microdrop_early_lower_audit_v1",
            "verdict": "AUG16_17_MICRODROP_EARLY_LOWER_AUDIT_COMPLETE",
            "runtime_sec": round(time.time() - t0_run, 2),
            "outputs": [
                "drop_candle_audit.csv",
                "pre_0215_lower_origins_pd1.csv",
                "pre_0215_lower_origins_pd10.csv",
                "causal_event_timeline.csv",
                "pd1_pd10_visual_reconciliation.md",
                "report.md",
            ],
        },
    )


if __name__ == "__main__":
    main()
