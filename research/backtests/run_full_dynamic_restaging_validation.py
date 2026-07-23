#!/usr/bin/env python3
"""FULL_DYNAMIC residual restaging validation (research-only).

Phases:
  1) gold forensics APTUSDT|early|4026
  2) small deterministic sample (only if gold gates green)
  3) REPORT.md + CSVs

Does NOT start a 3375-pair full run.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.full_dynamic_second_leg_restaging import FULL_DYNAMIC_PROFILE_NAMES
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    FULL_HISTORY_CANDLE_LIMIT,
    analyze_blocker_run,
    run_isolated_blocker,
)
from research.backtests.multicoin_price_staging_grid import assert_output_dir_safe, write_csv
from research.backtests.second_leg_price_staging import resolve_grid_profile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = (
    ROOT / "research/backtests/results/full_dynamic_restaging_validation_20260722"
)
SOURCE_FULL = (
    ROOT / "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722"
)
GOLD_PAIR = "APTUSDT|early|4026"
PARTIAL = ("two_early_medium", "adaptive_equal", "fixed_step_1pct_equal")
FULL = tuple(FULL_DYNAMIC_PROFILE_NAMES)
ALL_PROFILES = PARTIAL + FULL
SEED = 20260722


def log(msg: str) -> None:
    print(msg, flush=True)


def _load_starts(source: Path) -> dict[str, dict[str, Any]]:
    path = source / "start_points.csv"
    return {r["pair_key"]: r for r in csv.DictReader(path.open())}


def _run_pair(pair_key: str, profile: str, starts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sp = starts[pair_key]
    coin = str(sp["coin"]).upper()
    si = int(sp["start_index"])
    mw = int(float(sp["max_window_candles"]))
    candles = normalize_candles(coin, load_candles_for_symbol(coin, limit=FULL_HISTORY_CANDLE_LIMIT))
    series = candles[: si + mw]
    cfg = resolve_grid_profile(profile)
    result = run_isolated_blocker(coin=coin, candles=series, start_index=si, staging_config=cfg)
    analysis = analyze_blocker_run(
        coin=coin,
        trade_number=int(float(sp.get("trade_number") or 0)),
        start_index=si,
        profile=profile,
        result=result,
        candles=series,
    )
    ex = dict(result.final_strategy_state_excerpt or {})
    events = list(ex.get("research_fd_replan_events") or [])
    plan = dict(ex.get("research_second_leg_price_staging_plan") or {})
    cancels = sum(
        1
        for o in (result.order_log or [])
        if "SHORT_REDUCE" in str(o.get("purpose") or "").upper()
        and str(o.get("event_type") or "") == "cancelled"
        and int((o.get("metadata_excerpt") or {}).get("cycle_index") or 0) == 4
    )
    staged_creates = sum(
        1
        for i in (result.intent_log or [])
        if str(i.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
        and bool(
            (i.get("metadata_excerpt") or {}).get("is_staged_second_leg_tp")
            or (i.get("metadata_excerpt") or {}).get("research_price_staging")
        )
    )
    return {
        "pair_key": pair_key,
        "coin": coin,
        "start_index": si,
        "window": str(sp.get("window") or ""),
        "profile": profile,
        "full_dynamic": bool(cfg.full_dynamic),
        "status": result.final_status,
        "trade_flat": bool(int(safe_float(analysis.get("trade_flat")))),
        "total_pnl": safe_float(analysis.get("final_mtm", analysis.get("total_pnl"))),
        "closed_pnl": safe_float(analysis.get("realized_pnl", analysis.get("closed_pnl"))),
        "open_mtm": safe_float(analysis.get("open_mtm")),
        "worst_mtm": safe_float(analysis.get("worst_mtm")),
        "max_abs_net_exposure": safe_float(analysis.get("max_abs_net_exposure", analysis.get("net_exposure"))),
        "staging_activated": bool(int(safe_float(analysis.get("staging_activated")))),
        "planned_stages": safe_float(analysis.get("planned_stages")),
        "filled_stages": safe_float(analysis.get("filled_stages")),
        "replan_count": len(events),
        "max_plan_revision": max([int(e.get("plan_revision") or 0) for e in events] + [int(plan.get("plan_revision") or 0)]),
        "cancel_count": cancels,
        "stage_create_count": staged_creates,
        "pending_final": safe_float(ex.get("pending_cycle_loss_usdt")),
        "realized_c4": safe_float((ex.get("staged_second_leg_tp_realized_net") or {}).get("4")),
        "required_c4": safe_float((ex.get("staged_second_leg_tp_required_net_total") or {}).get("4")),
        "stale_generation_fills": int(ex.get("research_fd_stale_generation_fills") or 0),
        "error": getattr(result, "error", None),
        "replan_events": events,
        "final_plan": plan,
    }


def gold_gates(row: dict[str, Any]) -> dict[str, bool]:
    events = list(row.get("replan_events") or [])
    plan = dict(row.get("final_plan") or {})
    price_changed = any(
        list(e.get("old_residual_prices") or []) != list(e.get("new_stage_prices") or [])
        and (e.get("new_stage_prices") or e.get("old_residual_prices"))
        for e in events
    )
    qty_changed = any(
        list(e.get("old_residual_qtys") or []) != list(e.get("new_stage_qtys") or [])
        for e in events
    )
    rem_drop = any(
        float(e.get("remaining_required_after") or 0)
        < float(e.get("remaining_required_before") or 0) - 1e-9
        for e in events
    )
    elig_ok = all(
        e.get("new_stage_eligible_from_candle") is None
        or e.get("candle_index") is None
        or int(e["new_stage_eligible_from_candle"]) >= int(e["candle_index"]) + 1
        for e in events
    )
    gates = {
        "has_replan": len(events) > 0,
        "plan_revision_increases": any(int(e.get("plan_revision") or 0) >= 1 for e in events)
        or int(plan.get("plan_revision") or 0) >= 1,
        "rest_prices_change": price_changed,
        "rest_qty_change_or_covered": qty_changed
        or any(not e.get("new_stage_qtys") for e in events),
        "remaining_required_drops": rem_drop,
        "eligible_t_plus_1": elig_ok,
        "cancels": int(row.get("cancel_count") or 0) > 0,
        "no_error": str(row.get("status") or "") != "error",
        "stale_generation_fill_zero": int(row.get("stale_generation_fills") or 0) == 0,
    }
    gates["all_green"] = all(gates.values())
    return gates


def select_sample(source: Path, starts: dict[str, dict[str, Any]], *, n_each: int = 10) -> list[str]:
    raw = list(csv.DictReader((source / "raw_profile_runs.csv").open()))
    # Prefer fixed_step rows for stage-count diversity; require staging activated.
    by_bucket: dict[str, list[str]] = {"2": [], "4": [], "7_8": []}
    seen: set[str] = set()
    for r in raw:
        if str(r.get("profile")) != "fixed_step_1pct_equal":
            continue
        if str(r.get("staging_activated") or "0") not in {"1", "True", "true"}:
            # fallback: planned_stages > 1
            if safe_float(r.get("planned_stages")) <= 1:
                continue
        pk = f"{str(r.get('coin')).upper()}|early|{int(float(r.get('start_index') or 0))}"
        # reconstruct pair_key from start_points if present
        # Prefer exact keys from starts matching coin+start
        coin = str(r.get("coin") or "").upper()
        si = int(float(r.get("start_index") or 0))
        candidates = [
            k
            for k, sp in starts.items()
            if str(sp.get("coin")).upper() == coin and int(sp["start_index"]) == si
        ]
        if not candidates:
            continue
        key = candidates[0]
        if key in seen:
            continue
        pl = int(safe_float(r.get("planned_stages") or r.get("effective_stage_count_after_rounding")))
        if pl == 2:
            bucket = "2"
        elif pl == 4:
            bucket = "4"
        elif pl in {7, 8}:
            bucket = "7_8"
        else:
            continue
        by_bucket[bucket].append(key)
        seen.add(key)

    rng = random.Random(SEED)
    selected: list[str] = []
    for bucket, pool in by_bucket.items():
        pool = sorted(set(pool))
        rng.shuffle(pool)
        selected.extend(pool[:n_each])

    # Ensure multi-coin coverage
    coins = {starts[k]["coin"].upper() for k in selected if k in starts}
    needed = {"APTUSDT", "BTCUSDT", "ETHUSDT"}
    missing = needed - coins
    if missing:
        for r in raw:
            coin = str(r.get("coin") or "").upper()
            if coin not in missing:
                continue
            si = int(float(r.get("start_index") or 0))
            cands = [
                k
                for k, sp in starts.items()
                if str(sp.get("coin")).upper() == coin and int(sp["start_index"]) == si
            ]
            if not cands:
                continue
            if cands[0] not in selected:
                selected.append(cands[0])
                missing.discard(coin)
            if not missing:
                break
    return sorted(set(selected))


def pairwise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by = {(r["pair_key"], r["profile"]): r for r in rows}
    pairs = [
        ("two_early_medium_full_dynamic", "two_early_medium"),
        ("adaptive_equal_full_dynamic", "adaptive_equal"),
        ("fixed_step_1pct_equal_full_dynamic", "fixed_step_1pct_equal"),
        ("adaptive_equal_full_dynamic", "two_early_medium_full_dynamic"),
        ("fixed_step_1pct_equal_full_dynamic", "adaptive_equal_full_dynamic"),
    ]
    out = []
    for a, b in pairs:
        for pk in sorted({r["pair_key"] for r in rows}):
            ra, rb = by.get((pk, a)), by.get((pk, b))
            if not ra or not rb:
                continue
            da = safe_float(ra["total_pnl"]) - safe_float(rb["total_pnl"])
            dc = safe_float(ra["closed_pnl"]) - safe_float(rb["closed_pnl"])
            do = safe_float(ra["open_mtm"]) - safe_float(rb["open_mtm"])
            verdict = "Equal"
            if da > 1e-9:
                verdict = "better"
            elif da < -1e-9:
                verdict = "worse"
            out.append(
                {
                    "pair_key": pk,
                    "profile_a": a,
                    "profile_b": b,
                    "total_a": ra["total_pnl"],
                    "total_b": rb["total_pnl"],
                    "delta_total": da,
                    "delta_closed": dc,
                    "delta_open_mtm": do,
                    "verdict": verdict,
                    "replan_a": ra.get("replan_count"),
                    "replan_b": rb.get("replan_count"),
                    "cancel_a": ra.get("cancel_count"),
                    "cancel_b": rb.get("cancel_count"),
                    "status_a": ra.get("status"),
                    "status_b": rb.get("status"),
                }
            )
    return out


def summarize_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    return {
        "n": len(rows),
        "sum_total_pnl": sum(safe_float(r["total_pnl"]) for r in rows),
        "sum_closed_pnl": sum(safe_float(r["closed_pnl"]) for r in rows),
        "sum_open_mtm": sum(safe_float(r["open_mtm"]) for r in rows),
        "flat": sum(1 for r in rows if r.get("trade_flat") or r.get("status") == "closed"),
        "open": sum(1 for r in rows if r.get("status") == "open"),
        "errors": sum(1 for r in rows if r.get("status") == "error"),
        "sum_replan": sum(int(r.get("replan_count") or 0) for r in rows),
        "avg_replan": statistics.fmean(int(r.get("replan_count") or 0) for r in rows),
        "avg_max_revision": statistics.fmean(int(r.get("max_plan_revision") or 0) for r in rows),
        "sum_cancels": sum(int(r.get("cancel_count") or 0) for r in rows),
        "stale_fills": sum(int(r.get("stale_generation_fills") or 0) for r in rows),
        "avg_worst_mtm": statistics.fmean(safe_float(r.get("worst_mtm")) for r in rows),
        "avg_exposure": statistics.fmean(safe_float(r.get("max_abs_net_exposure")) for r in rows),
    }


def write_report(out_dir: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# FULL_DYNAMIC Restaging Validation",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Decision",
        f"- FULL_DYNAMIC implemented: **{payload['implemented']}**",
        f"- Gold gates green: **{payload['gold_all_green']}**",
        f"- full_run_justified: **{payload['full_run_justified']}**",
        f"- Reason: {payload['decision_reason']}",
        "",
        "## Canonical economics",
        "```",
        "required_net_total = initial_pending + target_profit",
        "confirmed_stage_realized_net = sum(confirmed SHORT_REDUCE stage nets)",
        "remaining_required_net = max(required_net_total - confirmed_stage_realized_net, 0)",
        "pending_cycle_loss_usdt := max(initial_pending - confirmed_stage_realized_net, 0)",
        "```",
        "",
        "## Gold APTUSDT|early|4026",
    ]
    for prof, g in payload["gold_gates"].items():
        lines.append(f"- `{prof}`: {g}")
    lines.extend(["", "## Sample profile sums", ""])
    for prof, s in payload["profile_summaries"].items():
        lines.append(
            f"- `{prof}`: n={s.get('n')} sum_total={s.get('sum_total_pnl'):.4f} "
            f"sum_closed={s.get('sum_closed_pnl'):.4f} sum_open={s.get('sum_open_mtm'):.4f} "
            f"replans={s.get('sum_replan')} cancels={s.get('sum_cancels')}"
        )
    lines.extend(["", "## Pairwise (sample)", ""])
    for name, s in payload["pairwise_summaries"].items():
        lines.append(
            f"- `{name}`: better={s['better']} equal={s['equal']} worse={s['worse']} "
            f"delta_total={s['delta_total']:.4f} leaveout_best>={s['delta_without_best']:.4f}"
        )
    lines.extend(
        [
            "",
            "## Safety",
            f"- stale_generation_fills: {payload['safety']['stale_generation_fills']}",
            f"- errors: {payload['safety']['errors']}",
            "",
            "## Manual full-run command (DO NOT auto-start)",
            "```bash",
            payload.get("full_run_command") or "# n/a",
            "```",
            "",
            "No live recommendation. No commit.",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--source-full", type=Path, default=SOURCE_FULL)
    ap.add_argument("--gold-only", action="store_true")
    ap.add_argument("--sample-per-bucket", type=int, default=10)
    args = ap.parse_args()
    out_dir: Path = args.output_dir
    assert_output_dir_safe(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    starts = _load_starts(args.source_full)
    if GOLD_PAIR not in starts:
        raise SystemExit(f"missing gold start {GOLD_PAIR}")

    log("=== GOLD forensics ===")
    gold_rows = []
    gold_gates_map = {}
    timelines = {}
    for prof in ALL_PROFILES:
        log(f"gold {prof}")
        row = _run_pair(GOLD_PAIR, prof, starts)
        gold_rows.append({k: v for k, v in row.items() if k not in {"replan_events", "final_plan"}})
        timelines[prof] = {
            "replan_events": row.get("replan_events"),
            "final_plan": row.get("final_plan"),
            "status": row.get("status"),
            "cancel_count": row.get("cancel_count"),
            "replan_count": row.get("replan_count"),
            "max_plan_revision": row.get("max_plan_revision"),
        }
        if row["full_dynamic"]:
            g = gold_gates(row)
            gold_gates_map[prof] = g
            log(f"  gates {g}")

    gold_all_green = bool(gold_gates_map) and all(g.get("all_green") for g in gold_gates_map.values())
    write_csv(out_dir / "gold_profile_runs.csv", gold_rows)
    (out_dir / "full_dynamic_forensic_timelines.json").write_text(
        json.dumps(timelines, indent=2, default=str)
    )
    # Flatten gold events
    gold_events = []
    for prof, tl in timelines.items():
        for e in tl.get("replan_events") or []:
            gold_events.append({"pair_key": GOLD_PAIR, "profile": prof, **e})
    write_csv(out_dir / "full_dynamic_replan_events.csv", gold_events)

    sample_rows: list[dict[str, Any]] = []
    pairwise_rows: list[dict[str, Any]] = []
    profile_summaries: dict[str, Any] = {}
    pairwise_summaries: dict[str, Any] = {}
    full_run_justified = False
    decision_reason = "gold gates failed — sample skipped"

    if gold_all_green and not args.gold_only:
        log("=== SAMPLE selection ===")
        sample_keys = select_sample(args.source_full, starts, n_each=args.sample_per_bucket)
        # Always include gold
        if GOLD_PAIR not in sample_keys:
            sample_keys = [GOLD_PAIR] + sample_keys
        (out_dir / "sample_pair_keys.json").write_text(json.dumps(sample_keys, indent=2))
        log(f"sample size {len(sample_keys)}")
        for pk in sample_keys:
            for prof in ALL_PROFILES:
                log(f"sample {pk} {prof}")
                row = _run_pair(pk, prof, starts)
                # attach events for gold-like pairs into events csv
                for e in row.get("replan_events") or []:
                    gold_events.append({"pair_key": pk, "profile": prof, **e})
                sample_rows.append({k: v for k, v in row.items() if k not in {"replan_events", "final_plan"}})
        write_csv(out_dir / "sample_raw_runs.csv", sample_rows)
        write_csv(out_dir / "full_dynamic_replan_events.csv", gold_events)
        pairwise_rows = pairwise(sample_rows)
        write_csv(out_dir / "full_dynamic_pairwise.csv", pairwise_rows)
        for prof in ALL_PROFILES:
            profile_summaries[prof] = summarize_profile([r for r in sample_rows if r["profile"] == prof])
        for a, b in [
            ("two_early_medium_full_dynamic", "two_early_medium"),
            ("adaptive_equal_full_dynamic", "adaptive_equal"),
            ("fixed_step_1pct_equal_full_dynamic", "fixed_step_1pct_equal"),
            ("adaptive_equal_full_dynamic", "two_early_medium_full_dynamic"),
            ("fixed_step_1pct_equal_full_dynamic", "adaptive_equal_full_dynamic"),
        ]:
            subset = [p for p in pairwise_rows if p["profile_a"] == a and p["profile_b"] == b]
            deltas = [safe_float(p["delta_total"]) for p in subset]
            best = max(deltas) if deltas else 0.0
            without = sum(deltas) - best if deltas else 0.0
            pairwise_summaries[f"{a}_vs_{b}"] = {
                "n": len(subset),
                "better": sum(1 for p in subset if p["verdict"] == "better"),
                "equal": sum(1 for p in subset if p["verdict"] == "equal"),
                "worse": sum(1 for p in subset if p["verdict"] == "worse"),
                "delta_total": sum(deltas),
                "delta_without_best": without,
            }

        # Justification: each FD vs its partial improves total, no error spike, leaveout>=0
        ok = True
        reasons = []
        for a, b in [
            ("two_early_medium_full_dynamic", "two_early_medium"),
            ("adaptive_equal_full_dynamic", "adaptive_equal"),
            ("fixed_step_1pct_equal_full_dynamic", "fixed_step_1pct_equal"),
        ]:
            s = pairwise_summaries[f"{a}_vs_{b}"]
            if s["delta_total"] <= 0:
                ok = False
                reasons.append(f"{a} delta_total {s['delta_total']:.4f} <= 0")
            if s["delta_without_best"] < 0:
                ok = False
                reasons.append(f"{a} leaveout_best {s['delta_without_best']:.4f} < 0")
        if any(int(r.get("stale_generation_fills") or 0) for r in sample_rows):
            ok = False
            reasons.append("stale generation fills > 0")
        if any(r.get("status") == "error" for r in sample_rows if r.get("full_dynamic")):
            ok = False
            reasons.append("FD profile errors")
        full_run_justified = ok
        decision_reason = "all economic/safety gates passed" if ok else "; ".join(reasons)
    elif not gold_all_green:
        decision_reason = "gold gates failed — sample skipped"
    else:
        decision_reason = "gold-only mode"

    integrity = {
        "gold_all_green": gold_all_green,
        "gold_gates": gold_gates_map,
        "sample_n_pairs": len({r["pair_key"] for r in sample_rows}) if sample_rows else 0,
        "full_run_justified": full_run_justified,
        "decision_reason": decision_reason,
        "seed": SEED,
    }
    (out_dir / "full_dynamic_integrity.json").write_text(json.dumps(integrity, indent=2, default=str))

    # stage generations export
    gens = []
    for e in gold_events:
        gens.append(
            {
                "pair_key": e.get("pair_key"),
                "profile": e.get("profile"),
                "cycle_index": e.get("cycle_index"),
                "plan_revision": e.get("plan_revision"),
                "candle_index": e.get("candle_index"),
                "new_stage_count": e.get("new_stage_count"),
                "new_stage_prices": e.get("new_stage_prices"),
                "new_stage_qtys": e.get("new_stage_qtys"),
                "eligible_from": e.get("new_stage_eligible_from_candle"),
            }
        )
    write_csv(out_dir / "full_dynamic_stage_generations.csv", gens)

    safety = {
        "stale_generation_fills": sum(int(r.get("stale_generation_fills") or 0) for r in sample_rows + gold_rows),
        "errors": sum(1 for r in sample_rows + gold_rows if r.get("status") == "error"),
    }
    payload = {
        "implemented": True,
        "gold_all_green": gold_all_green,
        "gold_gates": gold_gates_map,
        "profile_summaries": profile_summaries,
        "pairwise_summaries": pairwise_summaries,
        "full_run_justified": full_run_justified,
        "decision_reason": decision_reason,
        "safety": safety,
        "full_run_command": (
            "cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev && "
            "nohup env PYTHONPATH=. python -u research/backtests/run_full_dynamic_restaging_validation.py "
            "--output-dir research/backtests/results/full_dynamic_restaging_full_PLACEHOLDER "
            "--sample-per-bucket 9999 > /tmp/fd_full.log 2>&1 & echo $!"
            if full_run_justified
            else "# FULL_DYNAMIC not justified — no full-run command"
        ),
    }
    write_report(out_dir, payload)
    log(json.dumps({"gold_all_green": gold_all_green, "full_run_justified": full_run_justified, "reason": decision_reason}, indent=2))


if __name__ == "__main__":
    main()
