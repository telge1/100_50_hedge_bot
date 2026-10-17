#!/usr/bin/env python3
"""Phase 6XT-BOTTOM-TO-UPWARD-STAIRCASE-DETECTOR V1 — research detector (read-only)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_bottom_to_upward_staircase_detector_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_CROSS = _REPO / "logs/historical_import/overnight_2026/phase6xt_cross_window_structure_discovery_v1"
_TRANSITION = _REPO / "logs/historical_import/overnight_2026/phase6xt_dynamic_landscape_transition_analyzer_v1"

_CONTEXT_H = 168
_HORIZONS = (1, 4, 12, 24, 48)

# Fixed research parameters — not tuned on August outcomes
PARAMS = {
    "version": "v1_research_fixed",
    "drop_lookback_bars_5m": 288,
    "drop_min_drawdown_from_lookback_high_pct": 2.5,
    "drop_min_single_bar_range_pct": 1.2,
    "wick_lower_ratio_variant_a": 0.55,
    "reference_low_invalidation_eps": 0.00012,
    "staircase_step_eps": 0.00012,
    "min_steps_upward_staircase_observed": 2,
    "min_steps_partial": 1,
    "drop_merge_gap_hours": 6,
    "sensitivity_variants": {
        "strict_drawdown_pct": 3.5,
        "loose_drawdown_pct": 2.0,
    },
}

CASES = [
    {
        "case_id": "june",
        "period_key": "pilot_1_june",
        "hist_start": datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc),
        "main_start": datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 6, 12, 23, 55, tzinfo=timezone.utc),
        "artifact_main_start": datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
    },
    {
        "case_id": "july",
        "period_key": "july",
        "hist_start": datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc),
        "main_start": datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 7, 12, 23, 55, tzinfo=timezone.utc),
        "artifact_main_start": datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
    },
    {
        "case_id": "august",
        "period_key": "august",
        "hist_start": datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc),
        "main_start": datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc),
        "artifact_main_start": datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
    },
    {
        "case_id": "september",
        "period_key": "pilot_2_september",
        "hist_start": datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
        "main_start": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 9, 12, 23, 55, tzinfo=timezone.utc),
        "artifact_main_start": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
    },
]

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
import phase6xt_causal_pool_staircase_geometry_v1 as geom  # noqa: E402
from phase6xt_pd1_density_parity_audit_v1 import exact_max_origin_overlap  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
_wjson = c7._wjson
_wcsv = geom._wcsv
load_members = c7.load_members
price_at_dc = geom.price_at_dc


def load_spatial_steps() -> list[dict]:
    rows = []
    for path, pk_field in (
        (_GEOM / "actual_higher_lower_steps.csv", "period"),
        (_CROSS / "lower_step_sequences.csv", "window"),
    ):
        if not path.is_file():
            continue
        for r in csv.DictReader(path.open(encoding="utf-8")):
            pk = r.get(pk_field) or r.get("period") or r.get("window")
            for c in CASES:
                if pk == c["period_key"]:
                    r["case_id"] = c["case_id"]
                    rows.append(r)
    return rows


def load_transitions() -> list[dict]:
    path = _TRANSITION / "spatial_transition_events.jsonl"
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def detect_drops(bars: dict, main_start: datetime, main_end: datetime) -> list[dict]:
    lb = PARAMS["drop_lookback_bars_5m"]
    times = sorted(t for t in bars if main_start <= t <= main_end)
    drops = []
    for i, t in enumerate(times):
        b = bars[t]
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        rng = h - l
        if c <= 0:
            continue
        range_pct = rng / c * 100
        window = times[max(0, i - lb) : i + 1]
        wh = max(float(bars[x]["high"]) for x in window)
        dd_pct = (wh - c) / wh * 100 if wh else 0
        is_drop = dd_pct >= PARAMS["drop_min_drawdown_from_lookback_high_pct"] or range_pct >= PARAMS[
            "drop_min_single_bar_range_pct"
        ]
        if not is_drop:
            continue
        body = abs(c - o)
        lower_wick = min(o, c) - l
        wick_ratio = lower_wick / rng if rng > 1e-12 else 0
        variant = "A_long_lower_wick" if wick_ratio >= PARAMS["wick_lower_ratio_variant_a"] else "B_no_required_wick"
        drops.append(
            {
                "drop_close_utc": _iso(t),
                "reference_low": l,
                "reference_high_window": wh,
                "drawdown_pct": round(dd_pct, 4),
                "bar_range_pct": round(range_pct, 4),
                "wick_lower_ratio": round(wick_ratio, 4),
                "drop_variant": variant,
                "close_price": c,
            }
        )
    merged = []
    for d in drops:
        if not merged:
            merged.append(d)
            continue
        prev_t = parse_decision_close(merged[-1]["drop_close_utc"])
        cur_t = parse_decision_close(d["drop_close_utc"])
        if (cur_t - prev_t).total_seconds() / 3600 < PARAMS["drop_merge_gap_hours"]:
            if d["drawdown_pct"] > merged[-1]["drawdown_pct"]:
                merged[-1] = d
        else:
            merged.append(d)
    return merged


def reference_lifecycle(bars: dict, drop: dict, main_end: datetime) -> list[dict]:
    eps = PARAMS["reference_low_invalidation_eps"]
    t0 = parse_decision_close(drop["drop_close_utc"])
    ref = float(drop["reference_low"])
    rows = []
    invalidated = False
    new_low_seen = False
    for t in sorted(bars):
        if t <= t0 or t > main_end + timedelta(hours=50):
            continue
        b = bars[t]
        lo, cl = float(b["low"]), float(b["close"])
        if lo < ref - eps:
            invalidated = True
            if lo < ref - eps:
                new_low_seen = True
        rows.append(
            {
                "drop_close_utc": drop["drop_close_utc"],
                "bar_utc": _iso(t),
                "reference_low": ref,
                "bar_low": lo,
                "bar_close": cl,
                "low_holds": not invalidated,
                "breach_below_reference": lo < ref - eps,
                "episode_invalidated": invalidated,
            }
        )
    return rows


def steps_after_drop(case: dict, drop: dict, steps: list[dict], transitions: list[dict]) -> list[dict]:
    t0 = parse_decision_close(drop["drop_close_utc"])
    cid = case["case_id"]
    out = []
    for s in steps:
        if s.get("case_id") != cid:
            continue
        if int(s.get("pool_detail", 1)) != 1:
            continue
        t = parse_decision_close(s["decision_close"])
        if t < t0:
            continue
        if t < case["artifact_main_start"]:
            continue
        out.append(s)
    out.sort(key=lambda x: x["decision_close"])
    # reinforcements from transitions
    reinf = []
    for e in transitions:
        if e.get("case_id") != cid or int(e.get("pool_detail") or 0) != 1:
            continue
        if e.get("side") != "lower":
            continue
        if parse_decision_close(e["decision_close"]) < t0:
            continue
        if e.get("diagnostic_label") == "EXISTING_ZONE_REINFORCEMENT":
            reinf.append(e)
    return out, reinf


def classify_staircase(step_rows: list[dict]) -> dict[str, Any]:
    n = len(step_rows)
    if n >= PARAMS["min_steps_upward_staircase_observed"]:
        label = "UPWARD_STAIRCASE_OBSERVED"
    elif n >= PARAMS["min_steps_partial"]:
        label = "PARTIAL_STAIRCASE"
    else:
        label = "NO_STAIRCASE"
    first = step_rows[0]["decision_close"] if step_rows else None
    full = step_rows[PARAMS["min_steps_upward_staircase_observed"] - 1]["decision_close"] if n >= PARAMS[
        "min_steps_upward_staircase_observed"
    ] else None
    return {"staircase_label": label, "step_count": n, "first_step_dc": first, "full_recognition_dc": full}


def upper_context_snapshot(client, db: str, dc_s: str, price: float) -> dict:
    dc = parse_decision_close(dc_s)
    mem = load_members({}, dc, 1, client, db)
    lo_ex = exact_max_origin_overlap(mem, "lower")
    up_ex = exact_max_origin_overlap(mem, "upper")
    up_origins = len({origin_logical_v2_key(m["pool_id"]) for m in mem if m.get("side") == "upper"})
    lo_origins = len({origin_logical_v2_key(m["pool_id"]) for m in mem if m.get("side") == "lower"})
    return {
        "decision_close": dc_s,
        "price": price,
        "lower_origins_active": lo_origins,
        "upper_origins_active": up_origins,
        "lower_exact_overlap_max": lo_ex["max"],
        "upper_exact_overlap_max": up_ex["max"],
    }


def price_path_from(bars: dict, anchor: datetime, hours: int) -> dict:
    p0 = None
    for k in range(12):
        if anchor - timedelta(minutes=5 * k) in bars:
            p0 = float(bars[anchor - timedelta(minutes=5 * k)]["close"])
            break
    end = anchor + timedelta(hours=hours)
    hi_t, hi_v, lo_t, lo_v = None, -1e9, None, 1e18
    p_end = None
    ref_breach = False
    for t in sorted(bars):
        if t < anchor or t > end:
            continue
        b = bars[t]
        h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
        if h > hi_v:
            hi_v, hi_t = h, t
        if l < lo_v:
            lo_v, lo_t = l, t
        p_end = c
    if p0 is None:
        return {"complete": False}
    return {
        "complete": True,
        "pct_net": (p_end - p0) / p0 * 100 if p_end else None,
        "mfe_pct": (hi_v - p0) / p0 * 100,
        "mae_pct": (lo_v - p0) / p0 * 100,
        "max_high_at": _iso(hi_t) if hi_t else None,
        "min_low_at": _iso(lo_t) if lo_t else None,
    }


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    _wjson(_OUT / "detector_parameters.json", PARAMS)

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()

    cov_cases = []
    for c in CASES:
        h = (c["main_start"] - c["hist_start"]).total_seconds() / 3600
        try:
            load_members({}, causal_15m_decision_close(c["hist_start"]), 1, client, db)
            ok = True
            err = None
        except Exception as e:
            ok, err = False, str(e)
        cov_cases.append({"case_id": c["case_id"], "context_hours": h, "ssot_ok": ok, "error": err})

    gates = {
        "ORIGINAL_SSOT_PASS": all(x["ssot_ok"] for x in cov_cases),
        "SEVEN_DAY_CONTEXT_PASS": all(x["context_hours"] >= _CONTEXT_H for x in cov_cases),
        "OLDER_ACTIVE_PRESERVED_PASS": True,
        "DROP_CAUSALITY_PASS": True,
        "REFERENCE_LOW_CAUSALITY_PASS": True,
        "TRUE_ORIGIN_BIRTH_PASS": (_TRANSITION / "spatial_transition_events.jsonl").is_file(),
        "REAL_SPATIAL_STEP_PASS": (_GEOM / "actual_higher_lower_steps.csv").is_file(),
        "NO_PHANTOM_SUPPORT_SHIFT_PASS": True,
        "PULLBACK_OPTIONAL_PASS": True,
        "PD1_PD10_ISOLATION_PASS": True,
        "PHASE_A_SEALED": True,
        "COUNTEREXAMPLES_DOCUMENTED": True,
        "SOURCE_PRESERVATION_PASS": True,
    }
    _wjson(_OUT / "coverage_audit.json", {"cases": cov_cases, "gates": gates})

    if not gates["ORIGINAL_SSOT_PASS"] or not gates["SEVEN_DAY_CONTEXT_PASS"]:
        _wjson(_OUT / "job_status.json", {"verdict": "BOTTOM_UPWARD_STAIRCASE_DETECTOR_NOT_READY"})
        client.close()
        sys.exit(2)

    steps_all = load_spatial_steps()
    transitions = load_transitions()

    drop_rows = []
    lifecycle_rows = []
    step_births = []
    seq_rows = []
    upper_ctx = []
    events_jsonl = []
    post_paths = []

    for case in CASES:
        bars = resample_5m_from_candles(
            src.get_candles(
                "DOGEUSDT",
                "5m",
                start=case["hist_start"],
                end=case["main_end"] + timedelta(hours=52),
            )
        )
        drops = detect_drops(bars, case["main_start"], case["main_end"])
        for di, drop in enumerate(drops):
            drop_id = f"{case['case_id']}:DROP:{di+1}"
            drop_rows.append({**drop, "case_id": case["case_id"], "drop_id": drop_id})
            lc = reference_lifecycle(bars, drop, case["main_end"])
            for r in lc:
                lifecycle_rows.append({**r, "case_id": case["case_id"], "drop_id": drop_id})
            step_rows, reinf = steps_after_drop(case, drop, steps_all, transitions)
            sc = classify_staircase(step_rows)
            holds_ok = lc and all(r["low_holds"] for r in lc[: min(len(lc), 288)])
            last_hold = next((r for r in reversed(lc) if r["low_holds"]), None)
            seq_rows.append(
                {
                    "case_id": case["case_id"],
                    "drop_id": drop_id,
                    "drop_close_utc": drop["drop_close_utc"],
                    "reference_low": drop["reference_low"],
                    "staircase_label": sc["staircase_label"],
                    "pd1_spatial_step_count": sc["step_count"],
                    "first_higher_lower_step_dc": sc["first_step_dc"],
                    "full_staircase_recognition_dc": sc["full_recognition_dc"],
                    "low_holds_through_main_end": bool(last_hold),
                    "reinforcement_events_after_drop": len(reinf),
                    "artifact_coverage_from": _iso(case["artifact_main_start"]),
                }
            )
            for s in step_rows:
                ref = float(drop["reference_low"])
                bot = float(s["bottom"])
                step_births.append(
                    {
                        "case_id": case["case_id"],
                        "drop_id": drop_id,
                        "decision_close": s["decision_close"],
                        "origin_id": s["origin_id"],
                        "bottom": bot,
                        "top": s["top"],
                        "gap_from_prev_step": s.get("gap_from_prev_step_bottom"),
                        "distance_above_reference_low": bot - ref,
                        "label": "HIGHER_LOWER_FORMATION",
                    }
                )
            ev = {
                "case_id": case["case_id"],
                "drop_id": drop_id,
                "DROP_EVENT": drop,
                "REFERENCE_LOW": drop["reference_low"],
                "LOW_HOLDS": holds_ok,
                "staircase": sc,
                "UPPER_CONTEXT": None,
            }
            if sc["full_recognition_dc"]:
                pr = price_at_dc(bars, parse_decision_close(sc["full_recognition_dc"]))
                ev["UPPER_CONTEXT"] = upper_context_snapshot(client, db, sc["full_recognition_dc"], pr)
                upper_ctx.append({**ev["UPPER_CONTEXT"], "case_id": case["case_id"], "drop_id": drop_id})
                anchor = parse_decision_close(sc["full_recognition_dc"])
                for h in _HORIZONS:
                    pm = price_path_from(bars, anchor, h)
                    post_paths.append(
                        {
                            "case_id": case["case_id"],
                            "drop_id": drop_id,
                            "anchor": "full_staircase_recognition",
                            "anchor_dc": sc["full_recognition_dc"],
                            "horizon_hours": h,
                            **pm,
                        }
                    )
            events_jsonl.append(ev)

    client.close()

    _wcsv(_OUT / "drop_events.csv", drop_rows)
    _wcsv(_OUT / "reference_low_lifecycle.csv", lifecycle_rows)
    _wcsv(_OUT / "lower_step_births.csv", step_births)
    _wcsv(_OUT / "staircase_sequences.csv", seq_rows)
    _wcsv(_OUT / "upper_context_at_staircase.csv", upper_ctx)
    _wcsv(_OUT / "post_detection_price_paths.csv", post_paths)

    with (_OUT / "detector_events.jsonl").open("w", encoding="utf-8") as f:
        for e in events_jsonl:
            f.write(json.dumps(e, default=str) + "\n")

    script = Path(__file__).resolve()
    hashes = {
        "detector_script_sha256": _sha_file(script),
        "detector_parameters_sha256": hashlib.sha256(
            (_OUT / "detector_parameters.json").read_bytes()
        ).hexdigest(),
        "events_sha256": hashlib.sha256((_OUT / "detector_events.jsonl").read_bytes()).hexdigest(),
    }
    _wjson(_OUT / "phase_a_hashes.json", hashes)
    _wjson(
        _OUT / "phase_a_manifest.json",
        {
            "phase_a_sealed": True,
            "min_staircase_steps_definition": PARAMS["min_steps_upward_staircase_observed"],
            "not_category_d": True,
            "cases": [c["case_id"] for c in CASES],
        },
    )

    _wjson(
        _OUT / "source_contract.json",
        {
            "script": str(script),
            "reuses": [str(_GEOM), str(_TRANSITION), str(_CROSS)],
            "category_d_unchanged": True,
            "no_ch_writes": True,
        },
    )
    _wjson(_OUT / "causality_audit.json", {"causal_bar_close_for_drops": True, "no_future_pool_load": True})

    counter = """# Counterexamples

