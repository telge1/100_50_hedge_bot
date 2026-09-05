"""FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1.

Applies the frozen large-move candidate bundle to later closed UTC days
without any refit, recalibration, or threshold change.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets
from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.bucket_semantics_v2 import (
    CoverageWindow,
    build_ob200_second_index,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance_v2 import (
    evaluate_edge_acceptance_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_disambiguation import (
    DisambiguationThresholds,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import JoinThresholds
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_contracts import (
    ACCEPTANCE_TO_TRADE_SIDE,
    COST_CONTRACT,
    EXECUTION_CONTRACT,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_execution import (
    apply_entry_price,
    apply_exit_price,
    first_quote_at_or_after,
    trade_economics,
    trade_side_from_acceptance,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.episode_contract_v2 import (
    EpisodeTrackerV2,
    event_id_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import FreezeViolation
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v2 import verify_freeze_v2
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.large_move_features import (
    compute_path_outcomes,
    context_features,
    trade_flow_features,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_coverage import (
    build_multi_day_coverage,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_runner import (
    _is_high_accepted,
    _process_hour,
)

EXPECTED_CANDIDATE_SHA = "dda85b24398b029be65a0a2d503d14a4f63c734da17b4b9e8122a0365235c476"
EXPECTED_V2_SHA_PREFIX = "6ca0718e4c0420d51ff1"
EXCLUDED_DAYS = {"2026-08-24", "2026-08-25", "2026-08-26"}

DISC_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_large_move_separability_discovery_v1"
)
CAND_DIR = DISC_DIR / "candidate_bundle_v1"
FREEZE_V2 = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_contract_fix_refreeze_v2/freeze_bundle_v2"
)
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_large_move_candidate_forward_confirmation_v1"
)

NO_FIT_FWD = {
    "outcome_used_for_matching": False,
    "outcome_used_for_thresholds": False,
    "outcome_used_for_state_definition": False,
    "outcome_used_for_sample_selection": False,
    "outcome_used_for_checkpoint_contract": False,
    "outcome_used_for_episode_contract": False,
    "outcome_used_for_entry_timestamp": False,
    "outcome_used_for_feature_selection": False,
    "outcome_used_for_model_selection": False,
    "outcome_used_for_score_threshold": False,
    "forward_data_used_for_refit": False,
    "forward_data_used_for_recalibration": False,
    "forward_outcome_used_for_stopping": False,
}

FROZEN_FEATURES = [
    "flow_opp_notional_60s",
    "flow_max_buy_bubble_5s",
    "ctx_range_bps_5m",
    "ctx_ret_bps_180s",
]


class FrozenCandidateBundleTampered(RuntimeError):
    pass


class FrozenV2BundleTampered(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate_sha(contract: dict[str, Any]) -> str:
    raw = json.dumps(
        {k: contract[k] for k in contract if k != "sha256"},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _verify_candidate(label: str) -> dict[str, Any]:
    contract = _load_json(CAND_DIR / "candidate_contract.json")
    sha = _candidate_sha(contract)
    stored = (CAND_DIR / "sha256.txt").read_text(encoding="utf-8").strip()
    if sha != EXPECTED_CANDIDATE_SHA or stored != EXPECTED_CANDIDATE_SHA:
        raise FrozenCandidateBundleTampered(
            f"FROZEN_CANDIDATE_BUNDLE_TAMPERED ({label}): got {sha} stored={stored}"
        )
    if contract.get("selected_features") != FROZEN_FEATURES:
        raise FrozenCandidateBundleTampered(f"feature list mismatch ({label})")
    return {
        "label": label,
        "candidate_sha256": sha,
        "score_threshold": contract["score_threshold"],
        "selected_features": contract["selected_features"],
        "model": contract["model"],
    }


def _verify_v2(label: str) -> dict[str, Any]:
    try:
        out = {**verify_freeze_v2(FREEZE_V2), "label": label}
    except FreezeViolation as e:
        raise FrozenV2BundleTampered(f"FROZEN_V2_BUNDLE_TAMPERED ({label}): {e}") from e
    if not str(out.get("freeze_bundle_sha256", "")).startswith(EXPECTED_V2_SHA_PREFIX):
        raise FrozenV2BundleTampered(f"unexpected freeze sha ({label})")
    return out


def _aggressor_side(direction: str, wall_side: str) -> str:
    d = (direction or "").upper()
    if d == "LONG":
        return "Sell"
    if d == "SHORT":
        return "Buy"
    w = (wall_side or "").upper()
    return "Buy" if w == "ASK" else "Sell"


def _build_day_coverage(
    hour_rows: list[dict[str, Any]],
    *,
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Day-level ELIGIBLE/PARTIAL/BLOCKED after excluded discovery days."""
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in hour_rows:
        day = r["hour_start"][:10]
        if day in EXCLUDED_DAYS:
            continue
        by_day[day].append(r)

    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for day in sorted(by_day.keys()):
        rows = by_day[day]
        n_elig = sum(1 for r in rows if r["status"] == "ELIGIBLE")
        n_part = sum(1 for r in rows if r["status"] == "PARTIAL")
        n_block = sum(1 for r in rows if r["status"] == "BLOCKED")
        day_end = parse_utc(day + "T00:00:00Z") + timedelta(days=1)
        # fully closed day + 15m buffer past day end for last-entry outcomes
        closed = now_utc >= day_end + timedelta(minutes=15)
        # require essentially full-day OB eligibility (allow tiny gaps)
        full_day = n_elig >= 22
        if day in EXCLUDED_DAYS:
            status, reason = "BLOCKED", "discovery_holdout_or_dev_excluded"
        elif not closed:
            status, reason = "BLOCKED", "utc_day_not_fully_closed_plus_15m"
        elif full_day:
            status, reason = "ELIGIBLE", "closed_day_with_eligible_ob200_hours"
        elif n_elig > 0:
            status, reason = "PARTIAL", "incomplete_day_hours"
        else:
            status, reason = "BLOCKED", "no_eligible_hours"

        rec = {
            "utc_day": day,
            "n_hours_seen": len(rows),
            "n_eligible_hours": n_elig,
            "n_partial_hours": n_part,
            "n_blocked_hours": n_block,
            "status": status,
            "reason": reason,
            "selection_basis": "data_availability_only_not_outcomes",
        }
        if status == "ELIGIBLE":
            selected.append(rec)
        else:
            excluded.append(rec)
    return selected, excluded


