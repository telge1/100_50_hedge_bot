"""First-touch study runner with checkpoints, resume, and finalize."""

from __future__ import annotations

import csv
import json
import logging
import math
import resource
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
    atomic_write_text,
)
from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.mp_big_move_case_control_v1.stats import cliffs_delta, median
from obfull_research_engine.mp_qdh_30event_case_control_v1.paired import bootstrap_ci
from obfull_research_engine.mp_qdh_canonical_integration_v1.event_load import (
    load_events_csv,
    load_windows_csv,
)

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    CONTRACT_VERSION,
    PACKAGE_NAME,
    SCHEMA_VERSION,
    STUDY_ID,
    TARGET_REACH_PCT,
)
from .analyze_event import analyze_first_touch_event, load_or_reject_checkpoint
from .contract import CONTRACT_HASH
from .run_smoke import run_smoke
from .source_run import resolve_source_run_dir
from .universe import build_first_touch_universe, write_universe


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {
                k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                for k, v in r.items()
            }
            w.writerow(flat)


def _horizon_map(decision_out: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(h["horizon_min"]): h for h in (decision_out.get("horizons") or []) if h.get("horizon_min") is not None}


def build_overlap_groups(rows: list[dict[str, Any]], window_s: float) -> list[dict[str, Any]]:
    items = sorted(rows, key=lambda r: (int(float(r["first_touch_ts_ns"])), r["event_id"]))
    groups: list[list[dict[str, Any]]] = []
    for r in items:
        t = int(float(r["first_touch_ts_ns"]))
        placed = False
        for g in groups:
            # overlap if within window_s of any member
            if any(abs(t - int(float(x["first_touch_ts_ns"]))) <= window_s * 1e9 for x in g):
                g.append(r)
                placed = True
                break
        if not placed:
            groups.append([r])
    out = []
    for i, g in enumerate(groups, 1):
        if len(g) < 2:
            continue
        keep = sorted(g, key=lambda x: (int(float(x["first_touch_ts_ns"])), x["event_id"]))[0]
        out.append(
            {
                "overlap_group_id": f"OG{i:03d}_{int(window_s)}s",
                "window_s": window_s,
                "n_events": len(g),
                "event_ids": "|".join(x["event_id"] for x in g),
                "keep_event_id": keep["event_id"],
                "selection_rule": "earliest_first_touch_then_event_id",
            }
        )
    return out


def nonoverlap_keep(rows: list[dict[str, Any]], window_s: float) -> set[str]:
    items = sorted(rows, key=lambda r: (int(float(r["first_touch_ts_ns"])), r["event_id"]))
    keep: list[dict[str, Any]] = []
    blocked_until = -1
    for r in items:
        t = int(float(r["first_touch_ts_ns"]))
        if t >= blocked_until:
            keep.append(r)
            blocked_until = t + int(window_s * 1e9)
    return {r["event_id"] for r in keep}


