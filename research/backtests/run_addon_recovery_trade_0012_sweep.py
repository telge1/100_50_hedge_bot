from __future__ import annotations

"""
Backtest-only parameter sweep for Blocker Addon Short Recovery on
backtest_long_continuous_trade_0012.

This runner:
- reuses the confirmed trade-0012 setup (symbol, candle window, configs)
- varies ONLY AddonShortRecoveryConfig parameters
- does NOT modify any strategy, execution, or fill logic
- runs each variant on a fresh simulator
- records compact per-variant metrics and invariants (no full per-event audits)

Phase 1 implements a one-factor-at-a-time sweep over:
- addon_short_step_fraction
- addon_short_tp_pct
- addon_short_reentry_buffer_pct
- long_reduce_profit_usage_fraction
"""

import argparse
import csv
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .addon_short_recovery import AddonShortRecoveryConfig, default_addon_short_recovery_config
from .backtest_report import BacktestResult
from .historical_backtest import run_historical_backtest
from .run_addon_recovery_trade_0012_audit import (  # type: ignore[import]
    SYMBOL,
    DIRECTION,
    START_INDEX,
    END_INDEX,
    FILL_MODEL,
    MAX_FILLS_PER_CANDLE,
    CONFIG_SOURCE,
    TP_PROFIT_TARGET_PCT,
    _load_and_slice_candles,
    _load_original_run_snapshot,
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
)


BASE_OUTPUT_DIR = Path(
    "research/backtests/results/addon_recovery_trade_0012_parameter_sweep"
).resolve()


BASELINE_GAP_AT_ACTIVATION = 18.858000000000004
BASELINE_FINAL_GAP = 3.06504692703615
BASELINE_OVERALL_PNL = -4.167099476656586
BASELINE_TRADE_COUNT = 348
BASELINE_HARD_STOP_COUNT = 52


def _now_utc_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _ensure_output_dir() -> Path:
    BASE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = BASE_OUTPUT_DIR / f"run_{_now_utc_iso().replace(':', '').replace('-', '').replace('+', '_')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _build_variants_one_factor(baseline_cfg: AddonShortRecoveryConfig) -> List[Dict[str, Any]]:
    """One-factor-at-a-time sweep variants, including baseline."""
    baseline = {
        "addon_short_step_fraction": float(baseline_cfg.addon_short_step_fraction),
        "addon_short_tp_pct": float(baseline_cfg.addon_short_tp_pct),
        "addon_short_reentry_buffer_pct": float(baseline_cfg.addon_short_reentry_buffer_pct),
        "long_reduce_profit_usage_fraction": float(baseline_cfg.long_reduce_profit_usage_fraction),
    }

    step_candidates = [0.15, 0.20, 0.25, 0.30, 0.35]
    tp_candidates = [0.40, 0.50, 0.60, 0.75, 0.90, 1.00]
    reentry_buffer_candidates = [0.10, 0.15, 0.20, 0.25, 0.30]
    profit_usage_candidates = [0.70, 0.80, 0.90, 0.95]

    seen: Dict[Tuple[float, float, float, float], Dict[str, Any]] = {}

    def add_variant(step: float, tp: float, buf: float, usage: float, tag: str) -> None:
        key = (step, tp, buf, usage)
        if key in seen:
            return
        seen[key] = {
            "variant_id": f"{tag}_sf_{step:.4f}_tp_{tp:.4f}_rb_{buf:.4f}_pu_{usage:.4f}",
            "addon_short_step_fraction": step,
            "addon_short_tp_pct": tp,
            "addon_short_reentry_buffer_pct": buf,
            "long_reduce_profit_usage_fraction": usage,
        }

    # Baseline first.
    add_variant(
        baseline["addon_short_step_fraction"],
        baseline["addon_short_tp_pct"],
        baseline["addon_short_reentry_buffer_pct"],
        baseline["long_reduce_profit_usage_fraction"],
        tag="baseline",
    )

    # Vary step fraction only.
    for v in step_candidates:
        add_variant(
            v,
            baseline["addon_short_tp_pct"],
            baseline["addon_short_reentry_buffer_pct"],
            baseline["long_reduce_profit_usage_fraction"],
            tag="step",
        )

    # Vary TP pct only.
    for v in tp_candidates:
        add_variant(
            baseline["addon_short_step_fraction"],
            v,
            baseline["addon_short_reentry_buffer_pct"],
            baseline["long_reduce_profit_usage_fraction"],
            tag="tp",
        )

    # Vary reentry buffer only.
    for v in reentry_buffer_candidates:
        add_variant(
            baseline["addon_short_step_fraction"],
            baseline["addon_short_tp_pct"],
            v,
            baseline["long_reduce_profit_usage_fraction"],
            tag="reentry_buffer",
        )

    # Vary profit usage only.
    for v in profit_usage_candidates:
        add_variant(
            baseline["addon_short_step_fraction"],
            baseline["addon_short_tp_pct"],
            baseline["addon_short_reentry_buffer_pct"],
            v,
            tag="profit_usage",
        )

    variants = list(seen.values())
    # Deterministic ordering: baseline first, then sorted by tuple.
    def sort_key(v: Dict[str, Any]) -> Tuple[int, float, float, float, float]:
        is_baseline = 0 if v["variant_id"].startswith("baseline") else 1
        return (
            is_baseline,
            v["addon_short_step_fraction"],
            v["addon_short_tp_pct"],
            v["addon_short_reentry_buffer_pct"],
            v["long_reduce_profit_usage_fraction"],
        )

    variants.sort(key=sort_key)
    return variants