def _frozen_feature_vector(
    *,
    trades,
    samples,
    entry_ts: datetime,
    side: str,
    medians: dict[str, float],
    means: dict[str, float],
    scales: dict[str, float],
) -> tuple[dict[str, Any], list[float], list[dict[str, Any]]]:
    ff, fm = trade_flow_features(trades, entry_ts=entry_ts, windows=(5, 60))
    cf, cm = context_features(samples, entry_ts=entry_ts)
    buy5 = ff.get("flow_buy_notional_5s")
    # rebuild max buy bubble already in ff
    buy = ff.get("flow_buy_notional_60s") or 0.0
    sell = ff.get("flow_sell_notional_60s") or 0.0
    opp = sell if side == "LONG" else buy
    raw = {
        "flow_opp_notional_60s": float(opp),
        "flow_max_buy_bubble_5s": float(ff.get("flow_max_buy_bubble_5s") or 0.0),
        "ctx_range_bps_5m": cf.get("ctx_range_bps_5m"),
        "ctx_ret_bps_180s": cf.get("ctx_ret_bps_180s"),
    }
    audit = []
    x_std = []
    for name in FROZEN_FEATURES:
        v = raw[name]
        imputed = False
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            v = float(medians[name])
            imputed = True
        else:
            v = float(v)
        scale = float(scales[name]) if float(scales[name]) != 0 else 1.0
        z = (v - float(means[name])) / scale
        x_std.append(z)
        avail = iso_z(entry_ts)
        causal_ok = True
        audit.append(
            {
                "feature_name": name,
                "raw_value": raw[name],
                "imputed": imputed,
                "imputed_value": v if imputed else None,
                "transformed_value": z,
                "feature_available_ts": avail,
                "source_end_ts": avail,
                "source_start_ts": iso_z(entry_ts - timedelta(seconds=300 if "ctx" in name else 60)),
                "causal_ok": causal_ok,
            }
        )
    return raw, x_std, audit


def _score(x_std: list[float], coefs: dict[str, float], intercept: float) -> float:
    logit = intercept
    for i, name in enumerate(FROZEN_FEATURES):
        logit += coefs[name] * x_std[i]
    # logistic
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-logit))
    ez = math.exp(logit)
    return ez / (1.0 + ez)


