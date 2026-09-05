"""Frozen HIGH∩ACCEPTED sample expansion — no refit, no freeze regeneration."""

from __future__ import annotations

import json
import math
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import (
    aggregate_window,
    build_second_buckets,
    side_vwap,
)
from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.episodes import discover_episodes
from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.cohort_eval import (
    information_stack_label,
    sample_size_label,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    build_causal_edges_from_samples,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_disambiguation import (
    DisambiguationThresholds,
    select_disambiguated_match,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import (
    JoinThresholds,
    apply_join_to_event,
    evaluate_candidates,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    input_from_aef_compression,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import (
    FreezeViolation,
    verify_freeze,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.pipeline import process_event
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_coverage import (
    build_multi_day_coverage,
    chronological_eligible_hours,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.state_aligned_outcomes import (
    attach_forward_outcomes_for_event,
    build_decision_timestamps,
    first_acceptance_available_ts,
    price_at,
)

PRIOR_FREEZE_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_edge_forward_outcome_evaluation_v1"
)
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_sample_expansion_v1"
)

TARGET_HIGH_ACCEPTED_ANY = 30
# Evaluation horizons: frozen set + explicit 10m (600s). Do NOT mutate freeze_v1.py.
EXPANSION_HORIZONS_S = (10, 30, 60, 180, 300, 600, 900, 1800, 3600)

NO_FIT_FLAGS = {
    "outcome_used_for_matching": False,
    "outcome_used_for_thresholds": False,
    "outcome_used_for_state_definition": False,
    "outcome_used_for_sample_selection": False,
}

# Fixed before outcomes — acceptance_aligned direction.
ACCEPTANCE_ALIGN_SIGN = {
    "ACCEPTED_ABOVE": 1,  # LONG / bullish
    "ACCEPTED_BELOW": -1,  # SHORT / bearish
}


class FrozenBundleTampered(RuntimeError):
    pass


def _verify_or_tamper(freeze_dir: Path, label: str) -> dict[str, Any]:
    try:
        return {**verify_freeze(freeze_dir), "label": label}
    except FreezeViolation as e:
        raise FrozenBundleTampered(f"FROZEN_BUNDLE_TAMPERED ({label}): {e}") from e


def _copy_freeze_manifests(src: Path, dst: Path) -> None:
    ensure_outdir(dst)
    for name in (
        "frozen_contract.json",
        "frozen_thresholds.json",
        "frozen_rule_manifest.json",
        "frozen_source_manifest.json",
        "frozen_hashes.json",
    ):
        p = src / name
        if not p.is_file():
            raise FrozenBundleTampered(f"missing freeze file {p}")
        (dst / name).write_bytes(p.read_bytes())


def _is_high_accepted(feat: dict[str, Any]) -> bool:
    return feat.get("edge_match_confidence_class") == "HIGH" and feat.get(
        "final_acceptance_state"
    ) in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}


def _acceptance_any(feat: dict[str, Any]) -> bool:
    return feat.get("final_acceptance_state") in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}


def _finite(xs: list[float]) -> list[float]:
    return [x for x in xs if isinstance(x, (int, float)) and math.isfinite(x)]


def _quantile(xs: list[float], q: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = (len(ys) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    return ys[lo] * (hi - pos) + ys[hi] * (pos - lo)


def _summarize(vals: list[float], n_total: int) -> dict[str, Any]:
    xs = _finite(vals)
    n = len(xs)
    mean = sum(xs) / n if n else None
    return {
        "n": n_total,
        "n_complete": n,
        "mean": mean,
        "median": _quantile(xs, 0.5),
        "positive_fraction": (sum(1 for x in xs if x > 0) / n) if n else None,
        "q25": _quantile(xs, 0.25),
        "q75": _quantile(xs, 0.75),
        "sample_size_label": sample_size_label(n),
    }


def _bootstrap_median_ci(vals: list[float], n_boot: int = 500, seed: int = 42) -> dict[str, Any]:
    xs = _finite(vals)
    if len(xs) < 10:
        return {"ok": False, "reason": "n<10", "ci95_low": None, "ci95_high": None}
    import random

    rng = random.Random(seed)
    meds = []
    for _ in range(n_boot):
        sample = [xs[rng.randrange(len(xs))] for _ in range(len(xs))]
        meds.append(_quantile(sample, 0.5))
    meds.sort()
    lo = meds[int(0.025 * (len(meds) - 1))]
    hi = meds[int(0.975 * (len(meds) - 1))]
    return {"ok": True, "ci95_low": lo, "ci95_high": hi, "n_boot": n_boot}


def _process_hour(
    *,
    hour_start: str,
    raw_root: Path,
    cfg: TrapAcceptConfig,
    thr: JoinThresholds,
    dthr: DisambiguationThresholds,
    thr_accept: JoinThresholds,
    query_log: list[dict[str, Any]],
    seen_event_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Process one UTC hour for both symbols. Returns features, outcomes, decision_ts, meta."""
    ht = parse_utc(hour_start)
    event_start = ht
    event_end = ht + timedelta(hours=1)
    ob_start = ht - timedelta(hours=1)
    data_end = event_end + timedelta(seconds=3600)  # 60m beyond hour end
    symbols = ("BTCUSDT", "DOGEUSDT")

    samples_by, seg_meta, n_ok = load_ob200_samples(
        symbols=symbols, start=ob_start, end=event_end, raw_root=raw_root, sample_ms=250
    )
    edges, _, _ = build_causal_edges_from_samples(samples_by)
    features: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    dts_rows: list[dict[str, Any]] = []
    n_skip_dup = 0

    if n_ok == 0:
        return [], [], [], {"hour_start": hour_start, "n_ok": 0, "n_events": 0, "n_high_accepted": 0}

    for symbol in symbols:
        trades, _ = load_trades_clickhouse(
            symbol=symbol, start=event_start, end=data_end, query_log=query_log
        )
        if not trades:
            continue
        buckets = build_second_buckets(trades)
        disc = discover_episodes(
            symbol=symbol,
            trades=trades,
            buckets=buckets,
            start=event_start,
            end=event_end,
            cfg=cfg.aef_config(),
        )
        # All allowed compressions — no smoke [:25] cap (not part of freeze; avoids truncation bias)
        allowed = [c for c in disc["compressions"] if c.get("allowed")]
        sym_edges = [e for e in edges if e.symbol == symbol]
        samples = samples_by.get(symbol) or []

        for row in allowed:
            ev = input_from_aef_compression(row, source=f"sample_expansion:{symbol}")
            if ev.event_id in seen_event_ids:
                n_skip_dup += 1
                continue
            side = aggressor_side(ev.direction)
            flow = aggregate_window(buckets, ev.flow_start_ts, ev.flow_end_ts)
            vwap = side_vwap(trades, ev.flow_start_ts, ev.flow_end_ts, side)
            cands = evaluate_candidates(
                ev,
                sym_edges,
                samples,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=thr,
            )
            join, _, _ = select_disambiguated_match(
                ev,
                cands,
                trades=trades,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=thr,
                dthr=dthr,
            )
            ev2 = apply_join_to_event(deepcopy(ev), join, thr_accept)
            feat, _ = process_event(ev2, buckets=buckets, trades=trades, cfg=cfg, data_end=data_end)
            feat["information_stack"] = information_stack_label(feat)
            feat["source_hour"] = hour_start
            feat["utc_day"] = hour_start[:10]
            seen_event_ids.add(ev.event_id)
            features.append(feat)
            dts_rows.append(
                build_decision_timestamps(feat, ev.flow_start_ts, ev.flow_end_ts, ev.decision_ts)
            )
            outs = attach_forward_outcomes_for_event(
                feat=feat,
                buckets=buckets,
                data_end=data_end,
                flow_start=ev.flow_start_ts,
                flow_end=ev.flow_end_ts,
                decision_ts=ev.decision_ts,
                horizons=EXPANSION_HORIZONS_S,
            )
            # Enforce fixed acceptance_aligned for ACCEPTED_* (contract before outcomes)
            for o in outs:
                acc = feat.get("final_acceptance_state")
                sign = ACCEPTANCE_ALIGN_SIGN.get(acc) if acc else None
                if sign is not None and o.get("raw_return_bps") is not None:
                    o["acceptance_aligned_return_bps"] = float(o["raw_return_bps"]) * sign
                    o["acceptance_align_reason"] = (
                        "ACCEPTED_ABOVE→LONG" if sign > 0 else "ACCEPTED_BELOW→SHORT"
                    )
                o["source_hour"] = hour_start
                o["utc_day"] = hour_start[:10]
                o["information_stack"] = feat["information_stack"]
            outcomes.extend(outs)

    n_ha = sum(1 for f in features if _is_high_accepted(f))
    meta = {
        "hour_start": hour_start,
        "n_ok_segments": n_ok,
        "n_events": len(features),
        "n_high_accepted": n_ha,
        "n_skip_dup": n_skip_dup,
        "seg_meta_n": len(seg_meta),
    }
    return features, outcomes, dts_rows, meta


def _prefix_parity_checks(
    features: list[dict[str, Any]],
    *,
    raw_root: Path,
    cfg: TrapAcceptConfig,
    thr: JoinThresholds,
    dthr: DisambiguationThresholds,
    thr_accept: JoinThresholds,
    max_events: int = 20,
) -> dict[str, Any]:
    """Acceptance-first timestamp consistency on HIGH∩ACCEPTED sample.

    Events whose checkpoint scan is incomplete (UNKNOWN_DATA) are marked
    degraded but do not alone fail the suite if ≥80% of the sample is OK.
    """
    sample = [f for f in features if _is_high_accepted(f)][:max_events]
    checks = []
    for feat in sample:
        dts = parse_utc(feat["decision_ts"])
        acc_ts = first_acceptance_available_ts(feat, dts)
        # Fallback: flat acceptance_state_at_* fields
        if acc_ts is None:
            for sec in (5, 10, 30, 60):
                st = feat.get(f"acceptance_state_at_{sec}s")
                if st in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}:
                    acc_ts = dts + timedelta(seconds=sec)
                    break
        row = {
            "event_id": feat["event_id"],
            "acceptance_first_available_ts": iso_z(acc_ts) if acc_ts else None,
            "final_acceptance_state": feat.get("final_acceptance_state"),
            "edge_match_confidence_class": feat.get("edge_match_confidence_class"),
            "ok": acc_ts is not None
            and feat.get("final_acceptance_state") in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"},
        }
        checks.append(row)
    n_ok = sum(1 for c in checks if c["ok"])
    frac = (n_ok / len(checks)) if checks else 0.0
    return {
        "ok": frac >= 0.80,
        "frac_ok": frac,
        "n_checked": len(checks),
        "n_ok": n_ok,
        "checks": checks,
        "mode": "acceptance_ts_consistency_ge_80pct",
    }


def run_sample_expansion(
    *,
    output_dir: Path = DEFAULT_OUT,
    freeze_dir: Path = PRIOR_FREEZE_DIR,
    raw_root: Path = DEFAULT_RAW_ROOT,
    target_n: int = TARGET_HIGH_ACCEPTED_ANY,
    max_hours: Optional[int] = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)

    # --- Freeze: load existing, never regenerate ---
    _copy_freeze_manifests(freeze_dir, output_dir / "freeze_bundle")
    before = _verify_or_tamper(freeze_dir, "before")
    write_json(output_dir / "freeze_verification_before.json", before)

    cov_rows, cov_summary = build_multi_day_coverage(raw_root=raw_root)
    write_csv(output_dir / "coverage_inventory.csv", cov_rows)
    write_json(output_dir / "coverage_summary.json", cov_summary)

    eligible = chronological_eligible_hours(cov_rows)
    if max_hours is not None:
        eligible = eligible[:max_hours]

    excluded = [r for r in cov_rows if r["status"] != "ELIGIBLE"]
    write_csv(
        output_dir / "excluded_windows.csv",
        [
            {
                "hour_start": r["hour_start"],
                "status": r["status"],
                "reason": r["reason"],
                "exclusion_basis": "coverage_only_not_outcomes",
            }
            for r in excluded
        ],
    )

    if not eligible:
        after = _verify_or_tamper(freeze_dir, "after")
        write_json(output_dir / "freeze_verification_after.json", after)
        verdict = "FROZEN_HIGH_ACCEPTED_SAMPLE_EXPANSION_V1_INSUFFICIENT_COVERAGE"
        summary = {
            "verdict": verdict,
            "stop_reason": "COVERAGE_EXHAUSTED",
            **NO_FIT_FLAGS,
            "freeze_bundle_sha256_before": before.get("freeze_bundle_sha256"),
            "freeze_bundle_sha256_after": after.get("freeze_bundle_sha256"),
            "n_high_accepted_any": 0,
        }
        write_json(output_dir / "verdict.json", summary)
        write_json(output_dir / "SUMMARY.json", summary)
        return summary

    cfg = TrapAcceptConfig()
    thr = JoinThresholds()
    dthr = DisambiguationThresholds()
    thr_accept = JoinThresholds(accept_confidence=dthr.accept_confidence)
    query_log: list[dict[str, Any]] = []

    all_features: list[dict[str, Any]] = []
    all_outcomes: list[dict[str, Any]] = []
    all_dts: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    progress: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    stop_reason = "COVERAGE_EXHAUSTED"
    cum_ha = 0

    for i, hour in enumerate(eligible):
        print(f"expand hour {i+1}/{len(eligible)} {hour} cum_HIGH∩ACCEPTED={cum_ha}", flush=True)
        # Freeze check periodically
        if i % 10 == 0:
            _verify_or_tamper(freeze_dir, f"mid_{hour}")

        feats, outs, dts, meta = _process_hour(
            hour_start=hour,
            raw_root=raw_root,
            cfg=cfg,
            thr=thr,
            dthr=dthr,
            thr_accept=thr_accept,
            query_log=query_log,
            seen_event_ids=seen_ids,
        )
        all_features.extend(feats)
        all_outcomes.extend(outs)
        all_dts.extend(dts)
        cum_ha = sum(1 for f in all_features if _is_high_accepted(f))
        cum_above = sum(
            1
            for f in all_features
            if f.get("edge_match_confidence_class") == "HIGH"
            and f.get("final_acceptance_state") == "ACCEPTED_ABOVE"
        )
        cum_below = sum(
            1
            for f in all_features
            if f.get("edge_match_confidence_class") == "HIGH"
            and f.get("final_acceptance_state") == "ACCEPTED_BELOW"
        )
        day_c = Counter(f.get("utc_day") for f in all_features if _is_high_accepted(f))
        n_days = len(day_c)
        max_share = (max(day_c.values()) / cum_ha) if cum_ha else 0.0
        # Composition stop (not return-based): n>=target AND diversity gates for READY,
        # else continue until coverage exhausted. Never uses forward outcomes.
        dirs_ok = True
        if cum_above > 0:
            dirs_ok = dirs_ok and cum_above >= 5
        if cum_below > 0:
            dirs_ok = dirs_ok and cum_below >= 5
        if cum_above == 0 and cum_below == 0:
            dirs_ok = False
        composition_ready = (
            cum_ha >= target_n
            and n_days >= 3
            and max_share <= 0.50
            and dirs_ok
        )
        selected.append(
            {
                "hour_start": hour,
                "selection_order": i + 1,
                "selection_reason": "oldest_eligible_closed_hour_chronological",
                "n_events": meta["n_events"],
                "n_high_accepted_hour": meta["n_high_accepted"],
                "cumulative_high_accepted_any": cum_ha,
                "cumulative_days": n_days,
                "max_day_share": max_share,
                "outcome_used_for_selection": False,
            }
        )
        progress.append(
            {
                "step": i + 1,
                "hour_start": hour,
                "cumulative_events": len(all_features),
                "cumulative_high": sum(
                    1 for f in all_features if f.get("edge_match_confidence_class") == "HIGH"
                ),
                "cumulative_high_accepted_any": cum_ha,
                "cumulative_accepted_above": cum_above,
                "cumulative_accepted_below": cum_below,
                "cumulative_days": n_days,
                "max_day_share": max_share,
                "n_ge_target": cum_ha >= target_n,
                "composition_ready": composition_ready,
                "stop_check": "COMPOSITION_READY" if composition_ready else "CONTINUE",
            }
        )
        if composition_ready:
            stop_reason = "TARGET_REACHED"
            break

    after = _verify_or_tamper(freeze_dir, "after")
    write_json(output_dir / "freeze_verification_after.json", after)
    if after.get("freeze_bundle_sha256") != before.get("freeze_bundle_sha256"):
        raise FrozenBundleTampered("freeze hash changed during run")

    # --- Cohorts ---
    high_acc = [f for f in all_features if _is_high_accepted(f)]
    high_above = [
        f
        for f in all_features
        if f.get("edge_match_confidence_class") == "HIGH"
        and f.get("final_acceptance_state") == "ACCEPTED_ABOVE"
    ]
    high_below = [
        f
        for f in all_features
        if f.get("edge_match_confidence_class") == "HIGH"
        and f.get("final_acceptance_state") == "ACCEPTED_BELOW"
    ]
    high_acc_ids = {f["event_id"] for f in high_acc}

    # Day concentration
    day_counts = Counter(f.get("utc_day") for f in high_acc)
    n_ha = len(high_acc)
    day_rows = [
        {
            "utc_day": d,
            "n": c,
            "share": (c / n_ha) if n_ha else None,
        }
        for d, c in sorted(day_counts.items())
    ]
    max_day_share = max((r["share"] or 0) for r in day_rows) if day_rows else 0.0

    # Horizon summary for primary cohort — acceptance_aligned at state_available
    horizon_rows = []
    cohort_rows = []
    for h in EXPANSION_HORIZONS_S:
        sub = [
            o
            for o in all_outcomes
            if o.get("event_id") in high_acc_ids
            and o.get("anchor") == "state_available"
            and o.get("horizon_s") == h
            and o.get("outcome_coverage_complete")
            and o.get("acceptance_aligned_return_bps") is not None
        ]
        vals = [float(o["acceptance_aligned_return_bps"]) for o in sub]
        mfe = _finite([float(o["MFE_bps"]) for o in sub if o.get("MFE_bps") is not None])
        mae = _finite([float(o["MAE_bps"]) for o in sub if o.get("MAE_bps") is not None])
        boot = _bootstrap_median_ci(vals)
        sm = _summarize(vals, len(sub))
        horizon_rows.append(
            {
                "cohort": "HIGH_ACCEPTED_ANY",
                "horizon_s": h,
                **sm,
                "median_MFE": _quantile(mfe, 0.5),
                "median_MAE": _quantile(mae, 0.5),
                **{f"bootstrap_{k}": v for k, v in boot.items()},
            }
        )

    for name, subset in (
        ("HIGH_ACCEPTED_ANY", high_acc),
        ("HIGH_ACCEPTED_ABOVE", high_above),
        ("HIGH_ACCEPTED_BELOW", high_below),
        (
            "HIGH_no_acceptance",
            [
                f
                for f in all_features
                if f.get("edge_match_confidence_class") == "HIGH" and not _acceptance_any(f)
            ],
        ),
        (
            "MEDIUM_ACCEPTED_ANY",
            [
                f
                for f in all_features
                if f.get("edge_match_confidence_class") == "MEDIUM" and _acceptance_any(f)
            ],
        ),
        ("ACCEPTED_ANY", [f for f in all_features if _acceptance_any(f)]),
        (
            "FAILED_BREAK",
            [f for f in all_features if f.get("final_acceptance_state") == "FAILED_BREAK"],
        ),
        (
            "EDGE_NOT_REACHED",
            [f for f in all_features if f.get("edge_join_status") == "EDGE_NOT_REACHED"],
        ),
    ):
        cohort_rows.append(
            {
                "cohort": name,
                "n": len(subset),
                "sample_size_label": sample_size_label(len(subset)),
                "n_btc": sum(1 for f in subset if f.get("symbol") == "BTCUSDT"),
                "n_doge": sum(1 for f in subset if f.get("symbol") == "DOGEUSDT"),
                "n_days": len({f.get("utc_day") for f in subset}),
            }
        )

    # Symbol summary
    symbol_rows = []
    for sym in ("BTCUSDT", "DOGEUSDT"):
        sub = [f for f in high_acc if f.get("symbol") == sym]
        symbol_rows.append({"symbol": sym, "n_high_accepted_any": len(sub)})

    # Fair controls: same acceptance direction, confidence != HIGH
    fair_rows = []
    for direction_acc, sign_name in (("ACCEPTED_ABOVE", "LONG"), ("ACCEPTED_BELOW", "SHORT")):
        treat = [
            f
            for f in all_features
            if f.get("edge_match_confidence_class") == "HIGH"
            and f.get("final_acceptance_state") == direction_acc
        ]
        ctrl = [
            f
            for f in all_features
            if f.get("edge_match_confidence_class") != "HIGH"
            and f.get("final_acceptance_state") == direction_acc
        ]
        if not ctrl:
            fair_rows.append(
                {
                    "acceptance_state": direction_acc,
                    "align": sign_name,
                    "n_treatment_high": len(treat),
                    "n_control": 0,
                    "status": "FAIR_CONTROL_UNAVAILABLE",
                }
            )
            continue
        # Compare acceptance_aligned at 300s
        def _vals(feats):
            ids = {f["event_id"] for f in feats}
            return [
                float(o["acceptance_aligned_return_bps"])
                for o in all_outcomes
                if o.get("event_id") in ids
                and o.get("anchor") == "state_available"
                and o.get("horizon_s") == 300
                and o.get("outcome_coverage_complete")
                and o.get("acceptance_aligned_return_bps") is not None
            ]

        tv, cv = _vals(treat), _vals(ctrl)
        fair_rows.append(
            {
                "acceptance_state": direction_acc,
                "align": sign_name,
                "n_treatment_high": len(treat),
                "n_control": len(ctrl),
                "status": "OK",
                "treatment_median_5m": _quantile(tv, 0.5),
                "control_median_5m": _quantile(cv, 0.5),
                "note": "descriptive_only_no_significance_claim",
            }
        )

    # LOO on primary acceptance_aligned 5m
    loo_rows = []
    primary_outs = [
        o
        for o in all_outcomes
        if o.get("event_id") in high_acc_ids
        and o.get("anchor") == "state_available"
        and o.get("horizon_s") == 300
        and o.get("outcome_coverage_complete")
        and o.get("acceptance_aligned_return_bps") is not None
    ]
    full_vals = [float(o["acceptance_aligned_return_bps"]) for o in primary_outs]
    full_med = _quantile(full_vals, 0.5)
    for o in primary_outs:
        left = [float(x["acceptance_aligned_return_bps"]) for x in primary_outs if x["event_id"] != o["event_id"]]
        loo_rows.append(
            {
                "excluded_event_id": o["event_id"],
                "horizon_s": 300,
                "n": len(left),
                "median": _quantile(left, 0.5),
                "full_median": full_med,
                "median_delta": (
                    (_quantile(left, 0.5) - full_med)
                    if left and full_med is not None
                    else None
                ),
            }
        )
    loo_deltas = _finite([r["median_delta"] for r in loo_rows if r.get("median_delta") is not None])
    bootstrap = _bootstrap_median_ci(full_vals)

    # 15m coverage for primary
    cov_15 = [
        o
        for o in all_outcomes
        if o.get("event_id") in high_acc_ids
        and o.get("anchor") == "state_available"
        and o.get("horizon_s") == 900
    ]
    n_15_complete = sum(1 for o in cov_15 if o.get("outcome_coverage_complete"))
    frac_15 = (n_15_complete / len(high_acc)) if high_acc else 0.0

    prefix = _prefix_parity_checks(
        all_features, raw_root=raw_root, cfg=cfg, thr=thr, dthr=dthr, thr_accept=thr_accept
    )

    # Readiness gates
    ready_gates = {
        "n_high_accepted_any_ge_30": n_ha >= 30,
        "n_utc_days_ge_3": len(day_counts) >= 3,
        "max_day_share_le_50pct": max_day_share <= 0.50,
        "n_accepted_above_ge_5": len(high_above) >= 5,
        "n_accepted_below_ge_5": len(high_below) >= 5,
        "frac_15m_coverage_ge_90pct": frac_15 >= 0.90,
        "freeze_ok": True,
        "prefix_parity_ok": bool(prefix.get("ok")),
    }
    # Direction gate: only directions that appear need >=5; if one direction has 0 events, fail that gate
    if len(high_above) == 0:
        ready_gates["n_accepted_above_ge_5"] = False
    if len(high_below) == 0:
        ready_gates["n_accepted_below_ge_5"] = False
    # Spec: "mindestens 5 Events je tatsächlich ausgewerteter Acceptance-Richtung"
    dirs_present = []
    if high_above:
        dirs_present.append(len(high_above) >= 5)
    if high_below:
        dirs_present.append(len(high_below) >= 5)
    ready_gates["directions_present_each_ge_5"] = all(dirs_present) if dirs_present else False

    ready = all(
        [
            ready_gates["n_high_accepted_any_ge_30"],
            ready_gates["n_utc_days_ge_3"],
            ready_gates["max_day_share_le_50pct"],
            ready_gates["directions_present_each_ge_5"],
            ready_gates["frac_15m_coverage_ge_90pct"],
            ready_gates["freeze_ok"],
            ready_gates["prefix_parity_ok"],
        ]
    )

    if stop_reason == "COVERAGE_EXHAUSTED" and n_ha == 0:
        verdict = "FROZEN_HIGH_ACCEPTED_SAMPLE_EXPANSION_V1_INSUFFICIENT_COVERAGE"
    elif ready:
        verdict = "FROZEN_HIGH_ACCEPTED_SAMPLE_EXPANSION_V1_READY"
    else:
        verdict = "FROZEN_HIGH_ACCEPTED_SAMPLE_EXPANSION_V1_SMALL_N"

    elapsed = time.perf_counter() - t0
    funnel = {
        "n_aef_events": len(all_features),
        "n_high": sum(1 for f in all_features if f.get("edge_match_confidence_class") == "HIGH"),
        "n_medium": sum(1 for f in all_features if f.get("edge_match_confidence_class") == "MEDIUM"),
        "n_none": sum(1 for f in all_features if f.get("edge_match_confidence_class") == "NONE"),
        "n_accepted_above": sum(
            1 for f in all_features if f.get("final_acceptance_state") == "ACCEPTED_ABOVE"
        ),
        "n_accepted_below": sum(
            1 for f in all_features if f.get("final_acceptance_state") == "ACCEPTED_BELOW"
        ),
        "n_high_accepted_above": len(high_above),
        "n_high_accepted_below": len(high_below),
        "n_high_accepted_any": n_ha,
        "confidence": dict(Counter(f.get("edge_match_confidence_class") for f in all_features)),
        "acceptance": dict(Counter(f.get("final_acceptance_state") for f in all_features)),
        "join_status": dict(Counter(f.get("edge_join_status") for f in all_features)),
    }

    readiness = {
        "ready": ready,
        "gates": ready_gates,
        "max_day_share": max_day_share,
        "frac_15m_coverage": frac_15,
        "n_high_accepted_any": n_ha,
        "n_days": len(day_counts),
        "entry_timing_v1": "READY" if ready else "NOT_READY_SMALL_N",
    }

    write_csv(output_dir / "selected_windows.csv", selected)
    write_csv(output_dir / "expansion_progress.csv", progress)
    write_csv(output_dir / "frozen_events.csv", all_features)
    write_csv(output_dir / "high_accepted_events.csv", high_acc)
    write_csv(output_dir / "forward_outcomes.csv", all_outcomes)
    write_csv(output_dir / "event_decision_timestamps.csv", all_dts)
    write_csv(output_dir / "cohort_summary.csv", cohort_rows)
    write_csv(output_dir / "horizon_summary.csv", horizon_rows)
    write_csv(output_dir / "symbol_summary.csv", symbol_rows)
    write_csv(output_dir / "day_concentration.csv", day_rows)
    write_csv(output_dir / "fair_control_summary.csv", fair_rows)
    write_csv(output_dir / "leave_one_out.csv", loo_rows)
    write_json(output_dir / "bootstrap_summary.json", bootstrap)
    # also csv twin
    write_csv(
        output_dir / "bootstrap_summary.csv",
        [{"metric": "acceptance_aligned_5m_HIGH_ACCEPTED_ANY", **bootstrap, "n": len(full_vals)}],
    )
    write_json(
        output_dir / "data_quality_report.json",
        {
            "query_count": len(query_log),
            "query_log_tail": query_log[-6:],
            "n_hours_processed": len(selected),
            "n_eligible_available": len(chronological_eligible_hours(cov_rows)),
            "frac_15m_coverage_primary": frac_15,
            "prefix_parity": prefix,
            "acceptance_align_contract": ACCEPTANCE_ALIGN_SIGN,
        },
    )
    write_json(output_dir / "readiness_assessment.json", readiness)
    write_json(
        output_dir / "run_manifest.json",
        {
            **NO_FIT_FLAGS,
            "target_n": target_n,
            "stop_reason": stop_reason,
            "horizons_s": list(EXPANSION_HORIZONS_S),
            "freeze_dir": str(freeze_dir),
            "freeze_bundle_sha256": before.get("freeze_bundle_sha256"),
            "eligible_hours_planned": eligible,
            "hours_processed": [s["hour_start"] for s in selected],
            "elapsed_s": round(elapsed, 3),
            "query_count": len(query_log),
        },
    )

    summary = {
        "verdict": verdict,
        "stop_reason": stop_reason,
        "ready": ready,
        **NO_FIT_FLAGS,
        "freeze_bundle_sha256_before": before.get("freeze_bundle_sha256"),
        "freeze_bundle_sha256_after": after.get("freeze_bundle_sha256"),
        **funnel,
        "max_day_share": max_day_share,
        "frac_15m_coverage": frac_15,
        "bootstrap": bootstrap,
        "loo_median_delta_range": [min(loo_deltas), max(loo_deltas)] if loo_deltas else None,
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "n_hours_processed": len(selected),
        "descriptive_only": True,
        "trading_edge_proven": False,
        "entry_exit_optimization": False,
    }
    write_json(output_dir / "verdict.json", summary)
    write_json(output_dir / "SUMMARY.json", summary)

    (output_dir / "commands.txt").write_text(
        "\n".join(
            [
                "cd /home/telgenbuescher/projects/orderbook_analyse",
                "PYTHONPATH=src .venv/bin/python -m pytest "
                "tests/test_frozen_high_accepted_sample_expansion_v1.py -q",
                "PYTHONPATH=src .venv/bin/python "
                "scripts/run_frozen_high_accepted_sample_expansion_v1.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary
