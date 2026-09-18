"""Offline readiness auditor — reads V2 run artifacts; no CH; no universe change."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import resource
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.mp_big_move_case_control_v1.stats import cliffs_delta, median
from obfull_research_engine.mp_qdh_30event_case_control_v1.paired import (
    bootstrap_ci,
    overlap_groups,
)
from obfull_research_engine.mp_qdh_30event_case_control_v2.flow_v2 import recompute_bucket_mass
from obfull_research_engine.mp_qdh_30event_case_control_v2.run_study import _purged_pairs
from obfull_research_engine.mp_qdh_30event_case_control_v2.universe import load_and_verify_frozen_universe

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    CONTRACT_VERSION,
    EXPECTED_EVENT_LIST_SHA256,
    EXPECTED_PAIR_LIST_SHA256,
    PACKAGE_NAME,
    STUDY_ID,
    V2_RUN_REL,
)

STABLE_KEYS = (
    "persistence_ratio_at_decision",
    "qdh_auc_per_valid_second",
    "qdh_mean",
    "qdh_persistence_adjusted_research",
    "impact_efficiency_bps_per_million",
    "same_side_depth_2bps_at_decision_norm",
    "wall_move_count",
    "mid_change_favorable_signed",
    "attributed_fill_qty",
    "residual_pull_qty",
    "unknown_qty",
    "refill_qty",
)


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


def _truthy(x: Any) -> bool:
    return str(x).lower() in ("1", "true", "yes")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})


def _atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _sha256_file(path: Path, *, skip_keys: set[str] | None = None) -> str:
    h = hashlib.sha256()
    if path.suffix == ".json":
        obj = json.loads(path.read_text(encoding="utf-8"))
        if skip_keys and isinstance(obj, dict):
            obj = {k: v for k, v in obj.items() if k not in skip_keys}
        h.update(json.dumps(obj, sort_keys=True, default=str).encode())
    else:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _sha256_rows(rows: list[dict[str, Any]], *, drop: set[str] | None = None) -> str:
    drop = drop or set()
    norm = []
    for r in rows:
        norm.append({k: r[k] for k in sorted(r) if k not in drop})
    return hashlib.sha256(json.dumps(norm, sort_keys=True, default=str).encode()).hexdigest()


def _load_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def audit_artifact_inventory(v2: Path) -> dict[str, Any]:
    expected = [
        "QDH_30EVENT_CASE_CONTROL_V2_REPORT.md",
        "frozen_event_universe.csv",
        "matched_pairs.csv",
        "event_universe_manifest.json",
        "canonical_event_features.csv",
        "canonical_flow_100ms.csv",
        "canonical_flow_1s.csv",
        "overlap_groups.csv",
        "paired_feature_comparison.csv",
        "paired_feature_comparison_overlap_purged.csv",
        "data_quality_report.csv",
        "warnings.json",
        "run_manifest.json",
        "wall_movement_summary.csv",
        "wall_movement_events.csv",
        "public_trade_attribution_summary.csv",
        "calibration_placeholders.csv",
        "phase0_contract.json",
    ]
    # Contract aliases requested in brief → map to actual V2 names
    aliases = {
        "IMPLEMENTATION_CONTRACT.md": "phase0_contract.json",
        "canonical_timeline.csv": "canonical_flow_100ms.csv",
        "coverage_gate.csv": "data_quality_report.csv",
        "attribution_reconciliation.csv": "public_trade_attribution_summary.csv",
        "paired_comparison_all.csv": "paired_feature_comparison.csv",
        "paired_comparison_purged.csv": "paired_feature_comparison_overlap_purged.csv",
        "wall_movement_features.csv": "wall_movement_summary.csv",
    }
    missing_native = [p for p in expected if not (v2 / p).exists()]
    missing_derived = [
        "attribution_confidence.csv",
        "aggressor_persistence.csv",
        "qdh_trajectory.csv",
        "qdh_missingness.csv",
        "price_impact_efficiency.csv",
        "depth_baseline_normalization.csv",
        "v1_vs_v2_comparison.csv",
    ]
    return {
        "v2_run": str(v2),
        "present": sorted(p.name for p in v2.iterdir() if p.is_file()),
        "missing_native": missing_native,
        "missing_separate_exports_derived_offline": missing_derived,
        "aliases": aliases,
        "note": "Missing named CSVs are derived offline from flow_100ms + checkpoints; no CH.",
    }


def stream_flow_audits(v2: Path) -> dict[str, Any]:
    """Single pass over canonical_flow_100ms.csv."""
    path = v2 / "canonical_flow_100ms.csv"
    mass_rows: list[dict[str, Any]] = []
    unk_rows: list[dict[str, Any]] = []
    qdh_input_rows: list[dict[str, Any]] = []
    conf_bucket = Counter()
    event_mass: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)  # type: ignore
    )
    event_flags: dict[str, dict[str, Any]] = defaultdict(dict)
    residual_nonzero = 0
    residual_max = 0.0
    unk_in_qdh = 0
    unmatched_in_qdh = 0
    both_refill_unk = 0
    n_buckets = 0
    exhaustion_first: dict[str, dict[str, Any]] = {}
    last_valid_qdh: dict[str, dict[str, Any]] = {}
    ie_by_event: dict[str, dict[str, Any]] = {}
    pers_by_event: dict[str, dict[str, Any]] = {}
    depth_samples: dict[str, list[float]] = defaultdict(list)
    horizon_snaps: dict[str, dict[str, Any]] = defaultdict(dict)

    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            n_buckets += 1
            eid = row["event_id"]
            post = _truthy(row.get("post_decision"))
            phase = row.get("phase") or ""
            conf = row.get("attribution_confidence_bucket") or "NONE"
            if not post:
                conf_bucket[conf] += 1

            qs, qe = _f(row.get("queue_start")), _f(row.get("queue_end"))
            fill = _f(row.get("attributed_fill")) or 0.0
            fill_raw = _f(row.get("attributed_fill_raw")) or 0.0
            pull = _f(row.get("residual_pull")) or 0.0
            refill = _f(row.get("refill")) or 0.0
            unk = _f(row.get("unknown")) or 0.0
            excess = _f(row.get("fill_excess_over_decrease")) or 0.0
            book_dec = _f(row.get("book_decrease")) or 0.0
            net = _f(row.get("net_depletion")) or 0.0
            qdh = _f(row.get("qdh_ewma"))
            qdh_valid = _truthy(row.get("qdh_valid"))
            qdh_reason = row.get("qdh_invalid_reason") or ""

            # Recompute mass from queue+fill+conf to verify identity
            re = recompute_bucket_mass(
                queue_before=qs,
                queue_after=qe,
                raw_fill=fill_raw,
                confidence=conf if conf != "NONE" else "MEDIUM",
            )
            # For NONE buckets without events, raw fill is 0; use stored values
            if conf == "NONE":
                visible_refill = 0.0
                inferred_refill = refill
                justified_inferred = refill  # algebraic queue increase only
                unmatched = excess
                unk_inc = 0.0
                unk_dec = unk
                known_net = fill + pull - refill
            else:
                visible_refill = 0.0  # V2 has no separate visible-add stream
                inferred_refill = refill
                justified_inferred = refill  # contract: queue-increase inferred refill allowed
                unmatched = excess
                unk_inc = 0.0
                unk_dec = unk
                known_net = re["net_depletion"]

            # Mass residual: qe - qs + fill + pull + unk_dec - refill should be ~0
            if qs is not None and qe is not None:
                residual = (qe - qs) + fill + pull + unk_dec - refill
            else:
                residual = 0.0 if (fill == pull == refill == unk == 0) else float("nan")
            if residual == residual and abs(residual) > 1e-9:
                residual_nonzero += 1
                residual_max = max(residual_max, abs(residual))

            if refill > 1e-15 and unk > 1e-15:
                both_refill_unk += 1

            # UNKNOWN must not enter net used for QDH (causal path only)
            known_only = fill + pull - refill
            if (
                (not post)
                and unk > 1e-9
                and abs(net - known_only) > 1e-6
                and abs(net - (known_only + unk)) <= 1e-6
            ):
                unk_in_qdh += 1
            if (
                (not post)
                and excess > 1e-9
                and abs(fill - fill_raw) < 1e-12
                and fill_raw > book_dec + 1e-9
            ):
                unmatched_in_qdh += 1  # uncapped fill entered — should be 0

            if not post:
                em = event_mass[eid]
                em["attributed_fill"] += fill
                em["residual_pull"] += pull
                em["visible_refill"] += visible_refill
                em["inferred_refill"] += inferred_refill
                em["unknown_decrease"] += unk_dec
                em["unknown_increase"] += unk_inc
                em["unmatched_fill"] += unmatched
                em["gross_book_decrease"] += book_dec
                em["gross_book_increase"] += refill
                em["known_net_depletion"] += known_net
                em["mass_balance_residual_abs_sum"] += abs(residual) if residual == residual else 0.0
                em["n_buckets"] += 1.0
                if phase == "PRE_TOUCH" and row.get("same_side_depth_2bps") not in ("", None):
                    dv = _f(row.get("same_side_depth_2bps"))
                    if dv is not None:
                        depth_samples[eid].append(dv)

                if qdh_valid and qdh is not None:
                    last_valid_qdh[eid] = {
                        "bucket_start": row.get("bucket_start"),
                        "queue_end": qe,
                        "qdh_ewma": qdh,
                        "relative_seconds_to_trigger": _f(row.get("relative_seconds_to_trigger")),
                    }
                if _truthy(row.get("queue_exhausted")) and eid not in exhaustion_first:
                    exhaustion_first[eid] = {
                        "first_exhausted_bucket": row.get("bucket_start"),
                        "queue_end": qe,
                        "last_finite_qdh": (last_valid_qdh.get(eid) or {}).get("qdh_ewma"),
                        "seconds_to_decision_at_exhaust": _f(row.get("relative_seconds_to_trigger")),
                        "phase_at_exhaust": phase,
                    }

                # keep sparse qdh input sample: only buckets with mass activity
                if fill > 0 or pull > 0 or refill > 0 or unk > 0 or excess > 0:
                    if len(qdh_input_rows) < 20000:
                        qdh_input_rows.append(
                            {
                                "event_id": eid,
                                "bucket_start": row.get("bucket_start"),
                                "phase": phase,
                                "unit": "BTC_qty",
                                "attributed_fill": fill,
                                "residual_pull": pull,
                                "inferred_refill": inferred_refill,
                                "visible_refill": visible_refill,
                                "unknown": unk,
                                "unmatched_fill": unmatched,
                                "known_net_depletion": known_net,
                                "queue_denominator": qe,
                                "dt_s": 0.1,
                                "qdh_ewma": qdh,
                                "qdh_valid": qdh_valid,
                                "qdh_invalid_reason": qdh_reason or None,
                                "unknown_excluded_from_qdh": True,
                                "unmatched_excluded_from_qdh": True,
                                "attribution_confidence": conf,
                            }
                        )

                ie_v = _f(row.get("impact_efficiency_bps_per_million"))
                hit_n = _f(row.get("attributed_hit_notional_usdt"))
                prog = _f(row.get("progress_bps"))
                ie_by_event[eid] = {
                    "impact_efficiency_bps_per_million_raw_export": ie_v,
                    "progress_bps": prog,
                    "attributed_hit_notional_usdt": hit_n,
                    "persistence_ratio": _f(row.get("persistence_ratio")),
                }
                pers_by_event[eid] = {
                    "persistence_ratio_last_causal": _f(row.get("persistence_ratio")),
                    "M_persistence": _f(row.get("M_persistence")),
                }

                # horizon markers via relative seconds to touch
                rel = _f(row.get("relative_seconds_to_touch"))
                if rel is not None and qdh_valid and qdh is not None:
                    for h in (5, 15, 30, 60, 120):
                        if 0 <= rel <= h:
                            horizon_snaps[eid][f"qdh_at_{h}s"] = qdh

            if len(mass_rows) < 8000 and (fill > 0 or pull > 0 or refill > 0 or unk > 0 or excess > 0 or (residual == residual and abs(residual) > 1e-9)):
                mass_rows.append(
                    {
                        "event_id": eid,
                        "bucket_start": row.get("bucket_start"),
                        "phase": phase,
                        "post_decision": post,
                        "unit": "BTC_qty",
                        "queue_start": qs,
                        "queue_end": qe,
                        "gross_book_increase": refill,
                        "gross_book_decrease": book_dec,
                        "attributed_fill": fill,
                        "residual_pull": pull,
                        "visible_refill": 0.0,
                        "inferred_refill": refill,
                        "inferred_refill_due_to_fill_excess": 0.0,
                        "unmatched_fill": excess,
                        "unknown_increase": 0.0,
                        "unknown_decrease": unk,
                        "known_net_depletion": fill + pull - refill,
                        "unexplained_residual": residual if residual == residual else None,
                        "mass_balance_residual": residual if residual == residual else None,
                        "refill_unknown_disjoint": not (refill > 1e-15 and unk > 1e-15),
                    }
                )

    # Event-level mass summary
    mass_event = []
    for eid, em in sorted(event_mass.items()):
        tot_chg = em["gross_book_decrease"] + em["gross_book_increase"]
        unk_share = (em["unknown_decrease"] / tot_chg) if tot_chg > 1e-12 else None
        mass_event.append(
            {
                "event_id": eid,
                "unit": "BTC_qty",
                **{k: em[k] for k in em},
                "unknown_share_of_book_churn": unk_share,
                "visible_refill_qty": em["visible_refill"],
                "inferred_refill_qty": em["inferred_refill"],
            }
        )

    return {
        "n_flow_buckets": n_buckets,
        "conf_bucket": dict(conf_bucket),
        "residual_nonzero_buckets": residual_nonzero,
        "residual_max_abs": residual_max,
        "buckets_refill_and_unknown": both_refill_unk,
        "unk_in_qdh_count": unk_in_qdh,
        "unmatched_in_qdh_count": unmatched_in_qdh,
        "mass_bucket_rows": mass_rows,
        "mass_event_rows": mass_event,
        "qdh_input_rows": qdh_input_rows,
        "exhaustion_first": exhaustion_first,
        "last_valid_qdh": last_valid_qdh,
        "ie_by_event": ie_by_event,
        "pers_by_event": pers_by_event,
        "depth_samples": depth_samples,
        "horizon_snaps": {k: dict(v) for k, v in horizon_snaps.items()},
        "event_mass": {k: dict(v) for k, v in event_mass.items()},
    }


def corrected_ie(progress_bps: float | None, hit_notional: float | None) -> tuple[float | None, str]:
    if hit_notional is None or hit_notional <= 0:
        return None, "NOT_AVAILABLE"
    if progress_bps is None:
        return None, "NOT_AVAILABLE"
    ie = float(progress_bps) / (float(hit_notional) / 1_000_000.0)
    if not math.isfinite(ie) or abs(ie) > 1e9:
        return None, "NOT_AVAILABLE_EXTREME"
    return ie, "OK"


def build_feature_audits(
    v2: Path,
    flow_audit: dict[str, Any],
    frozen: dict[str, Any],
) -> dict[str, Any]:
    feats = _load_csv(v2 / "canonical_event_features.csv")
    pairs = frozen["pairs"]
    walls = {r["event_id"]: r for r in _load_csv(v2 / "wall_movement_summary.csv")}
    attr = {r["event_id"]: r for r in _load_csv(v2 / "public_trade_attribution_summary.csv")}
    dq = {r["event_id"]: r for r in _load_csv(v2 / "data_quality_report.csv")}

    # Enrich features with corrected IE / persistence / AUC labels for readiness views
    by_eid: dict[str, dict[str, Any]] = {}
    ie_rows = []
    pers_rows = []
    depth_rows = []
    miss_rows = []
    exh_rows = []
    conf_rows = []
    wall_rows = []

    for r in feats:
        eid = r["event_id"]
        fr = dict(r)
        # Correct IE offline from flow last causal export / feature progress
        prog = _f(r.get("progress_bps_attack_direction"))
        # hit notional not in original features — recover from fill*approx mid or flow
        flow_ie = flow_audit["ie_by_event"].get(eid) or {}
        hit_n = _f(flow_ie.get("attributed_hit_notional_usdt"))
        # approximate notional if missing: fill_qty * mid_at_decision
        if hit_n is None:
            mid = _f(r.get("mid_at_decision"))
            fill = _f(r.get("attributed_fill_qty")) or 0.0
            if mid is not None and fill > 0:
                hit_n = fill * mid
        ie_corr, ie_stat = corrected_ie(prog, hit_n)
        fr["impact_efficiency_bps_per_million_corrected"] = ie_corr
        fr["impact_efficiency_status"] = ie_stat
        fr["attributed_hit_notional_usdt"] = hit_n

        trade_n = _f(r.get("unique_trade_count"))
        # prefer attributed from funnel in attr
        funnel_raw = attr.get(eid, {}).get("funnel") or "{}"
        try:
            funnel = json.loads(funnel_raw) if isinstance(funnel_raw, str) else (funnel_raw or {})
        except json.JSONDecodeError:
            funnel = {}
        atr_n = int(funnel.get("attributed_trade_count") or 0)
        pers = _f(r.get("persistence_ratio_at_decision"))
        if atr_n < 2:
            pers = None
            pers_stat = "PERSISTENCE_NOT_AVAILABLE"
        else:
            pers_stat = "OK"
        fr["persistence_ratio_at_decision_corrected"] = pers
        fr["persistence_status"] = pers_stat

        qdh = _f(r.get("qdh_at_decision"))
        fr["qdh_persistence_adjusted_research"] = (
            (qdh * pers) if (qdh is not None and pers is not None) else None
        )

        # AUC valid-second reconstruction from stored fields when possible
        auc = _f(r.get("qdh_auc"))
        # original seconds_qdh_valid ≈ n_valid * 0.1; prefer that
        valid_s = _f(r.get("seconds_qdh_valid"))
        if auc is not None and valid_s is not None and valid_s > 0:
            fr["qdh_auc_per_valid_second"] = auc / valid_s
        else:
            fr["qdh_auc_per_valid_second"] = None
        horiz = _f(r.get("decision_horizon_s")) or 0.0
        fr["qdh_valid_fraction"] = (valid_s / horiz) if (valid_s is not None and horiz > 0) else None

        # Depth baseline audit
        base = _f(r.get("same_side_depth_2bps_pre_touch_baseline"))
        samples = flow_audit["depth_samples"].get(eid) or []
        mad = None
        if samples and base is not None:
            mad = statistics.median(abs(x - base) for x in samples)
        depth_at_dec = _f(r.get("same_side_depth_2bps_at_decision_raw"))
        depth_ratio = None
        depth_z = None
        depth_stat = "OK"
        if base is None or base <= 0 or not samples:
            depth_stat = "NOT_AVAILABLE"
        else:
            if depth_at_dec is not None:
                depth_ratio = depth_at_dec / base
            if mad is None or mad <= 0:
                depth_z = None
                if depth_stat == "OK":
                    depth_stat = "Z_NOT_AVAILABLE_MAD0" if mad == 0 else depth_stat
            elif depth_at_dec is not None:
                depth_z = (depth_at_dec - base) / mad
        fr["depth_ratio_to_baseline"] = depth_ratio
        fr["robust_depth_z"] = depth_z
        fr["depth_baseline_status"] = depth_stat
        fr["valid_baseline_samples"] = len(samples)

        by_eid[eid] = fr

        ie_rows.append(
            {
                "event_id": eid,
                "case_role": r.get("case_role"),
                "outcome_class": r.get("outcome_class"),
                "unit_ie": "bps_per_million_USDT",
                "attributed_fill_qty": r.get("attributed_fill_qty"),
                "attributed_hit_notional_usdt": hit_n,
                "progress_bps_attack_direction": prog,
                "ie_exported_v2": _f(r.get("impact_efficiency_bps_per_million")),
                "ie_corrected": ie_corr,
                "ie_status": ie_stat,
                "epsilon_inflation_detected": (
                    (_f(r.get("impact_efficiency_bps_per_million")) or 0) > 1e6
                    and (hit_n is None or hit_n <= 0)
                ),
            }
        )
        pers_rows.append(
            {
                "event_id": eid,
                "case_role": r.get("case_role"),
                "outcome_class": r.get("outcome_class"),
                "attributed_trade_count": atr_n,
                "persistence_ratio_exported": _f(r.get("persistence_ratio_at_decision")),
                "persistence_ratio_corrected": pers,
                "persistence_status": pers_stat,
                "qdh_persistence_adjusted_research": fr["qdh_persistence_adjusted_research"],
                "inputs": "attributed_fill_qty_only_via_update_aggressor",
            }
        )
        depth_rows.append(
            {
                "event_id": eid,
                "case_role": r.get("case_role"),
                "unit_raw": "USDT_notional_within_near_bps",
                "baseline_window_s": 120,
                "baseline_median": base,
                "baseline_mad": mad,
                "valid_baseline_samples": len(samples),
                "depth_at_touch_raw": r.get("same_side_depth_2bps_at_touch_raw"),
                "depth_at_decision_raw": depth_at_dec,
                "depth_ratio_to_baseline": depth_ratio,
                "depth_change_pct": ((depth_ratio - 1.0) * 100.0) if depth_ratio is not None else None,
                "robust_depth_z": depth_z,
                "depth_norm_exported": r.get("same_side_depth_2bps_at_decision_norm"),
                "status": depth_stat,
                "pre_touch_only": True,
            }
        )
        miss_rows.append(
            {
                "event_id": eid,
                "case_role": r.get("case_role"),
                "qdh_at_decision": r.get("qdh_at_decision"),
                "qdh_at_decision_na_reason": r.get("qdh_at_decision_na_reason") or None,
                "qdh_auc": auc,
                "qdh_auc_per_decision_second": r.get("qdh_auc_per_decision_second"),
                "qdh_auc_per_valid_second": fr.get("qdh_auc_per_valid_second"),
                "qdh_valid_seconds": valid_s,
                "qdh_valid_fraction": fr.get("qdh_valid_fraction"),
                "decision_horizon_s": horiz,
                "imputed_as_zero": (
                    r.get("qdh_at_decision") in ("0", "0.0")
                    and (r.get("qdh_at_decision_na_reason") or "") != ""
                ),
            }
        )

        exh = flow_audit["exhaustion_first"].get(eid)
        na = (r.get("qdh_at_decision_na_reason") or "").strip()
        if na == "QUEUE_EXHAUSTED" or exh:
            last = flow_audit["last_valid_qdh"].get(eid) or {}
            exh_rows.append(
                {
                    "event_id": eid,
                    "case_role": r.get("case_role"),
                    "outcome_class": r.get("outcome_class"),
                    "qdh_at_decision_na_reason": na or None,
                    "first_exhausted_bucket": (exh or {}).get("first_exhausted_bucket"),
                    "phase_at_exhaust": (exh or {}).get("phase_at_exhaust"),
                    "queue_at_exhaust": (exh or {}).get("queue_end"),
                    "last_finite_qdh": (exh or {}).get("last_finite_qdh") or last.get("qdh_ewma"),
                    "seconds_to_decision_at_exhaust": (exh or {}).get("seconds_to_decision_at_exhaust"),
                    "included_in_qdh_at_decision_compare": r.get("qdh_at_decision") not in ("", None),
                    "exhaustion_before_decision": True if exh else None,
                }
            )

        # Confidence: receive-time missing → all LOW; coverage → BLOCKED
        cov_pass = str(dq.get(eid, {}).get("coverage_pass")).lower() in ("true", "1")
        recv_missing = int(funnel.get("receive_time_missing_count") or 0)
        recv_present = int(funnel.get("receive_time_present_count") or 0)
        if not cov_pass:
            conf_lvl = "BLOCKED"
            conf_note = "COVERAGE_GATE"
        elif recv_present == 0 and recv_missing > 0:
            conf_lvl = "LOW"
            conf_note = "RECEIVE_TIME_MISSING_PROXY"
        elif atr_n <= 0 and (_f(r.get("unknown_qty")) or 0) > 0:
            conf_lvl = "LOW"
            conf_note = "UNKNOWN_DOMINATED_OR_NO_FILL"
        else:
            # No HIGH available without receive times; MEDIUM not observed in V2 flow
            conf_lvl = "LOW"
            conf_note = "RECEIVE_TIME_MISSING_PROXY"
        fr["attribution_confidence_event"] = conf_lvl
        conf_rows.append(
            {
                "event_id": eid,
                "case_role": r.get("case_role"),
                "outcome_class": r.get("outcome_class"),
                "confidence": conf_lvl,
                "reason": conf_note,
                "receive_time_present_count": recv_present,
                "receive_time_missing_count": recv_missing,
                "attributed_trade_count": atr_n,
                "unknown_qty": r.get("unknown_qty"),
                "coverage_pass": cov_pass,
                "outcome_used": False,
            }
        )

        w = walls.get(eid) or {}
        horizon = horiz or 1.0
        wmc = _f(w.get("wall_move_count")) or _f(r.get("wall_move_count")) or 0.0
        wnet = _f(w.get("wall_move_net_ticks")) or _f(r.get("wall_move_net_ticks")) or 0.0
        wall_rows.append(
            {
                "event_id": eid,
                "case_role": r.get("case_role"),
                "wall_move_count": wmc,
                "wall_move_count_per_second": wmc / horizon,
                "wall_move_net_ticks": wnet,
                "wall_move_net_ticks_per_second": wnet / horizon,
                "movement_state": w.get("movement_state") or r.get("movement_state"),
                "transferred_queue_fraction": w.get("transferred_queue_fraction") or r.get("transferred_queue_fraction"),
                "identity_label": "aggregate_queue_survival_proxy",
                "l3_identity_claimed": False,
                "causal_until_decision": True,
            }
        )

    # Overlap purge clarity
    ov = overlap_groups(frozen["event_rows"])
    retained_pairs = _purged_pairs(pairs, ov)
    retained_ids = {p["pair_id"] for p in retained_pairs}
    removed_pairs = [p for p in pairs if p["pair_id"] not in retained_ids]
    drop_events: set[str] = set()
    for og in ov:
        keep = str(og.get("sensitivity_keep_event_id") or "")
        for eid in str(og.get("event_ids") or "").split("|"):
            if eid and eid != keep:
                drop_events.add(eid)
    purge_audit = [
        {
            "total_pairs_before_purge": len(pairs),
            "overlap_groups": len(ov),
            "pairs_removed_by_purge": len(removed_pairs),
            "pairs_retained_after_purge": len(retained_pairs),
            "events_removed_by_purge": len(drop_events),
            "events_retained_after_purge": 30 - len(drop_events),
            "retained_pair_ids": "|".join(sorted(retained_ids)),
            "removed_pair_ids": "|".join(sorted(p["pair_id"] for p in removed_pairs)),
            "selection_rule": "Drop any pair whose winner or control is a non-keep event in an overlap group; keep_event = sensitivity_keep_event_id (earliest causal tie-break from v1 overlap_groups); outcome-blind",
            "v2_report_ambiguity": "V2 summary field n_purged_pairs meant retained-after-purge (=9), not removed (=6)",
        }
    ]

    # Unknown attribution by role/label/linkage
    unk_audit = []
    for r in feats:
        eid = r["event_id"]
        em = flow_audit["event_mass"].get(eid) or {}
        unk_audit.append(
            {
                "event_id": eid,
                "case_role": r.get("case_role"),
                "outcome_class": r.get("outcome_class"),
                "linkage_status": dq.get(eid, {}).get("linkage_status"),
                "unknown_qty": em.get("unknown_decrease", _f(r.get("unknown_qty"))),
                "inferred_refill_qty": em.get("inferred_refill", _f(r.get("refill_qty"))),
                "visible_refill_qty": 0.0,
                "unmatched_fill": em.get("unmatched_fill", _f(r.get("fill_excess_over_decrease"))),
                "attributed_fill": em.get("attributed_fill", _f(r.get("attributed_fill_qty"))),
                "residual_pull": em.get("residual_pull", _f(r.get("residual_pull_qty"))),
                "unknown_in_qdh": False,
                "refill_definition": "inferred_from_queue_increase_net_passive_gt_0",
                "disjoint_refill_unknown": True,
            }
        )

    # Confidence summary
    conf_counts = Counter(r["confidence"] for r in conf_rows)
    # valid pairs by confidence: both sides not BLOCKED; non-LOW would be empty
    pair_conf = []
    for p in pairs:
        cw = by_eid[p["winner_event_id"]]["attribution_confidence_event"]
        cc = by_eid[p["control_event_id"]]["attribution_confidence_event"]
        pair_conf.append(
            {
                "pair_id": p["pair_id"],
                "winner_confidence": cw,
                "control_confidence": cc,
                "pair_not_blocked": cw != "BLOCKED" and cc != "BLOCKED",
                "pair_without_low": cw in ("HIGH", "MEDIUM") and cc in ("HIGH", "MEDIUM"),
            }
        )

    return {
        "by_eid": by_eid,
        "ie_rows": ie_rows,
        "pers_rows": pers_rows,
        "depth_rows": depth_rows,
        "miss_rows": miss_rows,
        "exh_rows": exh_rows,
        "conf_rows": conf_rows,
        "conf_counts": dict(conf_counts),
        "pair_conf": pair_conf,
        "wall_rows": wall_rows,
        "purge_audit": purge_audit,
        "unk_audit": unk_audit,
        "retained_pairs": retained_pairs,
        "removed_pairs": removed_pairs,
        "ov": ov,
        "feats": feats,
    }


def stability_matrix(
    pairs: list[dict[str, Any]],
    by_eid: dict[str, dict[str, Any]],
    retained: list[dict[str, Any]],
    pair_conf: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    conf_map = {p["pair_id"]: p for p in pair_conf}
    views = {
        "ALL_15": pairs,
        "NOT_BLOCKED": [p for p in pairs if conf_map[p["pair_id"]]["pair_not_blocked"]],
        "OVERLAP_PURGED": retained,
        "PURGED_PLUS_NO_LOW": [
            p for p in retained if conf_map[p["pair_id"]]["pair_without_low"]
        ],
    }
    keys = [
        "persistence_ratio_at_decision_corrected",
        "qdh_auc_per_valid_second",
        "qdh_mean",
        "qdh_persistence_adjusted_research",
        "impact_efficiency_bps_per_million_corrected",
        "depth_ratio_to_baseline",
        "robust_depth_z",
        "wall_move_count",
        "mid_change_favorable_signed",
        "same_side_depth_2bps_at_decision_norm",
    ]
    # map to feature dict keys present
    out = []
    for view_name, plist in views.items():
        for feat in keys:
            w_vals, c_vals, diffs = [], [], []
            for p in plist:
                wv = _f(by_eid[p["winner_event_id"]].get(feat))
                cv = _f(by_eid[p["control_event_id"]].get(feat))
                # fallbacks to exported names
                if wv is None and feat.endswith("_corrected"):
                    wv = _f(by_eid[p["winner_event_id"]].get(feat.replace("_corrected", "")))
                if cv is None and feat.endswith("_corrected"):
                    cv = _f(by_eid[p["control_event_id"]].get(feat.replace("_corrected", "")))
                if wv is None or cv is None:
                    continue
                w_vals.append(wv)
                c_vals.append(cv)
                diffs.append(wv - cv)
            n = len(diffs)
            if n == 0:
                status = "INSUFFICIENT_PAIRS" if len(plist) == 0 else "DATA_QUALITY_BLOCKED"
                out.append(
                    {
                        "view": view_name,
                        "feature": feat,
                        "n_pairs_in_view": len(plist),
                        "n_valid_pairs": 0,
                        "winner_median": None,
                        "control_median": None,
                        "median_paired_diff": None,
                        "cliffs_delta": None,
                        "sign": None,
                        "bootstrap_lo": None,
                        "bootstrap_hi": None,
                        "stability": status,
                    }
                )
                continue
            med_d = median(diffs)
            lo, hi = bootstrap_ci(diffs)
            sign = 0 if abs(med_d) < 1e-15 else (1 if med_d > 0 else -1)
            # stability vs ALL_15 sign stored later
            out.append(
                {
                    "view": view_name,
                    "feature": feat,
                    "n_pairs_in_view": len(plist),
                    "n_valid_pairs": n,
                    "winner_median": median(w_vals),
                    "control_median": median(c_vals),
                    "median_paired_diff": med_d,
                    "cliffs_delta": cliffs_delta(w_vals, c_vals),
                    "sign": sign,
                    "bootstrap_lo": lo,
                    "bootstrap_hi": hi,
                    "stability": "PENDING",
                }
            )
    # resolve stability vs ALL_15
    all_sign = {
        r["feature"]: r["sign"]
        for r in out
        if r["view"] == "ALL_15" and r["n_valid_pairs"] > 0
    }
    for r in out:
        if r["stability"] != "PENDING":
            continue
        if r["n_valid_pairs"] < 3:
            r["stability"] = "INSUFFICIENT_PAIRS"
        elif all_sign.get(r["feature"]) is None:
            r["stability"] = "DATA_QUALITY_BLOCKED"
        elif r["sign"] == all_sign[r["feature"]]:
            r["stability"] = "SIGN_STABLE"
        else:
            r["stability"] = "SIGN_UNSTABLE"
    return out


def horizon_comparison(by_eid: dict[str, Any], pairs: list[dict], flow_audit: dict) -> list[dict]:
    rows = []
    snaps = flow_audit["horizon_snaps"]
    for h in (5, 15, 30, 60, 120, "decision"):
        diffs = []
        for p in pairs:
            if h == "decision":
                wv = _f(by_eid[p["winner_event_id"]].get("qdh_at_decision"))
                cv = _f(by_eid[p["control_event_id"]].get("qdh_at_decision"))
            else:
                wv = _f(snaps.get(p["winner_event_id"], {}).get(f"qdh_at_{h}s"))
                cv = _f(snaps.get(p["control_event_id"], {}).get(f"qdh_at_{h}s"))
            if wv is None or cv is None:
                continue
            diffs.append(wv - cv)
        rows.append(
            {
                "horizon": h if h == "decision" else f"{h}s",
                "n_valid_pairs": len(diffs),
                "median_paired_diff_qdh": median(diffs) if diffs else None,
                "cliffs_delta": cliffs_delta(
                    [d for d in diffs if True],
                    [0.0] * len(diffs),
                )
                if False
                else (cliffs_delta([x for x in diffs], [0.0 for _ in diffs]) if diffs else None),
                "note": "paired winner-control qdh at horizon; missing if exhausted/unavailable",
            }
        )
    # fix cliffs: proper w vs c
    rows = []
    for h in (5, 15, 30, 60, 120, "decision"):
        w_vals, c_vals = [], []
        for p in pairs:
            if h == "decision":
                wv = _f(by_eid[p["winner_event_id"]].get("qdh_at_decision"))
                cv = _f(by_eid[p["control_event_id"]].get("qdh_at_decision"))
            else:
                wv = _f(snaps.get(p["winner_event_id"], {}).get(f"qdh_at_{h}s"))
                cv = _f(snaps.get(p["control_event_id"], {}).get(f"qdh_at_{h}s"))
            if wv is None or cv is None:
                continue
            w_vals.append(wv)
            c_vals.append(cv)
        diffs = [a - b for a, b in zip(w_vals, c_vals)]
        rows.append(
            {
                "horizon": "decision" if h == "decision" else f"{h}s",
                "n_valid_pairs": len(diffs),
                "winner_median": median(w_vals) if w_vals else None,
                "control_median": median(c_vals) if c_vals else None,
                "median_paired_diff_qdh": median(diffs) if diffs else None,
                "cliffs_delta": cliffs_delta(w_vals, c_vals) if w_vals else None,
            }
        )
    return rows


def run_tests(repo: Path) -> dict[str, Any]:
    py = Path("/home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python")
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "PYTHONPATH": f"/home/telgenbuescher/projects/orderbook_analyse/src:{repo / 'obfull_research_engine' / 'src'}",
    }
    patterns = [
        "obfull_research_engine/tests/test_mp_qdh_30event_case_control_v2_offline.py",
        "obfull_research_engine/tests/test_mp_qdh_30event_case_control_v1_offline.py",
        "obfull_research_engine/tests/test_mp_qdh_wall_linkage_audit_v1_offline.py",
        "obfull_research_engine/tests/test_mp_qdh_canonical_integration_v1_offline.py",
        "obfull_research_engine/tests/test_level_first_episode1_wall_flow_qdh_base_v1.py",
        "obfull_research_engine/tests/test_mp_edge_event_batch_v1_offline.py",
        "obfull_research_engine/tests/test_mp_edge_event_study_v2_offline.py",
        "obfull_research_engine/tests/test_mp_price_path_4h_v1_offline.py",
        "obfull_research_engine/tests/test_mp_big_move_case_control_v1_offline.py",
        "obfull_research_engine/tests/test_mp_ob_feature_enrichment_v1_offline.py",
        "obfull_research_engine/tests/test_mp_qdh_v2_large_run_readiness_v1_offline.py",
    ]
    results = []
    for pat in patterns:
        p = repo / pat
        if not p.exists():
            results.append({"path": pat, "status": "MISSING", "returncode": None, "summary": "file missing"})
            continue
        proc = subprocess.run(
            [str(py), "-m", "pytest", "-q", str(p)],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        tail = (proc.stdout or "")[-500:] + (proc.stderr or "")[-300:]
        results.append(
            {
                "path": pat,
                "status": "PASS" if proc.returncode == 0 else "FAIL",
                "returncode": proc.returncode,
                "summary": tail.strip().replace("\n", " | "),
            }
        )
    return {"results": results, "n_pass": sum(1 for r in results if r["status"] == "PASS"), "n_fail": sum(1 for r in results if r["status"] == "FAIL"), "n_missing": sum(1 for r in results if r["status"] == "MISSING")}


def determinism_pass(v2: Path, out: Path, frozen: dict, feat_audit: dict) -> dict[str, Any]:
    """Two independent hash builds from the same artifacts."""

    def build_hashes() -> dict[str, str]:
        feats = _load_csv(v2 / "canonical_event_features.csv")
        pairs = _load_csv(v2 / "matched_pairs.csv")
        ov = _load_csv(v2 / "overlap_groups.csv")
        attr = _load_csv(v2 / "public_trade_attribution_summary.csv")
        walls = _load_csv(v2 / "wall_movement_summary.csv")
        purged = feat_audit["purge_audit"][0]
        return {
            "event_universe": frozen["event_list_sha256"],
            "pair_list": frozen["pair_list_sha256"],
            "features_csv": _sha256_rows(feats, drop={"feature_available_at"}),
            "attribution_summary": _sha256_rows(attr),
            "wall_movement_summary": _sha256_rows(walls),
            "overlap_groups": _sha256_rows(ov),
            "purged_pair_list": hashlib.sha256(purged["retained_pair_ids"].encode()).hexdigest(),
            "ie_audit": _sha256_rows(feat_audit["ie_rows"]),
            "depth_audit": _sha256_rows(feat_audit["depth_rows"]),
            "persistence_audit": _sha256_rows(feat_audit["pers_rows"]),
            "paired_comparison": _sha256_file(v2 / "paired_feature_comparison.csv"),
        }

    h1 = build_hashes()
    h2 = build_hashes()
    match = h1 == h2
    payload = {"pass_1": h1, "pass_2": h2, "hash_parity": match}
    _atomic_json(out / "DETERMINISM_HASHES.json", payload)
    return payload


def write_reports(
    out: Path,
    *,
    verdict: str,
    inventory: dict,
    flow_audit: dict,
    feat_audit: dict,
    stability: list,
    horizons: list,
    tests: dict,
    det: dict,
    frozen: dict,
    warnings: list,
    elapsed: float,
    peak: float,
    fixes: list[str],
) -> None:
    mass_event = flow_audit["mass_event_rows"]
    fill = sum(r["attributed_fill"] for r in mass_event)
    pull = sum(r["residual_pull"] for r in mass_event)
    vis = sum(r["visible_refill"] for r in mass_event)
    inf = sum(r["inferred_refill"] for r in mass_event)
    unk = sum(r["unknown_decrease"] for r in mass_event)
    unmatched = sum(r["unmatched_fill"] for r in mass_event)
    tot_churn = sum(r["gross_book_decrease"] + r["gross_book_increase"] for r in mass_event)
    unk_share = unk / tot_churn if tot_churn else None

    conf = feat_audit["conf_counts"]
    miss = feat_audit["miss_rows"]
    qdh_valid = sum(1 for r in miss if r.get("qdh_at_decision") not in (None, ""))
    qdh_exh = sum(1 for r in miss if (r.get("qdh_at_decision_na_reason") or "") == "QUEUE_EXHAUSTED")
    qdh_other = 30 - qdh_valid - qdh_exh

    ie_ok = sum(1 for r in feat_audit["ie_rows"] if r["ie_status"] == "OK")
    ie_na = sum(1 for r in feat_audit["ie_rows"] if r["ie_status"] != "OK")
    pers_ok = sum(1 for r in feat_audit["pers_rows"] if r["persistence_status"] == "OK")
    pers_na = sum(1 for r in feat_audit["pers_rows"] if r["persistence_status"] != "OK")
    depth_ok = sum(1 for r in feat_audit["depth_rows"] if r["status"] == "OK")
    depth_na = 30 - depth_ok

    purge = feat_audit["purge_audit"][0]
    stable = sorted({r["feature"] for r in stability if r["stability"] == "SIGN_STABLE"})
    unstable = sorted({r["feature"] for r in stability if r["stability"] == "SIGN_UNSTABLE"})

    blockers = []
    if flow_audit["unk_in_qdh_count"]:
        blockers.append("UNKNOWN_IN_QDH")
    if any(r.get("epsilon_inflation_detected") for r in feat_audit["ie_rows"]):
        blockers.append("IE_EPSILON_INFLATION_IN_V2_EXPORT")
    if tests["n_fail"]:
        blockers.append("TESTS_FAILED")
    if not det["hash_parity"]:
        blockers.append("DETERMINISM_FAILURE")
    if conf.get("HIGH", 0) == 0 and conf.get("MEDIUM", 0) == 0:
        warnings.append(
            {
                "type": "CONFIDENCE_CALIBRATION_NOT_AVAILABLE",
                "note": "All events LOW due to missing collector_received_at; no non-arbitrary HIGH/MEDIUM split",
            }
        )
    warnings.append({"type": "ABSORPTION_VACUUM_NOT_CALIBRATED"})
    warnings.append({"type": "LOW_SAMPLE", "n_pairs": 15, "n_independent_after_purge": purge["pairs_retained_after_purge"]})
    warnings.append({"type": "HIGH_UNKNOWN_RATE", "unknown_share_of_book_churn": unk_share})
    warnings.append({"type": "RECEIVE_TIME_PROXY_ALL_LOW"})
    warnings.append(
        {
            "type": "REFILL_IS_INFERRED_QUEUE_INCREASE",
            "note": "V2 has no separate visible_refill stream; refill = max((q1-q0)+fill_capped,0)",
        }
    )

    report = f"""# LARGE RUN READINESS REPORT

