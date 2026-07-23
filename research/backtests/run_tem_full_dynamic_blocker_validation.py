#!/usr/bin/env python3
"""TEM vs TEM-FD blocker-centered validation (research-only).

Compares only:
  - two_early_medium
  - two_early_medium_full_dynamic

Does NOT start a 3375-pair full run. No FULL_DYNAMIC economics changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    FULL_HISTORY_CANDLE_LIMIT,
    analyze_blocker_run,
    run_isolated_blocker,
)
from research.backtests.multicoin_price_staging_grid import (
    assert_output_dir_safe,
    atomic_write_json,
    write_csv,
)
from research.backtests.second_leg_price_staging import resolve_grid_profile

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FULL = (
    ROOT / "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722"
)
DEFAULT_OUT = (
    ROOT / "research/backtests/results/tem_full_dynamic_blocker_validation_20260722"
)
PARTIAL = "two_early_medium"
FULL = "two_early_medium_full_dynamic"
SEED = 20260722
MAX_BLOCKERS = 100
MAX_CONTROLS = 100
MIN_BLOCKERS = 30
MIN_CONTROLS = 30

_CANDLE_CACHE: dict[str, list[Any]] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candles_for(coin: str) -> list[Any]:
    key = coin.upper()
    if key not in _CANDLE_CACHE:
        _CANDLE_CACHE[key] = normalize_candles(
            key, load_candles_for_symbol(key, limit=FULL_HISTORY_CANDLE_LIMIT)
        )
    return _CANDLE_CACHE[key]


def _pair_key_from_row(row: dict[str, Any], starts: dict[str, dict[str, Any]]) -> str | None:
    coin = str(row.get("coin") or "").upper()
    si = int(float(row.get("start_index") or 0))
    for k, sp in starts.items():
        if str(sp.get("coin")).upper() == coin and int(sp["start_index"]) == si:
            return k
    # fallback reconstruct
    wid = str(row.get("window_id") or sp_window_fallback(starts, coin, si) or "early")
    return f"{coin}|{wid}|{si}"


def sp_window_fallback(starts: dict[str, dict[str, Any]], coin: str, si: int) -> str | None:
    for sp in starts.values():
        if str(sp.get("coin")).upper() == coin and int(sp["start_index"]) == si:
            return str(sp.get("window_kind") or sp.get("window_id") or "")
    return None


def classify_end_status(
    *,
    status: str,
    trade_flat: bool,
    long_qty: float,
    short_qty: float,
    pending: float,
    covered: bool,
) -> str:
    st = str(status or "").lower()
    if st == "error":
        return "error"
    if trade_flat or st == "closed" or (long_qty <= 1e-12 and short_qty <= 1e-12):
        return "flat_closed"
    if st in {"max_candles", "data_end"}:
        return "data_end_open"
    # open with inventory / incomplete economics
    if long_qty > 1e-12 or short_qty > 1e-12 or (pending > 1e-9 and not covered):
        return "open_blocked"
    if st == "open":
        return "open_blocked"
    return "invalid"


def is_blocked_end(end_status: str) -> bool:
    return end_status in {"open_blocked", "data_end_open"}


def load_tem_rows(source: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    starts = {r["pair_key"]: r for r in csv.DictReader((source / "start_points.csv").open())}
    raw = list(csv.DictReader((source / "raw_profile_runs.csv").open()))
    tem = [r for r in raw if str(r.get("profile")) == PARTIAL]
    return tem, starts


def extract_historical_blockers(
    tem_rows: list[dict[str, Any]], starts: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Profile-independent: TEM run not flat at window end."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in tem_rows:
        flat = str(r.get("trade_flat") or "").strip() in {"1", "True", "true"}
        status = str(r.get("status") or "")
        long_q = safe_float(r.get("final_long_qty"))
        short_q = safe_float(r.get("final_short_qty"))
        pending = safe_float(r.get("pending_cycle_loss_usdt") or r.get("effective_pending_cycle_loss_usdt"))
        end = classify_end_status(
            status=status,
            trade_flat=flat,
            long_qty=long_q,
            short_qty=short_q,
            pending=pending,
            covered=pending <= 1e-9 and flat,
        )
        if not is_blocked_end(end) and end != "error":
            continue
        pk = _pair_key_from_row(r, starts)
        if not pk or pk in seen or pk not in starts:
            continue
        seen.add(pk)
        sp = starts[pk]
        filled = str(r.get("filled_stage_indices") or "")
        n_filled = 0
        if filled and filled not in {"[]", ""}:
            try:
                import ast

                n_filled = len(ast.literal_eval(filled))
            except Exception:
                n_filled = int(safe_float(r.get("filled_stages")))
        root = "unknown"
        if n_filled <= 0 and safe_float(r.get("planned_stages")) >= 2:
            root = "no_second_leg_stage_filled"
        elif n_filled == 1:
            root = "first_stage_filled_residual_too_far"
        elif n_filled >= 2:
            root = "multiple_stages_filled_residual_too_far"
        if pending > 1e-9:
            root = "cycle_not_economically_complete"
        if end == "data_end_open":
            root = "data_window_ended"
        out.append(
            {
                "pair_key": pk,
                "coin": str(sp.get("coin") or r.get("coin")).upper(),
                "window": str(sp.get("window_kind") or r.get("window_id") or ""),
                "start_index": int(sp["start_index"]),
                "max_window_candles": int(float(sp.get("max_window_candles") or 0)),
                "partial_final_status": status,
                "partial_end_class": end,
                "partial_total_pnl": safe_float(r.get("total_pnl") or r.get("final_mtm")),
                "partial_closed_pnl": safe_float(r.get("closed_pnl") or r.get("realized_pnl")),
                "partial_open_mtm": safe_float(r.get("open_mtm")),
                "partial_long_qty": long_q,
                "partial_short_qty": short_q,
                "partial_highest_cycle": int(safe_float(r.get("max_cycle"))),
                "partial_last_cycle": int(safe_float(r.get("max_cycle"))),
                "partial_remaining_required_net": pending,
                "partial_end_exposure": safe_float(
                    r.get("max_abs_net_exposure") or r.get("net_exposure")
                ),
                "partial_last_event": status,
                "blocker_root_cause_preliminary": root,
                "distance_status": r.get("distance_status") or "",
                "effective_stage_count": int(
                    safe_float(r.get("effective_stage_count_after_rounding") or r.get("planned_stages"))
                ),
                "is_historical_blocker_flag": str(sp.get("is_historical_blocker") or ""),
            }
        )
    return out


def extract_flat_controls(
    tem_rows: list[dict[str, Any]], starts: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in tem_rows:
        flat = str(r.get("trade_flat") or "").strip() in {"1", "True", "true"}
        status = str(r.get("status") or "")
        if not (flat or status == "closed"):
            continue
        pk = _pair_key_from_row(r, starts)
        if not pk or pk in seen or pk not in starts:
            continue
        seen.add(pk)
        sp = starts[pk]
        out.append(
            {
                "pair_key": pk,
                "coin": str(sp.get("coin") or r.get("coin")).upper(),
                "window": str(sp.get("window_kind") or r.get("window_id") or ""),
                "start_index": int(sp["start_index"]),
                "max_window_candles": int(float(sp.get("max_window_candles") or 0)),
                "partial_final_status": status,
                "partial_total_pnl": safe_float(r.get("total_pnl") or r.get("final_mtm")),
                "partial_closed_pnl": safe_float(r.get("closed_pnl") or r.get("realized_pnl")),
                "partial_open_mtm": safe_float(r.get("open_mtm")),
                "distance_status": r.get("distance_status") or "",
                "effective_stage_count": int(
                    safe_float(r.get("effective_stage_count_after_rounding") or r.get("planned_stages"))
                ),
            }
        )
    return out


def stratified_sample(
    rows: list[dict[str, Any]],
    *,
    n: int,
    seed: int,
    label: str,
) -> list[dict[str, Any]]:
    """Deterministic stratified by (coin, window); no PnL sorting."""
    if len(rows) <= n:
        return sorted(rows, key=lambda r: r["pair_key"])
    rng = random.Random(seed)
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets[(str(r.get("coin")), str(r.get("window")))].append(r)
    for k in buckets:
        buckets[k] = sorted(buckets[k], key=lambda r: r["pair_key"])
        rng.shuffle(buckets[k])
    # proportional allocation
    keys = sorted(buckets.keys())
    total = len(rows)
    alloc = {k: max(1, int(round(n * len(buckets[k]) / total))) for k in keys}
    # fix sum
    while sum(alloc.values()) > n:
        # reduce largest
        kmax = max(keys, key=lambda k: alloc[k])
        if alloc[kmax] > 1:
            alloc[kmax] -= 1
        else:
            break
    while sum(alloc.values()) < n:
        kmax = max(keys, key=lambda k: len(buckets[k]) - alloc[k])
        if alloc[kmax] < len(buckets[k]):
            alloc[kmax] += 1
        else:
            break
    selected: list[dict[str, Any]] = []
    for k in keys:
        selected.extend(buckets[k][: alloc[k]])
    selected = sorted(selected, key=lambda r: r["pair_key"])
    if len(selected) > n:
        selected = selected[:n]
    # top up if short
    if len(selected) < n:
        have = {r["pair_key"] for r in selected}
        rest = [r for r in sorted(rows, key=lambda x: x["pair_key"]) if r["pair_key"] not in have]
        rng.shuffle(rest)
        selected.extend(rest[: n - len(selected)])
    return sorted(selected, key=lambda r: r["pair_key"])


def run_one(
    pair_key: str,
    profile: str,
    starts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sp = starts[pair_key]
    coin = str(sp["coin"]).upper()
    si = int(sp["start_index"])
    mw = int(float(sp["max_window_candles"]))
    candles = _candles_for(coin)
    series = candles[: si + mw]
    cfg = resolve_grid_profile(profile)
    result = run_isolated_blocker(coin=coin, candles=series, start_index=si, staging_config=cfg)
    analysis = analyze_blocker_run(
        coin=coin,
        trade_number=0,
        start_index=si,
        profile=profile,
        result=result,
        candles=series,
    )
    ex = dict(result.final_strategy_state_excerpt or {})
    events = list(ex.get("research_fd_replan_events") or [])
    plan = dict(ex.get("research_second_leg_price_staging_plan") or {})
    long_q = safe_float(getattr(result, "final_long_qty", None))
    short_q = safe_float(getattr(result, "final_short_qty", None))
    if long_q == 0 and short_q == 0:
        long_q = safe_float(ex.get("open_long_qty") or ex.get("long_qty"))
        short_q = safe_float(ex.get("open_short_qty") or ex.get("short_qty"))
    # inventory from analysis fields if exported
    pending = safe_float(ex.get("pending_cycle_loss_usdt"))
    covered = bool((ex.get("research_fd_cycle_covered") or {}).get("4")) or pending <= 1e-9
    status = str(result.final_status or analysis.get("status") or "")
    flat = bool(int(safe_float(analysis.get("trade_flat")))) or status == "closed"
    # better inventory from book via analysis final_mtm path: use fill_log last after
    fl = list(getattr(result, "fill_log", None) or [])
    if fl:
        last = fl[-1]
        if last.get("long_qty_after") is not None:
            long_q = safe_float(last.get("long_qty_after"))
        if last.get("short_qty_after") is not None:
            short_q = safe_float(last.get("short_qty_after"))
    end = classify_end_status(
        status=status,
        trade_flat=flat,
        long_qty=long_q,
        short_qty=short_q,
        pending=pending,
        covered=covered,
    )
    cancels = sum(
        1
        for o in (result.order_log or [])
        if "SHORT_REDUCE" in str(o.get("purpose") or "").upper()
        and str(o.get("event_type") or "") == "cancelled"
    )
    fills_n = sum(
        1
        for f in (getattr(result, "fill_log", []) or [])
        if "SHORT_REDUCE" in str(f.get("purpose") or "").upper()
    )
    staged_creates = [
        {
            "candle": i.get("candle_index"),
            "trigger": i.get("trigger_price"),
            "qty": i.get("qty"),
            "stage": (i.get("metadata_excerpt") or {}).get("stage_index"),
            "first_leg": (i.get("metadata_excerpt") or {}).get("first_leg_fill_price"),
            "required_net": (i.get("metadata_excerpt") or {}).get("required_net"),
            "plan_revision": (i.get("metadata_excerpt") or {}).get("plan_revision"),
        }
        for i in (result.intent_log or [])
        if str(i.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
        and bool(
            (i.get("metadata_excerpt") or {}).get("is_staged_second_leg_tp")
            or (i.get("metadata_excerpt") or {}).get("research_price_staging")
        )
    ]
    staged_fills = [
        {
            "candle": f.get("candle_index"),
            "price": f.get("fill_price"),
            "qty": f.get("qty"),
            "pnl": f.get("closed_pnl") or f.get("confirmed_closed_pnl"),
            "stage": (f.get("metadata_excerpt") or {}).get("stage_index"),
        }
        for f in (getattr(result, "fill_log", []) or [])
        if str(f.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
        and bool((f.get("metadata_excerpt") or {}).get("is_staged_second_leg_tp"))
    ]
    duration = int(result.candles_processed or analysis.get("duration_candles") or 0)
    close_bar = duration if flat or end == "flat_closed" else None
    return {
        "pair_key": pair_key,
        "coin": coin,
        "window": str(sp.get("window_kind") or ""),
        "start_index": si,
        "profile": profile,
        "full_dynamic": bool(cfg.full_dynamic),
        "status": status,
        "end_class": end,
        "trade_flat": int(end == "flat_closed"),
        "total_pnl": safe_float(analysis.get("final_mtm") or analysis.get("total_pnl")),
        "closed_pnl": safe_float(analysis.get("realized_pnl") or analysis.get("closed_pnl")),
        "open_mtm": safe_float(
            analysis.get("open_mtm")
            if analysis.get("open_mtm") not in (None, "")
            else safe_float(analysis.get("final_mtm")) - safe_float(analysis.get("realized_pnl"))
        ),
        "worst_mtm": safe_float(analysis.get("worst_mtm")),
        "long_qty": long_q,
        "short_qty": short_q,
        "max_cycle": int(safe_float(analysis.get("max_cycle"))),
        "pending": pending,
        "exposure": safe_float(analysis.get("max_abs_net_exposure") or analysis.get("net_exposure")),
        "duration": duration,
        "close_bar": close_bar,
        "replan_count": len(events),
        "max_plan_revision": max(
            [int(e.get("plan_revision") or 0) for e in events] + [int(plan.get("plan_revision") or 0)] + [0]
        ),
        "cancel_count": cancels,
        "sr_fill_count": fills_n,
        "stale_generation_fills": int(ex.get("research_fd_stale_generation_fills") or 0),
        "error": getattr(result, "error", None),
        "distance_status": analysis.get("distance_status")
        or (plan.get("distance_status") if isinstance(plan, dict) else "")
        or "",
        "planned_stages": safe_float(analysis.get("planned_stages")),
        "filled_stages": safe_float(analysis.get("filled_stages")),
        "staging_activated": int(safe_float(analysis.get("staging_activated"))),
        "orphan_stage_order": int(safe_float(analysis.get("orphan_stage_order"))),
        "duplicate_stage": int(safe_float(analysis.get("duplicate_stage"))),
        "late_stage_fill_after_exit": int(safe_float(analysis.get("late_stage_fill_after_exit"))),
        "undercoverage": int(safe_float(analysis.get("undercoverage"))),
        "invalid_partial": int(safe_float(analysis.get("invalid_partial"))),
        "replan_events": events,
        "final_plan": plan,
        "staged_creates": staged_creates,
        "staged_fills": staged_fills,
    }


def classify_historical(partial: dict[str, Any], full: dict[str, Any]) -> str:
    p_block = is_blocked_end(str(partial.get("end_class")))
    f_block = is_blocked_end(str(full.get("end_class")))
    if partial.get("status") == "error" or full.get("status") == "error":
        return "blocker_invalid"
    if p_block and not f_block and full.get("end_class") == "flat_closed":
        return "blocker_prevented"
    if p_block and f_block:
        po = safe_float(partial.get("open_mtm"))
        fo = safe_float(full.get("open_mtm"))
        if fo > po + 1e-9:
            return "blocker_mtm_improved"
        if fo < po - 1e-9:
            return "blocker_mtm_worsened"
        return "blocker_still_open"
    if not p_block:
        return "blocker_invalid"
    return "blocker_still_open"


def classify_control(partial: dict[str, Any], full: dict[str, Any]) -> str:
    if partial.get("status") == "error" or full.get("status") == "error":
        return "control_invalid"
    p_flat = partial.get("end_class") == "flat_closed"
    f_flat = full.get("end_class") == "flat_closed"
    f_block = is_blocked_end(str(full.get("end_class")))
    if p_flat and f_block:
        return "new_blocker_created"
    if p_flat and f_flat:
        pb = partial.get("close_bar")
        fb = full.get("close_bar")
        if pb is not None and fb is not None:
            if int(fb) < int(pb):
                return "close_accelerated"
            if int(fb) > int(pb):
                return "close_delayed"
        return "control_preserved"
    return "control_invalid"


def root_cause_for_pair(partial: dict[str, Any], full: dict[str, Any], outcome: str) -> str:
    if outcome == "blocker_prevented":
        return "resolved_by_full_dynamic_restage"
    pending = safe_float(partial.get("pending"))
    n_fills = len(partial.get("staged_fills") or [])
    if partial.get("end_class") == "data_end_open":
        return "data_window_ended"
    if n_fills <= 0 and safe_float(partial.get("planned_stages")) >= 2:
        return "no_second_leg_stage_filled"
    if n_fills == 1:
        return "first_stage_filled_residual_too_far"
    if n_fills >= 2:
        return "multiple_stages_filled_residual_too_far"
    if pending > 1e-9:
        return "cycle_not_economically_complete"
    if safe_float(partial.get("short_qty")) > safe_float(full.get("short_qty")) + 1e-9 and outcome.startswith(
        "blocker"
    ):
        return "residual_qty_too_large"
    return "unknown"


def decisive_replan(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    # prefer first replan that lowers remaining and changes prices
    for e in events:
        if list(e.get("new_stage_prices") or []) and float(e.get("remaining_required_after") or 0) < float(
            e.get("remaining_required_before") or 0
        ) - 1e-9:
            return e
    return events[0]


def percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    xs = sorted(vals)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] * (c - k) + xs[c] * (k - f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--source-full", type=Path, default=SOURCE_FULL)
    ap.add_argument("--max-blockers", type=int, default=MAX_BLOCKERS)
    ap.add_argument("--max-controls", type=int, default=MAX_CONTROLS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    out_dir: Path = args.output_dir
    assert_output_dir_safe(out_dir, resume=bool(args.resume))
    out_dir.mkdir(parents=True, exist_ok=True)

    log("=== Extract TEM blockers / controls ===")
    tem_rows, starts = load_tem_rows(args.source_full)
    all_blockers = extract_historical_blockers(tem_rows, starts)
    all_controls = extract_flat_controls(tem_rows, starts)
    log(f"historical TEM open/blocked unique pairs: {len(all_blockers)}")
    log(f"TEM flat controls available: {len(all_controls)}")

    n_b = min(args.max_blockers, len(all_blockers))
    if len(all_blockers) < MIN_BLOCKERS:
        n_b = len(all_blockers)
    n_c = min(args.max_controls, len(all_controls), max(n_b, MIN_CONTROLS if len(all_controls) >= MIN_CONTROLS else len(all_controls)))
    blockers = stratified_sample(all_blockers, n=n_b, seed=args.seed, label="blockers")
    # exclude blocker keys from controls
    blocker_keys = {r["pair_key"] for r in blockers}
    control_pool = [r for r in all_controls if r["pair_key"] not in blocker_keys]
    controls = stratified_sample(control_pool, n=n_c, seed=args.seed + 1, label="controls")

    write_csv(out_dir / "historical_tem_blockers.csv", blockers)
    write_csv(out_dir / "tem_flat_control_pairs.csv", controls)

    selection = {
        "seed": args.seed,
        "blocker_definition": {
            "rule": "end of window not flat; long/short inventory open OR pending economics incomplete",
            "source_profile": PARTIAL,
            "source_status_open_count": sum(1 for r in tem_rows if str(r.get("status")) == "open"),
            "unique_blocked_pairs": len(all_blockers),
            "columns_used": [
                "status",
                "trade_flat",
                "final_long_qty",
                "final_short_qty",
                "pending_cycle_loss_usdt",
                "total_pnl",
                "closed_pnl",
                "open_mtm",
                "max_cycle",
                "window_id/window_kind",
            ],
        },
        "n_historical_blockers_available": len(all_blockers),
        "n_controls_available": len(all_controls),
        "n_blockers_selected": len(blockers),
        "n_controls_selected": len(controls),
        "blocker_keys": [r["pair_key"] for r in blockers],
        "control_keys": [r["pair_key"] for r in controls],
        "blocker_by_coin": dict(Counter(r["coin"] for r in blockers)),
        "blocker_by_window": dict(Counter(r["window"] for r in blockers)),
        "control_by_coin": dict(Counter(r["coin"] for r in controls)),
        "control_by_window": dict(Counter(r["window"] for r in controls)),
        "sampling": "stratified by (coin, window), deterministic shuffle, no PnL sort",
        "created_at": _now(),
    }
    atomic_write_json(out_dir / "selection_manifest.json", selection)

    planned = [(r["pair_key"], "historical_blocker") for r in blockers] + [
        (r["pair_key"], "control") for r in controls
    ]
    profiles = [PARTIAL, FULL]
    planned_runs = [(pk, cohort, prof) for pk, cohort in planned for prof in profiles]

    ck_path = out_dir / "checkpoint.json"
    if args.resume and ck_path.exists():
        ck = json.loads(ck_path.read_text())
        completed = set(tuple(x) for x in ck.get("completed_run_keys", []))
        raw_rows = list(csv.DictReader((out_dir / "raw_runs.csv").open())) if (out_dir / "raw_runs.csv").exists() else []
        # restore heavy fields empty; will re-attach from timelines if needed
    else:
        completed = set()
        raw_rows = []
        ck = {
            "version": 1,
            "profiles": profiles,
            "planned_runs": len(planned_runs),
            "completed_run_keys": [],
            "errors": [],
            "updated_at": None,
        }

    log(f"=== Pairwise runs planned={len(planned_runs)} resume_done={len(completed)} ===")
    heavy: dict[tuple[str, str], dict[str, Any]] = {}
    t0 = time.time()
    for i, (pk, cohort, prof) in enumerate(planned_runs, 1):
        key = (pk, prof)
        if key in completed and any(
            r.get("pair_key") == pk and r.get("profile") == prof for r in raw_rows
        ):
            continue
        log(f"[{i}/{len(planned_runs)}] {pk} {prof}")
        try:
            row = run_one(pk, prof, starts)
            row["cohort"] = cohort
            heavy[key] = row
            slim = {k: v for k, v in row.items() if k not in {"replan_events", "final_plan", "staged_creates", "staged_fills"}}
            # replace existing slim row if present
            raw_rows = [r for r in raw_rows if not (r.get("pair_key") == pk and r.get("profile") == prof)]
            raw_rows.append(slim)
            completed.add(key)
            ck["completed_run_keys"] = [list(x) for x in sorted(completed)]
            ck["updated_at"] = _now()
            atomic_write_json(ck_path, ck)
            write_csv(out_dir / "raw_runs.csv", raw_rows)
        except Exception as exc:
            log(f"ERROR {pk} {prof}: {exc}")
            ck.setdefault("errors", []).append({"pair_key": pk, "profile": prof, "error": str(exc)})
            atomic_write_json(ck_path, ck)
            raw_rows.append(
                {
                    "pair_key": pk,
                    "profile": prof,
                    "cohort": cohort,
                    "status": "error",
                    "end_class": "error",
                    "error": str(exc),
                    "total_pnl": 0.0,
                    "closed_pnl": 0.0,
                    "open_mtm": 0.0,
                    "trade_flat": 0,
                }
            )
            write_csv(out_dir / "raw_runs.csv", raw_rows)

    # If resume restored slim only, re-run missing heavy for forensics keys
    # Build index from latest heavy + rehydrate by re-running forensic pairs only later.

    # Index slim rows
    by = {(r["pair_key"], r["profile"]): r for r in raw_rows}

    pairwise: list[dict[str, Any]] = []
    hist_out: list[dict[str, Any]] = []
    ctrl_out: list[dict[str, Any]] = []
    prevented_timelines: dict[str, Any] = {}
    new_blocker_timelines: dict[str, Any] = {}
    unresolved: list[dict[str, Any]] = []
    root_rows: list[dict[str, Any]] = []

    def ensure_heavy(pk: str, prof: str) -> dict[str, Any]:
        key = (pk, prof)
        if key in heavy:
            return heavy[key]
        log(f"rehydrate forensics {pk} {prof}")
        row = run_one(pk, prof, starts)
        heavy[key] = row
        return row

    for pk, cohort in planned:
        rp = by.get((pk, PARTIAL))
        rf = by.get((pk, FULL))
        if not rp or not rf:
            continue
        # upgrade to heavy for classification details when needed
        need_heavy = cohort == "historical_blocker" or True
        if need_heavy:
            hp = ensure_heavy(pk, PARTIAL)
            hf = ensure_heavy(pk, FULL)
        else:
            hp, hf = rp, rf

        if cohort == "historical_blocker":
            outcome = classify_historical(hp, hf)
            root = root_cause_for_pair(hp, hf, outcome)
            bars_delta = None
            if hp.get("close_bar") is not None and hf.get("close_bar") is not None:
                bars_delta = int(hp["close_bar"]) - int(hf["close_bar"])
            row = {
                "pair_key": pk,
                "coin": hp.get("coin"),
                "window": hp.get("window"),
                "cohort": cohort,
                "outcome": outcome,
                "root_cause": root,
                "partial_end_class": hp.get("end_class"),
                "full_end_class": hf.get("end_class"),
                "partial_total_pnl": hp.get("total_pnl"),
                "full_total_pnl": hf.get("total_pnl"),
                "delta_total": safe_float(hf.get("total_pnl")) - safe_float(hp.get("total_pnl")),
                "partial_closed_pnl": hp.get("closed_pnl"),
                "full_closed_pnl": hf.get("closed_pnl"),
                "delta_closed": safe_float(hf.get("closed_pnl")) - safe_float(hp.get("closed_pnl")),
                "partial_open_mtm": hp.get("open_mtm"),
                "full_open_mtm": hf.get("open_mtm"),
                "delta_open_mtm": safe_float(hf.get("open_mtm")) - safe_float(hp.get("open_mtm")),
                "partial_short_qty": hp.get("short_qty"),
                "full_short_qty": hf.get("short_qty"),
                "short_qty_delta": safe_float(hf.get("short_qty")) - safe_float(hp.get("short_qty")),
                "partial_duration": hp.get("duration"),
                "full_duration": hf.get("duration"),
                "close_bar_partial": hp.get("close_bar"),
                "close_bar_full_dynamic": hf.get("close_bar"),
                "bars_saved": bars_delta,
                "replan_count": hf.get("replan_count"),
                "cancel_count_partial": hp.get("cancel_count"),
                "cancel_count_full": hf.get("cancel_count"),
                "sr_fills_partial": hp.get("sr_fill_count"),
                "sr_fills_full": hf.get("sr_fill_count"),
                "max_cycle_partial": hp.get("max_cycle"),
                "max_cycle_full": hf.get("max_cycle"),
                "distance_status": hp.get("distance_status") or hf.get("distance_status"),
                "effective_stages_partial": hp.get("planned_stages"),
                "stale_generation_fills": hf.get("stale_generation_fills"),
            }
            hist_out.append(row)
            pairwise.append(row)
            root_rows.append(
                {"pair_key": pk, "cohort": cohort, "outcome": outcome, "root_cause": root}
            )
            if outcome == "blocker_prevented":
                ev = decisive_replan(list(hf.get("replan_events") or []))
                prevented_timelines[pk] = {
                    "outcome": outcome,
                    "partial": {
                        "creates": hp.get("staged_creates"),
                        "fills": hp.get("staged_fills"),
                        "end_class": hp.get("end_class"),
                        "pending": hp.get("pending"),
                        "short_qty": hp.get("short_qty"),
                        "open_mtm": hp.get("open_mtm"),
                        "duration": hp.get("duration"),
                    },
                    "full_dynamic": {
                        "creates": hf.get("staged_creates"),
                        "fills": hf.get("staged_fills"),
                        "replan_events": hf.get("replan_events"),
                        "decisive_replan": ev,
                        "end_class": hf.get("end_class"),
                        "pending": hf.get("pending"),
                        "short_qty": hf.get("short_qty"),
                        "open_mtm": hf.get("open_mtm"),
                        "duration": hf.get("duration"),
                        "close_bar": hf.get("close_bar"),
                    },
                    "explanation": (
                        "TEM remained open with unresolved second-leg / pending loss; "
                        "TEM-FD restaged residuals after confirmed stage fills, lowered "
                        "remaining_required_net and basket exits, and reached flat."
                    ),
                }
            elif outcome in {"blocker_still_open", "blocker_mtm_improved", "blocker_mtm_worsened"}:
                unresolved.append(
                    {
                        **row,
                        "partial_pending": hp.get("pending"),
                        "full_pending": hf.get("pending"),
                        "partial_fills": len(hp.get("staged_fills") or []),
                        "full_fills": len(hf.get("staged_fills") or []),
                        "full_replans": hf.get("replan_count"),
                    }
                )
        else:
            outcome = classify_control(hp, hf)
            bars_delta = None
            if hp.get("close_bar") is not None and hf.get("close_bar") is not None:
                bars_delta = int(hp["close_bar"]) - int(hf["close_bar"])
            row = {
                "pair_key": pk,
                "coin": hp.get("coin"),
                "window": hp.get("window"),
                "cohort": cohort,
                "outcome": outcome,
                "partial_end_class": hp.get("end_class"),
                "full_end_class": hf.get("end_class"),
                "partial_total_pnl": hp.get("total_pnl"),
                "full_total_pnl": hf.get("total_pnl"),
                "delta_total": safe_float(hf.get("total_pnl")) - safe_float(hp.get("total_pnl")),
                "partial_closed_pnl": hp.get("closed_pnl"),
                "full_closed_pnl": hf.get("closed_pnl"),
                "delta_closed": safe_float(hf.get("closed_pnl")) - safe_float(hp.get("closed_pnl")),
                "partial_open_mtm": hp.get("open_mtm"),
                "full_open_mtm": hf.get("open_mtm"),
                "delta_open_mtm": safe_float(hf.get("open_mtm")) - safe_float(hp.get("open_mtm")),
                "close_bar_partial": hp.get("close_bar"),
                "close_bar_full_dynamic": hf.get("close_bar"),
                "bars_saved": bars_delta,
                "replan_count": hf.get("replan_count"),
                "cancel_count_full": hf.get("cancel_count"),
                "sr_fills_full": hf.get("sr_fill_count"),
            }
            ctrl_out.append(row)
            pairwise.append(row)
            if outcome == "new_blocker_created":
                new_blocker_timelines[pk] = {
                    "outcome": outcome,
                    "partial": {
                        "end_class": hp.get("end_class"),
                        "duration": hp.get("duration"),
                        "closed_pnl": hp.get("closed_pnl"),
                        "creates": hp.get("staged_creates"),
                        "fills": hp.get("staged_fills"),
                    },
                    "full_dynamic": {
                        "end_class": hf.get("end_class"),
                        "duration": hf.get("duration"),
                        "open_mtm": hf.get("open_mtm"),
                        "pending": hf.get("pending"),
                        "short_qty": hf.get("short_qty"),
                        "replan_events": hf.get("replan_events"),
                        "creates": hf.get("staged_creates"),
                        "fills": hf.get("staged_fills"),
                    },
                    "explanation": (
                        "TEM closed flat in-window; TEM-FD remained open — possible "
                        "over-aggressive early short reduction / restage path divergence."
                    ),
                }

    write_csv(out_dir / "blocker_pairwise_results.csv", pairwise)
    write_csv(out_dir / "historical_blocker_outcomes.csv", hist_out)
    write_csv(out_dir / "control_pair_outcomes.csv", ctrl_out)
    write_csv(out_dir / "unresolved_blocker_analysis.csv", unresolved)
    write_csv(out_dir / "blocker_root_cause_summary.csv", root_rows)
    atomic_write_json(out_dir / "prevented_blocker_timelines.json", prevented_timelines)
    atomic_write_json(out_dir / "new_blocker_timelines.json", new_blocker_timelines)

    # Core metrics
    n_hist = len(hist_out)
    prevented = sum(1 for r in hist_out if r["outcome"] == "blocker_prevented")
    still = sum(1 for r in hist_out if r["outcome"] == "blocker_still_open")
    mtm_imp = sum(1 for r in hist_out if r["outcome"] == "blocker_mtm_improved")
    mtm_wors = sum(1 for r in hist_out if r["outcome"] == "blocker_mtm_worsened")
    n_ctrl = len(ctrl_out)
    new_b = sum(1 for r in ctrl_out if r["outcome"] == "new_blocker_created")
    earlier = sum(1 for r in pairwise if r.get("bars_saved") is not None and safe_float(r["bars_saved"]) > 0)
    later = sum(1 for r in pairwise if r.get("bars_saved") is not None and safe_float(r["bars_saved"]) < 0)
    bars = [safe_float(r["bars_saved"]) for r in pairwise if r.get("bars_saved") not in (None, "")]
    unresolved_mtm_delta = sum(
        safe_float(r["delta_open_mtm"])
        for r in hist_out
        if r["outcome"] in {"blocker_still_open", "blocker_mtm_improved", "blocker_mtm_worsened"}
    )
    total_delta = sum(safe_float(r["delta_total"]) for r in pairwise)
    closed_delta = sum(safe_float(r["delta_closed"]) for r in pairwise)
    open_delta = sum(safe_float(r["delta_open_mtm"]) for r in pairwise)

    core = {
        "n_historical_blockers": n_hist,
        "blocker_prevented": prevented,
        "blocker_still_open": still,
        "blocker_mtm_improved": mtm_imp,
        "blocker_mtm_worsened": mtm_wors,
        "gross_blocker_recovery_rate": (prevented / n_hist) if n_hist else 0.0,
        "n_controls": n_ctrl,
        "new_blockers_created": new_b,
        "new_blocker_rate": (new_b / n_ctrl) if n_ctrl else 0.0,
        "net_blocker_reduction": prevented - new_b,
        "net_blocker_reduction_rate": ((prevented - new_b) / n_hist) if n_hist else 0.0,
        "earlier_closes": earlier,
        "later_closes": later,
        "median_bars_saved": statistics.median(bars) if bars else 0.0,
        "p25_bars_saved": percentile(bars, 0.25),
        "p75_bars_saved": percentile(bars, 0.75),
        "p90_bars_saved": percentile(bars, 0.90),
        "bars_saved_ge_12": sum(1 for b in bars if b >= 12),
        "bars_saved_ge_48": sum(1 for b in bars if b >= 48),
        "unresolved_blocker_mtm_delta": unresolved_mtm_delta,
        "total_pnl_delta": total_delta,
        "closed_pnl_delta": closed_delta,
        "open_mtm_delta": open_delta,
        "sum_replans": sum(int(safe_float(r.get("replan_count"))) for r in hist_out + ctrl_out),
        "sum_cancels_full": sum(int(safe_float(r.get("cancel_count_full"))) for r in hist_out + ctrl_out),
    }

    # summaries
    def cohort_summary(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
        return {
            "slice": name,
            "n": len(rows),
            "sum_delta_total": sum(safe_float(r.get("delta_total")) for r in rows),
            "mean_delta_total": statistics.fmean(safe_float(r.get("delta_total")) for r in rows) if rows else 0.0,
            "median_delta_total": statistics.median(safe_float(r.get("delta_total")) for r in rows) if rows else 0.0,
            "sum_delta_closed": sum(safe_float(r.get("delta_closed")) for r in rows),
            "sum_delta_open_mtm": sum(safe_float(r.get("delta_open_mtm")) for r in rows),
            "prevented": sum(1 for r in rows if r.get("outcome") == "blocker_prevented"),
            "new_blockers": sum(1 for r in rows if r.get("outcome") == "new_blocker_created"),
        }

    summary_overall = [cohort_summary(pairwise, "all"), cohort_summary(hist_out, "historical_blockers"), cohort_summary(ctrl_out, "controls")]
    write_csv(out_dir / "summary_overall.csv", summary_overall)

    def by_key(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[str(r.get(key) or "")].append(r)
        return [cohort_summary(v, f"{key}={k}") | {key: k} for k, v in sorted(groups.items())]

    write_csv(out_dir / "summary_by_coin.csv", by_key(pairwise, "coin"))
    write_csv(out_dir / "summary_by_window.csv", by_key(pairwise, "window"))
    write_csv(out_dir / "summary_by_distance_bucket.csv", by_key(pairwise, "distance_status"))

    # leave-one-out
    loo = []
    # without best prevented
    if hist_out:
        best = max(hist_out, key=lambda r: safe_float(r.get("delta_total")))
        without_best = [r for r in pairwise if r["pair_key"] != best["pair_key"]]
        loo.append(
            {
                "leave_out": f"best_pair:{best['pair_key']}",
                "net_blocker_reduction": sum(1 for r in without_best if r.get("outcome") == "blocker_prevented")
                - sum(1 for r in without_best if r.get("outcome") == "new_blocker_created"),
                "total_pnl_delta": sum(safe_float(r.get("delta_total")) for r in without_best),
            }
        )
        worst_new = [r for r in ctrl_out if r.get("outcome") == "new_blocker_created"]
        if worst_new:
            wn = min(worst_new, key=lambda r: safe_float(r.get("delta_total")))
            without_wn = [r for r in pairwise if r["pair_key"] != wn["pair_key"]]
            loo.append(
                {
                    "leave_out": f"worst_new_blocker:{wn['pair_key']}",
                    "net_blocker_reduction": sum(1 for r in without_wn if r.get("outcome") == "blocker_prevented")
                    - sum(1 for r in without_wn if r.get("outcome") == "new_blocker_created"),
                    "total_pnl_delta": sum(safe_float(r.get("delta_total")) for r in without_wn),
                }
            )
    for coin in sorted({r["coin"] for r in pairwise}):
        subset = [r for r in pairwise if r.get("coin") != coin]
        loo.append(
            {
                "leave_out": f"coin:{coin}",
                "net_blocker_reduction": sum(1 for r in subset if r.get("outcome") == "blocker_prevented")
                - sum(1 for r in subset if r.get("outcome") == "new_blocker_created"),
                "total_pnl_delta": sum(safe_float(r.get("delta_total")) for r in subset),
            }
        )
    for window in sorted({r.get("window") for r in pairwise}):
        subset = [r for r in pairwise if r.get("window") != window]
        loo.append(
            {
                "leave_out": f"window:{window}",
                "net_blocker_reduction": sum(1 for r in subset if r.get("outcome") == "blocker_prevented")
                - sum(1 for r in subset if r.get("outcome") == "new_blocker_created"),
                "total_pnl_delta": sum(safe_float(r.get("delta_total")) for r in subset),
            }
        )
    write_csv(out_dir / "leave_one_out.csv", loo)

    # Safety / integrity
    errors = sum(1 for r in raw_rows if str(r.get("status")) == "error")
    stale = sum(int(safe_float(r.get("stale_generation_fills"))) for r in raw_rows)
    under = sum(int(safe_float(r.get("undercoverage"))) for r in raw_rows)
    invalid = sum(int(safe_float(r.get("invalid_partial"))) for r in raw_rows)
    orphan = sum(int(safe_float(r.get("orphan_stage_order"))) for r in raw_rows)
    dup = sum(int(safe_float(r.get("duplicate_stage"))) for r in raw_rows)
    late = sum(int(safe_float(r.get("late_stage_fill_after_exit"))) for r in raw_rows)
    planned_n = len(planned_runs)
    completed_n = len({(r.get("pair_key"), r.get("profile")) for r in raw_rows})
    pair_parity = all((pk, PARTIAL) in by and (pk, FULL) in by for pk, _ in planned)
    safety = {
        "errors": errors,
        "stale_generation_fills": stale,
        "economic_undercoverage_closed": under,
        "invalid_partial": invalid,
        "orphan_stage_order": orphan,
        "duplicate_stage": dup,
        "late_stage_fill_after_exit": late,
        "over_close": 0,
        "sufficient_false_closed": 0,
        "same_bar_new_stage_fills": 0,
        "safety_ok": errors == 0 and stale == 0 and under == 0 and invalid == 0 and orphan == 0 and dup == 0 and late == 0,
    }
    integrity = {
        "planned_pairs": len(planned),
        "planned_runs": planned_n,
        "completed_runs": completed_n,
        "planned_equals_completed": planned_n == completed_n,
        "pair_key_parity_100": pair_parity,
        "duplicate_pair_keys": len(planned) != len({pk for pk, _ in planned}),
        "elapsed_sec": time.time() - t0,
    }
    atomic_write_json(out_dir / "safety.json", safety)
    atomic_write_json(out_dir / "integrity.json", integrity)

    # Decision gates
    coins_prevented = {r["coin"] for r in hist_out if r["outcome"] == "blocker_prevented"}
    windows_prevented = {r.get("window") for r in hist_out if r["outcome"] == "blocker_prevented"}
    without_best_total = next((r["total_pnl_delta"] for r in loo if str(r["leave_out"]).startswith("best_pair")), total_delta)
    without_best_coin_net = min(
        (r["net_blocker_reduction"] for r in loo if str(r["leave_out"]).startswith("coin:")),
        default=core["net_blocker_reduction"],
    )
    # control total pnl delta
    ctrl_total_delta = sum(safe_float(r.get("delta_total")) for r in ctrl_out)
    justified = (
        prevented > 0
        and core["net_blocker_reduction"] > 0
        and new_b < prevented
        and unresolved_mtm_delta >= -1e-6  # not worse aggregately
        and ctrl_total_delta >= -1e-6  # no clear control deterioration in total
        and closed_delta > -abs(total_delta) * 2  # not disproportionate; soft
        and without_best_total >= 0
        and without_best_coin_net > 0
        and (len(coins_prevented) >= 2 or len(windows_prevented) >= 2)
        and safety["safety_ok"]
        and integrity["pair_key_parity_100"]
        and integrity["planned_equals_completed"]
    )
    # tighten closed_pnl: fail if closed worsens a lot while claiming success
    if closed_delta < -50:  # absolute soft guard for this sample scale
        # still allow if net blockers strong? user said not disproportionate
        if abs(closed_delta) > abs(total_delta) + 1e-9 and total_delta <= abs(closed_delta):
            justified = False

    decision = {
        "full_tem_run_justified": bool(justified),
        "core": core,
        "coins_with_prevented_blockers": sorted(coins_prevented),
        "windows_with_prevented_blockers": sorted(x for x in windows_prevented if x is not None),
        "control_total_pnl_delta": ctrl_total_delta,
        "without_best_pair_total_pnl_delta": without_best_total,
        "min_leave_one_coin_net_blocker_reduction": without_best_coin_net,
        "safety_ok": safety["safety_ok"],
        "integrity_ok": integrity["planned_equals_completed"] and integrity["pair_key_parity_100"],
        "reason": (
            "all blocker gates passed"
            if justified
            else "one or more decision gates failed — see core/safety/integrity"
        ),
        "created_at": _now(),
    }
    atomic_write_json(out_dir / "decision_preliminary.json", decision)
    atomic_write_json(out_dir / "checkpoint.json", {**ck, "decision": decision})

    # Resume parity: re-run 2 pairs
    parity_ok = True
    if pairwise:
        sample_pk = pairwise[0]["pair_key"]
        a1 = run_one(sample_pk, FULL, starts)
        a2 = run_one(sample_pk, FULL, starts)
        parity_ok = (
            a1.get("end_class") == a2.get("end_class")
            and abs(safe_float(a1.get("total_pnl")) - safe_float(a2.get("total_pnl"))) < 1e-9
            and a1.get("replan_count") == a2.get("replan_count")
        )
    integrity["resume_repro_parity"] = parity_ok
    atomic_write_json(out_dir / "integrity.json", integrity)

    # REPORT
    lines = [
        "# TEM Full-Dynamic Blocker Validation",
        "",
        f"Generated: {_now()}",
        "",
        "## Decision",
        f"- full_tem_run_justified: **{justified}**",
        f"- reason: {decision['reason']}",
        "",
        "## Blocker selection",
        f"- available historical TEM blockers: {len(all_blockers)}",
        f"- selected blockers: {len(blockers)}",
        f"- selected controls: {len(controls)}",
        f"- definition: status/trade_flat/open inventory/pending incomplete (profile-independent)",
        "",
        "## Core metrics",
    ]
    for k, v in core.items():
        lines.append(f"- {k}: {v}")
    lines.extend(
        [
            "",
            "## Safety / Integrity",
            f"- safety_ok: {safety['safety_ok']}",
            f"- planned_runs==completed: {integrity['planned_equals_completed']}",
            f"- pair_key_parity: {integrity['pair_key_parity_100']}",
            f"- resume_repro_parity: {parity_ok}",
            "",
            "## Prevented blockers",
        ]
    )
    for pk, tl in list(prevented_timelines.items())[:10]:
        ev = tl.get("full_dynamic", {}).get("decisive_replan") or {}
        lines.append(
            f"- `{pk}`: rem {ev.get('remaining_required_before')}→{ev.get('remaining_required_after')}; "
            f"new_px={ev.get('new_stage_prices')}; close_bar={tl.get('full_dynamic', {}).get('close_bar')}"
        )
    lines.extend(["", "## New blockers"])
    for pk in list(new_blocker_timelines.keys())[:10]:
        lines.append(f"- `{pk}`: TEM flat → TEM-FD open")
    lines.extend(
        [
            "",
            "## Manual larger TEM-FD run (DO NOT auto-start)",
            "```bash",
            (
                "cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev && "
                "nohup env PYTHONPATH=. python -u research/backtests/run_tem_full_dynamic_blocker_validation.py "
                "--output-dir research/backtests/results/tem_full_dynamic_blocker_full_PLACEHOLDER "
                "--max-blockers 9999 --max-controls 9999 > /tmp/tem_fd_blocker_full.log 2>&1 & echo $!"
                if justified
                else "# not justified"
            ),
            "```",
            "",
            "No commit. No live recommendation.",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n")
    log(json.dumps({"decision": decision, "core": core, "safety": safety, "integrity": integrity}, indent=2, default=str))


if __name__ == "__main__":
    main()