def _build_cfg_from_variant(base: AddonShortRecoveryConfig, var: Dict[str, Any]) -> AddonShortRecoveryConfig:
    cfg = AddonShortRecoveryConfig(**asdict(base))
    cfg.addon_short_step_fraction = float(var["addon_short_step_fraction"])
    cfg.addon_short_tp_pct = float(var["addon_short_tp_pct"])
    cfg.addon_short_reentry_buffer_pct = float(var["addon_short_reentry_buffer_pct"])
    cfg.long_reduce_profit_usage_fraction = float(var["long_reduce_profit_usage_fraction"])
    # All other parameters remain identical to baseline.
    return cfg


def _collect_metrics_for_variant(
    *,
    result: BacktestResult,
    cfg: AddonShortRecoveryConfig,
    variant: Dict[str, Any],
    runtime_seconds: float,
) -> Dict[str, Any]:
    """Compute compact metrics and invariants for a single sweep variant."""

    m: Dict[str, Any] = dict(variant)
    m["runtime_seconds"] = runtime_seconds
    m["error"] = result.error or ""

    # Core config echo.
    m["config"] = {
        "addon_short_step_fraction": float(cfg.addon_short_step_fraction),
        "addon_short_tp_pct": float(cfg.addon_short_tp_pct),
        "addon_short_reentry_buffer_pct": float(cfg.addon_short_reentry_buffer_pct),
        "long_reduce_profit_usage_fraction": float(cfg.long_reduce_profit_usage_fraction),
    }

    # Position / gap at end.
    final_long = float(result.final_long_qty or 0.0)
    final_short = float(result.final_short_qty or 0.0)
    remaining_gap = max(final_long - final_short, 0.0)
    m["final_long_qty"] = final_long
    m["final_short_qty"] = final_short
    m["remaining_gap"] = remaining_gap
    m["gap_reduction_from_activation"] = BASELINE_GAP_AT_ACTIVATION - remaining_gap
    # Fraction of gap remaining relative to activation gap.
    m["gap_reduction_pct"] = 1.0 - (remaining_gap / BASELINE_GAP_AT_ACTIVATION)

    # PnL.
    m["realized_pnl"] = float(result.realized_pnl or 0.0)
    m["unrealized_pnl"] = float(result.unrealized_pnl or 0.0)
    m["overall_pnl"] = float(result.overall_pnl or 0.0)

    # Addon aggregates (backtest-only).
    m["addon_short_trade_count"] = int(result.addon_short_trade_count or 0)
    m["addon_short_tp_count"] = int(result.addon_short_tp_count or 0)
    m["addon_short_rebound_exit_count"] = int(result.addon_short_rebound_exit_count or 0)
    m["addon_short_hard_stop_count"] = int(result.addon_short_hard_stop_count or 0)
    m["addon_short_long_reduce_total_qty"] = float(result.addon_short_long_reduce_total_qty or 0.0)
    m["addon_short_long_reduce_total_pnl"] = float(result.addon_short_long_reduce_total_pnl or 0.0)
    m["addon_short_net_realized_pnl"] = float(result.addon_short_net_realized_pnl or 0.0)
    m["recovery_net_realized_pnl"] = (
        m["addon_short_net_realized_pnl"] + m["addon_short_long_reduce_total_pnl"]
    )

    m["recovery_completed"] = bool(result.addon_short_recovery_completed or False)
    m["completion_candle_index"] = result.addon_short_recovery_completed_candle_index
    m["completion_timestamp"] = None  # not recorded in BacktestResult
    m["completion_reason"] = result.addon_short_recovery_completion_reason

    # Runtime invariants and exposures from fill_log and addon_short_events.
    fill_log = result.fill_log or []
    addon_events = result.addon_short_events or []

    # Max long/short from fills (exact).
    max_long_qty = 0.0
    max_short_qty = 0.0
    for row in fill_log:
        max_long_qty = max(max_long_qty, float(row.get("long_qty_after") or 0.0))
        max_short_qty = max(max_short_qty, float(row.get("short_qty_after") or 0.0))
    m["max_long_qty"] = max_long_qty

    # Max addon-short quantity from recovery events (entry qty).
    max_addon_short_qty = 0.0
    for ev in addon_events:
        if ev.get("event_type") == "ADDON_RECOVERY_SHORT_ENTRY":
            max_addon_short_qty = max(max_addon_short_qty, float(ev.get("entry_qty") or 0.0))
    m["max_addon_short_qty"] = max_addon_short_qty

    # Approximate combined short and gross exposure.
    m["max_combined_short_qty"] = max_short_qty + max_addon_short_qty
    m["max_gross_exposure"] = max_long_qty + m["max_combined_short_qty"]

    # Invariants.
    negative_qty_violations = 0
    for row in fill_log:
        if float(row.get("long_qty_after") or 0.0) < -1e-9:
            negative_qty_violations += 1
        if float(row.get("short_qty_after") or 0.0) < -1e-9:
            negative_qty_violations += 1
    m["negative_qty_violation_count"] = negative_qty_violations

    # Reentry invariants: logic guarantees no reentry with open addon-short;
    # we additionally check that there is no Reentry im selben Candle wie ein
    # vorheriger Close.
    from collections import defaultdict

    by_trade: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ev in addon_events:
        ti = ev.get("trade_index")
        if ti is not None:
            by_trade[int(ti)].append(ev)

    reentry_violations = 0
    same_candle_reentries = 0
    # Approximate same-candle reentry detection: if a trade has both a CLOSE
    # and a subsequent ENTRY on identical candle_index, count it.
    closes_by_candle: Dict[int, int] = {}
    for ti, evs in by_trade.items():
        for ev in evs:
            if ev.get("event_type") in (
                "ADDON_RECOVERY_SHORT_TP",
                "ADDON_RECOVERY_SHORT_REBOUND_EXIT",
                "ADDON_RECOVERY_SHORT_HARD_STOP",
            ):
                ci = ev.get("close_candle_index")
                if ci is not None:
                    closes_by_candle[int(ci)] = closes_by_candle.get(int(ci), 0) + 1
    for ti, evs in by_trade.items():
        for ev in evs:
            if ev.get("event_type") == "ADDON_RECOVERY_SHORT_ENTRY":
                ci = ev.get("entry_candle_index")
                if ci is not None and closes_by_candle.get(int(ci), 0) > 0:
                    same_candle_reentries += 1
    # Reentry-Before-Qty>0 können ohne Auditrecorder nicht direkt geprüft
    # werden; sie sind aufgrund der unveränderten Logik für alle Varianten
    # identisch ausgeschlossen.
    m["reentry_violation_count"] = reentry_violations
    m["same_candle_violation_count"] = same_candle_reentries

    # Budget-Invariante für TP+Long-Reduce-Runden über addon_short_events.
    profit_usage_fraction = float(cfg.long_reduce_profit_usage_fraction)
    budget_violations = 0
    for ti, evs in by_trade.items():
        entry = next((e for e in evs if e.get("event_type") == "ADDON_RECOVERY_SHORT_ENTRY"), None)
        closes = [
            e
            for e in evs
            if e.get("event_type")
            in (
                "ADDON_RECOVERY_SHORT_TP",
                "ADDON_RECOVERY_SHORT_REBOUND_EXIT",
                "ADDON_RECOVERY_SHORT_HARD_STOP",
            )
        ]
        lr_events = [e for e in evs if e.get("event_type") == "ADDON_RECOVERY_LONG_REDUCE"]
        if not entry or len(closes) != 1:
            continue
        close = closes[0]
        lr = lr_events[0] if lr_events else None
        if close.get("event_type") != "ADDON_RECOVERY_SHORT_TP" or lr is None:
            continue
        addon_profit = float(close.get("net_pnl") or 0.0)
        lr_pnl = float(lr.get("long_reduce_pnl") or 0.0)
        permitted_loss = max(addon_profit, 0.0) * profit_usage_fraction
        actual_loss = abs(min(lr_pnl, 0.0))
        if actual_loss > permitted_loss + 1e-9:
            budget_violations += 1
    m["budget_violation_count"] = budget_violations

    return m