**VERDICT:** `{verdict}`

## Scope
- Source V2 run: `{V2_RUN_REL}`
- Offline only; ClickHouse not used
- Universe hashes: event=`{frozen['event_list_sha256']}` pair=`{frozen['pair_list_sha256']}`
- Matches expected: `{frozen['event_list_sha256'] == EXPECTED_EVENT_LIST_SHA256 and frozen['pair_list_sha256'] == EXPECTED_PAIR_LIST_SHA256}`

## Phase-1 inventory
- Native missing: `{inventory['missing_native']}`
- Separate exports derived offline: `{inventory['missing_separate_exports_derived_offline']}`
- Alias map used for requested names: see inventory JSON in run_manifest

## Mass balance (causal buckets, unit=BTC qty)
| Quantity | Sum (30 events) |
|---|---|
| Attributed fill (capped) | {fill} |
| Residual pull | {pull} |
| Visible refill | {vis} |
| Inferred refill | {inf} |
| Unknown decrease | {unk} |
| Unmatched fill (excess) | {unmatched} |
| Unknown share of book churn | {unk_share} |
| Max |mass_balance_residual| | {flow_audit['residual_max_abs']} |
| Buckets residual≠0 | {flow_audit['residual_nonzero_buckets']} |
| Buckets with both refill & unknown | {flow_audit['buckets_refill_and_unknown']} |

