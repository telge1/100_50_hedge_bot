"""Compare DCOS mild qty-only variants against baseline (120-start APTUSDT long)."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


STUCK_RE = re.compile(r"CYCLE_[56]_(SHORT_REDUCE|LONG_ADD)")


def load_runs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    return list(payload.get("runs") or [])


def analyze_runs(runs: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    pnls: list[float] = []
    last_fills: Counter[str] = Counter()
    cycles_seen: list[int] = []
    notionals: list[float] = []
    stuck56 = 0
    closed = 0
    max_candles = 0

    for run in runs:
        pnl = float(run.get("realized_pnl") or 0.0)
        pnls.append(pnl)
        status = str(run.get("final_status") or "")
        if status == "closed":
            closed += 1
        elif status == "max_candles":
            max_candles += 1

        last_purpose = str((run.get("last_fill") or {}).get("purpose") or "")
        last_fills[last_purpose] += 1
        if status == "max_candles" and STUCK_RE.search(last_purpose):
            stuck56 += 1

        cycles_seen.append(int(run.get("cycles_seen") or 0))

        long_qty = float(run.get("final_long_qty") or 0.0)
        short_qty = float(run.get("final_short_qty") or 0.0)
        long_avg = float(run.get("final_long_avg_price") or 0.0)
        short_avg = float(run.get("final_short_avg_price") or 0.0)
        notionals.append(long_qty * long_avg + short_qty * short_avg)

    net_sum = sum(pnls)
    worst = min(pnls) if pnls else 0.0
    worst_run = runs[pnls.index(worst)] if pnls else None

    return {
        "label": label,
        "runs": len(runs),
        "net_sum_pct": net_sum,
        "closed": closed,
        "max_candles": max_candles,
        "unfinished": max_candles,
        "stuck_cycle_5_6": stuck56,
        "max_cycles_seen": max(cycles_seen) if cycles_seen else 0,
        "avg_cycles_seen": sum(cycles_seen) / len(cycles_seen) if cycles_seen else 0.0,
        "worst_pnl": worst,
        "worst_start_index": worst_run.get("start_index") if worst_run else None,
        "worst_last_fill": (worst_run.get("last_fill") or {}).get("purpose") if worst_run else None,
        "max_notional_usdt": max(notionals) if notionals else 0.0,
        "avg_notional_usdt": sum(notionals) / len(notionals) if notionals else 0.0,
        "last_fill_distribution": dict(last_fills.most_common(12)),
    }


def passes_decision_rule(variant: dict[str, Any], baseline: dict[str, Any]) -> dict[str, bool]:
    return {
        "net_not_worse_than_baseline": variant["net_sum_pct"] >= baseline["net_sum_pct"],
        "stuck56_improves": variant["stuck_cycle_5_6"] < baseline["stuck_cycle_5_6"],
        "unfinished_not_increased": variant["unfinished"] <= baseline["unfinished"],
        "no_capital_explosion": variant["max_notional_usdt"] <= baseline["max_notional_usdt"] * 1.15,
    }


def format_row(metrics: dict[str, Any]) -> str:
    return (
        f"{metrics['label']:28} | net={metrics['net_sum_pct']:8.4f} | "
        f"closed={metrics['closed']:3d} max_c={metrics['max_candles']:3d} | "
        f"stuck56={metrics['stuck_cycle_5_6']:2d} | max_cyc={metrics['max_cycles_seen']} | "
        f"worst={metrics['worst_pnl']:7.4f} | max_notional={metrics['max_notional_usdt']:7.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("research/backtests/results/dcos_baseline_120/APTUSDT_original_hedge_5m_multi_start_results.json"),
    )
    parser.add_argument(
        "--variant",
        action="append",
        nargs=2,
        metavar=("LABEL", "RESULTS_JSON"),
        required=True,
        help="Label and path to multi_start results JSON",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    baseline_metrics = analyze_runs(load_runs(args.baseline), label="baseline_120")
    variants = [
        analyze_runs(load_runs(Path(path)), label=label)
        for label, path in args.variant
    ]

    print("APTUSDT long conservative 120-start DCOS comparison\n")
    print(format_row(baseline_metrics))
    for metrics in variants:
        print(format_row(metrics))

    print("\nDecision rule (all must pass):")
    for metrics in variants:
        checks = passes_decision_rule(metrics, baseline_metrics)
        interesting = all(checks.values())
        print(f"  {metrics['label']}: interesting={interesting}")
        for key, ok in checks.items():
            print(f"    {key}: {'PASS' if ok else 'FAIL'}")

    print("\nLast-fill distribution (unfinished-relevant purposes):")
    for metrics in [baseline_metrics, *variants]:
        dist = metrics["last_fill_distribution"]
        subset = {k: v for k, v in dist.items() if k != "SHORT_SL_EXIT"}
        print(f"  {metrics['label']}: {subset}")

    payload = {
        "baseline": baseline_metrics,
        "variants": variants,
        "decision": {
            metrics["label"]: {
                **passes_decision_rule(metrics, baseline_metrics),
                "interesting": all(passes_decision_rule(metrics, baseline_metrics).values()),
            }
            for metrics in variants
        },
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2))
        print(f"\nWrote {args.json_out}")


if __name__ == "__main__":
    main()