def finalize(out_dir: Path, results: dict[str, dict[str, Any]], uni: dict[str, Any]) -> dict[str, Any]:
    feat_rows = []
    conf_rows = []
    cov_rows = []
    mass_rows = []
    qdh_rows = []
    pers_rows = []
    ie_rows = []
    depth_rows = []
    wall_rows = []
    decision_out_rows = []
    touch_out_rows = []
    mfe_rows = []
    mae_before_rows = []
    reach_rows = []
    tp_sl_rows = []
    dq_rows = []

    for eid, r in sorted(results.items()):
        f = r.get("features") or {}
        d = r.get("decision_outcomes") or {}
        t = r.get("touch_outcomes") or {}
        u = r.get("universe") or {}
        feat_rows.append({"event_id": eid, **u, **{k: f.get(k) for k in f}})
        conf_rows.append(
            {
                "event_id": eid,
                "flow_attribution_confidence": f.get("flow_attribution_confidence"),
                "availability_confidence": f.get("availability_confidence"),
                "unknown_fraction": f.get("unknown_fraction"),
                "usable_for_historical_research": f.get("usable_for_historical_research"),
                "usable_for_live_latency_claim": f.get("usable_for_live_latency_claim"),
                "receive_time_present_count": f.get("receive_time_present_count"),
                "receive_time_missing_count": f.get("receive_time_missing_count"),
            }
        )
        cov = r.get("coverage") or {}
        cov_rows.append(
            {
                "event_id": eid,
                "coverage_ok": cov.get("pass"),
                "coverage_block_reason": "|".join(cov.get("blockers") or []),
                "n_trades_raw": cov.get("n_trades_raw"),
                "cross_epoch": cov.get("cross_epoch"),
                "seq_gaps": cov.get("seq_gaps"),
            }
        )
        mass_rows.append(
            {
                "event_id": eid,
                "fill": f.get("attributed_fill_qty"),
                "pull": f.get("residual_pull_qty"),
                "inferred_refill": f.get("refill_qty"),
                "unknown": f.get("unknown_qty"),
                "unmatched_fill": f.get("fill_excess_over_decrease"),
                "unknown_in_qdh": False,
            }
        )
        qdh_rows.append(
            {
                "event_id": eid,
                "qdh_at_decision": f.get("qdh_at_decision"),
                "qdh_na_reason": f.get("qdh_at_decision_na_reason"),
                "qdh_auc_per_valid_second": f.get("qdh_auc_per_valid_second") or f.get("qdh_auc_per_decision_second"),
                "qdh_mean": f.get("qdh_mean"),
                "qdh_persistence_adjusted_research": f.get("qdh_persistence_adjusted_research"),
            }
        )
        pers_rows.append(
            {
                "event_id": eid,
                "persistence_ratio_at_decision": f.get("persistence_ratio_at_decision"),
                "persistence_status": f.get("persistence_status"),
            }
        )
        ie_rows.append(
            {
                "event_id": eid,
                "impact_efficiency_bps_per_million": f.get("impact_efficiency_bps_per_million"),
                "impact_efficiency_status": f.get("impact_efficiency_status"),
                "attributed_hit_notional_usdt": f.get("attributed_hit_notional_usdt"),
            }
        )
        depth_rows.append(
            {
                "event_id": eid,
                "depth_norm": f.get("same_side_depth_2bps_at_decision_norm"),
                "baseline": f.get("same_side_depth_2bps_pre_touch_baseline"),
                "raw": f.get("same_side_depth_2bps_at_decision_raw"),
            }
        )
        wall_rows.append(
            {
                "event_id": eid,
                "wall_move_count": f.get("wall_move_count"),
                "movement_state": f.get("movement_state"),
                "identity_label": "aggregate_queue_survival_proxy",
            }
        )
        hm = _horizon_map(d)
        decision_out_rows.append(
            {
                "event_id": eid,
                "label": u.get("label_price_only"),
                "trade_side": u.get("trade_side"),
                "reached_0_41_pct": d.get("reached_0_41_pct"),
                "minutes_to_0_41": d.get("minutes_to_0_41"),
                "mae_before_0_41_pct": d.get("mae_before_0_41_pct"),
                "mfe_30m": (hm.get(30) or {}).get("mfe_pct"),
                "mae_30m": (hm.get(30) or {}).get("mae_pct"),
                "mfe_60m": (hm.get(60) or {}).get("mfe_pct"),
                "mae_60m": (hm.get(60) or {}).get("mae_pct"),
                "mfe_120m": (hm.get(120) or {}).get("mfe_pct"),
                "mae_120m": (hm.get(120) or {}).get("mae_pct"),
                "mfe_240m": (hm.get(240) or {}).get("mfe_pct"),
                "mae_240m": (hm.get(240) or {}).get("mae_pct"),
                "close_net_008_240m": (hm.get(240) or {}).get("close_return_net_008_pct"),
                "close_net_012_240m": (hm.get(240) or {}).get("close_return_net_012_pct"),
            }
        )
        touch_out_rows.append(
            {
                "event_id": eid,
                "reached_0_41_pct": t.get("reached_0_41_pct"),
                "minutes_to_0_41": t.get("minutes_to_0_41"),
                "mae_before_0_41_pct": t.get("mae_before_0_41_pct"),
                "mfe_240m": (_horizon_map(t).get(240) or {}).get("mfe_pct"),
                "mae_240m": (_horizon_map(t).get(240) or {}).get("mae_pct"),
            }
        )
        for h in d.get("horizons") or []:
            mfe_rows.append({"event_id": eid, "perspective": "DECISION", **h})
        mae_before_rows.append(
            {
                "event_id": eid,
                "reached_0_41_pct": d.get("reached_0_41_pct"),
                "mae_before_0_41_pct": d.get("mae_before_0_41_pct"),
                "max_mae_before_0_41_pct": d.get("max_mae_before_0_41_pct"),
                "minutes_to_0_41": d.get("minutes_to_0_41"),
                "mae_before_4h_mfe_peak_exclusive_pct": d.get("mae_before_4h_mfe_peak_exclusive_pct"),
                "mae_before_4h_mfe_peak_inclusive_pct": d.get("mae_before_4h_mfe_peak_inclusive_pct"),
                "extreme_order": d.get("extreme_order"),
            }
        )
        for thr in d.get("reach_thresholds") or []:
            reach_rows.append({"event_id": eid, **thr})
        for row in d.get("tp_sl") or []:
            tp_sl_rows.append({"event_id": eid, **row})
        dq_rows.append(
            {
                "event_id": eid,
                "status": r.get("status"),
                "ok": r.get("ok"),
                "blocked_reason": r.get("blocked_reason"),
                "linkage_status": r.get("linkage_status"),
                "db_mutation": r.get("db_mutation"),
            }
        )

    # Overlap
    base_meta = [
        {
            "event_id": r.get("event_id"),
            "first_touch_ts_ns": (r.get("universe") or {}).get("first_touch_ts_ns"),
        }
        for r in results.values()
        if (r.get("universe") or {}).get("first_touch_ts_ns")
    ]
    # enrich from uni
    by_u = {x["event_id"]: x for x in uni["included"]}
    for b in base_meta:
        if b["event_id"] in by_u:
            b["first_touch_ts_ns"] = by_u[b["event_id"]]["first_touch_ts_ns"]

    ov30 = build_overlap_groups(base_meta, 1800)
    ov60 = build_overlap_groups(base_meta, 3600)
    ov240 = build_overlap_groups(base_meta, 14400)
    keep30 = nonoverlap_keep(base_meta, 1800)
    keep60 = nonoverlap_keep(base_meta, 3600)
    keep240 = nonoverlap_keep(base_meta, 14400)

    # Group summaries
    def group_summary(ids: set[str] | None, name: str) -> dict[str, Any]:
        rows = [decision_out_rows[i] for i, _ in enumerate(decision_out_rows)]
        # filter
        sel = [r for r in decision_out_rows if ids is None or r["event_id"] in ids]
        n = len(sel)
        if n == 0:
            return {"group": name, "n": 0, "note": "EMPTY"}
        reach = [r for r in sel if r.get("reached_0_41_pct") in (True, "True", "true", 1, "1")]
        def med(key):
            xs = [_f(r.get(key)) for r in sel]
            xs = [x for x in xs if x is not None]
            return median(xs) if xs else None
        def med_reach(key):
            xs = [_f(r.get(key)) for r in reach]
            xs = [x for x in xs if x is not None]
            return median(xs) if xs else None
        # tp/sl counts for 240m / 0.15
        tp_counts = Counter()
        for r in results.values():
            if ids is not None and r.get("event_id") not in ids:
                continue
            for row in (r.get("decision_outcomes") or {}).get("tp_sl") or []:
                if int(row.get("horizon_min") or 0) == 240 and abs(float(row.get("sl_pct") or 0) - 0.15) < 1e-12:
                    tp_counts[row.get("result")] += 1
        return {
            "group": name,
            "n": n,
            "low_sample": n < 10,
            "reach_0_41_rate": len(reach) / n if n else None,
            "reach_0_41_count": len(reach),
            "median_minutes_to_0_41": med_reach("minutes_to_0_41"),
            "median_mae_before_0_41": med_reach("mae_before_0_41_pct"),
            "median_mfe_30m": med("mfe_30m"),
            "median_mae_30m": med("mae_30m"),
            "median_mfe_60m": med("mfe_60m"),
            "median_mae_60m": med("mae_60m"),
            "median_mfe_120m": med("mfe_120m"),
            "median_mae_120m": med("mae_120m"),
            "median_mfe_240m": med("mfe_240m"),
            "median_mae_240m": med("mae_240m"),
            "median_net_008_240m": med("close_net_008_240m"),
            "median_net_012_240m": med("close_net_012_240m"),
            "tp041_sl015_240m": {str(k): v for k, v in tp_counts.items()},
        }

    all_ids = set(results)
    groups = [
        group_summary(all_ids, "ALL"),
        group_summary(keep30, "NON_OVERLAP_30M"),
        group_summary(keep60, "NON_OVERLAP_1H"),
        group_summary(keep240, "NON_OVERLAP_4H"),
    ]
    for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
        groups.append(group_summary({r["event_id"] for r in decision_out_rows if r.get("label") == lab}, f"LABEL_{lab}"))
    for side in ("LONG", "SHORT"):
        groups.append(group_summary({r["event_id"] for r in decision_out_rows if r.get("trade_side") == side}, f"SIDE_{side}"))

    # Feature vs 0.41
    feat_keys = [
        "persistence_ratio_at_decision",
        "qdh_at_decision",
        "qdh_auc_per_decision_second",
        "qdh_persistence_adjusted_research",
        "impact_efficiency_bps_per_million",
        "same_side_depth_2bps_at_decision_norm",
        "attributed_fill_qty",
        "residual_pull_qty",
        "unknown_qty",
        "wall_move_count",
    ]
    reached_ids = {r["event_id"] for r in decision_out_rows if r.get("reached_0_41_pct") in (True, "True", "true", 1, "1")}
    feat_cmp = []
    for fk in feat_keys:
        a, b = [], []
        for eid, r in results.items():
            v = _f((r.get("features") or {}).get(fk))
            if v is None:
                continue
            (a if eid in reached_ids else b).append(v)
        if len(a) < 3 or len(b) < 3:
            feat_cmp.append(
                {
                    "feature": fk,
                    "n_reached": len(a),
                    "n_not": len(b),
                    "stability": "INSUFFICIENT_SAMPLE",
                    "edge_verdict": "NO_CONFIRMED_EDGE",
                }
            )
            continue
        md = median(a) - median(b)
        cd = cliffs_delta(a, b)
        feat_cmp.append(
            {
                "feature": fk,
                "n_reached": len(a),
                "n_not": len(b),
                "median_reached": median(a),
                "median_not_reached": median(b),
                "median_diff_reached_minus_not": md,
                "cliffs_delta": cd,
                "statement": "DESCRIPTIVE_DIFFERENCE",
                "edge_verdict": "NO_CONFIRMED_EDGE",
                "stability": "SIGN_STABLE" if abs(cd or 0) > 0.05 else "SIGN_UNSTABLE",
            }
        )

    # TP/SL summary counts
    tpsl_sum = Counter()
    for r in results.values():
        for row in (r.get("decision_outcomes") or {}).get("tp_sl") or []:
            if int(row.get("horizon_min") or 0) == 240:
                tpsl_sum[(float(row.get("sl_pct")), row.get("result"))] += 1

    _write_csv(out_dir / "canonical_features.csv", feat_rows)
    _write_csv(out_dir / "confidence_summary.csv", conf_rows)
    _write_csv(out_dir / "coverage_summary.csv", cov_rows)
    _write_csv(out_dir / "mass_balance_summary.csv", mass_rows)
    _write_csv(out_dir / "qdh_summary.csv", qdh_rows)
    _write_csv(out_dir / "persistence_summary.csv", pers_rows)
    _write_csv(out_dir / "impact_efficiency_summary.csv", ie_rows)
    _write_csv(out_dir / "depth_normalization_summary.csv", depth_rows)
    _write_csv(out_dir / "wall_movement_summary.csv", wall_rows)
    _write_csv(out_dir / "decision_relative_outcomes.csv", decision_out_rows)
    _write_csv(out_dir / "touch_relative_outcomes.csv", touch_out_rows)
    _write_csv(out_dir / "mfe_mae_by_horizon.csv", mfe_rows)
    _write_csv(out_dir / "mae_before_target.csv", mae_before_rows)
    _write_csv(out_dir / "target_reach_summary.csv", reach_rows)
    _write_csv(out_dir / "tp_sl_first_passage.csv", tp_sl_rows)
    _write_csv(out_dir / "outcome_group_summary.csv", groups)
    _write_csv(out_dir / "overlap_groups.csv", ov30 + ov60 + ov240)
    _write_csv(
        out_dir / "nonoverlap_sensitivity.csv",
        [
            {"policy": "ALL", "n": len(all_ids)},
            {"policy": "NON_OVERLAP_30M", "n": len(keep30)},
            {"policy": "NON_OVERLAP_1H", "n": len(keep60)},
            {"policy": "NON_OVERLAP_4H", "n": len(keep240)},
        ],
    )
    _write_csv(out_dir / "feature_outcome_comparison.csv", feat_cmp)
    _write_csv(out_dir / "data_quality_report.csv", dq_rows)
    _write_csv(out_dir / "event_summary.csv", decision_out_rows)

    g_all = groups[0]
    warnings = [
        {"type": "NO_CONFIRMED_EDGE"},
        {"type": "ABSORPTION_RATIO_NOT_CALIBRATED"},
        {"type": "VACUUM_SCORE_NOT_CALIBRATED"},
        {"type": "RECEIVE_TIME_NOT_AVAILABLE", "n": sum(1 for r in conf_rows if r.get("availability_confidence") == "RECEIVE_TIME_NOT_AVAILABLE")},
        {"type": "HIGH_UNKNOWN_RATE"},
    ]
    if g_all.get("low_sample"):
        warnings.append({"type": "LOW_SAMPLE", "n": g_all["n"]})

    n_ok = sum(1 for r in results.values() if r.get("ok"))
    n_blocked = sum(
        1
        for r in results.values()
        if (not r.get("ok")) and str(r.get("status") or "").upper() == "BLOCKED"
    )
    n_failed = sum(
        1
        for r in results.values()
        if (not r.get("ok")) and str(r.get("status") or "").upper() == "FAILED"
    )
    # SUCCESS if every universe event finished OK or transparently BLOCKED (no crash-FAILED)
    all_accounted = len(results) == len(uni["included"])
    transparent_complete = all_accounted and n_failed == 0 and (n_ok + n_blocked) == len(results)
    verdict = (
        "FIRST_TOUCH_STUDY_SUCCESS"
        if transparent_complete
        else (
            "FIRST_TOUCH_STUDY_PARTIAL"
            if n_ok > 0
            else "FIRST_TOUCH_STUDY_FAILED"
        )
    )

    flow_c = Counter(str(r.get("flow_attribution_confidence") or "MISSING") for r in conf_rows)
    avail_c = Counter(str(r.get("availability_confidence") or "MISSING") for r in conf_rows)
    qdh_valid = sum(1 for r in qdh_rows if r.get("qdh_at_decision") not in (None, ""))
    qdh_exh = sum(1 for r in qdh_rows if (r.get("qdh_na_reason") or "") == "QUEUE_EXHAUSTED")
    ie_ok = sum(1 for r in ie_rows if r.get("impact_efficiency_status") == "OK")
    pers_ok = sum(1 for r in pers_rows if r.get("persistence_status") == "OK")

    # reach by horizon approx from minutes_to_0_41
    def reach_within(mins: int) -> int:
        c = 0
        for r in decision_out_rows:
            m = _f(r.get("minutes_to_0_41"))
            if r.get("reached_0_41_pct") in (True, "True", "true", 1, "1") and m is not None and m < mins:
                c += 1
        return c

    exec_summary = f"""# FIRST TOUCH EXECUTIVE SUMMARY

**VERDICT:** `{verdict}`

## Universe
- First-touch found (raw): {uni['manifest']['n_first_touch_raw']}
- Tradeable included: {uni['manifest']['n_included']}
- Excluded: {uni['manifest']['exclusion_counts']}
- Universe hash: `{uni['universe_hash'][:16]}…`
- Contract hash: `{CONTRACT_HASH[:16]}…`

## 0.41% move (decision-relative)
- Reach 4h: {g_all.get('reach_0_41_count')}/{g_all.get('n')} = {(100*(g_all.get('reach_0_41_rate') or 0)):.1f}%
- Reach within 30m / 1h / 2h / 4h: {reach_within(30)} / {reach_within(60)} / {reach_within(120)} / {g_all.get('reach_0_41_count')}
- Median time to 0.41%: {g_all.get('median_minutes_to_0_41')} minutes
- Median MAE before 0.41%: {g_all.get('median_mae_before_0_41')} %

## MFE / MAE medians (decision-relative, %)
- 30m MFE/MAE: {g_all.get('median_mfe_30m')} / {g_all.get('median_mae_30m')}
- 1h  MFE/MAE: {g_all.get('median_mfe_60m')} / {g_all.get('median_mae_60m')}
- 2h  MFE/MAE: {g_all.get('median_mfe_120m')} / {g_all.get('median_mae_120m')}
- 4h  MFE/MAE: {g_all.get('median_mfe_240m')} / {g_all.get('median_mae_240m')}

## Costs (median close net @ 4h)
- 0.08% RT: {g_all.get('median_net_008_240m')} %
- 0.12% RT: {g_all.get('median_net_012_240m')} %

## Confidence
- Flow attribution: {dict(flow_c)}
- Availability: {dict(avail_c)}

## QDH / IE / Persistence
- QDH valid/exhausted: {qdh_valid}/{qdh_exh}
- IE OK: {ie_ok}
- Persistence OK: {pers_ok}

## Overlap sensitivity (n)
- ALL / 30m / 1h / 4h: {len(all_ids)} / {len(keep30)} / {len(keep60)} / {len(keep240)}

## Edge
- **NO_CONFIRMED_EDGE** (descriptive only; Absorption/Vacuum NOT_CALIBRATED)
"""
    atomic_write_text(out_dir / "FIRST_TOUCH_EXECUTIVE_SUMMARY.md", exec_summary)

    report = exec_summary + f"""
## Full study notes
- Events OK/Blocked: {n_ok}/{n_blocked}
- TP/SL 4h counts (sl, result): { {str(k): v for k, v in tpsl_sum.items()} }
- Feature comparisons: see `feature_outcome_comparison.csv`
- Warnings: {warnings}
"""
    atomic_write_text(out_dir / "FIRST_TOUCH_STUDY_REPORT.md", report)
    atomic_write_json(out_dir / "warnings.json", warnings)

    summary = {
        "verdict": verdict,
        "n_included": len(uni["included"]),
        "n_ok": n_ok,
        "n_blocked": n_blocked,
        "flow_confidence": {str(k): v for k, v in flow_c.items()},
        "availability_confidence": {str(k): v for k, v in avail_c.items()},
        "qdh_valid": qdh_valid,
        "qdh_exhausted": qdh_exh,
        "ie_ok": ie_ok,
        "pers_ok": pers_ok,
        "reach": {
            "n": g_all.get("n"),
            "count": g_all.get("reach_0_41_count"),
            "rate": g_all.get("reach_0_41_rate"),
            "within_30m": reach_within(30),
            "within_60m": reach_within(60),
            "within_120m": reach_within(120),
            "within_240m": g_all.get("reach_0_41_count"),
            "median_minutes": g_all.get("median_minutes_to_0_41"),
            "median_mae_before": g_all.get("median_mae_before_0_41"),
        },
        "mfe_mae": {
            "mfe_30": g_all.get("median_mfe_30m"),
            "mae_30": g_all.get("median_mae_30m"),
            "mfe_60": g_all.get("median_mfe_60m"),
            "mae_60": g_all.get("median_mae_60m"),
            "mfe_120": g_all.get("median_mfe_120m"),
            "mae_120": g_all.get("median_mae_120m"),
            "mfe_240": g_all.get("median_mfe_240m"),
            "mae_240": g_all.get("median_mae_240m"),
        },
        "net": {
            "008": g_all.get("median_net_008_240m"),
            "012": g_all.get("median_net_012_240m"),
        },
        "overlap_n": {"all": len(all_ids), "30m": len(keep30), "1h": len(keep60), "4h": len(keep240)},
        "groups": groups,
        "feature_cmp": feat_cmp,
        "tp_sl_240": {str(k): v for k, v in tpsl_sum.items()},
        "contract_hash": CONTRACT_HASH,
        "universe_hash": uni["universe_hash"],
        "exclusions": uni["manifest"]["exclusion_counts"],
        "edge_verdict": "NO_CONFIRMED_EDGE",
    }
    return summary