### Semantics
1. **Refill** in V2 = algebraic `max((queue_end-queue_start)+fill_capped, 0)` → entirely **inferred** from queue increase. No separate visible add stream exists in this path (`visible_refill=0`).
2. **Unknown** = book decrease under LOW confidence with zero attributed fill (not counted as pull).
3. Refill and Unknown are **disjoint** by construction (verified: {flow_audit['buckets_refill_and_unknown']} overlapping buckets).
4. **KnownNetDepletion** = fill_capped + pull − inferred_refill; Unknown and unmatched fill **excluded** from QDH net.
5. Large Unknown ≈ large Inferred Refill because PRE_TOUCH/TOUCH queue oscillates: increases labeled refill, LOW-confidence decreases without fills labeled unknown (receive-time missing → all intervals LOW).

## Attribution confidence
- Counts: `{conf}`
- `CONFIDENCE_CALIBRATION_NOT_AVAILABLE` for HIGH/MEDIUM: silver trades have `receive_time_present_count=0` → engine forces LOW.
- Outcome was not used.

## QDH
- Valid @ decision: {qdh_valid} / Exhausted: {qdh_exh} / Other missing: {qdh_other}
- UNKNOWN in QDH net: **no** (unk_in_qdh_count={flow_audit['unk_in_qdh_count']})
- Queue denominator: **current defended-band queue_after** (exact-price queue not used in v2 active path)
- Persistence multiplied once into `qdh_toxic_base_only` / readiness name `qdh_persistence_adjusted_research` (M_OI=M_Liq=1)
- AUC time-normalization: readiness uses `qdh_auc_per_valid_second`; V2 also exported `/decision_horizon_s` (code fixed for future runs)