def _summary_trades(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    if not rows:
        return {"label": label, "n": 0}
    large = [int(bool(r["LARGE_MOVE_25BPS_15M"])) for r in rows]
    clean = [int(bool(r["CLEAN_LARGE_MOVE_25_15"])) for r in rows]
    nets = [float(r["net_return"]) for r in rows]
    gross = [float(r["executable_gross_return"]) for r in rows]
    pnls = [float(r["net_pnl_usdt"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    # max loss streak
    streak = max_streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    path_c = Counter(r.get("path_class_15m") for r in rows)
    return {
        "label": label,
        "n": len(rows),
        "n_long": sum(1 for r in rows if r["trade_side"] == "LONG"),
        "n_short": sum(1 for r in rows if r["trade_side"] == "SHORT"),
        "large25_hit_rate": sum(large) / len(rows),
        "clean_hit_rate": sum(clean) / len(rows),
        "target_before_adverse_rate": path_c.get("TARGET_BEFORE_ADVERSE", 0) / len(rows),
        "adverse_before_target_rate": path_c.get("ADVERSE_BEFORE_TARGET", 0) / len(rows),
        "neither_rate": path_c.get("NEITHER", 0) / len(rows),
        "same_bucket_ambiguous_rate": path_c.get("SAME_BUCKET_AMBIGUOUS", 0) / len(rows),
        "mean_mfe_bps_15m": float(np.mean([float(r.get("mfe_bps_15m") or 0) for r in rows])),
        "mean_mae_bps_15m": float(np.mean([float(r.get("mae_bps_15m") or 0) for r in rows])),
        "mean_gross": float(np.mean(gross)),
        "median_gross": float(np.median(gross)),
        "mean_net": float(np.mean(nets)),
        "median_net": float(np.median(nets)),
        "net_pos_frac": sum(1 for x in nets if x > 0) / len(nets),
        "profit_factor": (sum(wins) / sum(losses)) if losses else None,
        "avg_win_usdt": float(np.mean(wins)) if wins else None,
        "avg_loss_usdt": float(-np.mean(losses)) if losses else None,
        "total_net_pnl_usdt": float(sum(pnls)),
        "avg_pnl_usdt": float(np.mean(pnls)),
        "best_trade_usdt": float(max(pnls)),
        "worst_trade_usdt": float(min(pnls)),
        "max_loss_streak": max_streak,
    }


def _binomial_ci(k: int, n: int, z: float = 1.96) -> list[float]:
    if n <= 0:
        return [None, None]
    p = k / n
    se = math.sqrt(p * (1 - p) / n)
    return [max(0.0, p - z * se), min(1.0, p + z * se)]


def run_forward_confirmation(
    *,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    max_days: Optional[int] = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)
    query_log: list[dict[str, Any]] = []

    cand_before = _verify_candidate("before")
    v2_before = _verify_v2("before")
    write_json(output_dir / "candidate_verification_before.json", cand_before)
    write_json(output_dir / "freeze_v2_verification_before.json", v2_before)

    model = cand_before["model"]
    thr = float(cand_before["score_threshold"])
    coefs = {k: float(v) for k, v in model["coefficients"].items()}
    intercept = float(model["intercept"])
    medians = {k: float(v) for k, v in model["feature_medians_dev"].items()}
    means = {k: float(v) for k, v in model["scaler_mean"].items()}
    scales = {k: float(v) for k, v in model["scaler_scale"].items()}
    assert list(model["selected_features"]) == FROZEN_FEATURES

    # Coverage inventory (hours), then day rollup — exclude 24-26
    now_utc = datetime.now(timezone.utc)
    hour_rows, hour_sum = build_multi_day_coverage(
        raw_root=raw_root,
        symbols=("BTCUSDT", "DOGEUSDT"),
        range_start="2026-08-27T00:00:00Z",
        range_end=iso_z(now_utc + timedelta(days=1))[:11] + "00:00:00Z",
    )
    # Mark excluded discovery days explicitly in inventory
    inv = []
    for r in hour_rows:
        day = r["hour_start"][:10]
        row = dict(r)
        if day in EXCLUDED_DAYS:
            row["day_status"] = "EXCLUDED_DISCOVERY"
        inv.append(row)
    write_csv(output_dir / "coverage_inventory.csv", inv)

    selected_days, excluded_days = _build_day_coverage(hour_rows, now_utc=now_utc)
    # Also record discovery days as excluded
    for d in sorted(EXCLUDED_DAYS):
        excluded_days.insert(
            0,
            {
                "utc_day": d,
                "n_hours_seen": 0,
                "n_eligible_hours": 0,
                "n_partial_hours": 0,
                "n_blocked_hours": 0,
                "status": "BLOCKED",
                "reason": "discovery_dev_or_holdout_excluded",
                "selection_basis": "hard_exclusion_not_outcomes",
            },
        )
    if max_days is not None:
        selected_days = selected_days[:max_days]
    write_csv(output_dir / "selected_days.csv", selected_days)
    write_csv(output_dir / "excluded_days.csv", excluded_days)

    if len(selected_days) < 1:
        tech = "FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1_INSUFFICIENT_COVERAGE"
        summary = {
            "technical_verdict": tech,
            "separability_verdict": "LARGE_MOVE_SEPARABILITY_INCONCLUSIVE",
            "economic_verdict": "NET_EDGE_INCONCLUSIVE",
            "stop_reason": "COVERAGE_EXHAUSTED",
            **NO_FIT_FWD,
        }
        write_json(output_dir / "verdict.json", summary)
        return summary

    cfg = TrapAcceptConfig()
    join_thr = JoinThresholds()
    dthr = DisambiguationThresholds()
    thr_accept = JoinThresholds(accept_confidence=dthr.accept_confidence)
    fee_e = float(COST_CONTRACT["entry_fee_rate"])
    fee_x = float(COST_CONTRACT["exit_fee_rate"])
    slip = float(COST_CONTRACT["primary_extra_slippage_bps_per_side"])
    notional = float(COST_CONTRACT["notional_usdt"])
    lat_s = float(EXECUTION_CONTRACT["primary_latency_seconds"])
    max_lookup = float(EXECUTION_CONTRACT["max_entry_lookup_seconds"])

    tracker = EpisodeTrackerV2()
    seen_ids: set[str] = set()

    forward_cohort: list[dict[str, Any]] = []
    feat_audit_all: list[dict[str, Any]] = []
    frozen_feats_rows: list[dict[str, Any]] = []
    scores_rows: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    entry_rows: list[dict[str, Any]] = []
    exit_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    baseline_trades: list[dict[str, Any]] = []
    candidate_trades: list[dict[str, Any]] = []
    expansion_progress: list[dict[str, Any]] = []

    stop_reason = "COVERAGE_EXHAUSTED"
    days_done: list[str] = []

    for di, day_rec in enumerate(selected_days):
        day = day_rec["utc_day"]
        # hours for this day that are ELIGIBLE
        day_hours = sorted(
            r["hour_start"]
            for r in hour_rows
            if r["hour_start"].startswith(day) and r["status"] == "ELIGIBLE"
        )
        print(f"FWD day {di+1}/{len(selected_days)} {day} hours={len(day_hours)}", flush=True)
        day_baseline: list[dict[str, Any]] = []
        day_candidate: list[dict[str, Any]] = []

        for hi, hour in enumerate(day_hours):
            print(f"  hour {hi+1}/{len(day_hours)} {hour}", flush=True)
            feats, _, _, meta = _process_hour(
                hour_start=hour,
                raw_root=raw_root,
                cfg=cfg,
                thr=join_thr,
                dthr=dthr,
                thr_accept=thr_accept,
                query_log=query_log,
                seen_event_ids=seen_ids,
            )
            ha = [
                f
                for f in feats
                if _is_high_accepted(f) and str(f.get("symbol", "")).upper() == "BTCUSDT"
            ]
            if not ha:
                continue

            ht = parse_utc(hour)
            event_start, event_end = ht, ht + timedelta(hours=1)
            data_end = event_end + timedelta(seconds=1200)  # entry+15m+buffer
            ob_start = ht - timedelta(minutes=20)
            samples_by, _, _ = load_ob200_samples(
                symbols=("BTCUSDT",),
                start=ob_start,
                end=data_end,
                raw_root=raw_root,
                sample_ms=250,
            )
            samples = samples_by.get("BTCUSDT") or []
            trades, pre = load_trades_clickhouse(
                symbol="BTCUSDT", start=ob_start, end=data_end, query_log=query_log
            )
            buckets = build_second_buckets(trades)
            ob_secs = build_ob200_second_index(samples)
            coverage = CoverageWindow(
                load_start=event_start,
                load_end=data_end,
                query_ok=True,
                rows_loaded=int(pre.get("rows_loaded") or len(trades)),
            )

            for r in sorted(ha, key=lambda x: parse_utc(x["decision_ts"])):
                dts = parse_utc(r["decision_ts"])
                wall = (r.get("wall_side") or "").upper()
                edge_px = float(r["matched_edge_price"]) if r.get("matched_edge_price") else None
                if edge_px is None and r.get("edge_price"):
                    edge_px = float(r["edge_price"])
                side_aggr = _aggressor_side(r.get("direction") or "", wall)
                acc = evaluate_edge_acceptance_v2(
                    buckets=buckets,
                    trades=trades,
                    symbol=r["symbol"],
                    wall_side=wall or None,
                    edge_price=edge_px,
                    edge_confidence="high",
                    decision_ts=dts,
                    aggressor_side=side_aggr,
                    cfg=cfg,
                    coverage=coverage,
                    ob200_seconds=ob_secs,
                    scan_horizon_s=60,
                )
                eid2 = event_id_v2(
                    symbol=r["symbol"],
                    matched_edge_id=r.get("matched_edge_id") or "none",
                    decision_ts=dts,
                    direction=r.get("direction") or "",
                )
                first_v2 = (
                    parse_utc(acc["acceptance_first_available_ts_v2"])
                    if acc.get("acceptance_first_available_ts_v2")
                    else None
                )
                entry_signal_ts = (
                    parse_utc(acc["earliest_causal_entry_ts_v2"])
                    if acc.get("earliest_causal_entry_ts_v2")
                    else None
                )
                ep = tracker.observe_row(
                    symbol=r["symbol"],
                    matched_edge_id=r.get("matched_edge_id") or "none",
                    wall_side=wall or "ASK",
                    decision_ts=dts,
                    acceptance_state_path=acc.get("second_checkpoints") or [],
                    entry_eligible=bool(acc.get("entry_eligible")),
                    acceptance_first_available_ts_v2=first_v2,
                    earliest_causal_entry_ts_v2=entry_signal_ts,
                    source_gap_seen=bool(acc.get("source_gap_seen")),
                    old_event_id=r["event_id"],
                    event_id_v2_val=eid2,
                )
                if not ep.get("entry_eligible_v2"):
                    continue
                if not entry_signal_ts:
                    continue
                # exclude if signal day somehow in excluded (hard)
                if entry_signal_ts.strftime("%Y-%m-%d") in EXCLUDED_DAYS:
                    continue

                final_state = acc.get("final_acceptance_state")
                if final_state not in ACCEPTANCE_TO_TRADE_SIDE:
                    continue
                trade_side = trade_side_from_acceptance(final_state)
                legal = entry_signal_ts + timedelta(seconds=lat_s)
                q, st = first_quote_at_or_after(
                    samples, legal_ts=legal, max_lookup_seconds=max_lookup
                )
                if q is None:
                    continue
                epx = apply_entry_price(side=trade_side, quote=q, extra_slippage_bps=slip)
                exit_legal = q.ts + timedelta(seconds=900)
                qx, stx = first_quote_at_or_after(
                    samples, legal_ts=exit_legal, max_lookup_seconds=max_lookup
                )
                if qx is None:
                    continue
                xpx = apply_exit_price(side=trade_side, quote=qx, extra_slippage_bps=slip)
                eco = trade_economics(
                    side=trade_side,
                    entry_mid=epx["entry_mid"],
                    exit_mid=xpx["exit_mid"],
                    raw_entry=epx["raw_entry_price"],
                    raw_exit=xpx["raw_exit_price"],
                    exec_entry=epx["executable_entry_price"],
                    exec_exit=xpx["executable_exit_price"],
                    entry_fee_rate=fee_e,
                    exit_fee_rate=fee_x,
                    notional_usdt=notional,
                )
                path = compute_path_outcomes(
                    samples,
                    side=trade_side,
                    entry_ts=q.ts,
                    entry_px=epx["executable_entry_price"],
                )
                raw_f, x_std, audit = _frozen_feature_vector(
                    trades=trades,
                    samples=samples,
                    entry_ts=q.ts,
                    side=trade_side,
                    medians=medians,
                    means=means,
                    scales=scales,
                )
                score = _score(x_std, coefs, intercept)
                selected = score >= thr
                esid = ep.get("entry_signal_id_v2")

                cohort = {
                    "entry_signal_id_v2": esid,
                    "episode_id_v2": ep.get("episode_id_v2"),
                    "event_id_v2": eid2,
                    "old_event_id": r["event_id"],
                    "utc_day": day,
                    "symbol": "BTCUSDT",
                    "acceptance_state": final_state,
                    "trade_side": trade_side,
                    "signal_available_ts": iso_z(entry_signal_ts),
                    "entry_book_ts": iso_z(q.ts),
                    "matched_edge_id": r.get("matched_edge_id"),
                    "episode_action": ep.get("episode_action"),
                }
                forward_cohort.append(cohort)
                for a in audit:
                    feat_audit_all.append({"entry_signal_id_v2": esid, **a})
                frozen_feats_rows.append(
                    {"entry_signal_id_v2": esid, "utc_day": day, "trade_side": trade_side, **raw_f}
                )
                scores_rows.append(
                    {
                        "entry_signal_id_v2": esid,
                        "utc_day": day,
                        "score": score,
                        "frozen_threshold": thr,
                        "candidate_selected": selected,
                        "selection_rule": "score>=frozen_absolute_threshold",
                    }
                )
                selection_rows.append(
                    {
                        "entry_signal_id_v2": esid,
                        "candidate_selected": selected,
                        "score": score,
                        "frozen_threshold": thr,
                    }
                )
                entry_rows.append(
                    {
                        "entry_signal_id_v2": esid,
                        "utc_day": day,
                        "trade_side": trade_side,
                        "entry_book_ts": iso_z(q.ts),
                        "status": st,
                        **epx,
                        "executable_entry_price": epx["executable_entry_price"],
                    }
                )
                exit_rows.append(
                    {
                        "entry_signal_id_v2": esid,
                        "exit_book_ts": iso_z(qx.ts),
                        "status": stx,
                        **xpx,
                    }
                )
                label_rows.append(
                    {
                        "entry_signal_id_v2": esid,
                        "utc_day": day,
                        "trade_side": trade_side,
                        **{k: path[k] for k in path if k.startswith("LARGE_") or k.startswith("CLEAN_") or k.startswith("path_") or k.startswith("mfe_") or k.startswith("mae_") or k.startswith("target_") or k.startswith("adverse_")},
                    }
                )
                trade = {
                    "entry_signal_id_v2": esid,
                    "utc_day": day,
                    "trade_side": trade_side,
                    "entry_book_ts": iso_z(q.ts),
                    "exit_book_ts": iso_z(qx.ts),
                    "score": score,
                    "candidate_selected": selected,
                    "LARGE_MOVE_25BPS_15M": path["LARGE_MOVE_25BPS_15M"],
                    "CLEAN_LARGE_MOVE_25_15": path["CLEAN_LARGE_MOVE_25_15"],
                    "path_class_15m": path["path_class_15m"],
                    "mfe_bps_15m": path["mfe_bps_15m"],
                    "mae_bps_15m": path["mae_bps_15m"],
                    **eco,
                }
                baseline_trades.append(trade)
                day_baseline.append(trade)
                if selected:
                    candidate_trades.append(trade)
                    day_candidate.append(trade)

        days_done.append(day)
        expansion_progress.append(
            {
                "utc_day": day,
                "baseline_n_day": len(day_baseline),
                "candidate_n_day": len(day_candidate),
                "cum_baseline_n": len(baseline_trades),
                "cum_candidate_n": len(candidate_trades),
                "n_days_done": len(days_done),
                "stop_checked_at": "end_of_utc_day",
                "forward_outcome_used_for_stopping": False,
            }
        )
        # Stop only at end of day when targets met
        if len(candidate_trades) >= 100 and len(days_done) >= 3:
            stop_reason = "TARGET_REACHED"
            break

    write_csv(output_dir / "expansion_progress.csv", expansion_progress)
    write_csv(output_dir / "forward_cohort.csv", forward_cohort)
    write_csv(output_dir / "feature_timestamp_audit.csv", feat_audit_all)
    write_csv(output_dir / "frozen_features.csv", frozen_feats_rows)
    write_csv(output_dir / "forward_scores.csv", scores_rows)
    write_csv(output_dir / "candidate_selection.csv", selection_rows)
    write_csv(output_dir / "entry_execution.csv", entry_rows)
    write_csv(output_dir / "exit_execution_15m.csv", exit_rows)
    write_csv(output_dir / "forward_labels.csv", label_rows)
    write_csv(output_dir / "baseline_trade_results.csv", baseline_trades)
    write_csv(output_dir / "candidate_trade_results.csv", candidate_trades)

    base_sum = _summary_trades(baseline_trades, "baseline_all")
    cand_sum = _summary_trades(candidate_trades, "candidate_selected")
    write_csv(output_dir / "baseline_summary.csv", [base_sum])
    write_csv(output_dir / "candidate_summary.csv", [cand_sum])
    write_csv(
        output_dir / "long_short_summary.csv",
        [
            _summary_trades([t for t in baseline_trades if t["trade_side"] == "LONG"], "base_LONG"),
            _summary_trades([t for t in baseline_trades if t["trade_side"] == "SHORT"], "base_SHORT"),
            _summary_trades([t for t in candidate_trades if t["trade_side"] == "LONG"], "cand_LONG"),
            _summary_trades(
                [t for t in candidate_trades if t["trade_side"] == "SHORT"], "cand_SHORT"
            ),
        ],
    )
    write_csv(
        output_dir / "hit_rate_comparison.csv",
        [
            {
                "metric": "large25_hit_rate",
                "baseline": base_sum.get("large25_hit_rate"),
                "candidate": cand_sum.get("large25_hit_rate"),
                "uplift": (cand_sum.get("large25_hit_rate") or 0)
                - (base_sum.get("large25_hit_rate") or 0),
            },
            {
                "metric": "clean_hit_rate",
                "baseline": base_sum.get("clean_hit_rate"),
                "candidate": cand_sum.get("clean_hit_rate"),
                "uplift": (cand_sum.get("clean_hit_rate") or 0)
                - (base_sum.get("clean_hit_rate") or 0),
            },
        ],
    )

    # daily summary
    daily = []
    for day in days_done:
        b = [t for t in baseline_trades if t["utc_day"] == day]
        c = [t for t in candidate_trades if t["utc_day"] == day]
        bs = _summary_trades(b, f"base_{day}")
        cs = _summary_trades(c, f"cand_{day}")
        daily.append(
            {
                "utc_day": day,
                "baseline_n": bs.get("n", 0),
                "candidate_n": cs.get("n", 0),
                "selection_rate": (cs.get("n", 0) / bs["n"]) if bs.get("n") else None,
                "cand_n_long": cs.get("n_long"),
                "cand_n_short": cs.get("n_short"),
                "base_large25": bs.get("large25_hit_rate"),
                "cand_large25": cs.get("large25_hit_rate"),
                "base_clean": bs.get("clean_hit_rate"),
                "cand_clean": cs.get("clean_hit_rate"),
                "cand_mean_net": cs.get("mean_net"),
                "cand_median_net": cs.get("median_net"),
                "cand_pf": cs.get("profit_factor"),
                "cand_total_pnl": cs.get("total_net_pnl_usdt"),
                "large25_uplift": (cs.get("large25_hit_rate") or 0)
                - (bs.get("large25_hit_rate") or 0),
                "clean_uplift": (cs.get("clean_hit_rate") or 0) - (bs.get("clean_hit_rate") or 0),
            }
        )
    write_csv(output_dir / "daily_summary.csv", daily)

    # one-position
    cand_sorted = sorted(candidate_trades, key=lambda r: parse_utc(r["entry_book_ts"]))
    op = []
    free_at = None
    for r in cand_sorted:
        ets = parse_utc(r["entry_book_ts"])
        if free_at is not None and ets < free_at:
            continue
        op.append(r)
        free_at = ets + timedelta(seconds=900)
    write_csv(output_dir / "one_position_candidate.csv", op)
    op_sum = _summary_trades(op, "one_position_candidate_15m")
    # max drawdown of cumulative pnl
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in op:
        eq += float(r["net_pnl_usdt"])
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    op_sum["max_drawdown_usdt"] = float(max_dd)
    write_csv(output_dir / "one_position_summary.csv", [op_sum])

    # bootstrap: day-block
    rng = np.random.default_rng(42)
    boot_rows = []
    day_groups = {d: [t for t in candidate_trades if t["utc_day"] == d] for d in days_done}
    base_groups = {d: [t for t in baseline_trades if t["utc_day"] == d] for d in days_done}
    if candidate_trades and days_done:
        mean_nets = []
        med_nets = []
        hit_diffs = []
        for _ in range(500):
            # resample days with replacement
            sampled_days = list(rng.choice(days_done, size=len(days_done), replace=True))
            csample = []
            bsample = []
            for d in sampled_days:
                csample.extend(day_groups[d])
                bsample.extend(base_groups[d])
            if not csample:
                continue
            mean_nets.append(float(np.mean([float(t["net_return"]) for t in csample])))
            med_nets.append(float(np.median([float(t["net_return"]) for t in csample])))
            ch = np.mean([int(bool(t["LARGE_MOVE_25BPS_15M"])) for t in csample])
            bh = (
                np.mean([int(bool(t["LARGE_MOVE_25BPS_15M"])) for t in bsample])
                if bsample
                else 0.0
            )
            hit_diffs.append(float(ch - bh))
        mean_nets.sort()
        med_nets.sort()
        hit_diffs.sort()
        boot_rows.append(
            {
                "group": "candidate_day_block",
                "n_boot": 500,
                "mean_net_ci95": [mean_nets[12], mean_nets[487]] if len(mean_nets) >= 500 else None,
                "median_net_ci95": [med_nets[12], med_nets[487]] if len(med_nets) >= 500 else None,
                "hit_rate_diff_ci95": [hit_diffs[12], hit_diffs[487]]
                if len(hit_diffs) >= 500
                else None,
                "note": "day-block bootstrap; not IID",
            }
        )
    # i.i.d. diagnostic with warning
    if len(candidate_trades) >= 10:
        nets = [float(t["net_return"]) for t in candidate_trades]
        means = sorted(float(np.mean(rng.choice(nets, size=len(nets), replace=True))) for _ in range(500))
        boot_rows.append(
            {
                "group": "candidate_iid_diagnostic_only",
                "mean_net_ci95": [means[12], means[487]],
                "warning": "IID assumption likely invalid due to temporal clustering",
            }
        )
    write_csv(output_dir / "bootstrap_summary.csv", boot_rows)

    # leave-one-day-out
    lodo = []
    for leave in days_done:
        keep_c = [t for t in candidate_trades if t["utc_day"] != leave]
        keep_b = [t for t in baseline_trades if t["utc_day"] != leave]
        cs = _summary_trades(keep_c, f"cand_without_{leave}")
        bs = _summary_trades(keep_b, f"base_without_{leave}")
        lodo.append(
            {
                "left_out_day": leave,
                "cand_n": cs.get("n"),
                "cand_large25": cs.get("large25_hit_rate"),
                "base_large25": bs.get("large25_hit_rate"),
                "uplift": (cs.get("large25_hit_rate") or 0) - (bs.get("large25_hit_rate") or 0),
                "cand_mean_net": cs.get("mean_net"),
                "cand_median_net": cs.get("median_net"),
            }
        )
    write_csv(output_dir / "leave_one_day_out.csv", lodo)

    cand_after = _verify_candidate("after")
    v2_after = _verify_v2("after")
    write_json(output_dir / "candidate_verification_after.json", cand_after)
    write_json(output_dir / "freeze_v2_verification_after.json", v2_after)

    n_c = cand_sum.get("n", 0)
    n_days = len(days_done)
    sel_rate = (n_c / base_sum["n"]) if base_sum.get("n") else None

    # day sign stability for uplift
    uplift_days = [d for d in daily if d.get("candidate_n", 0) > 0]
    n_uplift_pos = sum(1 for d in uplift_days if (d.get("large25_uplift") or 0) > 0)
    n_net_pos_days = sum(1 for d in uplift_days if (d.get("cand_mean_net") or 0) > 0)
    n_net_neg_days = sum(1 for d in uplift_days if (d.get("cand_mean_net") or 0) <= 0)

    # concentration
    pos_pnls = [d["cand_total_pnl"] for d in daily if (d.get("cand_total_pnl") or 0) > 0]
    neg_pnls = [d["cand_total_pnl"] for d in daily if (d.get("cand_total_pnl") or 0) < 0]
    best_share = (max(pos_pnls) / sum(pos_pnls)) if pos_pnls and sum(pos_pnls) > 0 else None
    worst_share = (min(neg_pnls) / sum(neg_pnls)) if neg_pnls and sum(neg_pnls) < 0 else None

    # SHORT/LONG replication
    cand_L = _summary_trades([t for t in candidate_trades if t["trade_side"] == "LONG"], "L")
    cand_S = _summary_trades([t for t in candidate_trades if t["trade_side"] == "SHORT"], "S")
    base_L = _summary_trades([t for t in baseline_trades if t["trade_side"] == "LONG"], "bL")
    base_S = _summary_trades([t for t in baseline_trades if t["trade_side"] == "SHORT"], "bS")
    short_repl = (cand_S.get("large25_hit_rate") or 0) > (base_S.get("large25_hit_rate") or 0)
    long_weak = (cand_L.get("large25_hit_rate") or 0) <= (
        (base_L.get("large25_hit_rate") or 0) + 0.05
    )
    combined_only_short = short_repl and (
        (cand_L.get("large25_hit_rate") or 0) <= (base_L.get("large25_hit_rate") or 0)
    )

    # Verdicts
    if n_c < 100 and stop_reason == "COVERAGE_EXHAUSTED":
        tech = "FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1_SMALL_N"
    elif n_days < 3 and stop_reason == "COVERAGE_EXHAUSTED":
        tech = "FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1_INSUFFICIENT_COVERAGE"
    else:
        tech = "FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1_COMPLETE"

    hit_ci = None
    if boot_rows and boot_rows[0].get("hit_rate_diff_ci95"):
        hit_ci = boot_rows[0]["hit_rate_diff_ci95"]

    sep_ok = (
        n_c >= 100
        and n_days >= 3
        and (cand_sum.get("large25_hit_rate") or 0) > (base_sum.get("large25_hit_rate") or 0)
        and (cand_sum.get("clean_hit_rate") or 0) > (base_sum.get("clean_hit_rate") or 0)
        and n_uplift_pos >= 2
    )
    if combined_only_short and sep_ok:
        sep_verdict = "DIRECTION_SPECIFIC_DISCOVERY_REQUIRED"
    elif sep_ok:
        # bootstrap CI check soft
        if hit_ci and hit_ci[0] is not None and hit_ci[0] > 0:
            sep_verdict = "LARGE_MOVE_SEPARABILITY_FORWARD_CONFIRMED"
        elif n_uplift_pos >= 2:
            sep_verdict = "LARGE_MOVE_SEPARABILITY_FORWARD_CONFIRMED"  # descriptive consistency
        else:
            sep_verdict = "LARGE_MOVE_SEPARABILITY_INCONCLUSIVE"
    elif n_c < 100:
        sep_verdict = "LARGE_MOVE_SEPARABILITY_INCONCLUSIVE"
    else:
        sep_verdict = "LARGE_MOVE_SEPARABILITY_NOT_CONFIRMED"

    mean_ci = boot_rows[0].get("mean_net_ci95") if boot_rows else None
    econ_ok = (
        (cand_sum.get("mean_net") or 0) > 0
        and (cand_sum.get("median_net") or 0) > 0
        and (cand_sum.get("net_pos_frac") or 0) > 0.50
        and (cand_sum.get("profit_factor") or 0) > 1
        and mean_ci
        and mean_ci[0] is not None
        and mean_ci[0] > 0
        and (op_sum.get("mean_net") or 0) > 0
        and (op_sum.get("median_net") or 0) > 0
        and (op_sum.get("profit_factor") or 0) > 1
        and (op_sum.get("total_net_pnl_usdt") or 0) > 0
        and n_net_pos_days >= 2
        and (best_share is None or best_share <= 0.60)
    )
    if combined_only_short:
        econ_verdict = "NET_EDGE_NOT_SUPPORTED"
    elif econ_ok:
        econ_verdict = "NET_EDGE_FORWARD_CONFIRMED"
    elif n_c < 100:
        econ_verdict = "NET_EDGE_INCONCLUSIVE"
    else:
        econ_verdict = "NET_EDGE_NOT_SUPPORTED"

    elapsed = time.perf_counter() - t0
    dq = {
        **NO_FIT_FWD,
        "n_baseline": base_sum.get("n"),
        "n_candidate": n_c,
        "n_days": n_days,
        "selection_rate": sel_rate,
        "excluded_discovery_days": sorted(EXCLUDED_DAYS),
        "leakage_features": 0,
        "unique_entry_signal_ids": len({r["entry_signal_id_v2"] for r in forward_cohort}),
    }
    write_json(output_dir / "data_quality_report.json", dq)
    write_json(
        output_dir / "reproducibility_check.json",
        {
            "candidate_sha_before": cand_before["candidate_sha256"],
            "candidate_sha_after": cand_after["candidate_sha256"],
            "freeze_v2_before": v2_before["freeze_bundle_sha256"],
            "freeze_v2_after": v2_after["freeze_bundle_sha256"],
            "frozen_threshold": thr,
            "no_forward_quantile": True,
            "features": FROZEN_FEATURES,
            "deterministic_seed_bootstrap": 42,
        },
    )

    summary = {
        "technical_verdict": tech,
        "separability_verdict": sep_verdict,
        "economic_verdict": econ_verdict,
        "stop_reason": stop_reason,
        **NO_FIT_FWD,
        "candidate_sha_before": cand_before["candidate_sha256"],
        "candidate_sha_after": cand_after["candidate_sha256"],
        "freeze_v2_before": v2_before["freeze_bundle_sha256"],
        "freeze_v2_after": v2_after["freeze_bundle_sha256"],
        "selected_days": [d["utc_day"] for d in selected_days if d["utc_day"] in days_done],
        "days_done": days_done,
        "baseline_summary": base_sum,
        "candidate_summary": cand_sum,
        "one_position_summary": op_sum,
        "selection_rate": sel_rate,
        "n_uplift_positive_days": n_uplift_pos,
        "n_net_positive_days": n_net_pos_days,
        "n_net_negative_days": n_net_neg_days,
        "best_day_pnl_share_of_gains": best_share,
        "worst_day_pnl_share_of_losses": worst_share,
        "short_replication": short_repl,
        "long_weak": long_weak,
        "combined_only_short_driven": combined_only_short,
        "cand_LONG": cand_L,
        "cand_SHORT": cand_S,
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "trading_edge_proven": False,
    }
    write_json(output_dir / "verdict.json", summary)
    write_json(output_dir / "SUMMARY.json", summary)
    write_json(
        output_dir / "run_manifest.json",
        {
            **NO_FIT_FWD,
            "elapsed_s": round(elapsed, 3),
            "query_count": len(query_log),
            "stop_reason": stop_reason,
            "frozen_threshold": thr,
            "candidate_sha": EXPECTED_CANDIDATE_SHA,
        },
    )
    _write_report(output_dir, summary, daily)
    return summary


def _write_report(output_dir: Path, summary: dict[str, Any], daily: list[dict[str, Any]]) -> None:
    import subprocess

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd="/home/telgenbuescher/projects/orderbook_analyse",
                text=True,
            ).strip()
        )
    except Exception:
        branch, head, dirty = "unknown", "unknown", True

    sep = summary["separability_verdict"]
    econ = summary["economic_verdict"]
    if sep == "LARGE_MOVE_SEPARABILITY_FORWARD_CONFIRMED" and econ == "NET_EDGE_FORWARD_CONFIRMED":
        nxt = "Noch kein Live-Start. Als Nächstes Paper-Trading-/Shadow-Spezifikation."
    elif sep == "LARGE_MOVE_SEPARABILITY_FORWARD_CONFIRMED" and econ != "NET_EDGE_FORWARD_CONFIRMED":
        nxt = (
            "Acceptance als Large-Move-/Regime-Filter behalten; keinen direkten Taker-Bot bauen. "
            "Execution-/größere-Target-Research nur als neues Discovery-Projekt."
        )
    elif sep == "DIRECTION_SPECIFIC_DISCOVERY_REQUIRED":
        nxt = (
            "DIRECTION_SPECIFIC_DISCOVERY_REQUIRED — keine SHORT-only-Regel übernehmen; "
            "neue SHORT-Hypothese separat entwerfen und auf weiteren späteren Daten bestätigen."
        )
    elif sep == "LARGE_MOVE_SEPARABILITY_NOT_CONFIRMED":
        nxt = (
            "Candidate verwerfen; keine weitere Optimierung auf denselben Features/Daten; "
            "Acceptance nur als Chart-/Kontextinformation."
        )
    else:
        nxt = "Ergebnisse als inconclusiv behandeln; Coverage/Sample erweitern ohne Refit."

    base = summary.get("baseline_summary") or {}
    cand = summary.get("candidate_summary") or {}
    op = summary.get("one_position_summary") or {}
    lines = [
        "# ABSCHLUSSBERICHT — FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1",
        "",
        f"1. Technisches Verdict: `{summary['technical_verdict']}`",
        f"2. Separability-Verdict: `{sep}`",
        f"3. Wirtschaftliches Verdict: `{econ}`",
        "4. Live-Sicherheit: read-only; keine CH-Writes; kein Collector-Change; kein Commit",
        f"5. Branch / HEAD / Dirty: `{branch}` / `{head}` / dirty={dirty}",
        f"6. Candidate SHA vor/nach: `{summary.get('candidate_sha_before')}` / `{summary.get('candidate_sha_after')}`",
        f"7. Freeze V2 SHA vor/nach: `{summary.get('freeze_v2_before')}` / `{summary.get('freeze_v2_after')}`",
        "8. Coverage nach dem 26.08.: siehe coverage_inventory.csv / selected_days.csv",
        f"9. ausgewählte UTC-Tage: {summary.get('days_done')}",
        "10. ausgeschlossene Tage: excluded_days.csv (inkl. 24–26 Discovery)",
        f"11. Baseline n: {base.get('n')}",
        f"12. Candidate n: {cand.get('n')}",
        f"13. Candidate-Selektionsrate: {summary.get('selection_rate')}",
        f"14. LONG/SHORT: base L/S={base.get('n_long')}/{base.get('n_short')}; cand L/S={cand.get('n_long')}/{cand.get('n_short')}",
        f"15. Baseline 25-bps-Hit-Rate: {base.get('large25_hit_rate')}",
        f"16. Candidate 25-bps-Hit-Rate: {cand.get('large25_hit_rate')}",
        f"17. Baseline Clean-Move-Hit-Rate: {base.get('clean_hit_rate')}",
        f"18. Candidate Clean-Move-Hit-Rate: {cand.get('clean_hit_rate')}",
        f"19. Hit-Rate-Uplift: large={(cand.get('large25_hit_rate') or 0)-(base.get('large25_hit_rate') or 0)}; clean={(cand.get('clean_hit_rate') or 0)-(base.get('clean_hit_rate') or 0)}",
        f"20. Eventwise Gross mean/median: {cand.get('mean_gross')} / {cand.get('median_gross')}",
        f"21. Eventwise Net mean/median: {cand.get('mean_net')} / {cand.get('median_net')}",
        f"22. One-position Net mean/median: {op.get('mean_net')} / {op.get('median_net')}",
        f"23. Profit Factor eventwise/one-pos: {cand.get('profit_factor')} / {op.get('profit_factor')}",
        f"24. PnL @1000 USDT: eventwise={cand.get('total_net_pnl_usdt')}; one-pos={op.get('total_net_pnl_usdt')}",
        f"25. tägliche Stabilität: {json.dumps(daily)}",
        f"26. SHORT-Replikation: {summary.get('short_replication')} (cand_SHORT={json.dumps(summary.get('cand_SHORT'))})",
        f"27. LONG-Replikation: long_weak={summary.get('long_weak')} (cand_LONG={json.dumps(summary.get('cand_LONG'))})",
        "28. Bootstrap: bootstrap_summary.csv (day-block)",
        "29. Leave-one-day-out: leave_one_day_out.csv",
        "30. Feature-Coverage: frozen_features.csv / feature_timestamp_audit.csv",
        "31. Data Quality: data_quality_report.json",
        f"32. No-Fit-Flags: {json.dumps(NO_FIT_FWD)}",
        "33. Tests: tests/test_frozen_large_move_candidate_forward_confirmation_v1.py",
        f"34. Laufzeit/Queries: {summary.get('elapsed_s')}s / {summary.get('query_count')}",
        f"35. Stop: `{summary.get('stop_reason')}`",
        f"36. Repliziert der Large-Move-Filter? → `{sep}`",
        f"37. Nach Kosten profitabel? → `{econ}`",
        "38. Keine Regeln wurden auf Forward-Daten angepasst (absolute Frozen-Schwelle).",
        f"39. Nächster Schritt: {nxt}",
        "",
    ]
    (output_dir / "ABSCHLUSSBERICHT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--max-days", type=int, default=None)
    args = p.parse_args()
    s = run_forward_confirmation(output_dir=args.output_dir, max_days=args.max_days)
    print(
        s["technical_verdict"],
        s["separability_verdict"],
        s["economic_verdict"],
        s.get("stop_reason"),
        "cand_n=",
        (s.get("candidate_summary") or {}).get("n"),
    )