def run_study(
    *,
    repo_root: Path | None = None,
    out_dir: Path | None = None,
    require_smoke: bool = True,
    smoke_out: Path | None = None,
    resume: bool = True,
    max_events: int | None = None,
    source_run_dir: Path | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    repo = Path(repo_root or _repo_root())
    out = Path(
        out_dir
        or (repo / "obfull_research_engine/runs/mp_qdh_first_touch_study_v1_20260917")
    )
    out.mkdir(parents=True, exist_ok=True)
    ckpt_dir = out / "checkpoints"
    ckpt_dir.mkdir(exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(out / "study.log", encoding="utf-8")],
    )
    log = logging.getLogger("first_touch_study")

    resolved = resolve_source_run_dir(source_run_dir=source_run_dir, repo_root=repo)
    assert resolved is not None

    if require_smoke:
        smoke = run_smoke(repo_root=repo, out_dir=smoke_out, source_run_dir=resolved.source_run_dir)
        if not smoke.get("ok"):
            atomic_write_json(
                out / "run_manifest.json",
                {
                    "ok": False,
                    "verdict": "FIRST_TOUCH_STUDY_BLOCKED",
                    "reason": "FIRST_TOUCH_RUN_BLOCKED_BY_SMOKE",
                    "smoke": smoke,
                },
            )
            return {"verdict": "FIRST_TOUCH_STUDY_BLOCKED", "ok": False, "out_dir": str(out)}

    uni = build_first_touch_universe(repo, source_run_dir=resolved.source_run_dir)
    write_universe(out, uni)
    atomic_write_json(
        out / "phase0_contract.json",
        {
            "contract_hash": CONTRACT_HASH,
            "contract_version": CONTRACT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source_run": uni.get("source_run"),
        },
    )

    batch = resolved.source_run_dir
    events = load_events_csv(batch)
    windows = load_windows_csv(batch)
    client = get_clickhouse_client()

    included = list(uni["included"])
    if max_events is not None:
        included = included[: int(max_events)]

    results: dict[str, dict[str, Any]] = {}
    times: list[float] = []
    for i, urow in enumerate(included, 1):
        eid = urow["event_id"]
        ckpt_path = ckpt_dir / f"{eid}.json"
        if resume and ckpt_path.exists():
            loaded = load_or_reject_checkpoint(ckpt_path, universe_hash=uni["universe_hash"])
            if loaded and loaded.get("_rejected"):
                log.warning("STALE_CHECKPOINT_REJECTED %s %s", eid, loaded.get("detail"))
            elif loaded:
                results[eid] = loaded
                log.info("RESUME %s (%d/%d)", eid, i, len(included))
                continue
        ev = dict(events[eid])
        if not ev.get("window_id"):
            ev["window_id"] = urow.get("window_id") or ""
        win = windows.get(ev.get("window_id") or "") or {}
        log.info("RUN %s %s (%d/%d)", eid, urow["label_price_only"], i, len(included))
        t1 = time.monotonic()
        try:
            res = analyze_first_touch_event(
                universe_row=urow,
                event_row=ev,
                window=win,
                client=client,
                universe_hash=uni["universe_hash"],
                out_event_dir=ckpt_dir / eid,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("FAIL %s", eid)
            res = {
                "ok": False,
                "event_id": eid,
                "status": "FAILED",
                "error": str(exc),
                "contract_hash": CONTRACT_HASH,
                "universe_hash": uni["universe_hash"],
                "db_mutation": False,
            }
        dt = time.monotonic() - t1
        times.append(dt)
        results[eid] = res
        atomic_write_json(
            ckpt_path,
            {
                "contract_hash": CONTRACT_HASH,
                "universe_hash": uni["universe_hash"],
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": dt,
                "result": res,
            },
        )
        avg = sum(times) / len(times)
        eta = avg * (len(included) - i)
        log.info(
            "DONE %s ok=%s elapsed=%.1fs avg=%.1fs eta=%.0fs ram=%.0fMB",
            eid,
            res.get("ok"),
            dt,
            avg,
            eta,
            _peak_mb(),
        )

    summary = finalize(out, results, uni)
    elapsed = time.monotonic() - t0
    peak = _peak_mb()
    atomic_write_json(
        out / "run_manifest.json",
        {
            "ok": summary["verdict"] in ("FIRST_TOUCH_STUDY_SUCCESS", "FIRST_TOUCH_STUDY_PARTIAL"),
            "verdict": summary["verdict"],
            "study_id": STUDY_ID,
            "package": PACKAGE_NAME,
            "contract_hash": CONTRACT_HASH,
            "universe_hash": uni["universe_hash"],
            "summary": summary,
            "elapsed_s": elapsed,
            "peak_ram_mb": peak,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    atomic_write_json(out / "runtime_metrics.json", {"elapsed_s": elapsed, "peak_ram_mb": peak, "n": len(results)})
    atomic_write_json(out / "compact_summary.json", summary)
    log.info("DONE %s", summary["verdict"])
    return {"verdict": summary["verdict"], "ok": True, "out_dir": str(out), "summary": summary}