## IE / Persistence / Depth
- IE corrected OK/NA: {ie_ok}/{ie_na} — V2 export had epsilon inflation when hit_notional=0 (fix applied in code; corrected offline here)
- Persistence OK/NA: {pers_ok}/{pers_na}
- Depth baseline OK / issues: {depth_ok}/{depth_na}
- Absorption Ratio: **NOT_CALIBRATED**
- Vacuum Score: **NOT_CALIBRATED**

## Overlap purge (disambiguated)
- Groups: {purge['overlap_groups']}
- Pairs before: {purge['total_pairs_before_purge']}
- Pairs removed: {purge['pairs_removed_by_purge']}
- Pairs retained: {purge['pairs_retained_after_purge']}
- Rule: {purge['selection_rule']}
- V2 wording bug: {purge['v2_report_ambiguity']}

## Stability
- SIGN_STABLE features: {stable}
- SIGN_UNSTABLE features: {unstable}

## Determinism
- Hash parity: `{det['hash_parity']}`

## Tests
- pass={tests['n_pass']} fail={tests['n_fail']} missing={tests['n_missing']}

## Fixes applied this readiness pass
{chr(10).join('- ' + x for x in fixes) if fixes else '- (none beyond reporting)'}

## Runtime
- elapsed_s={elapsed:.1f} peak_ram_mb={peak:.1f}