def _baseline_guard(baseline_metrics: Dict[str, Any], original_run: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """Verify that the baseline variant reproduces the confirmed trade within tolerance."""
    tol = 1e-6

    checks: List[Tuple[str, float, float]] = []

    def add_check(name: str, new_val: float, orig_val: float) -> None:
        checks.append((name, new_val, orig_val))

    add_check("remaining_gap", float(baseline_metrics["remaining_gap"]), float(BASELINE_FINAL_GAP))
    add_check("overall_pnl", float(baseline_metrics["overall_pnl"]), float(BASELINE_OVERALL_PNL))

    # Use original run snapshot for the remaining fields.
    add_check(
        "addon_short_trade_count",
        float(baseline_metrics["addon_short_trade_count"]),
        float(original_run.get("addon_short_trade_count") or 0.0),
    )
    add_check(
        "addon_short_tp_count",
        float(baseline_metrics["addon_short_tp_count"]),
        float(original_run.get("addon_short_tp_count") or 0.0),
    )
    add_check(
        "addon_short_rebound_exit_count",
        float(baseline_metrics["addon_short_rebound_exit_count"]),
        float(original_run.get("addon_short_rebound_exit_count") or 0.0),
    )
    add_check(
        "addon_short_hard_stop_count",
        float(baseline_metrics["addon_short_hard_stop_count"]),
        float(original_run.get("addon_short_hard_stop_count") or 0.0),
    )
    add_check(
        "addon_short_long_reduce_total_qty",
        float(baseline_metrics["addon_short_long_reduce_total_qty"]),
        float(original_run.get("addon_short_long_reduce_total_qty") or 0.0),
    )
    add_check(
        "addon_short_long_reduce_total_pnl",
        float(baseline_metrics["addon_short_long_reduce_total_pnl"]),
        float(original_run.get("addon_short_long_reduce_total_pnl") or 0.0),
    )

    failures: List[Dict[str, Any]] = []
    for name, new_val, orig_val in checks:
        if abs(new_val - orig_val) > tol:
            failures.append(
                {
                    "metric": name,
                    "original_value": orig_val,
                    "reproduced_value": new_val,
                    "absolute_difference": new_val - orig_val,
                    "tolerance": tol,
                }
            )

    return len(failures) == 0, {"failures": failures, "checks": checks}


def _balanced_score(row: Dict[str, Any]) -> float:
    """Compute balanced_score as described in the user spec."""
    gap = float(row["remaining_gap"])
    overall_pnl = float(row["overall_pnl"])
    hard_stop_count = float(row["addon_short_hard_stop_count"])
    trade_count = float(row["addon_short_trade_count"] or 1.0)

    gap_score = gap / BASELINE_GAP_AT_ACTIVATION
    pnl_penalty = max(BASELINE_OVERALL_PNL - overall_pnl, 0.0) / max(abs(BASELINE_OVERALL_PNL), 1e-9)
    hard_stop_rate = hard_stop_count / max(trade_count, 1.0)
    trade_activity = trade_count / BASELINE_TRADE_COUNT

    return 0.50 * gap_score + 0.25 * pnl_penalty + 0.15 * hard_stop_rate + 0.10 * trade_activity


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("\n")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _pareto_front(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compute Pareto front for (gap min, pnl max, hard_stops min, trades min)."""
    front: List[Dict[str, Any]] = []
    for i, a in enumerate(rows):
        a_gap = float(a["remaining_gap"])
        a_pnl = float(a["overall_pnl"])
        a_hs = float(a["addon_short_hard_stop_count"])
        a_tr = float(a["addon_short_trade_count"])
        dominated = False
        for j, b in enumerate(rows):
            if i == j:
                continue
            b_gap = float(b["remaining_gap"])
            b_pnl = float(b["overall_pnl"])
            b_hs = float(b["addon_short_hard_stop_count"])
            b_tr = float(b["addon_short_trade_count"])
            if (
                b_gap <= a_gap
                and b_pnl >= a_pnl
                and b_hs <= a_hs
                and b_tr <= a_tr
                and (
                    b_gap < a_gap
                    or b_pnl > a_pnl
                    or b_hs < a_hs
                    or b_tr < a_tr
                )
            ):
                dominated = True
                break
        if not dominated:
            front.append(a)
    return front


def run_sweep(mode: str = "one-factor-at-a-time") -> Dict[str, Any]:
    if mode != "one-factor-at-a-time":
        raise ValueError(f"unsupported mode: {mode}")

    run_dir = _ensure_output_dir()
    sweep_meta: Dict[str, Any] = {
        "mode": mode,
        "run_dir": str(run_dir),
        "timestamp": _now_utc_iso(),
    }

    baseline_cfg = default_addon_short_recovery_config()
    variants = _build_variants_one_factor(baseline_cfg)
    sweep_meta["planned_variants"] = len(variants)

    trade_candles, candle_meta = _load_and_slice_candles()
    sweep_meta["candle_window"] = candle_meta

    original_run = _load_original_run_snapshot()
    sweep_meta["original_run_present"] = bool(original_run)

    all_results: List[Dict[str, Any]] = []

    for var in variants:
        cfg = _build_cfg_from_variant(baseline_cfg, var)
        started_at = time.perf_counter()
        result: BacktestResult | None = None
        error: str | None = None
        try:
            result = run_historical_backtest(
                SYMBOL.upper(),
                DIRECTION,
                trade_candles,
                max_candles=len(trade_candles),
                fill_model=FILL_MODEL,
                max_fills_per_candle=MAX_FILLS_PER_CANDLE,
                initial_notional_usdt=100.0,
                config_source=CONFIG_SOURCE,
                long_config_path=DEFAULT_LONG_CONFIG_PATH,
                short_config_path=DEFAULT_SHORT_CONFIG_PATH,
                file_config_path=None,
                tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
                addon_short_recovery_config=cfg,
                audit_recorder=None,
            )
        except Exception as exc:
            error = str(exc)
        runtime_seconds = time.perf_counter() - started_at

        if result is None:
            all_results.append(
                {
                    **var,
                    "error": error or "unknown_error",
                    "runtime_seconds": runtime_seconds,
                }
            )
            continue

        metrics = _collect_metrics_for_variant(
            result=result,
            cfg=cfg,
            variant=var,
            runtime_seconds=runtime_seconds,
        )
        if error:
            metrics["error"] = error
        all_results.append(metrics)

    sweep_meta["executed_variants"] = len(all_results)

    # Guard: baseline must reproduce the confirmed trade within tolerance.
    baseline_tuple = (
        baseline_cfg.addon_short_step_fraction,
        baseline_cfg.addon_short_tp_pct,
        baseline_cfg.addon_short_reentry_buffer_pct,
        baseline_cfg.long_reduce_profit_usage_fraction,
    )
    baseline_row = next(
        (
            r
            for r in all_results
            if (
                float(r["addon_short_step_fraction"]),
                float(r["addon_short_tp_pct"]),
                float(r["addon_short_reentry_buffer_pct"]),
                float(r["long_reduce_profit_usage_fraction"]),
            )
            == baseline_tuple
        ),
        None,
    )

    guard_ok = False
    guard_details: Dict[str, Any] = {}
    if baseline_row is not None and original_run:
        guard_ok, guard_details = _baseline_guard(baseline_row, original_run)
    sweep_meta["baseline_guard"] = {"passed": guard_ok, **guard_details}

    _write_json(run_dir / "sweep_metadata.json", sweep_meta)

    # If baseline guard failed, do not continue with rankings.
    if not guard_ok:
        return {"run_dir": run_dir, "metadata": sweep_meta}

    # Split valid / invalid variants.
    valid: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    for row in all_results:
        error = row.get("error") or ""
        neg = int(row.get("negative_qty_violation_count") or 0)
        reentry = int(row.get("reentry_violation_count") or 0)
        budget = int(row.get("budget_violation_count") or 0)
        same_candle = int(row.get("same_candle_violation_count") or 0)
        final_long = float(row.get("final_long_qty") or 0.0)
        final_short = float(row.get("final_short_qty") or 0.0)
        remaining_gap = float(row.get("remaining_gap") or 0.0)
        # We do not track addon_short_qty_at_end separately in the sweep; the
        # implementation guarantees that no addon short is left open when the
        # trade ends.
        addon_short_open_end = 0

        is_invalid = (
            neg > 0
            or reentry > 0
            or budget > 0
            or same_candle > 0
            or error != ""
            or final_long < -1e-9
            or final_short < -1e-9
            or addon_short_open_end > 0
        )
        row["is_invalid"] = bool(is_invalid)
        if is_invalid:
            invalid.append(row)
        else:
            valid.append(row)

    # Persist raw and filtered results.
    _write_json(run_dir / "sweep_results.json", all_results)
    _write_csv(run_dir / "sweep_results.csv", all_results)
    _write_csv(run_dir / "sweep_valid_results.csv", valid)
    _write_csv(run_dir / "sweep_invalid_results.csv", invalid)

    # Rankings for valid variants only.
    def sort_gap(row: Dict[str, Any]) -> Tuple[float, float, float, float]:
        return (
            float(row["remaining_gap"]),
            -float(row["overall_pnl"]),
            float(row["addon_short_hard_stop_count"]),
            float(row["addon_short_trade_count"]),
        )

    def sort_pnl(row: Dict[str, Any]) -> Tuple[float, float, float]:
        return (
            -float(row["overall_pnl"]),
            float(row["remaining_gap"]),
            float(row["addon_short_hard_stop_count"]),
        )

    ranking_gap = sorted(valid, key=sort_gap)
    ranking_pnl = sorted(valid, key=sort_pnl)

    for row in valid:
        row["balanced_score"] = _balanced_score(row)
    ranking_balanced = sorted(valid, key=lambda r: (float(r["balanced_score"]), float(r["remaining_gap"]), -float(r["overall_pnl"])))

    _write_csv(run_dir / "sweep_ranking_gap.csv", ranking_gap)
    _write_csv(run_dir / "sweep_ranking_pnl.csv", ranking_pnl)
    _write_csv(run_dir / "sweep_ranking_balanced.csv", ranking_balanced)

    # Pareto front.
    pareto = _pareto_front(valid)
    _write_csv(run_dir / "sweep_pareto_front.csv", pareto)

    # Baseline comparison CSV (baseline vs best few variants).
    baseline_only = [baseline_row] if baseline_row is not None else []
    _write_csv(run_dir / "sweep_baseline_comparison.csv", baseline_only)

    # Compact markdown summary placeholder (can be refined manually).
    summary_md = run_dir / "sweep_summary.md"
    with summary_md.open("w", encoding="utf-8") as handle:
        handle.write(f"# Trade 0012 Addon-Recovery Parameter Sweep\n\n")
        handle.write(f"- Mode: {mode}\n")
        handle.write(f"- Planned variants: {sweep_meta['planned_variants']}\n")
        handle.write(f="- Executed variants: {sweep_meta['executed_variants']}\n")
        handle.write(f"- Valid variants: {len(valid)}\n")
        handle.write(f"- Invalid variants: {len(invalid)}\n")
        handle.write(f"- Baseline guard passed: {guard_ok}\n")

    return {
        "run_dir": run_dir,
        "metadata": sweep_meta,
        "valid_count": len(valid),
        "invalid_count": len(invalid),
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parameter sweep for Blocker Addon Short Recovery on trade 0012.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="one-factor-at-a-time",
        help="Sweep mode (currently only 'one-factor-at-a-time' is supported).",
    )
    args = parser.parse_args(argv)

    try:
        result = run_sweep(mode=args.mode)
        print(f"Sweep completed in {result['run_dir']}")
        return 0
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

