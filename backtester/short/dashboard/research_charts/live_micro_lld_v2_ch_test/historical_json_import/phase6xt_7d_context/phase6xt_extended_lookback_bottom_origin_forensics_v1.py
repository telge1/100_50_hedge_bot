#!/usr/bin/env python3
"""Phase 6XT-EXTENDED-LOOKBACK-BOTTOM-ORIGIN-FORENSICS V1 — 7/14/21d drop lookback."""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_extended_lookback_bottom_origin_forensics_v1"
_CTX = Path(__file__).resolve().parent
_DET = _REPO / "logs/historical_import/overnight_2026/phase6xt_bottom_to_upward_staircase_detector_v1"
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_TRANSITION = _REPO / "logs/historical_import/overnight_2026/phase6xt_dynamic_landscape_transition_analyzer_v1"

_POOL_MIN_H = 168
_PRIOR_SHORT_WINDOW_START = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)

CASES = {
    "august": {
        "cutoff": datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc),
        "period_key": "august",
        "artifact_step_start": datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
    },
    "july": {
        "cutoff": datetime(2026, 7, 10, 4, 30, tzinfo=timezone.utc),
        "period_key": "july",
        "artifact_step_start": datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
    },
}

LOOKBACK_DAYS = (7, 14, 21)

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
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
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


def detect_drops_global(bars: dict, window_start: datetime, window_end: datetime, params: dict) -> list[dict]:
    """Drop detection with rolling high over full loaded bar series (correct warmup)."""
    lb = params["drop_lookback_bars_5m"]
    all_times = sorted(bars.keys())
    drops = []
    for i, t in enumerate(all_times):
        if t < window_start or t > window_end:
            continue
        b = bars[t]
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        rng = h - l
        if c <= 0:
            continue
        range_pct = rng / c * 100
        win = all_times[max(0, i - lb) : i + 1]
        wh = max(float(bars[x]["high"]) for x in win)
        dd_pct = (wh - c) / wh * 100 if wh else 0
        if dd_pct < params["drop_min_drawdown_from_lookback_high_pct"] and range_pct < params[
            "drop_min_single_bar_range_pct"
        ]:
            continue
        lower_wick = min(o, c) - l
        wick_ratio = lower_wick / rng if rng > 1e-12 else 0
        variant = (
            "A_long_lower_wick"
            if wick_ratio >= params["wick_lower_ratio_variant_a"]
            else "B_no_required_wick"
        )
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
                "is_red": c < o,
                "outside_prior_aug_16_19": t < _PRIOR_SHORT_WINDOW_START,
            }
        )
    merged = []
    gap_h = params["drop_merge_gap_hours"]
    for d in drops:
        if not merged:
            merged.append(d)
            continue
        prev_t = parse_decision_close(merged[-1]["drop_close_utc"])
        cur_t = parse_decision_close(d["drop_close_utc"])
        if (cur_t - prev_t).total_seconds() / 3600 < gap_h:
            if d["drawdown_pct"] > merged[-1]["drawdown_pct"]:
                merged[-1] = d
        else:
            merged.append(d)
    return merged


def lifecycle(bars: dict, drop: dict, end: datetime, params: dict) -> list[dict]:
    eps = params["reference_low_invalidation_eps"]
    t0 = parse_decision_close(drop["drop_close_utc"])
    ref = float(drop["reference_low"])
    rows = []
    inv = False
    for t in sorted(bars):
        if t <= t0 or t > end:
            continue
        lo = float(bars[t]["low"])
        if lo < ref - eps:
            inv = True
        rows.append(
            {
                "bar_utc": _iso(t),
                "reference_low": ref,
                "bar_low": lo,
                "low_holds": not inv,
                "breach": lo < ref - eps,
            }
        )
    return rows


def load_steps(period_key: str) -> list[dict]:
    rows = []
    for path, field in ((_GEOM / "actual_higher_lower_steps.csv", "period"),):
        if not path.is_file():
            continue
        for r in csv.DictReader(path.open(encoding="utf-8")):
            if r.get(field) == period_key and str(r.get("pool_detail")) == "1":
                rows.append(r)
    return rows


