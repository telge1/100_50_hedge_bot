"""Compare DCOS qty-sweep post-fix variants against baseline_post_fix (120-start APTUSDT long)."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

STUCK_RE = re.compile(r"CYCLE_[56]_(SHORT_REDUCE|LONG_ADD)$")
RESULTS_ROOT = Path("research/backtests/results/dcos_qty_sweep_post_fix")
DEFAULT_BASELINE = RESULTS_ROOT / "baseline_post_fix" / "APTUSDT_original_hedge_5m_multi_start_results.json"
RESULTS_GLOB = "APTUSDT_original_hedge_5m_multi_start_results.json"


def load_runs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    return list(payload.get("runs") or [])


def _notional(run: dict[str, Any]) -> float:
    long_qty = float(run.get("final_long_qty") or 0.0)
    short_qty = float(run.get("final_short_qty") or 0.0)
    long_avg = float(run.get("final_long_avg_price") or 0.0)
    short_avg = float(run.get("final_short_avg_price") or 0.0)
    return long_qty * long_avg + short_qty * short_avg


def _profit_factor(pnls: list[float]) -> float | None:
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses <= 0:
        return None if wins <= 0 else float("inf")
    return wins / losses


def analyze_runs(runs: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    pnls: list[float] = []
    closed_pnls: list[float] = []
    last_fills: Counter[str] = Counter()
    cycles_seen: list[int] = []
    notionals: list[float] = []
    stuck56 = 0
    closed = 0
    max_candles = 0
    bad_lt_1 = bad_lt_2 = bad_lt_3 = 0

    for run in runs:
        pnl = float(run.get("realized_pnl") or 0.0)
        pnls.append(pnl)
        if pnl < -1.0:
            bad_lt_1 += 1
        if pnl < -2.0:
            bad_lt_2 += 1
        if pnl < -3.0:
            bad_lt_3 += 1

        status = str(run.get("final_status") or "")
        if status == "closed":
            closed += 1
            closed_pnls.append(pnl)
        elif status == "max_candles":
            max_candles += 1

        last_purpose = str((run.get("last_fill") or {}).get("purpose") or "")
        last_fills[last_purpose] += 1
        if status == "max_candles" and STUCK_RE.search(last_purpose):
            stuck56 += 1

        cycles_seen.append(int(run.get("cycles_seen") or 0))
        notionals.append(_notional(run))

    net_sum = sum(pnls)
    worst = min(pnls) if pnls else 0.0
    worst_idx = pnls.index(worst) if pnls else -1
    worst_run = runs[worst_idx] if worst_idx >= 0 else None

    return {
        "label": label,
        "runs": len(runs),
        "total_pnl": net_sum,
        "net_sum_pct": net_sum,
        "closed": closed,
        "max_candles": max_candles,
        "unfinished": max_candles,
        "stuck_cycle_5_6": stuck56,
        "max_cycles_seen": max(cycles_seen) if cycles_seen else 0,
        "avg_cycles_seen": sum(cycles_seen) / len(cycles_seen) if cycles_seen else 0.0,
        "worst_trade": worst,
        "worst_pnl": worst,
        "worst_start_index": worst_run.get("start_index") if worst_run else None,
        "worst_last_fill": (worst_run.get("last_fill") or {}).get("purpose") if worst_run else None,
        "max_notional_usdt": max(notionals) if notionals else 0.0,
        "avg_notional_usdt": sum(notionals) / len(notionals) if notionals else 0.0,
        "profit_factor": _profit_factor(pnls),
        "avg_pnl_closed": statistics.mean(closed_pnls) if closed_pnls else None,
        "median_pnl_closed": statistics.median(closed_pnls) if closed_pnls else None,
        "bad_trades_pnl_lt_1": bad_lt_1,
        "bad_trades_pnl_lt_2": bad_lt_2,
        "bad_trades_pnl_lt_3": bad_lt_3,
        "last_fill_distribution": dict(last_fills.most_common(20)),
    }


def passes_decision_rule(variant: dict[str, Any], baseline: dict[str, Any]) -> dict[str, bool]:
    max_notional_cap = baseline["max_notional_usdt"] * 1.05
    return {
        "total_pnl_gte_baseline": variant["total_pnl"] >= baseline["total_pnl"],
        "stuck56_lte_baseline": variant["stuck_cycle_5_6"] <= baseline["stuck_cycle_5_6"],
        "unfinished_lte_baseline": variant["unfinished"] <= baseline["unfinished"],
        "worst_trade_not_worse": variant["worst_trade"] >= baseline["worst_trade"],
        "max_notional_within_cap": variant["max_notional_usdt"] <= max_notional_cap,
    }


def discover_variant_results(results_root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not results_root.is_dir():
        return out
    for child in sorted(results_root.iterdir()):
        if not child.is_dir():
            continue
        candidate = child / RESULTS_GLOB
        if candidate.is_file():
            out[child.name] = candidate
    return out


def format_row(metrics: dict[str, Any]) -> str:
    pf = metrics.get("profit_factor")
    pf_str = "n/a" if pf is None else ("inf" if pf == float("inf") else f"{pf:.3f}")
    return (
        f"{metrics['label']:32} | pnl={metrics['total_pnl']:8.4f} | "
        f"closed={metrics['closed']:3d} unfin={metrics['unfinished']:3d} | "
        f"stuck56={metrics['stuck_cycle_5_6']:2d} | max_cyc={metrics['max_cycles_seen']} | "
        f"worst={metrics['worst_trade']:7.4f} | max_not={metrics['max_notional_usdt']:7.2f} | "
        f"pf={pf_str} | bad<-1={metrics['bad_trades_pnl_lt_1']:2d}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=RESULTS_ROOT,
        help="Root directory with per-variant result folders",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="Path to baseline_post_fix multi_start results JSON",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=RESULTS_ROOT / "dcos_qty_sweep_post_fix_comparison.json",
    )
    args = parser.parse_args()

    if not args.baseline.is_file():
        raise SystemExit(f"Baseline results missing: {args.baseline}")

    discovered = discover_variant_results(args.results_root)
    baseline_metrics = analyze_runs(load_runs(args.baseline), label="baseline_post_fix")
    variants = [
        analyze_runs(load_runs(path), label=label)
        for label, path in discovered.items()
        if label != "baseline_post_fix"
    ]
    variants.sort(key=lambda item: item["label"])

    print("APTUSDT long conservative 120-start DCOS qty-sweep post-fix comparison\n")
    print(format_row(baseline_metrics))
    for metrics in variants:
        print(format_row(metrics))

    print("\nDecision rule (all must pass to be interesting):")
    interesting: list[dict[str, Any]] = []
    for metrics in variants:
        checks = passes_decision_rule(metrics, baseline_metrics)
        ok = all(checks.values())
        if ok:
            interesting.append(metrics)
        print(f"  {metrics['label']}: interesting={ok}")
        for key, passed in checks.items():
            delta = ""
            if key == "total_pnl_gte_baseline":
                delta = f" ({metrics['total_pnl'] - baseline_metrics['total_pnl']:+.4f})"
            elif key == "stuck56_lte_baseline":
                delta = f" ({metrics['stuck_cycle_5_6'] - baseline_metrics['stuck_cycle_5_6']:+d})"
            elif key == "worst_trade_not_worse":
                delta = f" ({metrics['worst_trade'] - baseline_metrics['worst_trade']:+.4f})"
            print(f"    {key}: {'PASS' if passed else 'FAIL'}{delta}")

    print(f"\nInteresting variants: {len(interesting)} / {len(variants)}")
    for metrics in interesting:
        print(f"  - {metrics['label']}: pnl={metrics['total_pnl']:.4f} stuck56={metrics['stuck_cycle_5_6']}")

    by_pnl = sorted(variants, key=lambda m: m["total_pnl"], reverse=True)
    by_worst = sorted(variants, key=lambda m: m["worst_trade"], reverse=True)

    print("\nTop-5 by total_pnl:")
    for metrics in by_pnl[:5]:
        print(
            f"  {metrics['label']:32} pnl={metrics['total_pnl']:.4f} "
            f"worst={metrics['worst_trade']:.4f} stuck56={metrics['stuck_cycle_5_6']}"
        )

    print("\nTop-5 by highest worst_trade (least bad tail):")
    for metrics in by_worst[:5]:
        print(
            f"  {metrics['label']:32} worst={metrics['worst_trade']:.4f} "
            f"pnl={metrics['total_pnl']:.4f} stuck56={metrics['stuck_cycle_5_6']}"
        )

    print("\nClosed-trade PnL stats:")
    for metrics in [baseline_metrics, *variants]:
        avg_c = metrics.get("avg_pnl_closed")
        med_c = metrics.get("median_pnl_closed")
        avg_s = f"{avg_c:.4f}" if avg_c is not None else "n/a"
        med_s = f"{med_c:.4f}" if med_c is not None else "n/a"
        print(
            f"  {metrics['label']:32} avg_closed={avg_s} median_closed={med_s} "
            f"bad<-1={metrics['bad_trades_pnl_lt_1']} bad<-2={metrics['bad_trades_pnl_lt_2']} "
            f"bad<-3={metrics['bad_trades_pnl_lt_3']}"
        )

    print("\nLast-fill distribution (excluding SHORT_SL_EXIT):")
    for metrics in [baseline_metrics, *variants]:
        dist = {
            k: v
            for k, v in metrics["last_fill_distribution"].items()
            if k != "SHORT_SL_EXIT"
        }
        print(f"  {metrics['label']}: {dist}")

    payload = {
        "baseline": baseline_metrics,
        "variants": variants,
        "interesting": [m["label"] for m in interesting],
        "decision": {
            metrics["label"]: {
                **passes_decision_rule(metrics, baseline_metrics),
                "interesting": all(passes_decision_rule(metrics, baseline_metrics).values()),
            }
            for metrics in variants
        },
        "top5_total_pnl": [m["label"] for m in by_pnl[:5]],
        "top5_worst_trade": [m["label"] for m in by_worst[:5]],
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {args.json_out}")


if __name__ == "__main__":
    main()
