"""Backtest-only sweep: recovery timeout close_all vs gap-reduction baseline."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol_with_slice_info
from .continuous_reentry_backtest import (
    aggregate_continuous_results,
    run_continuous_reentry_for_direction,
    write_continuous_results_json,
)
from .historical_backtest import normalize_candles
from .multi_start_backtest import compact_result_dict
from .recovery_bot_config import RecoveryBotConfig, default_recovery_bot_config

SYMBOL = "APTUSDT"
DIRECTION = "long"
LIMIT = 52569
WAIT_CANDLES = 244
BASELINE_WAIT_CANDLES = 576
DEFAULT_OUTPUT = Path("research/backtests/results/recovery_timeout_close_sweep")
MIN_LOSS_VALUES: tuple[float | None, ...] = (
    None,
    0.00,
    0.25,
    0.50,
    0.75,
    1.00,
    1.25,
    1.50,
    2.00,
    3.00,
    4.00,
)

SWEEP_CSV_FIELDS = (
    "variant",
    "wait_candles",
    "min_loss_usdt",
    "timeout_action",
    "trades_started",
    "trades_closed",
    "timeout_closes",
    "recovery_activations",
    "profitable_timeout_closes",
    "losing_timeout_closes",
    "total_net_pnl",
    "average_pnl",
    "median_pnl",
    "profit_factor",
    "win_rate",
    "worst_trade",
    "best_trade",
    "max_drawdown",
    "average_trade_duration_candles",
)


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_recovery_config(
    *,
    wait_candles: int,
    timeout_action: str,
    min_loss_usdt: float | None,
    purpose: str = "CYCLE_4_LONG_ADD",
) -> RecoveryBotConfig:
    cfg = default_recovery_bot_config()
    cfg.enabled = True
    cfg.recovery_start_purpose = purpose
    cfg.recovery_wait_candles = int(wait_candles)
    cfg.recovery_timeout_action = timeout_action
    cfg.recovery_timeout_min_loss_usdt = min_loss_usdt
    cfg.name = f"{timeout_action}_wait{wait_candles}_minloss{min_loss_usdt}"
    return cfg


def _profit_factor(pnls: list[float]) -> float:
    gains = sum(value for value in pnls if value > 0)
    losses = sum(-value for value in pnls if value < 0)
    if losses <= 1e-12:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def summarize_variant_runs(
    runs: list[Any],
    *,
    variant: str,
    wait_candles: int,
    min_loss_usdt: float | None,
    timeout_action: str,
) -> dict[str, Any]:
    compact = [compact_result_dict(run) if hasattr(run, "to_dict") else dict(run) for run in runs]
    pnls = [float(run.get("realized_pnl") or 0.0) for run in compact]
    closed = [
        run
        for run in compact
        if str(run.get("final_status") or "") == "closed"
        or str(run.get("exit_reason") or "")
        in {"flat_no_active_orders", "recovery_joint_exit", "recovery_timeout_close_all"}
    ]
    timeout_closes = [
        run for run in compact if str(run.get("exit_reason") or "") == "recovery_timeout_close_all"
    ]
    recovery_activations = [
        run
        for run in compact
        if bool(run.get("recovery_activated"))
        and str(run.get("exit_reason") or "") != "recovery_timeout_close_all"
    ]
    timeout_pnls = [float(run.get("realized_pnl") or 0.0) for run in timeout_closes]
    durations = [int(run.get("candles_processed") or 0) for run in compact]
    drawdowns = [float(run.get("max_drawdown_pct") or 0.0) for run in compact]
    wins = sum(1 for value in pnls if value > 0)
    return {
        "variant": variant,
        "wait_candles": wait_candles,
        "min_loss_usdt": min_loss_usdt,
        "timeout_action": timeout_action,
        "trades_started": len(compact),
        "trades_closed": len(closed),
        "timeout_closes": len(timeout_closes),
        "recovery_activations": len(recovery_activations),
        "profitable_timeout_closes": sum(1 for value in timeout_pnls if value > 0),
        "losing_timeout_closes": sum(1 for value in timeout_pnls if value < 0),
        "total_net_pnl": sum(pnls),
        "average_pnl": statistics.mean(pnls) if pnls else 0.0,
        "median_pnl": statistics.median(pnls) if pnls else 0.0,
        "profit_factor": _profit_factor(pnls),
        "win_rate": (wins / len(pnls) * 100.0) if pnls else 0.0,
        "worst_trade": min(pnls) if pnls else 0.0,
        "best_trade": max(pnls) if pnls else 0.0,
        "max_drawdown": max(drawdowns) if drawdowns else 0.0,
        "average_trade_duration_candles": statistics.mean(durations) if durations else 0.0,
    }


def run_variant(
    *,
    candles: list[Any],
    input_slice_start_index: int,
    wait_candles: int,
    timeout_action: str,
    min_loss_usdt: float | None,
    max_trades: int | None,
) -> tuple[list[Any], dict[str, Any]]:
    cfg = _build_recovery_config(
        wait_candles=wait_candles,
        timeout_action=timeout_action,
        min_loss_usdt=min_loss_usdt,
    )
    runs = run_continuous_reentry_for_direction(
        SYMBOL,
        DIRECTION,
        candles,
        continuous_start_index=0,
        continuous_max_trades=max_trades,
        config_source="live",
        fill_model="conservative",
        recovery_bot_config=cfg,
        input_slice_start_index=input_slice_start_index,
    )
    variant = (
        f"close_all_wait{wait_candles}_minloss"
        f"{'none' if min_loss_usdt is None else f'{min_loss_usdt:.2f}'}"
        if timeout_action == "close_all"
        else f"gap_reduction_wait{wait_candles}"
    )
    summary = summarize_variant_runs(
        runs,
        variant=variant,
        wait_candles=wait_candles,
        min_loss_usdt=min_loss_usdt,
        timeout_action=timeout_action,
    )
    return runs, summary


def compare_timeout_vs_baseline(
    timeout_runs: list[Any],
    baseline_runs: list[Any],
) -> dict[str, Any]:
    """Pair trades by trade_number where both exist; compare realized PnL."""
    baseline_by_number = {
        int(run.trade_number or 0): run for run in baseline_runs if run.trade_number
    }
    better = 0
    worse = 0
    equal = 0
    compared = 0
    for run in timeout_runs:
        number = int(run.trade_number or 0)
        baseline = baseline_by_number.get(number)
        if baseline is None:
            continue
        if not bool(getattr(run, "recovery_timeout_close_triggered", False)):
            continue
        compared += 1
        delta = float(run.realized_pnl or 0.0) - float(baseline.realized_pnl or 0.0)
        if delta > 1e-8:
            better += 1
        elif delta < -1e-8:
            worse += 1
        else:
            equal += 1
    return {
        "timeout_closes_compared_to_baseline": compared,
        "timeout_better_than_recovery": better,
        "timeout_worse_than_recovery": worse,
        "timeout_equal_to_recovery": equal,
    }


def write_sweep_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SWEEP_CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in SWEEP_CSV_FIELDS})


def run_sweep(
    *,
    output_dir: Path,
    limit: int = LIMIT,
    wait_candles: int = WAIT_CANDLES,
    min_loss_values: tuple[float | None, ...] = MIN_LOSS_VALUES,
    max_trades: int | None = None,
    include_baseline: bool = True,
) -> dict[str, Any]:
    started = time.time()
    output_dir.mkdir(parents=True, exist_ok=True)
    candles_raw, slice_info = load_candles_for_symbol_with_slice_info(
        SYMBOL,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=limit,
    )
    candles = normalize_candles(SYMBOL, candles_raw)

    summaries: list[dict[str, Any]] = []
    baseline_runs: list[Any] = []
    always_close_runs: list[Any] = []

    if include_baseline:
        baseline_runs, baseline_summary = run_variant(
            candles=candles,
            input_slice_start_index=slice_info.input_slice_start_index,
            wait_candles=BASELINE_WAIT_CANDLES,
            timeout_action="gap_reduction",
            min_loss_usdt=None,
            max_trades=max_trades,
        )
        summaries.append(baseline_summary)
        write_continuous_results_json(
            output_dir / "baseline_gap_reduction_wait576_results.json",
            metadata={
                "variant": baseline_summary["variant"],
                "recovery_timeout_action": "gap_reduction",
                "recovery_wait_candles": BASELINE_WAIT_CANDLES,
            },
            runs=baseline_runs,
            aggregate=aggregate_continuous_results(baseline_runs),
        )

    for min_loss in min_loss_values:
        runs, summary = run_variant(
            candles=candles,
            input_slice_start_index=slice_info.input_slice_start_index,
            wait_candles=wait_candles,
            timeout_action="close_all",
            min_loss_usdt=min_loss,
            max_trades=max_trades,
        )
        summaries.append(summary)
        slug = "none" if min_loss is None else f"{min_loss:.2f}".replace(".", "p")
        write_continuous_results_json(
            output_dir / f"close_all_wait{wait_candles}_minloss_{slug}_results.json",
            metadata={
                "variant": summary["variant"],
                "recovery_timeout_action": "close_all",
                "recovery_wait_candles": wait_candles,
                "recovery_timeout_min_loss_usdt": min_loss,
            },
            runs=runs,
            aggregate=aggregate_continuous_results(runs),
        )
        if min_loss is None:
            always_close_runs = runs

    write_sweep_csv(output_dir / "recovery_timeout_close_sweep.csv", summaries)
    (output_dir / "recovery_timeout_close_sweep.json").write_text(
        json.dumps({"summaries": summaries}, indent=2),
        encoding="utf-8",
    )

    comparison = {}
    if baseline_runs and always_close_runs:
        comparison = compare_timeout_vs_baseline(always_close_runs, baseline_runs)

    best_total = max(summaries, key=lambda row: float(row["total_net_pnl"]))
    best_worst = max(summaries, key=lambda row: float(row["worst_trade"]))
    report = {
        "elapsed_seconds": time.time() - started,
        "symbol": SYMBOL,
        "direction": DIRECTION,
        "candle_count": len(candles),
        "wait_candles": wait_candles,
        "baseline_wait_candles": BASELINE_WAIT_CANDLES,
        "summaries": summaries,
        "best_by_total_net_pnl": best_total,
        "best_by_worst_trade": best_worst,
        "timeout_vs_baseline": comparison,
    }
    (output_dir / "REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Recovery Timeout Close Sweep",
        "",
        f"- Wait candles (close_all): `{wait_candles}`",
        f"- Baseline: gap_reduction wait `{BASELINE_WAIT_CANDLES}`",
        f"- Best total PnL: `{best_total['variant']}` = {best_total['total_net_pnl']:.6f}",
        f"- Best worst-trade: `{best_worst['variant']}` = {best_worst['worst_trade']:.6f}",
        "",
        "## Sweep table",
        "",
        "| variant | wait | min_loss | timeout_closes | recovery_act | total_pnl | avg | worst | best |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['variant']} | {row['wait_candles']} | {row['min_loss_usdt']} | "
            f"{row['timeout_closes']} | {row['recovery_activations']} | "
            f"{row['total_net_pnl']:.4f} | {row['average_pnl']:.4f} | "
            f"{row['worst_trade']:.4f} | {row['best_trade']:.4f} |"
        )
    if comparison:
        lines.extend(
            [
                "",
                "## Timeout vs baseline (always-close vs gap_reduction 576)",
                "",
                json.dumps(comparison, indent=2),
            ]
        )
    (output_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sweep recovery timeout close_all variants")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=LIMIT)
    parser.add_argument("--wait-candles", type=int, default=WAIT_CANDLES)
    parser.add_argument("--max-trades", type=int, default=None)
    parser.add_argument("--skip-baseline", action="store_true")
    args = parser.parse_args(argv)
    output_dir = args.output_dir
    if output_dir == DEFAULT_OUTPUT:
        output_dir = DEFAULT_OUTPUT.parent / f"recovery_timeout_close_sweep_{_timestamp_slug()}"
    report = run_sweep(
        output_dir=output_dir.resolve(),
        limit=args.limit,
        wait_candles=args.wait_candles,
        max_trades=args.max_trades,
        include_baseline=not args.skip_baseline,
    )
    print(json.dumps({
        "output_dir": str(output_dir.resolve()),
        "best_by_total_net_pnl": report["best_by_total_net_pnl"]["variant"],
        "best_total_net_pnl": report["best_by_total_net_pnl"]["total_net_pnl"],
        "best_by_worst_trade": report["best_by_worst_trade"]["variant"],
        "timeout_vs_baseline": report.get("timeout_vs_baseline"),
        "elapsed_seconds": report["elapsed_seconds"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