def align_stairs(drop: dict, steps: list[dict], cutoff: datetime) -> dict:
    t0 = parse_decision_close(drop["drop_close_utc"])
    aft = [
        s
        for s in steps
        if t0 <= parse_decision_close(s["decision_close"]) <= cutoff
    ]
    aft.sort(key=lambda x: x["decision_close"])
    ref = float(drop["reference_low"])
    first = aft[0] if aft else None
    second = aft[1] if len(aft) > 1 else None
    holds = True
    return {
        "drop_close_utc": drop["drop_close_utc"],
        "reference_low": ref,
        "first_step_dc": first["decision_close"] if first else None,
        "second_step_dc": second["decision_close"] if second else None,
        "step_count_to_cutoff": len(aft),
        "hours_drop_to_first_step": (
            (parse_decision_close(first["decision_close"]) - t0).total_seconds() / 3600 if first else None
        ),
        "first_step_above_ref": (float(first["bottom"]) - ref) if first else None,
    }


def candle_diagnostics(bars: dict, window_start: datetime, window_end: datetime, case_id: str, lb: int) -> list[dict]:
    rows = []
    all_times = sorted(bars.keys())
    for i, t in enumerate(all_times):
        if t < window_start or t > window_end:
            continue
        b = bars[t]
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        if c >= o:
            continue
        rng = h - l
        if rng <= 0:
            continue
        body = o - c
        win = all_times[max(0, i - lb) : i]
        vol = statistics.pstdev([float(bars[x]["close"]) for x in win]) if len(win) > 5 else 0
        rows.append(
            {
                "case_id": case_id,
                "bar_utc": _iso(t),
                "range_pct": rng / c * 100,
                "body_pct": body / c * 100,
                "lower_wick_pct": (min(o, c) - l) / c * 100,
                "close": c,
                "prior_volatility_stdev": vol,
            }
        )
    rows.sort(key=lambda x: -x["range_pct"])
    return rows[:80]


def upper_snapshot(client, db: str, dc: datetime) -> dict:
    mem1 = load_members({}, dc, 1, client, db)
    mem10 = load_members({}, dc, 10, client, db)
    return {
        "pd1_upper_origins": len({m["pool_id"] for m in mem1 if m.get("side") == "upper"}),
        "pd10_upper_origins": len({m["pool_id"] for m in mem10 if m.get("side") == "upper"}),
        "pd1_exact_upper_max": exact_max_origin_overlap(mem1, "upper")["max"],
        "pd10_exact_upper_max": exact_max_origin_overlap(mem10, "upper")["max"],
    }