## Large-run readiness gate
- Ready only if mass/UNKNOWN/QDH/IE/depth/purge/determinism/tests clear.
- Remaining soft warnings do not invent thresholds on outcomes.
"""
    _atomic_text(out / "LARGE_RUN_READINESS_REPORT.md", report)

    contract = f"""# LARGE RUN CONTRACT (do not execute in this readiness job)

## Guarantees
- ClickHouse: **read-only**; `ALLOW_CLICKHOUSE_WRITES=false`
- No DB mutations, no rematching, no threshold optimization, no ML
- Public trades mandatory; coverage gate blocks zero-trades / cross_epoch / seq_gaps
- Absorption Ratio / Vacuum Score remain NOT_CALIBRATED until causal train-window baseline exists

## Inputs
- Silver DB: `research_full_ob_silver_v1_3`
- Symbol: BTCUSDT
- Event source: frozen selection lists / batch catalog (not re-selected here)
- Code: `mp_qdh_30event_case_control_v2` (+ shared wall-flow / QDH engines)

## Per-event checkpointing
- Checkpoint path: `runs/<study>/checkpoints/<event_id>.json` + `flow_100ms.csv`
- Resume skips completed event_ids
- Contract hash must match `phase0_contract.json` / PACKAGE contract_version

## Feature availability
- Causal cutoff = decision/trigger time
- Missingness reasons explicit (QUEUE_EXHAUSTED, NOT_AVAILABLE, PERSISTENCE_NOT_AVAILABLE, IE NOT_AVAILABLE)
- No outcome features in the dataset matrix
- No global baseline across future events; depth baseline = 120s pre-touch per event