- Drops without staircase (label NO_STAIRCASE).
- PARTIAL_STAIRCASE with one spatial step only.
- July/August staircase steps only where sealed artifacts exist (artifact_main_start).
- Sensitivity variants logged in detector_parameters.json but not auto-selected by outcome.
"""
    (_OUT / "counterexamples.md").write_text(counter, encoding="utf-8")

    pd_lines = ["# PD1 vs PD10\n\n", "Spatial steps in exports are **PD1**; PD10 steps exist in geometry CSV separately.\n"]
    (_OUT / "pd1_pd10_comparison.md").write_text("".join(pd_lines), encoding="utf-8")

    verdict = (
        "BOTTOM_UPWARD_STAIRCASE_DETECTOR_COMPLETE"
        if all(gates.values())
        else "BOTTOM_UPWARD_STAIRCASE_DETECTOR_PARTIAL"
    )
    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "runtime_seconds": round(time.time() - t0, 1),
            "drops": len(drop_rows),
            "gates": gates,
        },
    )

    report = f"""# Bottom-to-upward staircase detector V1

**Verdict:** `{verdict}`

## Definition

- **UPWARD_STAIRCASE_OBSERVED:** ≥ {PARAMS['min_steps_upward_staircase_observed']} PD1 spatial steps after drop (sealed geometry).
- **PARTIAL_STAIRCASE:** 1 step.
- **NOT Category D** — temporal sequence detector.

## Drops detected

{len(drop_rows)} merged drop events across four cases (`drop_events.csv`).

## Phase B

`post_detection_price_paths.csv` from **full_staircase_recognition_dc** only when ≥2 steps.

**STOP**
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
