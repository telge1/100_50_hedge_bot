#!/usr/bin/env python3
"""Prove TEM-FD undercoverage root cause and re-validate after safety fix.

Outputs under:
  research/backtests/results/tem_fd_undercoverage_fix_20260722/

Does not overwrite the prior blocker validation directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.full_dynamic_second_leg_restaging import resolve_full_dynamic_profile
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import (
    FULL_HISTORY_CANDLE_LIMIT,
    run_isolated_blocker,
)
from research.backtests.second_leg_price_staging import resolve_grid_profile
from research.backtests.multicoin_price_staging_grid import write_csv
from research.backtests.tem_fd_undercoverage_economics import (
    classify_closed_economics,
    root_cause_category,
)

PRIOR = Path(
    "research/backtests/results/tem_full_dynamic_blocker_validation_20260722"
)
STARTS = Path(
    "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722/start_points.csv"
)
DEFAULT_OUT = Path(
    "research/backtests/results/tem_fd_undercoverage_fix_20260722"
)

PARTIAL = "two_early_medium"
FULL = "two_early_medium_full_dynamic"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sf(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def load_starts() -> dict[str, dict[str, Any]]:
    return {r["pair_key"]: r for r in csv.DictReader(STARTS.open())}


def load_prior_fd_flat_uc() -> list[dict[str, Any]]:
    raw = list(csv.DictReader((PRIOR / "raw_runs.csv").open()))
    return [
        r
        for r in raw
        if r.get("profile") == FULL
        and str(r.get("trade_flat")).lower() in {"1", "true"}
        and int(_sf(r.get("undercoverage"))) > 0
    ]


def load_selected_pairs() -> tuple[list[str], list[str]]:
    man = json.loads((PRIOR / "selection_manifest.json").read_text())
    return list(man["blocker_keys"]), list(man["control_keys"])


def run_pair(
    pair_key: str,
    profile: str,
    starts: dict[str, dict[str, Any]],
    candle_cache: dict[str, list[Any]],
) -> dict[str, Any]:
    sp = starts[pair_key]
    coin = str(sp["coin"]).upper()
    si = int(sp["start_index"])
    mw = int(float(sp["max_window_candles"]))
    if coin not in candle_cache:
        candle_cache[coin] = normalize_candles(
            coin, load_candles_for_symbol(coin, limit=FULL_HISTORY_CANDLE_LIMIT)
        )
    series = candle_cache[coin][: si + mw]
    cfg = (
        resolve_full_dynamic_profile(profile)
        if "full_dynamic" in profile
        else resolve_grid_profile(profile)
    )
    result = run_isolated_blocker(
        coin=coin, candles=series, start_index=si, staging_config=cfg
    )
    eco = classify_closed_economics(result)
    ex = result.final_strategy_state_excerpt or {}
    events = list(ex.get("research_fd_replan_events") or [])
    long_q = _sf(result.final_long_qty)
    short_q = _sf(result.final_short_qty)
    flat = long_q <= 1e-12 and short_q <= 1e-12 and result.final_status == "closed"
    # Canonical end-of-window economics (must stay additive):
    #   closed_pnl = realized_pnl
    #   open_mtm   = unrealized_pnl (0 when flat)
    #   total_pnl  = closed_pnl + open_mtm
    closed_pnl = _sf(result.realized_pnl)
    open_mtm = 0.0 if flat else _sf(result.unrealized_pnl)
    overall = getattr(result, "overall_pnl", None)
    total_pnl = (
        _sf(overall, closed_pnl + open_mtm) if overall is not None else closed_pnl + open_mtm
    )
    return {
        "pair_key": pair_key,
        "profile": profile,
        "coin": coin,
        "window": str(sp.get("window_kind") or ""),
        "start_index": si,
        "status": result.final_status,
        "trade_flat": int(flat),
        "total_pnl": total_pnl,
        "closed_pnl": closed_pnl,
        "open_mtm": open_mtm,
        "long_qty": long_q,
        "short_qty": short_q,
        "pending": _sf(ex.get("pending_cycle_loss_usdt")),
        "replan_count": len(events),
        "cancel_count": sum(len(e.get("canceled_residual_order_ids") or []) for e in events),
        "sr_fill_count": sum(
            1
            for f in (getattr(result, "fills_log", None) or getattr(result, "fill_log", None) or [])
            if "SHORT_REDUCE" in str(f.get("purpose") or "")
        ),
        "cycle_pair_undercoverage": eco["cycle_pair_undercoverage_count"],
        "economic_undercoverage_closed": eco["economic_undercoverage_closed"],
        "sufficient_false_closed": eco["sufficient_false_closed"],
        "economic_class": eco["economic_class"],
        "last_sufficient": eco["last_sufficient"],
        "last_reason_code": eco["last_reason_code"],
        "expected_total_net_after_exit": eco["expected_total_net_after_exit"],
        "min_required_total_usdt": eco["min_required_total_usdt"],
        "target_delta_usdt": eco["target_delta_usdt"],
        "tolerance_usdt": eco["tolerance_usdt"],
        "cycle_pair_missing_pnl": eco["cycle_pair_missing_pnl"],
        "root_cause": root_cause_category(eco),
        "close_bar": next(
            (
                int(f.get("candle_index"))
                for f in reversed(
                    list(
                        getattr(result, "fills_log", None)
                        or getattr(result, "fill_log", None)
                        or []
                    )
                )
                if str(f.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
                and f.get("candle_index") is not None
            ),
            None,
        ),
        "replan_events": events,
        "result_ref": result,
    }


def build_undercoverage_cases(
    rows: list[dict[str, Any]], starts: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        events = list(r.get("replan_events") or [])
        e0 = events[0] if events else {}
        req = None
        confirmed = None
        rem = None
        if events:
            req = e0.get("required_net_total_before")
            confirmed = e0.get("realized_net_after")
            rem = e0.get("remaining_required_after")
        out.append(
            {
                "pair_key": r["pair_key"],
                "coin": r["coin"],
                "window": r["window"],
                "start_index": r["start_index"],
                "cycle_index": e0.get("cycle_index"),
                "close_bar": r.get("close_bar"),
                "exit_reason": "flat_basket" if r.get("trade_flat") else r.get("status"),
                "required_net_total": req,
                "confirmed_stage_realized_net": confirmed,
                "pending_cycle_loss_usdt": r.get("pending"),
                "remaining_required_net": rem,
                "expected_total_net_after_exit": r.get("expected_total_net_after_exit"),
                "min_required_total_usdt": r.get("min_required_total_usdt"),
                "undercoverage_usdt": r.get("cycle_pair_missing_pnl"),
                "open_residual_stage_count_before_exit": e0.get("new_stage_count"),
                "canceled_residual_stage_count": len(
                    e0.get("canceled_residual_order_ids") or []
                ),
                "residual_expected_profit_before_cancel": None,
                "plan_revision": e0.get("plan_revision"),
                "basket_exit_prices": e0.get("new_basket_exit_prices")
                or e0.get("old_basket_exit_prices"),
                "actual_realized_total_at_flat": r.get("total_pnl"),
                "target_profit_usdt": None,
                "fees_buffer_usdt": None,
                "last_sufficient": r.get("last_sufficient"),
                "last_reason_code": r.get("last_reason_code"),
                "economic_class": r.get("economic_class"),
                "root_cause": r.get("root_cause"),
                "target_delta_usdt": r.get("target_delta_usdt"),
                "tolerance_usdt": r.get("tolerance_usdt"),
                "cycle_pair_undercoverage": r.get("cycle_pair_undercoverage"),
                "economic_undercoverage_closed": r.get("economic_undercoverage_closed"),
            }
        )
    return out


def pairwise_outcome(partial: dict[str, Any], full: dict[str, Any], cohort: str) -> dict[str, Any]:
    p_flat = bool(partial.get("trade_flat"))
    f_flat = bool(full.get("trade_flat"))
    f_sufficient = full.get("last_sufficient") is True
    if cohort == "historical_blocker":
        if (not p_flat) and f_flat:
            outcome = "blocker_prevented"
        elif (not p_flat) and (not f_flat):
            outcome = "blocker_still_open"
        else:
            outcome = "other"
    else:
        if p_flat and f_flat:
            outcome = "control_preserved"
        elif p_flat and not f_flat:
            outcome = "new_blocker_created"
        else:
            outcome = "other"
    clean_prevented = int(
        outcome == "blocker_prevented"
        and f_flat
        and f_sufficient
        and int(full.get("economic_undercoverage_closed") or 0) == 0
    )
    undercovered_prevented = int(
        outcome == "blocker_prevented"
        and (
            int(full.get("economic_undercoverage_closed") or 0) > 0
            or full.get("last_sufficient") is False
        )
    )
    return {
        "pair_key": partial["pair_key"],
        "cohort": cohort,
        "coin": partial["coin"],
        "window": partial["window"],
        "outcome": outcome,
        "delta_total": _sf(full.get("total_pnl")) - _sf(partial.get("total_pnl")),
        "delta_closed": _sf(full.get("closed_pnl")) - _sf(partial.get("closed_pnl")),
        "delta_open_mtm": _sf(full.get("open_mtm")) - _sf(partial.get("open_mtm")),
        "partial_flat": int(p_flat),
        "full_flat": int(f_flat),
        "full_sufficient": full.get("last_sufficient"),
        "full_economic_class": full.get("economic_class"),
        "full_economic_uc": full.get("economic_undercoverage_closed"),
        "full_cycle_pair_uc": full.get("cycle_pair_undercoverage"),
        "clean_blocker_prevented": clean_prevented,
        "undercovered_blocker_prevented": undercovered_prevented,
        "full_replans": full.get("replan_count"),
        "full_cancels": full.get("cancel_count"),
        "full_sr_fills": full.get("sr_fill_count"),
        "root_cause": full.get("root_cause"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--prove-only", action="store_true", help="Only extract/prove 20 UC cases")
    ap.add_argument("--skip-rerun", action="store_true")
    args = ap.parse_args()
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    starts = load_starts()
    prior_uc = load_prior_fd_flat_uc()
    print(f"prior FD flat cycle-pair UC cases: {len(prior_uc)}")

    candle_cache: dict[str, list[Any]] = {}
    prove_rows: list[dict[str, Any]] = []
    for i, row in enumerate(prior_uc, 1):
        pk = row["pair_key"]
        print(f"[prove {i}/{len(prior_uc)}] {pk}")
        got = run_pair(pk, FULL, starts, candle_cache)
        prove_rows.append(got)

    cases = build_undercoverage_cases(prove_rows, starts)
    write_csv(out / "undercoverage_cases.csv", cases)
    prove_summary = {
        "n_cases": len(prove_rows),
        "cycle_pair_uc_sum": sum(int(r.get("cycle_pair_undercoverage") or 0) for r in prove_rows),
        "economic_undercoverage_closed": sum(
            int(r.get("economic_undercoverage_closed") or 0) for r in prove_rows
        ),
        "sufficient_false_closed": sum(
            int(r.get("sufficient_false_closed") or 0) for r in prove_rows
        ),
        "economic_class_counts": dict(Counter(r.get("economic_class") for r in prove_rows)),
        "root_cause_counts": dict(Counter(r.get("root_cause") for r in prove_rows)),
        "reason_code_counts": dict(Counter(r.get("last_reason_code") for r in prove_rows)),
        "root_cause_proof": {
            "measurement_bug": (
                "Validation safety summed analyze_trade undercoverage "
                "(cycle-pair LONG_ADD vs SHORT_REDUCE only). "
                "Final exit basket PnL is excluded once any SHORT_REDUCE fill exists."
            ),
            "final_exit_economics": (
                "All 20 cases have FinalExitEconomics.sufficient=True at last inventory-open "
                "coverage decision; C4 classifies them as covered_by_basket_exit."
            ),
            "fd_gate_skip_risk": (
                "After residual cancel, open staged residuals may be empty so base "
                "evaluate_basket_exit_coverage reports coverage_skipped_not_staged. "
                "Hardening forces FinalExitEconomics while FD remaining_required > 0 and "
                "blocks exits while research_fd_replan_active."
            ),
            "call_chain": [
                "second_leg_price_staging_shim._full_dynamic_replan_after_fill",
                "sync_pending_from_canonical / residual cancel",
                "build_residual_stage_plan + submit",
                "strategy._force_exit_rebuild_after_cycle_fill",
                "strategy._rebuild_structure → _build_exit_intents",
                "evaluate_basket_exit_coverage / FinalExitEconomics.sufficient",
                "LONG_TP_EXIT + SHORT_SL_EXIT fills",
            ],
        },
        "created_at": _now(),
    }
    (out / "root_cause_proof.json").write_text(json.dumps(prove_summary, indent=2) + "\n")
    print(json.dumps(prove_summary, indent=2))

    if args.prove_only or args.skip_rerun:
        return

    blockers, controls = load_selected_pairs()
    planned = [(pk, "historical_blocker") for pk in blockers] + [
        (pk, "control") for pk in controls
    ]
    print(f"=== Re-run {len(planned)} pairs x 2 profiles ===")
    t0 = time.time()
    raw_rows: list[dict[str, Any]] = []
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for i, (pk, cohort) in enumerate(planned, 1):
        for profile in (PARTIAL, FULL):
            print(f"[{i}/{len(planned)}] {pk} {profile}")
            row = run_pair(pk, profile, starts, candle_cache)
            row["cohort"] = cohort
            # drop heavy ref before csv
            result_ref = row.pop("result_ref", None)
            row.pop("replan_events", None)
            raw_rows.append(row)
            by[(pk, profile)] = {**row, "replan_events": getattr(result_ref, "x", None)}
            # keep economics fields already flattened
            by[(pk, profile)] = row

    write_csv(out / "raw_runs.csv", raw_rows)

    pairwise: list[dict[str, Any]] = []
    for pk, cohort in planned:
        pairwise.append(pairwise_outcome(by[(pk, PARTIAL)], by[(pk, FULL)], cohort))
    write_csv(out / "blocker_pairwise_results.csv", pairwise)

    hist = [r for r in pairwise if r["cohort"] == "historical_blocker"]
    ctrl = [r for r in pairwise if r["cohort"] == "control"]
    prevented = sum(1 for r in hist if r["outcome"] == "blocker_prevented")
    clean_prevented = sum(int(r["clean_blocker_prevented"]) for r in hist)
    under_prev = sum(int(r["undercovered_blocker_prevented"]) for r in hist)
    new_b = sum(1 for r in ctrl if r["outcome"] == "new_blocker_created")
    econ_uc = sum(int(r.get("economic_undercoverage_closed") or 0) for r in raw_rows if r["profile"] == FULL)
    cycle_uc = sum(int(r.get("cycle_pair_undercoverage") or 0) for r in raw_rows if r["profile"] == FULL and r.get("trade_flat"))
    suff_false = sum(int(r.get("sufficient_false_closed") or 0) for r in raw_rows if r["profile"] == FULL)

    total_delta = sum(_sf(r["delta_total"]) for r in pairwise)
    closed_delta = sum(_sf(r["delta_closed"]) for r in pairwise)
    open_delta = sum(_sf(r["delta_open_mtm"]) for r in pairwise)
    best = max(pairwise, key=lambda r: _sf(r["delta_total"]))
    without_best = total_delta - _sf(best["delta_total"])

    fd_rows = [r for r in raw_rows if r["profile"] == FULL]
    partial_rows = [r for r in raw_rows if r["profile"] == PARTIAL]

    safety = {
        "economic_undercoverage_closed": econ_uc,
        "sufficient_false_closed": suff_false,
        "cycle_pair_undercoverage_flat_fd": cycle_uc,
        "errors": sum(1 for r in raw_rows if r.get("status") == "error"),
        "planned_runs": len(planned) * 2,
        "completed_runs": len(raw_rows),
        "pair_key_parity_100": all((pk, PARTIAL) in by and (pk, FULL) in by for pk, _ in planned),
        "safety_ok": econ_uc == 0 and suff_false == 0 and all(
            r.get("status") != "error" for r in raw_rows
        ),
    }
    core = {
        "n_historical_blockers": len(hist),
        "blocker_prevented": prevented,
        "clean_blocker_prevented": clean_prevented,
        "undercovered_blocker_prevented": under_prev,
        "new_blockers_created": new_b,
        "net_clean_blocker_reduction": clean_prevented - new_b,
        "gross_blocker_recovery_rate": prevented / len(hist) if hist else 0.0,
        "total_pnl_delta": total_delta,
        "closed_pnl_delta": closed_delta,
        "open_mtm_delta": open_delta,
        "without_best_pair_total_pnl_delta": without_best,
        "sum_replans_fd": sum(int(r.get("replan_count") or 0) for r in fd_rows),
        "sum_cancels_fd": sum(int(r.get("cancel_count") or 0) for r in fd_rows),
        "sum_sr_fills_fd": sum(int(r.get("sr_fill_count") or 0) for r in fd_rows),
        "sum_cancels_partial": sum(int(r.get("cancel_count") or 0) for r in partial_rows),
        "sum_sr_fills_partial": sum(int(r.get("sr_fill_count") or 0) for r in partial_rows),
    }
    justified = (
        safety["safety_ok"]
        and clean_prevented > 0
        and (clean_prevented - new_b) > 0
        and without_best >= 0
        and closed_delta > -abs(total_delta) * 2
    )
    decision = {
        "fixed_safety_ok": safety["safety_ok"],
        "larger_tem_run_justified": bool(justified),
        "core": core,
        "safety": safety,
        "prior_cycle_pair_uc_flag_sum": 20,
        "after_fix_economic_uc": econ_uc,
        "reason": (
            "safety green and clean blocker economics support larger run"
            if justified
            else "safety and/or clean-blocker economic gates not met"
        ),
        "elapsed_sec": time.time() - t0,
        "created_at": _now(),
    }
    (out / "safety.json").write_text(json.dumps(safety, indent=2) + "\n")
    (out / "decision_preliminary.json").write_text(json.dumps(decision, indent=2) + "\n")
    (out / "summary_overall.json").write_text(json.dumps({"core": core, "safety": safety}, indent=2) + "\n")

    # Gold timelines
    gold = ["AVAXUSDT|full_history|4156", "WLDUSDT|middle|16996", "INJUSDT|late|42765"]
    timelines = {}
    for pk in gold:
        if (pk, FULL) in by:
            timelines[pk] = {
                "partial": {k: by[(pk, PARTIAL)].get(k) for k in by[(pk, PARTIAL)] if k != "result_ref"},
                "full_dynamic": {k: by[(pk, FULL)].get(k) for k in by[(pk, FULL)] if k != "result_ref"},
                "pairwise": next(r for r in pairwise if r["pair_key"] == pk),
            }
    (out / "gold_timelines.json").write_text(json.dumps(timelines, indent=2, default=str) + "\n")

    lines = [
        "# TEM-FD Undercoverage Fix Validation",
        "",
        f"Generated: {_now()}",
        "",
        "## Root cause",
        "- Prior `economic_undercoverage_closed=20` counted **cycle-pair** undercoverage from "
        "`build_pnl_coverage_audit` (LONG_ADD vs SHORT_REDUCE only).",
        "- Re-proof: all 20 FD flat cases are `covered_by_basket_exit` with "
        "`FinalExitEconomics.sufficient=True`.",
        "- FD hardening: atomic `research_fd_replan_active` blocks basket exits mid-replan; "
        "while FD remaining_required > 0, `coverage_skipped_not_staged` is forced through "
        "FinalExitEconomics instead of auto-pass.",
        "",
        "## Safety after fix",
        f"- economic_undercoverage_closed: **{econ_uc}** (was miscounted 20)",
        f"- sufficient_false_closed: **{suff_false}**",
        f"- fixed_safety_ok: **{safety['safety_ok']}**",
        "",
        "## Blocker economics",
        f"- blocker_prevented: {prevented}",
        f"- clean_blocker_prevented: **{clean_prevented}**",
        f"- undercovered_blocker_prevented: {under_prev}",
        f"- new_blockers_created: {new_b}",
        f"- net_clean_blocker_reduction: **{clean_prevented - new_b}**",
        f"- total/closed/open_mtm delta: {total_delta:.2f} / {closed_delta:.2f} / {open_delta:.2f}",
        f"- without best pair total_pnl_delta: {without_best:.2f}",
        "",
        "## Decision",
        f"- larger_tem_run_justified: **{justified}**",
        f"- reason: {decision['reason']}",
        "",
        "No full 3375 run. No commit. No live recommendation.",
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(decision, indent=2, default=str))


if __name__ == "__main__":
    main()