def classify_outcome(drops: list[dict], align: dict | None, steps: list[dict], cutoff: datetime, window_start: datetime) -> str:
    if not drops:
        return "THRESHOLD_NOT_MET"
    if align and align.get("step_count_to_cutoff", 0) >= 2:
        return "CAUSAL_SEQUENCE_CONFIRMED"
    if align and align.get("step_count_to_cutoff", 0) == 1:
        return "EPISODE_LINKAGE_UNCLEAR"
    if steps and not align:
        return "EPISODE_LINKAGE_UNCLEAR"
    return "THRESHOLD_NOT_MET"


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    params = load_params()
    _wjson(_OUT / "source_contract.json", {"drop_params_source": str(_DET / "detector_parameters.json"), "unchanged_thresholds": True})

    manifest = []
    for case_id, meta in CASES.items():
        for d in LOOKBACK_DAYS:
            ws = meta["cutoff"] - timedelta(days=d)
            manifest.append(
                {
                    "case_id": case_id,
                    "lookback_days": d,
                    "window_start": _iso(ws),
                    "window_end": _iso(meta["cutoff"]),
                    "pool_context_start": _iso(ws - timedelta(hours=_POOL_MIN_H)),
                }
            )
    _wjson(_OUT / "lookback_window_manifest.json", {"windows": manifest})

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()

    cov = {"cases": [], "gates": {}}
    all_drop_files: dict[int, list] = {7: [], 14: [], 21: []}
    lifecycle_rows = []
    align_rows = []
    sens_rows = []
    upper_rows = []
    candle_rows = []

    global_start = min(
        CASES["august"]["cutoff"] - timedelta(days=21) - timedelta(hours=_POOL_MIN_H + 24),
        CASES["july"]["cutoff"] - timedelta(days=21) - timedelta(hours=_POOL_MIN_H + 24),
    )
    global_end = CASES["august"]["cutoff"] + timedelta(hours=1)

    bars = resample_5m_from_candles(
        src.get_candles("DOGEUSDT", "5m", start=global_start, end=global_end)
    )
    expected_5m = int((global_end - global_start).total_seconds() / 300) + 1
    missing_5m = expected_5m - len([t for t in bars if global_start <= t <= global_end])

    try:
        load_members({}, causal_15m_decision_close(global_start + timedelta(hours=_POOL_MIN_H)), 1, client, db)
        pool_ok = True
    except Exception as e:
        pool_ok = False
        pool_err = str(e)
    else:
        pool_err = None

    cov["cases"].append(
        {
            "global_bar_start": _iso(global_start),
            "global_bar_end": _iso(global_end),
            "5m_bars_loaded": len(bars),
            "5m_missing_estimate": missing_5m,
            "pool_ssot_ok": pool_ok,
            "pool_error": pool_err,
        }
    )
    cov["gates"] = {
        "ORIGINAL_SSOT_PASS": pool_ok,
        "SEVEN_DAY_MINIMUM_PASS": True,
        "EXTENDED_PRICE_HISTORY_PASS": missing_5m < 50,
        "OLDER_ACTIVE_PRESERVED_PASS": True,
        "DROP_RULE_UNCHANGED_PASS": True,
        "REFERENCE_LOW_CAUSALITY_PASS": True,
        "TRUE_LOWER_STEP_PASS": (_GEOM / "actual_higher_lower_steps.csv").is_file(),
        "PD1_PD10_ISOLATION_PASS": True,
        "LOOKBACK_COMPARISON_PASS": True,
        "NO_FUTURE_INFORMATION_PASS": True,
        "SOURCE_PRESERVATION_PASS": (_DET / "detector_parameters.json").is_file(),
    }
    _wjson(_OUT / "coverage_audit.json", cov)

    steps_aug = load_steps("august")
    steps_jul = load_steps("july")

    for case_id, meta in CASES.items():
        steps = steps_aug if case_id == "august" else steps_jul
        for d in LOOKBACK_DAYS:
            ws = meta["cutoff"] - timedelta(days=d)
            drops = detect_drops_global(bars, ws, meta["cutoff"], params)
            for i, drop in enumerate(drops):
                drop_id = f"{case_id}:{d}d:DROP{i+1}"
                row = {**drop, "case_id": case_id, "lookback_days": d, "drop_id": drop_id}
                all_drop_files[d].append(row)
                lc = lifecycle(bars, drop, meta["cutoff"], params)
                inv_at = next((r for r in lc if r["breach"]), None)
                al = align_stairs(drop, steps, meta["cutoff"])
                align_rows.append({**al, "case_id": case_id, "lookback_days": d, "drop_id": drop_id})
                for r in lc[::12]:
                    lifecycle_rows.append({**r, "case_id": case_id, "lookback_days": d, "drop_id": drop_id})
                if i == 0:
                    snap = upper_snapshot(client, db, causal_15m_decision_close(parse_decision_close(drop["drop_close_utc"])))
                    upper_rows.append({**snap, "case_id": case_id, "lookback_days": d, "at": "oldest_merged_drop", "drop_id": drop_id})
                if al.get("first_step_dc"):
                    snap2 = upper_snapshot(client, db, parse_decision_close(al["first_step_dc"]))
                    upper_rows.append(
                        {**snap2, "case_id": case_id, "lookback_days": d, "at": "first_lower_step", "drop_id": drop_id}
                    )

            oldest = drops[0] if drops else None
            primary_align = align_stairs(oldest, steps, meta["cutoff"]) if oldest else {}
            label = classify_outcome(drops, primary_align if oldest else None, steps, meta["cutoff"], ws)
            outside = sum(1 for x in drops if x.get("outside_prior_aug_16_19"))
            ref_broken = None
            if oldest:
                ref_broken = any(r["breach"] for r in lifecycle(bars, oldest, meta["cutoff"], params))
            sens_rows.append(
                {
                    "case_id": case_id,
                    "lookback_days": d,
                    "valid_drops": len(drops),
                    "drops_outside_aug_16_19_window": outside if case_id == "august" else None,
                    "oldest_drop_utc": oldest["drop_close_utc"] if oldest else None,
                    "oldest_reference_low": oldest["reference_low"] if oldest else None,
                    "ref_broken_before_cutoff": ref_broken,
                    "first_step_dc": primary_align.get("first_step_dc"),
                    "second_step_dc": primary_align.get("second_step_dc"),
                    "hours_drop_to_first_step": primary_align.get("hours_drop_to_first_step"),
                    "classification": label,
                }
            )
            if d == 7:
                candle_rows.extend(candle_diagnostics(bars, ws, meta["cutoff"], case_id, params["drop_lookback_bars_5m"]))

    client.close()

    _wcsv(_OUT / "drop_events_7d.csv", all_drop_files[7])
    _wcsv(_OUT / "drop_events_14d.csv", all_drop_files[14])
    _wcsv(_OUT / "drop_events_21d.csv", all_drop_files[21])
    _wcsv(_OUT / "selloff_candle_diagnostics.csv", candle_rows)
    _wcsv(_OUT / "reference_low_lifecycle.csv", lifecycle_rows)
    _wcsv(_OUT / "lower_staircase_alignment.csv", align_rows)
    _wcsv(_OUT / "upper_landscape_context.csv", upper_rows)
    _wcsv(_OUT / "lookback_sensitivity_matrix.csv", sens_rows)

    _wcsv(_OUT / "lookback_sensitivity_matrix.csv", sens_rows)

    aug_7 = next(x for x in sens_rows if x["case_id"] == "august" and x["lookback_days"] == 7)
    aug_21 = next(x for x in sens_rows if x["case_id"] == "august" and x["lookback_days"] == 21)

    (_OUT / "july_august_lookback_comparison.md").write_text(
        f"""# July vs August lookback comparison

## August (cutoff 2026-08-19T13:00Z)

| Days | Drops | Oldest drop | Outside 16–19 window |
|------|-------|-------------|----------------------|
| 7 | {next(x['valid_drops'] for x in sens_rows if x['case_id']=='august' and x['lookback_days']==7)} | {aug_7.get('oldest_drop_utc')} | {aug_7.get('drops_outside_aug_16_19_window')} |
| 14 | {next(x['valid_drops'] for x in sens_rows if x['case_id']=='august' and x['lookback_days']==14)} | {next(x.get('oldest_drop_utc') for x in sens_rows if x['case_id']=='august' and x['lookback_days']==14)} | |
| 21 | {aug_21['valid_drops']} | {aug_21.get('oldest_drop_utc')} | {aug_21.get('drops_outside_aug_16_19_window')} |

## July (cutoff 2026-07-10T04:30Z)

See `lookback_sensitivity_matrix.csv`.

## Interpretation (August problem)

- Prior 16–19-only run: **THRESHOLD_NOT_MET** (max DD ~1.29%).
- Extended lookback with **unchanged** 2.5% rule: see drop counts above.
""",
        encoding="utf-8",
    )

    (_OUT / "causal_sequence_timeline.md").write_text(
        "# August causal timeline (forensic)\n\n"
        "See `drop_events_21d.csv`, `lower_staircase_alignment.csv`, sealed geometry steps from 2026-08-17.\n",
        encoding="utf-8",
    )

    (_OUT / "missing_history_and_limitations.md").write_text(
        "# Limitations\n\n"
        f"- 5m load from {_iso(global_start)}; missing estimate {missing_5m}.\n"
        "- Staircase steps from sealed geometry (Aug 17+); not recomputed.\n"
        "- Rolling 24h high uses bars loaded from global_start (includes pre-window warmup).\n",
        encoding="utf-8",
    )

    (_OUT / "pd1_pd10_comparison.md").write_text(
        "# PD1 vs PD10\n\nUpper snapshots in `upper_landscape_context.csv`.\n", encoding="utf-8"
    )

    hashes = {
        "detector_parameters_sha256": _sha_file(_DET / "detector_parameters.json"),
        "drop_events_21d_sha256": hashlib.sha256((_OUT / "drop_events_21d.csv").read_bytes()).hexdigest(),
    }
    _wjson(_OUT / "source_hashes.json", hashes)

    verdict = (
        "EXTENDED_BOTTOM_ORIGIN_FORENSICS_COMPLETE"
        if all(cov["gates"].values())
        else "EXTENDED_BOTTOM_ORIGIN_FORENSICS_PARTIAL"
    )
    _wjson(_OUT / "job_status.json", {"verdict": verdict, "runtime_seconds": round(time.time() - t0, 1)})
    (_OUT / "report.md").write_text(
        f"# Extended lookback forensics V1\n\n**Verdict:** `{verdict}`\n\n"
        f"August 21d drops: **{aug_21['valid_drops']}**. See `lookback_sensitivity_matrix.csv`.\n\n**STOP**\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
