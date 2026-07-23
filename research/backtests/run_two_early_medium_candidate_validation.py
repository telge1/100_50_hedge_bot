#!/usr/bin/env python3
"""Targeted candidate validation: two_early_medium vs legacy @1000/500 after C4 coverage fix.

Replays the same 27 baseline blockers as the completed full grid. Does not overwrite
full-grid artifacts. No strategy-economy changes. No commit/push.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    DEFAULT_BASELINE,
    FULL_HISTORY_CANDLE_LIMIT,
    analyze_blocker_run,
    run_isolated_blocker,
)
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.run_c4_undercoverage_fix_validation import (
    _capture_basket_close_economics,
    _fills,
    _restore_basket_coverage_method,
    _stage_events,
    _sum_fill_net_pnls,
)
from research.backtests.second_leg_price_staging import (
    resolve_grid_profile,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[2]
GRID = ROOT / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721"
DEFAULT_OUT = (
    ROOT / "research/backtests/results/two_early_medium_candidate_validation_1000_500_20260721"
)
PRIMARY = ("legacy", "two_early_medium")
OPTIONAL_REFS = ("four_small_early", "two_equal")
MTM_TOL = 1e-6


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] * (c - k) + xs[c] * (k - f)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(
                {
                    k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                    for k, v in row.items()
                }
            )


def _load_blocker_universe() -> list[dict[str, Any]]:
    """Exact coin/trade/start set from the completed full-grid legacy rows."""
    rows = list(csv.DictReader((GRID / "per_coin_per_profile.csv").open(encoding="utf-8")))
    by_coin: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("profile") or "") != "legacy":
            continue
        coin = str(row.get("coin") or "").upper()
        by_coin[coin] = {
            "coin": coin,
            "trade_number": int(safe_float(row.get("trade_number"))),
            "start_index": int(safe_float(row.get("start_index"))),
            "grid_legacy_final_mtm": safe_float(row.get("final_mtm")),
            "grid_legacy_trade_flat": int(safe_float(row.get("trade_flat"))),
            "grid_legacy_realized_pnl": safe_float(row.get("realized_pnl")),
            "grid_legacy_duration": int(safe_float(row.get("duration_candles"))),
            "grid_legacy_status": str(row.get("status") or ""),
        }
    # Prefer baseline blockers for any missing metadata, but universe = grid coins.
    blockers = {
        str(r.get("coin") or "").upper(): r
        for r in load_baseline_blockers(DEFAULT_BASELINE / "blocker_trades.csv")
    }
    out = []
    for coin in sorted(by_coin):
        item = dict(by_coin[coin])
        item["baseline_row"] = blockers.get(coin)
        out.append(item)
    return out


def _load_grid_profile_row(coin: str, profile: str) -> dict[str, Any] | None:
    rows = list(csv.DictReader((GRID / "per_coin_per_profile.csv").open(encoding="utf-8")))
    for row in rows:
        if str(row.get("coin") or "").upper() == coin and str(row.get("profile") or "") == profile:
            return dict(row)
    return None


def _active_exit_price(result: Any) -> float | None:
    best = None
    for order in result.order_log or []:
        purpose = str(order.get("purpose") or "")
        if purpose not in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}:
            continue
        if str(order.get("event_type") or "").lower() not in {"submitted", "replaced", "updated"}:
            # keep last known submitted/replaced
            pass
        px = order.get("trigger_price")
        if px is None:
            px = order.get("price")
        if px is None:
            continue
        best = float(px)
    # Prefer currently active if present in final diagnostics
    for order in getattr(result, "final_active_orders", None) or []:
        purpose = str(order.get("purpose") or "")
        if "EXIT" in purpose.upper() and ("TP" in purpose.upper() or "SL" in purpose.upper()):
            px = order.get("trigger_price") or order.get("price")
            if px is not None:
                return float(px)
    return best


def _classify_coverage(
    *,
    profile: str,
    result: Any,
    capture: dict[str, Any] | None,
    stage_info: dict[str, Any],
) -> str:
    flat = str(getattr(result, "final_status", "")) == "closed"
    if not flat:
        return "open_pending_coverage"
    if profile == "legacy":
        return "covered_by_second_leg"
    filled = list(stage_info.get("filled_stages") or [])
    cancelled = list(stage_info.get("cancelled_stages") or [])
    sufficient = bool((capture or {}).get("sufficient")) if capture else None
    last = (getattr(result, "final_strategy_state_excerpt", None) or {}).get(
        "last_basket_exit_coverage_decision"
    ) or {}
    if sufficient is None and last:
        sufficient = bool(last.get("sufficient") or last.get("coverage_ok"))
    if sufficient is False:
        return "economic_undercoverage_closed"
    # All planned stages filled and no residual cancels → second-leg cover.
    planned = 0
    excerpt = getattr(result, "final_strategy_state_excerpt", None) or {}
    sc = excerpt.get("staged_second_leg_tp_stage_count") or {}
    if isinstance(sc, dict) and "4" in sc:
        planned = int(sc.get("4") or 0)
    if planned <= 0:
        # fall back to intents
        stages = set()
        for intent in result.intent_log or []:
            if str(intent.get("purpose") or "") != "CYCLE_4_SHORT_REDUCE":
                continue
            meta = intent.get("metadata_excerpt") or {}
            if meta.get("stage_index") is not None:
                stages.add(int(meta["stage_index"]))
        planned = len(stages)
    if planned >= 2 and len(filled) >= planned and not cancelled:
        return "covered_by_second_leg"
    if cancelled or (planned >= 2 and len(filled) < planned):
        if sufficient is True or last.get("coverage_ok") is True:
            return "covered_by_basket_exit"
        # Closed with partial stages but no capture — still basket path if exits present.
        exits = [
            f
            for f in _fills(result)
            if str(f.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
        ]
        if exits:
            return "covered_by_basket_exit"
    return "covered_by_second_leg"


def _run_one(
    *,
    coin: str,
    trade_number: int,
    start_index: int,
    profile: str,
    candles: list[Any],
    baseline_row: dict[str, Any] | None,
    capture_economics: bool,
) -> dict[str, Any]:
    cfg = resolve_profile("legacy") if profile == "legacy" else resolve_grid_profile(profile)
    captures: list[dict[str, Any]] = []
    original = None
    if capture_economics and profile != "legacy":
        original = _capture_basket_close_economics(captures)
    try:
        result = run_isolated_blocker(
            coin=coin,
            candles=candles,
            start_index=start_index,
            staging_config=cfg,
            trade_number=trade_number,
        )
    finally:
        if original is not None:
            _restore_basket_coverage_method(original)

    analysis = analyze_blocker_run(
        coin=coin,
        trade_number=trade_number,
        start_index=start_index,
        profile=profile,
        result=result,
        candles=candles,
        baseline_row=baseline_row,
    )
    stage_info = _stage_events(result)
    capture = captures[-1] if captures else None
    # Prefer persisted decision for legacy/non-capture
    if capture is None:
        last = (result.final_strategy_state_excerpt or {}).get(
            "last_basket_exit_coverage_decision"
        )
        if isinstance(last, dict) and last:
            capture = {
                "sufficient": last.get("sufficient"),
                "tolerance_usdt": last.get("tolerance_usdt"),
                "target_delta_usdt": last.get("target_delta_usdt"),
                "min_required_total_usdt": last.get("min_required_total_usdt"),
                "expected_total_net_after_exit": last.get("expected_total_net_after_exit"),
                "reason_code": last.get("reason_code"),
                "effective_pending_cycle_loss_usdt": None,
                "target_profit_usdt": None,
                "buffer_usdt": None,
            }
    coverage_class = _classify_coverage(
        profile=profile,
        result=result,
        capture=capture,
        stage_info=stage_info,
    )
    fill_sum, fill_missing = _sum_fill_net_pnls(result)
    flat = int(bool(analysis.get("trade_flat")))
    realized = safe_float(result.realized_pnl)
    total = safe_float(analysis.get("final_mtm"))
    open_mtm = 0.0 if flat else total - realized
    # Prefer inventory-consistent open mtm from result unrealized when open
    if not flat and result.unrealized_pnl is not None:
        open_mtm = float(result.unrealized_pnl)
        total = realized + open_mtm

    next_exit = _active_exit_price(result)
    final_px = float(result.final_price or 0.0) or None
    dist_to_exit_pct = None
    if next_exit and final_px and final_px > 0:
        dist_to_exit_pct = (float(next_exit) - float(final_px)) / float(final_px) * 100.0

    excerpt = dict(result.final_strategy_state_excerpt or {})
    pending = safe_float(excerpt.get("pending_cycle_loss_usdt"))
    refill = int(
        bool(excerpt.get("refill_pending"))
        or bool(excerpt.get("refill_required"))
        or bool(excerpt.get("refill_in_progress"))
    )
    recovery = int(bool(getattr(result, "addon_short_recovery_activated", False)))

    late = list(stage_info.get("late_stage_fills_after_exit") or [])
    economic_uc_closed = int(
        flat and coverage_class == "economic_undercoverage_closed"
    )
    sufficient_false_closed = int(
        flat and capture is not None and capture.get("sufficient") is False
    )

    row = {
        **analysis,
        "realized_pnl": realized,
        "open_mtm": open_mtm,
        "total_pnl": total,
        "coverage_class": coverage_class,
        "economic_undercoverage_closed": economic_uc_closed,
        "sufficient_false_closed": sufficient_false_closed,
        "filled_stage_indices": stage_info.get("filled_stages"),
        "cancelled_stage_indices": stage_info.get("cancelled_stages"),
        "late_stage_fills_after_exit": late,
        "orphan_stage_order": int(bool(late)),
        "stage_exit_fills": stage_info.get("exit_fills"),
        "fill_net_pnl_sum": fill_sum,
        "fill_net_missing": fill_missing,
        "final_long_qty": getattr(result, "final_long_qty", None),
        "final_short_qty": getattr(result, "final_short_qty", None),
        "final_long_avg": getattr(result, "final_long_avg_price", None),
        "final_short_avg": getattr(result, "final_short_avg_price", None),
        "final_price": final_px,
        "pending_cycle_loss_usdt": pending,
        "next_exit_price": next_exit,
        "distance_to_exit_pct": dist_to_exit_pct,
        "refill_active": refill,
        "recovery_active": recovery,
        "max_drawdown_pct": getattr(result, "max_drawdown_pct", None),
        "candles_processed": getattr(result, "candles_processed", None),
        "sufficient": None if capture is None else capture.get("sufficient"),
        "tolerance_usdt": None if capture is None else capture.get("tolerance_usdt"),
        "target_delta_usdt": None if capture is None else capture.get("target_delta_usdt"),
        "min_required_total_usdt": None
        if capture is None
        else capture.get("min_required_total_usdt"),
        "expected_total_net_after_exit": None
        if capture is None
        else capture.get("expected_total_net_after_exit"),
        "target_profit_usdt": None if capture is None else capture.get("target_profit_usdt"),
        "buffer_usdt": None if capture is None else capture.get("buffer_usdt"),
        "effective_pending_cycle_loss_usdt": None
        if capture is None
        else capture.get("effective_pending_cycle_loss_usdt"),
        "coverage_reason_code": None if capture is None else capture.get("reason_code"),
        "duplicate_stage": int(safe_float(analysis.get("duplicate_stage"))),
        "over_close": int(safe_float(analysis.get("over_close"))),
    }
    # analyze_blocker_run may not include duplicate/over_close — default 0
    if "duplicate_stage" not in analysis:
        row["duplicate_stage"] = 0
    if "over_close" not in analysis:
        row["over_close"] = 0
    from research.backtests.adaptive_distance_staging_metrics import enrich_profile_row

    return enrich_profile_row(row, result)


def _pair_rows(
    by_profile: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    legacy = by_profile["legacy"]
    staged = by_profile["two_early_medium"]
    pairs = []
    for coin in sorted(legacy):
        l = legacy[coin]
        s = staged[coin]
        l_flat = int(l.get("trade_flat") or 0)
        s_flat = int(s.get("trade_flat") or 0)
        if l_flat and s_flat:
            bucket = "both_closed"
        elif (not l_flat) and s_flat:
            bucket = "legacy_open_staging_closed"
        elif l_flat and (not s_flat):
            bucket = "legacy_closed_staging_open"
        else:
            bucket = "both_open"
        delta_total = safe_float(s.get("total_pnl")) - safe_float(l.get("total_pnl"))
        if abs(delta_total) <= MTM_TOL:
            better = "equal"
        elif delta_total > 0:
            better = "staging_better"
        else:
            better = "staging_worse"
        pairs.append(
            {
                "coin": coin,
                "trade_number": l.get("trade_number"),
                "start_index": l.get("start_index"),
                "bucket": bucket,
                "better": better,
                "legacy_status": l.get("status"),
                "staging_status": s.get("status"),
                "legacy_flat": l_flat,
                "staging_flat": s_flat,
                "legacy_realized_pnl": l.get("realized_pnl"),
                "staging_realized_pnl": s.get("realized_pnl"),
                "legacy_open_mtm": l.get("open_mtm"),
                "staging_open_mtm": s.get("open_mtm"),
                "legacy_total_pnl": l.get("total_pnl"),
                "staging_total_pnl": s.get("total_pnl"),
                "delta_total_pnl": delta_total,
                "legacy_duration": l.get("duration_candles"),
                "staging_duration": s.get("duration_candles"),
                "delta_duration": int(s.get("duration_candles") or 0)
                - int(l.get("duration_candles") or 0),
                "legacy_max_cycle": l.get("max_cycle"),
                "staging_max_cycle": s.get("max_cycle"),
                "legacy_coverage_class": l.get("coverage_class"),
                "staging_coverage_class": s.get("coverage_class"),
                "staging_filled_stages": s.get("filled_stage_indices"),
                "staging_cancelled_stages": s.get("cancelled_stage_indices"),
                "staging_exit_reason": s.get("exit_reason"),
                "legacy_exit_reason": l.get("exit_reason"),
                "staging_sufficient": s.get("sufficient"),
                "staging_target_delta": s.get("target_delta_usdt"),
                "staging_tolerance": s.get("tolerance_usdt"),
                "staging_min_required": s.get("min_required_total_usdt"),
                "staging_expected_total": s.get("expected_total_net_after_exit"),
                "staging_target_profit": s.get("target_profit_usdt"),
                "staging_buffer": s.get("buffer_usdt"),
                "legacy_final_long_qty": l.get("final_long_qty"),
                "legacy_final_short_qty": l.get("final_short_qty"),
                "staging_final_long_qty": s.get("final_long_qty"),
                "staging_final_short_qty": s.get("final_short_qty"),
                "legacy_distance_to_exit_pct": l.get("distance_to_exit_pct"),
                "staging_distance_to_exit_pct": s.get("distance_to_exit_pct"),
                "legacy_pending_loss": l.get("pending_cycle_loss_usdt"),
                "staging_pending_loss": s.get("pending_cycle_loss_usdt"),
                "legacy_refill": l.get("refill_active"),
                "staging_refill": s.get("refill_active"),
                "legacy_recovery": l.get("recovery_active"),
                "staging_recovery": s.get("recovery_active"),
                "economically_valid_staging_close": int(
                    s_flat
                    and s.get("coverage_class")
                    in {"covered_by_second_leg", "covered_by_basket_exit"}
                    and int(s.get("economic_undercoverage_closed") or 0) == 0
                ),
                "additional_valid_close": int(
                    (not l_flat)
                    and s_flat
                    and s.get("coverage_class")
                    in {"covered_by_second_leg", "covered_by_basket_exit"}
                    and int(s.get("economic_undercoverage_closed") or 0) == 0
                ),
            }
        )
    return pairs


def _dist_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "n": 0,
            "sum": 0.0,
            "mean": None,
            "median": None,
            "p10": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p90": None,
            "best": None,
            "worst": None,
        }
    return {
        "n": len(values),
        "sum": float(sum(values)),
        "mean": float(statistics.mean(values)),
        "median": float(statistics.median(values)),
        "p10": _percentile(values, 10),
        "p25": _percentile(values, 25),
        "p50": _percentile(values, 50),
        "p75": _percentile(values, 75),
        "p90": _percentile(values, 90),
        "best": float(max(values)),
        "worst": float(min(values)),
    }


def run_validation(
    *,
    output_dir: Path,
    profiles: list[str],
    candle_limit: int = FULL_HISTORY_CANDLE_LIMIT,
) -> dict[str, Any]:
    if output_dir.resolve() == GRID.resolve():
        raise RuntimeError("refusing to overwrite full-grid directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    universe = _load_blocker_universe()
    assert len(universe) == 27, f"expected 27 coins, got {len(universe)}"

    t0 = time.time()
    all_rows: list[dict[str, Any]] = []
    by_profile: dict[str, dict[str, dict[str, Any]]] = {p: {} for p in profiles}
    candle_cache: dict[str, list[Any]] = {}

    for i, blocker in enumerate(universe, 1):
        coin = blocker["coin"]
        print(
            f"[{i}/{len(universe)}] LOAD {coin} T{blocker['trade_number']} "
            f"@{blocker['start_index']}",
            flush=True,
        )
        if coin not in candle_cache:
            candle_cache[coin] = normalize_candles(
                coin, load_candles_for_symbol(coin, limit=candle_limit)
            )
        candles = candle_cache[coin]
        for profile in profiles:
            print(f"  RUN {profile}", flush=True)
            row = _run_one(
                coin=coin,
                trade_number=int(blocker["trade_number"]),
                start_index=int(blocker["start_index"]),
                profile=profile,
                candles=candles,
                baseline_row=blocker.get("baseline_row"),
                capture_economics=profile != "legacy",
            )
            # attach grid legacy reference for parity
            row["grid_legacy_final_mtm"] = blocker.get("grid_legacy_final_mtm")
            row["grid_legacy_trade_flat"] = blocker.get("grid_legacy_trade_flat")
            row["grid_legacy_realized_pnl"] = blocker.get("grid_legacy_realized_pnl")
            if profile == "legacy":
                row["legacy_parity_vs_grid"] = int(
                    abs(safe_float(row["total_pnl"]) - safe_float(blocker["grid_legacy_final_mtm"]))
                    < 1.0
                    and int(row["trade_flat"]) == int(blocker["grid_legacy_trade_flat"])
                )
            all_rows.append(row)
            by_profile[profile][coin] = row

    pairs = _pair_rows(by_profile)
    additional = [p for p in pairs if int(p.get("additional_valid_close") or 0) == 1]
    regressions = [p for p in pairs if p.get("bucket") == "legacy_closed_staging_open"]
    both_open = [p for p in pairs if p.get("bucket") == "both_open"]
    both_closed = [p for p in pairs if p.get("bucket") == "both_closed"]

    # Integrity
    expected_keys = {(b["coin"], p) for b in universe for p in profiles}
    got_keys = {(r["coin"], r["profile"]) for r in all_rows}
    errors = [r for r in all_rows if r.get("error")]
    legacy_rows = [r for r in all_rows if r["profile"] == "legacy"]
    legacy_parity_ok = all(int(r.get("legacy_parity_vs_grid") or 0) == 1 for r in legacy_rows)
    safety = {
        "economic_undercoverage_closed": sum(
            int(r.get("economic_undercoverage_closed") or 0) for r in all_rows
        ),
        "invalid_partial": sum(int(r.get("invalid_partial") or 0) for r in all_rows),
        "over_close": sum(int(r.get("over_close") or 0) for r in all_rows),
        "duplicate_stage": sum(int(r.get("duplicate_stage") or 0) for r in all_rows),
        "late_stage_fill_after_exit": sum(
            len(r.get("late_stage_fills_after_exit") or []) for r in all_rows
        ),
        "orphan_stage_order": sum(int(r.get("orphan_stage_order") or 0) for r in all_rows),
        "sufficient_false_closed": sum(
            int(r.get("sufficient_false_closed") or 0) for r in all_rows
        ),
    }
    integrity = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_coins": len(universe),
        "profiles": profiles,
        "expected_combinations": len(expected_keys),
        "got_combinations": len(got_keys),
        "missing": sorted(expected_keys - got_keys),
        "duplicates": len(all_rows) - len(got_keys),
        "errors": [r.get("error") for r in errors],
        "legacy_parity_vs_full_grid": legacy_parity_ok,
        "safety": safety,
        "safety_ok": all(v == 0 for v in safety.values()),
        "pass": (
            expected_keys == got_keys
            and not errors
            and legacy_parity_ok
            and all(v == 0 for v in safety.values())
        ),
    }

    # Profile summaries
    profile_summary = []
    for profile in profiles:
        rows = [r for r in all_rows if r["profile"] == profile]
        closed = [r for r in rows if int(r.get("trade_flat") or 0) == 1]
        open_rows = [r for r in rows if int(r.get("trade_flat") or 0) == 0]
        valid_closed = [
            r
            for r in closed
            if r.get("coverage_class")
            in {"covered_by_second_leg", "covered_by_basket_exit"}
            and int(r.get("economic_undercoverage_closed") or 0) == 0
        ]
        basket = [r for r in valid_closed if r.get("coverage_class") == "covered_by_basket_exit"]
        second = [r for r in valid_closed if r.get("coverage_class") == "covered_by_second_leg"]
        profile_summary.append(
            {
                "profile": profile,
                "n": len(rows),
                "technically_closed": len(closed),
                "economically_valid_closed": len(valid_closed),
                "closed_by_second_leg": len(second),
                "closed_by_basket_exit": len(basket),
                "open_pending_coverage": len(open_rows),
                "sum_closed_pnl": sum(safe_float(r.get("realized_pnl")) for r in closed),
                "sum_open_mtm": sum(safe_float(r.get("open_mtm")) for r in open_rows),
                "sum_total_pnl": sum(safe_float(r.get("total_pnl")) for r in rows),
                "median_total_pnl": statistics.median(
                    [safe_float(r.get("total_pnl")) for r in rows]
                )
                if rows
                else None,
                "mean_total_pnl": statistics.mean(
                    [safe_float(r.get("total_pnl")) for r in rows]
                )
                if rows
                else None,
                "median_duration": statistics.median(
                    [safe_float(r.get("duration_candles")) for r in rows]
                )
                if rows
                else None,
                "mean_duration": statistics.mean(
                    [safe_float(r.get("duration_candles")) for r in rows]
                )
                if rows
                else None,
                "median_closed_duration": statistics.median(
                    [safe_float(r.get("duration_candles")) for r in closed]
                )
                if closed
                else None,
                "economic_undercoverage_closed": sum(
                    int(r.get("economic_undercoverage_closed") or 0) for r in rows
                ),
            }
        )

    # PnL distribution primary pair
    leg_totals = [safe_float(by_profile["legacy"][c]["total_pnl"]) for c in by_profile["legacy"]]
    tem_totals = [
        safe_float(by_profile["two_early_medium"][c]["total_pnl"])
        for c in by_profile["two_early_medium"]
    ]
    deltas = [safe_float(p["delta_total_pnl"]) for p in pairs]
    better = sum(1 for p in pairs if p["better"] == "staging_better")
    equal = sum(1 for p in pairs if p["better"] == "equal")
    worse = sum(1 for p in pairs if p["better"] == "staging_worse")
    pos_deltas = [d for d in deltas if d > MTM_TOL]
    neg_deltas = [d for d in deltas if d < -MTM_TOL]

    pnl_distribution = {
        "legacy_total": _dist_stats(leg_totals),
        "two_early_medium_total": _dist_stats(tem_totals),
        "legacy_closed_pnl_sum": sum(
            safe_float(r.get("realized_pnl"))
            for r in legacy_rows
            if int(r.get("trade_flat") or 0) == 1
        ),
        "staging_closed_pnl_sum": sum(
            safe_float(r.get("realized_pnl"))
            for r in all_rows
            if r["profile"] == "two_early_medium" and int(r.get("trade_flat") or 0) == 1
        ),
        "legacy_open_mtm_sum": sum(
            safe_float(r.get("open_mtm"))
            for r in legacy_rows
            if int(r.get("trade_flat") or 0) == 0
        ),
        "staging_open_mtm_sum": sum(
            safe_float(r.get("open_mtm"))
            for r in all_rows
            if r["profile"] == "two_early_medium" and int(r.get("trade_flat") or 0) == 0
        ),
        "delta_total": _dist_stats(deltas),
        "sum_positive_deltas": float(sum(pos_deltas)),
        "sum_negative_deltas": float(sum(neg_deltas)),
        "better": better,
        "equal": equal,
        "worse": worse,
        "win_rate_vs_legacy": better / len(pairs) if pairs else None,
        "median_delta": float(statistics.median(deltas)) if deltas else None,
        "mean_delta": float(statistics.mean(deltas)) if deltas else None,
        "worst_case_delta": float(min(deltas)) if deltas else None,
        "best_case_delta": float(max(deltas)) if deltas else None,
    }

    # Coin drivers / leave-one-out
    coin_deltas = sorted(
        (
            {
                "coin": p["coin"],
                "delta_total_pnl": safe_float(p["delta_total_pnl"]),
                "bucket": p["bucket"],
                "additional_valid_close": int(p.get("additional_valid_close") or 0),
            }
            for p in pairs
        ),
        key=lambda x: -x["delta_total_pnl"],
    )
    total_delta = sum(d["delta_total_pnl"] for d in coin_deltas)
    without_apt = [d for d in coin_deltas if d["coin"] != "APTUSDT"]
    top3 = coin_deltas[:3]
    without_top3 = coin_deltas[3:]
    coin_drivers = {
        "total_delta_pnl": total_delta,
        "ranked_coins": coin_deltas,
        "top3": top3,
        "without_apt_sum_delta": float(sum(d["delta_total_pnl"] for d in without_apt)),
        "without_apt_better_worse": {
            "better": sum(
                1
                for p in pairs
                if p["coin"] != "APTUSDT" and p["better"] == "staging_better"
            ),
            "worse": sum(
                1
                for p in pairs
                if p["coin"] != "APTUSDT" and p["better"] == "staging_worse"
            ),
            "equal": sum(
                1 for p in pairs if p["coin"] != "APTUSDT" and p["better"] == "equal"
            ),
        },
        "without_top3_sum_delta": float(sum(d["delta_total_pnl"] for d in without_top3)),
        "without_top3_coins_excluded": [c["coin"] for c in top3],
        "concentration_top1_share": (
            abs(top3[0]["delta_total_pnl"]) / abs(total_delta) if total_delta else None
        ),
        "concentration_top3_share": (
            sum(abs(c["delta_total_pnl"]) for c in top3) / abs(total_delta)
            if total_delta
            else None
        ),
    }

    # Open MTM risk
    open_staging = [
        by_profile["two_early_medium"][c]
        for c in by_profile["two_early_medium"]
        if int(by_profile["two_early_medium"][c].get("trade_flat") or 0) == 0
    ]
    open_sorted = sorted(open_staging, key=lambda r: safe_float(r.get("open_mtm")))
    open_mtms = [safe_float(r.get("open_mtm")) for r in open_staging]
    open_sum = sum(open_mtms) if open_mtms else 0.0
    worst3 = open_sorted[:3]
    open_mtm_risk = {
        "n_open_staging": len(open_staging),
        "sum_open_mtm": open_sum,
        "worst_three": [
            {
                "coin": r["coin"],
                "open_mtm": r.get("open_mtm"),
                "long_qty": r.get("final_long_qty"),
                "long_avg": r.get("final_long_avg"),
                "short_qty": r.get("final_short_qty"),
                "short_avg": r.get("final_short_avg"),
                "final_price": r.get("final_price"),
                "pending_loss": r.get("pending_cycle_loss_usdt"),
                "next_exit": r.get("next_exit_price"),
                "distance_to_exit_pct": r.get("distance_to_exit_pct"),
                "duration": r.get("duration_candles"),
                "max_drawdown_pct": r.get("max_drawdown_pct"),
            }
            for r in worst3
        ],
        "top1_share_of_abs_open_mtm": (
            abs(safe_float(worst3[0].get("open_mtm"))) / abs(open_sum)
            if open_sum and worst3
            else None
        ),
        "top3_share_of_abs_open_mtm": (
            sum(abs(safe_float(r.get("open_mtm"))) for r in worst3) / abs(open_sum)
            if open_sum and worst3
            else None
        ),
        "legacy_open_sum": sum(
            safe_float(r.get("open_mtm"))
            for r in legacy_rows
            if int(r.get("trade_flat") or 0) == 0
        ),
    }

    # Close rates
    leg_valid = sum(
        1
        for r in legacy_rows
        if int(r.get("trade_flat") or 0) == 1
        and r.get("coverage_class") in {"covered_by_second_leg", "covered_by_basket_exit"}
    )
    tem_rows = [r for r in all_rows if r["profile"] == "two_early_medium"]
    tem_valid = sum(
        1
        for r in tem_rows
        if int(r.get("trade_flat") or 0) == 1
        and r.get("coverage_class") in {"covered_by_second_leg", "covered_by_basket_exit"}
        and int(r.get("economic_undercoverage_closed") or 0) == 0
    )
    tem_tech = sum(1 for r in tem_rows if int(r.get("trade_flat") or 0) == 1)
    leg_tech = sum(1 for r in legacy_rows if int(r.get("trade_flat") or 0) == 1)

    # Duration / capital
    add_durs = [safe_float(p["staging_duration"]) for p in additional]
    duration_capital = {
        "legacy_median_duration": statistics.median(
            [safe_float(r.get("duration_candles")) for r in legacy_rows]
        ),
        "staging_median_duration": statistics.median(
            [safe_float(r.get("duration_candles")) for r in tem_rows]
        ),
        "legacy_mean_duration": statistics.mean(
            [safe_float(r.get("duration_candles")) for r in legacy_rows]
        ),
        "staging_mean_duration": statistics.mean(
            [safe_float(r.get("duration_candles")) for r in tem_rows]
        ),
        "additional_closes_median_duration": statistics.median(add_durs) if add_durs else None,
        "additional_closes_mean_duration": statistics.mean(add_durs) if add_durs else None,
        "closed_only_legacy_median": statistics.median(
            [
                safe_float(r.get("duration_candles"))
                for r in legacy_rows
                if int(r.get("trade_flat") or 0) == 1
            ]
        )
        if leg_tech
        else None,
        "closed_only_staging_median": statistics.median(
            [
                safe_float(r.get("duration_candles"))
                for r in tem_rows
                if int(r.get("trade_flat") or 0) == 1
            ]
        )
        if tem_tech
        else None,
    }

    # Decision rubric
    valid_closes_higher = tem_valid > leg_valid
    total_better = total_delta > 1.0  # clear improvement threshold USDT @1000 size
    median_delta_ok = (pnl_distribution["median_delta"] or 0) >= -MTM_TOL
    more_better_than_worse = better > worse
    no_econ_uc = safety["economic_undercoverage_closed"] == 0
    not_only_apt = abs(coin_drivers["without_apt_sum_delta"]) > 1.0 or (
        coin_drivers["without_apt_sum_delta"] > 0
    )
    not_only_top3 = coin_drivers["without_top3_sum_delta"] > 0
    worst_ok = (pnl_distribution["worst_case_delta"] or 0) > -50.0  # soft guard @1000 size
    safety_hold = integrity["pass"]

    if not safety_hold or not no_econ_uc:
        verdict = "kein Kandidat"
        next_test = "Coverage/Safety erneut prüfen — kein wirtschaftlicher Candidate-Test."
    elif (
        valid_closes_higher
        and total_better
        and median_delta_ok
        and more_better_than_worse
        and not_only_apt
        and not_only_top3
        and worst_ok
    ):
        verdict = "Kandidat für breitere Multi-Start-Validierung"
        next_test = (
            "Breitere Multi-Start-/Zeitfenster-Validierung (nicht nur Baseline-Blocker); "
            "keine Runtime-Integration bevor Multi-Start grün."
        )
    elif valid_closes_higher and total_better and median_delta_ok and more_better_than_worse:
        verdict = "Research-Kandidat"
        next_test = (
            "Sensitivity ohne APT/Top-Treiber + Open-MTM-Tail vertiefen; "
            "dann Multi-Start-Validierung."
        )
    else:
        verdict = "kein Kandidat"
        next_test = "Kein breiterer Test; Profil verwerfen oder Staging-Parameter neu wählen."

    decision = {
        "verdict": verdict,
        "next_minimal_test": next_test,
        "answers": {
            "1_additional_economically_valid_closes_gross": len(additional),
            "1_additional_economically_valid_closes_net": tem_valid - leg_valid,
            "1_lost_closes": len(regressions),
            "1b_legacy_valid_closes": leg_valid,
            "1c_staging_valid_closes": tem_valid,
            "2_additional_close_trades": [
                {
                    "coin": p["coin"],
                    "trade_number": p["trade_number"],
                    "start_index": p["start_index"],
                    "staging_realized_pnl": p["staging_realized_pnl"],
                    "legacy_total_pnl": p["legacy_total_pnl"],
                    "coverage_class": p["staging_coverage_class"],
                    "duration": p["staging_duration"],
                }
                for p in additional
            ],
            "3_regressions_legacy_closed_staging_open": regressions,
            "4_total_pnl_advantage": total_delta,
            "5_advantage_without_apt": coin_drivers["without_apt_sum_delta"],
            "6_advantage_without_top3": coin_drivers["without_top3_sum_delta"],
            "7_tail_or_duration_tradeoff": {
                "worst_case_delta": pnl_distribution["worst_case_delta"],
                "duration": duration_capital,
                "open_mtm_risk": {
                    "staging_open_sum": open_mtm_risk["sum_open_mtm"],
                    "legacy_open_sum": open_mtm_risk["legacy_open_sum"],
                },
            },
            "8_verdict": verdict,
            "9_next_test": next_test,
        },
        "rubric": {
            "valid_closes_higher": valid_closes_higher,
            "total_pnl_clearly_better": total_better,
            "median_delta_non_negative": median_delta_ok,
            "more_better_than_worse": more_better_than_worse,
            "no_economic_undercoverage": no_econ_uc,
            "not_only_apt": not_only_apt,
            "not_only_top3": not_only_top3,
            "worst_case_ok": worst_ok,
            "safety_and_legacy_hold": safety_hold,
        },
    }

    # Write artifacts
    _write_csv(output_dir / "raw_runs.csv", all_rows)
    _write_csv(output_dir / "trade_pair_comparison.csv", pairs)
    _write_csv(output_dir / "additional_valid_closes.csv", additional)
    _write_csv(output_dir / "staging_regressions.csv", regressions)
    _write_csv(output_dir / "both_open_risk_comparison.csv", both_open)
    _write_csv(output_dir / "both_closed_comparison.csv", both_closed)
    _write_csv(output_dir / "profile_summary.csv", profile_summary)
    (output_dir / "integrity.json").write_text(
        json.dumps(integrity, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "pnl_distribution.json").write_text(
        json.dumps(pnl_distribution, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "open_mtm_risk.json").write_text(
        json.dumps(open_mtm_risk, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "coin_drivers.json").write_text(
        json.dumps(coin_drivers, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "decision.json").write_text(
        json.dumps(decision, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "duration_capital.json").write_text(
        json.dumps(duration_capital, indent=2, default=str), encoding="utf-8"
    )

    # REPORT
    lines = [
        "# two_early_medium Candidate Validation @1000/500",
        "",
        f"Generated: `{integrity['generated_at']}`",
        f"Elapsed: `{round(time.time() - t0, 2)}s`",
        "",
        "Coverage-fix-aware replay of the same 27 baseline blockers as the full grid.",
        "Full-grid artifacts were not overwritten.",
        "",
        "## Integrity",
        "",
        f"- pass: **{integrity['pass']}**",
        f"- legacy parity vs full grid: **{legacy_parity_ok}**",
        f"- safety: `{json.dumps(safety)}`",
        "",
        "## Valid closes",
        "",
        f"- Legacy technically / economically valid: **{leg_tech} / {leg_valid}**",
        f"- two_early_medium technically / economically valid: **{tem_tech} / {tem_valid}**",
        f"- Additional economically valid closes (gross legacy-open→staging-closed): **{len(additional)}**",
        f"- Lost closes (legacy closed → staging open): **{len(regressions)}**",
        f"- Net valid-close gain: **{tem_valid - leg_valid}**",
        "",
        "### Additional valid closes",
        "",
    ]
    if not additional:
        lines.append("_None._")
    else:
        for p in additional:
            lines.append(
                f"- `{p['coin']}` T{p['trade_number']}: staging PnL={p['staging_realized_pnl']}, "
                f"legacy total={p['legacy_total_pnl']}, class=`{p['staging_coverage_class']}`, "
                f"duration={p['staging_duration']}"
            )
    lines.extend(
        [
            "",
            "## PnL",
            "",
            f"- Legacy Total PnL sum: **{pnl_distribution['legacy_total']['sum']:.4f}**",
            f"- Staging Total PnL sum: **{pnl_distribution['two_early_medium_total']['sum']:.4f}**",
            f"- Delta: **{total_delta:.4f}**",
            f"- Closed PnL L/S: **{pnl_distribution['legacy_closed_pnl_sum']:.4f} / "
            f"{pnl_distribution['staging_closed_pnl_sum']:.4f}**",
            f"- Open MTM L/S: **{pnl_distribution['legacy_open_mtm_sum']:.4f} / "
            f"{pnl_distribution['staging_open_mtm_sum']:.4f}**",
            f"- better/equal/worse: **{better}/{equal}/{worse}**",
            f"- median/mean/worst/best delta: "
            f"**{pnl_distribution['median_delta']:.4f} / {pnl_distribution['mean_delta']:.4f} / "
            f"{pnl_distribution['worst_case_delta']:.4f} / {pnl_distribution['best_case_delta']:.4f}**",
            "",
            "## Robustness leave-outs",
            "",
            f"- without APT delta: **{coin_drivers['without_apt_sum_delta']:.4f}**",
            f"- without Top-3 {coin_drivers['without_top3_coins_excluded']} delta: "
            f"**{coin_drivers['without_top3_sum_delta']:.4f}**",
            "",
            "## Decision",
            "",
            f"**{verdict}**",
            "",
            f"Next: {next_test}",
            "",
            "## Artifacts",
            "",
            f"- `{output_dir}`",
            "",
        ]
    )
    (output_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    summary = {
        "integrity_pass": integrity["pass"],
        "legacy_parity": legacy_parity_ok,
        "legacy_valid_closes": leg_valid,
        "staging_valid_closes": tem_valid,
        "additional_valid_closes_gross": len(additional),
        "additional_valid_closes_net": tem_valid - leg_valid,
        "lost_closes": len(regressions),
        "regressions": len(regressions),
        "legacy_total_pnl": pnl_distribution["legacy_total"]["sum"],
        "staging_total_pnl": pnl_distribution["two_early_medium_total"]["sum"],
        "delta_total_pnl": total_delta,
        "better_equal_worse": [better, equal, worse],
        "without_apt_delta": coin_drivers["without_apt_sum_delta"],
        "without_top3_delta": coin_drivers["without_top3_sum_delta"],
        "verdict": verdict,
        "elapsed_sec": round(time.time() - t0, 2),
        "output_dir": str(output_dir),
    }
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--profiles",
        default="legacy,two_early_medium,four_small_early,two_equal",
        help="Comma-separated profiles (legacy+two_early_medium required)",
    )
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    args = parser.parse_args(argv)
    profiles = [p.strip() for p in str(args.profiles).split(",") if p.strip()]
    if "legacy" not in profiles or "two_early_medium" not in profiles:
        raise SystemExit("profiles must include legacy and two_early_medium")
    run_validation(
        output_dir=args.output_dir,
        profiles=profiles,
        candle_limit=args.candle_limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