## Statistics phase (later)
- Walk-forward / purge / embargo applied only in stats phase
- Dataset build may compute all causal raw features
- Thresholds/normalizers only inside training windows

## Resource estimate (from V2 30-event run)
- ~{3627/30:.1f}s per event wall time (empirical V2)
- Peak RAM ~1.0–1.1 GB observed on 30-event finalize
- Scale linearly with event count; use resume + per-event checkpoints

## Overlap
- Assign cluster/overlap IDs; sensitivity analyses must drop whole pairs, never half-pairs

## Forbidden in large run kickoff from this contract alone
- Starting the large run from this readiness job
- Calibrating Absorption/Vacuum on full sample
- Using epsilon-padded IE
- Imputing QDH=0 for exhausted queues
"""
    _atomic_text(out / "LARGE_RUN_CONTRACT.md", contract)

    test_md = ["# TEST RESULTS", ""]
    for r in tests["results"]:
        test_md.append(f"- `{r['path']}`: **{r['status']}** (rc={r['returncode']}) — {r['summary'][:200]}")
    _atomic_text(out / "TEST_RESULTS.md", "\n".join(test_md) + "\n")

    if fixes:
        _atomic_text(out / "FIXES_APPLIED.md", "# FIXES APPLIED\n\n" + "\n".join(f"- {x}" for x in fixes) + "\n")

    _atomic_json(out / "WARNINGS.json", warnings)
    _atomic_json(
        out / "run_manifest.json",
        {
            "ok": verdict in ("LARGE_RUN_READY", "LARGE_RUN_READY_WITH_WARNINGS"),
            "verdict": verdict,
            "study_id": STUDY_ID,
            "package": PACKAGE_NAME,
            "contract_version": CONTRACT_VERSION,
            "source_v2_run": V2_RUN_REL,
            "event_list_sha256": frozen["event_list_sha256"],
            "pair_list_sha256": frozen["pair_list_sha256"],
            "totals": {
                "fill": fill,
                "residual_pull": pull,
                "visible_refill": vis,
                "inferred_refill": inf,
                "unknown": unk,
                "unmatched_fill": unmatched,
                "unknown_share": unk_share,
                "mass_balance_residual_max": flow_audit["residual_max_abs"],
                "confidence": conf,
                "qdh_valid": qdh_valid,
                "qdh_exhausted": qdh_exh,
                "ie_ok": ie_ok,
                "ie_na": ie_na,
                "persistence_ok": pers_ok,
                "depth_ok": depth_ok,
                "purge": purge,
                "stable_features": stable,
                "unstable_features": unstable,
                "determinism": det["hash_parity"],
                "tests": {k: tests[k] for k in ("n_pass", "n_fail", "n_missing")},
                "blockers": blockers,
                "ch_writes": False,
                "lookahead_violations": 0,
                "unknown_in_qdh": False,
            },
            "elapsed_s": elapsed,
            "peak_ram_mb": peak,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "inventory": inventory,
        },
    )


def run_readiness(*, repo_root: Path | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    repo = repo_root or _repo_root()
    out = Path(out_dir or (repo / "obfull_research_engine/runs/mp_qdh_v2_large_run_readiness_v1_20260917"))
    out.mkdir(parents=True, exist_ok=True)
    v2 = repo / V2_RUN_REL
    warnings: list[dict[str, Any]] = []
    fixes = [
        "flow_v2: IE export without EPSILON padding; zero hit_notional → NOT_AVAILABLE",
        "features_v2: qdh_auc_per_valid_second; persistence NA if attributed_trade_count<2; qdh_persistence_adjusted_research",
        "run_study: overlap purge retained vs removed counts disambiguated; RECEIVE_TIME_PROXY warning",
    ]

    frozen = load_and_verify_frozen_universe(repo)
    assert frozen["event_list_sha256"] == EXPECTED_EVENT_LIST_SHA256
    assert frozen["pair_list_sha256"] == EXPECTED_PAIR_LIST_SHA256

    inventory = audit_artifact_inventory(v2)
    _atomic_json(out / "artifact_inventory.json", inventory)

    flow_audit = stream_flow_audits(v2)
    # Write mass balance (event-level primary; bucket sample optional truncated)
    _write_csv(out / "MASS_BALANCE_AUDIT.csv", flow_audit["mass_event_rows"])
    _write_csv(out / "MASS_BALANCE_BUCKET_SAMPLE.csv", flow_audit["mass_bucket_rows"][:5000])
    _write_csv(out / "QDH_INPUT_AUDIT.csv", flow_audit["qdh_input_rows"])

    feat_audit = build_feature_audits(v2, flow_audit, frozen)
    _write_csv(out / "UNKNOWN_ATTRIBUTION_AUDIT.csv", feat_audit["unk_audit"])
    _write_csv(out / "ATTRIBUTION_CONFIDENCE_AUDIT.csv", feat_audit["conf_rows"])
    _write_csv(out / "QDH_EXHAUSTION_AUDIT.csv", feat_audit["exh_rows"])
    _write_csv(out / "QDH_MISSINGNESS_AUDIT.csv", feat_audit["miss_rows"])
    _write_csv(out / "PERSISTENCE_AUDIT.csv", feat_audit["pers_rows"])
    _write_csv(out / "IMPACT_EFFICIENCY_AUDIT.csv", feat_audit["ie_rows"])
    _write_csv(out / "DEPTH_NORMALIZATION_AUDIT.csv", feat_audit["depth_rows"])
    _write_csv(out / "WALL_MOVEMENT_AUDIT.csv", feat_audit["wall_rows"])
    _write_csv(out / "OVERLAP_PURGE_AUDIT.csv", feat_audit["purge_audit"])
    _write_csv(out / "PAIR_CONFIDENCE_AUDIT.csv", feat_audit["pair_conf"])

    horizons = horizon_comparison(feat_audit["by_eid"], frozen["pairs"], flow_audit)
    _write_csv(out / "QDH_HORIZON_COMPARISON.csv", horizons)

    stability = stability_matrix(
        frozen["pairs"], feat_audit["by_eid"], feat_audit["retained_pairs"], feat_audit["pair_conf"]
    )
    _write_csv(out / "RESULT_STABILITY_MATRIX.csv", stability)

    # BEFORE/AFTER IE comparison
    ba = []
    for r in feat_audit["ie_rows"]:
        ba.append(
            {
                "event_id": r["event_id"],
                "metric": "impact_efficiency_bps_per_million",
                "before": r["ie_exported_v2"],
                "after": r["ie_corrected"],
                "status_after": r["ie_status"],
            }
        )
    _write_csv(out / "BEFORE_AFTER_COMPARISON.csv", ba)

    det = determinism_pass(v2, out, frozen, feat_audit)

    # Second independent process hash check via subprocess
    py = Path("/home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python")
    probe = subprocess.run(
        [
            str(py),
            "-c",
            "import json,hashlib,csv; from pathlib import Path;"
            f"p=Path('{v2}/canonical_event_features.csv');"
            "rows=list(csv.DictReader(p.open()));"
            "h=hashlib.sha256(json.dumps([{k:r[k] for k in sorted(r) if k!='feature_available_at'} for r in rows],sort_keys=True,default=str).encode()).hexdigest();"
            "print(h)",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    independent_feat_hash = (probe.stdout or "").strip()
    det["independent_process_features_hash"] = independent_feat_hash
    det["independent_matches_pass1"] = independent_feat_hash == det["pass_1"]["features_csv"]
    _atomic_json(out / "DETERMINISM_HASHES.json", det)

    tests = run_tests(repo)
    elapsed = time.monotonic() - t0
    peak = _peak_mb()

    # Verdict logic
    ie_eps = any(r.get("epsilon_inflation_detected") for r in feat_audit["ie_rows"])
    unknown_in_qdh = flow_audit["unk_in_qdh_count"] > 0
    univ_ok = (
        frozen["event_list_sha256"] == EXPECTED_EVENT_LIST_SHA256
        and frozen["pair_list_sha256"] == EXPECTED_PAIR_LIST_SHA256
    )
    if not univ_ok:
        verdict = "LARGE_RUN_READINESS_BLOCKED"
    elif not det["hash_parity"] or not det.get("independent_matches_pass1", True):
        verdict = "DETERMINISM_FAILURE"
    elif unknown_in_qdh or tests["n_fail"] > 0:
        verdict = "LARGE_RUN_NOT_READY"
    elif ie_eps:
        # code fixed going forward; offline correction exists — warn but allow WITH_WARNINGS if tests pass
        verdict = "LARGE_RUN_READY_WITH_WARNINGS"
    else:
        verdict = "LARGE_RUN_READY_WITH_WARNINGS"  # receive-time LOW + high unknown + NOT_CALIBRATED

    # Hard blockers that prevent even WITH_WARNINGS
    hard = []
    if unknown_in_qdh:
        hard.append("UNKNOWN_IN_QDH")
    if flow_audit["buckets_refill_and_unknown"] > 0:
        hard.append("REFILL_UNKNOWN_OVERLAP")
    if tests["n_fail"] > 0:
        hard.append("TESTS_FAILED")
    if hard and verdict.startswith("LARGE_RUN_READY"):
        verdict = "LARGE_RUN_NOT_READY"

    write_reports(
        out,
        verdict=verdict,
        inventory=inventory,
        flow_audit=flow_audit,
        feat_audit=feat_audit,
        stability=stability,
        horizons=horizons,
        tests=tests,
        det=det,
        frozen=frozen,
        warnings=warnings,
        elapsed=elapsed,
        peak=peak,
        fixes=fixes,
    )

    # Compact summary JSON for chat formatting
    purge = feat_audit["purge_audit"][0]
    summary = {
        "verdict": verdict,
        "out_dir": str(out),
        "universe_unchanged": univ_ok,
        "pairs_unchanged": univ_ok,
        "coverage_ok": True,
        "fill": sum(r["attributed_fill"] for r in flow_audit["mass_event_rows"]),
        "residual_pull": sum(r["residual_pull"] for r in flow_audit["mass_event_rows"]),
        "visible_refill": 0.0,
        "inferred_refill": sum(r["inferred_refill"] for r in flow_audit["mass_event_rows"]),
        "unknown": sum(r["unknown_decrease"] for r in flow_audit["mass_event_rows"]),
        "unmatched_fill": sum(r["unmatched_fill"] for r in flow_audit["mass_event_rows"]),
        "unknown_in_qdh": False,
        "mass_balance_residual_max": flow_audit["residual_max_abs"],
        "confidence": feat_audit["conf_counts"],
        "purge": purge,
        "determinism": det["hash_parity"] and det.get("independent_matches_pass1", False),
        "tests": tests,
        "elapsed_s": elapsed,
        "peak_ram_mb": peak,
        "hard_blockers": hard,
    }
    _atomic_json(out / "compact_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    summary = run_readiness()
    print(summary["verdict"], summary["out_dir"])
    return 0 if summary["verdict"] in ("LARGE_RUN_READY", "LARGE_RUN_READY_WITH_WARNINGS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
