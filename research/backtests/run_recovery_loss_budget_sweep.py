from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.fill_models import resolve_fill_model_config
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.calculations import compute_net_long_qty
from research.backtests.recovery_bot.config import (
    RecoveryBotConfig,
    config_from_dict as recovery_config_from_dict,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = (
    REPO_ROOT / "research" / "backtests" / "results" / "recovery_bot_current_three_audit"
)
SWEEP_DIR = RESULTS_DIR / "loss_budget_sweep"


SWEEP_BUDGETS = [
    1.50,
    1.75,
    2.00,
    2.20,
    2.25,
    2.50,
    2.75,
    3.00,
    3.50,
    4.00,
    5.00,
    6.00,
    7.50,
    10.00,
    15.00,
    20.00,
]


@dataclass
class SweepRun:
    start_index: int
    loss_budget_usdt: float
    result: BacktestResult


def _load_baseline_meta(start_index: int) -> dict[str, Any]:
    """Load the existing *_full.json file for a trade and return its payload."""
    path = RESULTS_DIR / f"APTUSDT_start{start_index}_full.json"
    return __import__("json").loads(path.read_text(encoding="utf-8"))


def _load_candles_for_symbol() -> list[dict[str, Any]]:
    """Load the full APTUSDT candle series used for the backtests."""
    # Use a sufficiently large limit to cover all requested windows.
    return load_candles_for_symbol(
        "APTUSDT",
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=60000,
    )


def _window_for_start(
    all_candles: list[dict[str, Any]],
    *,
    source_timestamp: str,
    candles_processed: int,
) -> list[dict[str, Any]]:
    """Slice das Candle-Fenster anhand des im Full-Result gespeicherten Zeitstempels."""
    start_idx = None
    for idx, row in enumerate(all_candles):
        ts = row.get("timestamp")
        if ts is None:
            continue
        if hasattr(ts, "isoformat"):
            ts_str = ts.isoformat()
        else:
            ts_str = str(ts)
        if ts_str == source_timestamp:
            start_idx = idx
            break
    if start_idx is None:
        raise ValueError(
            f"could not find candle with timestamp={source_timestamp!r} "
            "in loaded APTUSDT candles"
        )
    end_idx = start_idx + candles_processed
    return list(all_candles[start_idx:end_idx])


def _build_recovery_config_for_budget(budget: float) -> RecoveryBotConfig:
    """Create a RecoveryBotConfig that differs nur im Loss-Budget."""
    base = RecoveryBotConfig(enabled=True)
    payload = {**base.__dict__}
    payload["enabled"] = True
    payload["loss_budget_mode"] = "fixed"
    payload["fixed_loss_budget_usdt"] = float(budget)
    # Keine weiteren Budget-Grenzen erzwingen; Minimum/Maximum bleiben 0/None.
    return recovery_config_from_dict(payload)


def _run_window_with_budget(
    *,
    all_candles: list[dict[str, Any]],
    baseline_meta: dict[str, Any],
    start_index: int,
    loss_budget_usdt: float,
) -> SweepRun:
    recovery_summary = dict(baseline_meta.get("recovery_summary") or {})
    requested_start_index = int(baseline_meta.get("requested_start_index") or start_index)
    candles_processed = int(baseline_meta.get("candles_processed") or 0)
    source_timestamp = str(baseline_meta.get("source_candle_timestamp") or "")
    window = _window_for_start(
        all_candles,
        source_timestamp=source_timestamp,
        candles_processed=candles_processed,
    )
    if not window:
        raise ValueError(
            f"empty candle window for start={start_index} "
            f"(requested_start_index={requested_start_index}, "
            f"candles_processed={candles_processed})"
        )

    # Strategy-/Config-Quelle aus den bestehenden Resultaten übernehmen.
    cfg_diag = dict(baseline_meta.get("config_diagnostics") or {})
    config_source = str(cfg_diag.get("config_source") or "live")
    long_config_path = cfg_diag.get("config_path") or ""

    # Für den Long-Only-Backtest genügt der Long-Config-Pfad; Short-Config
    # wird aus denselben Defaults wie im ursprünglichen Runner geladen.
    from research.backtests.backtest_config_loader import DEFAULT_SHORT_CONFIG_PATH

    # Fill-Model aus dem bestehenden Ergebnis ableiten (falls vorhanden).
    fill_model = str(baseline_meta.get("fill_model") or "conservative")
    max_fills_per_candle = None

    recovery_bot_config = _build_recovery_config_for_budget(loss_budget_usdt)

    fill_cfg = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )

    result = run_historical_backtest(
        "APTUSDT",
        "long",
        window,
        max_candles=max(0, len(window) - 1),
        fill_model=fill_cfg.fill_model,
        max_fills_per_candle=fill_cfg.max_fills_per_candle,
        config_source=config_source,  # z.B. "live"
        long_config_path=long_config_path,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        file_config_path=None,
        recovery_bot_config=recovery_bot_config,
    )
    result.start_index = requested_start_index
    result.window_candles = len(window)
    return SweepRun(start_index=start_index, loss_budget_usdt=loss_budget_usdt, result=result)


def _first_trace_time(
    trace: list[dict[str, Any]],
    actions: set[str],
) -> tuple[int | None, str | None]:
    for entry in trace:
        if str(entry.get("action") or "") in actions:
            return entry.get("candle_index"), entry.get("timestamp")
    return None, None


def _count_actions(trace: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in trace:
        action = str(entry.get("action") or "")
        counts[action] = counts.get(action, 0) + 1
    return counts


def _write_all_runs_csv(path: Path, runs: list[SweepRun]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "start_index",
        "loss_budget_usdt",
        "final_status",
        "exit_reason",
        "final_recovery_state",
        "blocked_reason",
        "candles_processed",
        "realized_pnl",
        "unrealized_pnl",
        "overall_pnl",
        "final_long_qty",
        "final_short_qty",
        "final_qty_diff",
        "active_orders_count",
        "neutralization_count",
        "pair_reduction_count",
        "reload_count",
        "final_exit_attempted",
        "minimum_pair_reached",
        "recovery_realized_pnl",
        "loss_budget_used_usdt",
        "first_block_candle",
        "first_block_time",
        "first_pair_reducing_candle",
        "first_pair_reducing_time",
        "first_minimum_pair_candle",
        "first_minimum_pair_time",
        "first_ready_to_close_candle",
        "first_ready_to_close_time",
        "first_waiting_for_reload_candle",
        "first_waiting_for_reload_time",
        "first_closed_candle",
        "first_closed_time",
        "trace_action_counts",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for run in runs:
            res = run.result
            summary = dict(res.recovery_summary or {})
            trace = list(res.recovery_trace or [])
            # Endzustand für Recovery.
            final_state = summary.get("final_state") or ""
            blocked_reason = summary.get("blocked_reason") or ""
            # Zähler aus Summary.
            neutralization_count = summary.get("neutralization_count")
            pair_reduction_count = summary.get("pair_reduction_count")
            reload_count = summary.get("reload_count")
            final_exit_attempted = summary.get("final_exit_attempted")
            minimum_pair_reached = summary.get("minimum_pair_reached")
            recovery_realized_pnl = summary.get("recovery_realized_pnl")
            loss_budget_used_usdt = summary.get("loss_budget_used_usdt")
            # Blockierung / Phasen-Events.
            first_block_candle, first_block_time = _first_trace_time(
                trace, {"NEUTRALIZATION_BLOCKED"}
            )
            first_pr_candle, first_pr_time = _first_trace_time(
                trace,
                {
                    "PAIR_REDUCTION_SUBMITTED",
                    "PAIR_REDUCTION_FILLED",
                    "MINIMUM_PAIR_REACHED",
                },
            )
            first_min_pair_candle, first_min_pair_time = _first_trace_time(
                trace, {"MINIMUM_PAIR_REACHED"}
            )
            first_ready_candle, first_ready_time = _first_trace_time(
                trace, {"FINAL_EXIT_EVALUATED"}
            )
            first_wait_reload_candle, first_wait_reload_time = _first_trace_time(
                trace, {"RELOAD_WAITING"}
            )
            first_closed_candle, first_closed_time = _first_trace_time(
                trace, {"RECOVERY_CLOSED", "FINAL_EXIT_FILLED"}
            )
            counts = _count_actions(trace)
            net_diff = None
            if res.final_long_qty is not None and res.final_short_qty is not None:
                net_diff = compute_net_long_qty(res.final_long_qty, res.final_short_qty)
            writer.writerow(
                {
                    "start_index": run.start_index,
                    "loss_budget_usdt": run.loss_budget_usdt,
                    "final_status": res.final_status,
                    "exit_reason": res.exit_reason,
                    "final_recovery_state": final_state,
                    "blocked_reason": blocked_reason,
                    "candles_processed": res.candles_processed,
                    "realized_pnl": res.realized_pnl,
                    "unrealized_pnl": res.unrealized_pnl,
                    "overall_pnl": res.overall_pnl,
                    "final_long_qty": res.final_long_qty,
                    "final_short_qty": res.final_short_qty,
                    "final_qty_diff": net_diff,
                    "active_orders_count": res.active_orders_count,
                    "neutralization_count": neutralization_count,
                    "pair_reduction_count": pair_reduction_count,
                    "reload_count": reload_count,
                    "final_exit_attempted": final_exit_attempted,
                    "minimum_pair_reached": minimum_pair_reached,
                    "recovery_realized_pnl": recovery_realized_pnl,
                    "loss_budget_used_usdt": loss_budget_used_usdt,
                    "first_block_candle": first_block_candle,
                    "first_block_time": first_block_time,
                    "first_pair_reducing_candle": first_pr_candle,
                    "first_pair_reducing_time": first_pr_time,
                    "first_minimum_pair_candle": first_min_pair_candle,
                    "first_minimum_pair_time": first_min_pair_time,
                    "first_ready_to_close_candle": first_ready_candle,
                    "first_ready_to_close_time": first_ready_time,
                    "first_waiting_for_reload_candle": first_wait_reload_candle,
                    "first_waiting_for_reload_time": first_wait_reload_time,
                    "first_closed_candle": first_closed_candle,
                    "first_closed_time": first_closed_time,
                    "trace_action_counts": "|".join(
                        f"{k}:{v}" for k, v in sorted(counts.items())
                    ),
                }
            )


def _write_per_start_csvs(runs: list[SweepRun]) -> None:
    by_start: dict[int, list[SweepRun]] = {}
    for run in runs:
        by_start.setdefault(run.start_index, []).append(run)
    for start_index, group in by_start.items():
        path = SWEEP_DIR / f"APTUSDT_start{start_index}_budget_sweep.csv"
        _write_all_runs_csv(path, group)


def _write_summary_markdown(path: Path, runs: list[SweepRun]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    by_start: dict[int, list[SweepRun]] = {}
    for run in runs:
        by_start.setdefault(run.start_index, []).append(run)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Loss-Budget-Sweep Zusammenfassung\n\n")
        for start_index in sorted(by_start):
            handle.write(f"## Startindex {start_index}\n\n")
            group = sorted(by_start[start_index], key=lambda r: r.loss_budget_usdt)
            handle.write(
                "| Budget | final_status | final_recovery_state | blocked_reason | "
                "final_long_qty | final_short_qty | neutralization_count | pair_reduction_count | "
                "reload_count | loss_budget_used_usdt |\n"
            )
            handle.write(
                "| ------: | ----------- | -------------------- | ------------- | -------------: | --------------: | -------------------: | -------------------: | ----------: | ---------------------: |\n"
            )
            for run in group:
                res = run.result
                summary = dict(res.recovery_summary or {})
                handle.write(
                    f"| {run.loss_budget_usdt:.2f} | {res.final_status} | "
                    f"{summary.get('final_state','')} | {summary.get('blocked_reason','')} | "
                    f"{res.final_long_qty} | {res.final_short_qty} | "
                    f"{summary.get('neutralization_count','')} | "
                    f"{summary.get('pair_reduction_count','')} | "
                    f"{summary.get('reload_count','')} | "
                    f"{summary.get('loss_budget_used_usdt','')} |\n"
                )
            handle.write("\n")


def _write_minimum_thresholds_csv(path: Path, runs: list[SweepRun]) -> None:
    """Bestimme einfache Mindestbudgets auf Basis der getesteten Stufen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    by_start: dict[int, list[SweepRun]] = {}
    for run in runs:
        by_start.setdefault(run.start_index, []).append(run)

    fieldnames = [
        "start_index",
        "threshold_additional_neutralization",
        "threshold_minimum_pair_reached",
        "threshold_pair_reduction_filled",
        "threshold_ready_to_close",
        "threshold_reload_reached",
        "threshold_trade_closed",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for start_index in sorted(by_start):
            group = sorted(by_start[start_index], key=lambda r: r.loss_budget_usdt)
            baseline = next((r for r in group if abs(r.loss_budget_usdt - 1.5) < 1e-9), None)
            base_neut = None
            if baseline:
                base_neut = (baseline.result.recovery_summary or {}).get(
                    "neutralization_count"
                )

            def first_where(pred) -> float | None:
                for r in group:
                    if pred(r):
                        return r.loss_budget_usdt
                return None

            thr_additional_neut = first_where(
                lambda r: base_neut is not None
                and (r.result.recovery_summary or {}).get("neutralization_count", 0)
                > base_neut
            )
            thr_min_pair = first_where(
                lambda r: (r.result.recovery_summary or {}).get("minimum_pair_reached")
            )
            thr_pair_filled = first_where(
                lambda r: any(
                    entry.get("action") == "PAIR_REDUCTION_FILLED"
                    for entry in (r.result.recovery_trace or [])
                )
            )
            thr_ready = first_where(
                lambda r: any(
                    entry.get("action") == "FINAL_EXIT_EVALUATED"
                    for entry in (r.result.recovery_trace or [])
                )
            )
            thr_reload = first_where(
                lambda r: (r.result.recovery_summary or {}).get("reload_count", 0) > 0
                or any(
                    entry.get("action") in {"RELOAD_WAITING", "RELOAD_FILLED"}
                    for entry in (r.result.recovery_trace or [])
                )
            )
            thr_closed = first_where(lambda r: r.result.final_status == "closed")

            writer.writerow(
                {
                    "start_index": start_index,
                    "threshold_additional_neutralization": thr_additional_neut,
                    "threshold_minimum_pair_reached": thr_min_pair,
                    "threshold_pair_reduction_filled": thr_pair_filled,
                    "threshold_ready_to_close": thr_ready,
                    "threshold_reload_reached": thr_reload,
                    "threshold_trade_closed": thr_closed,
                }
            )


def main() -> int:
    SWEEP_DIR.mkdir(parents=True, exist_ok=True)
    candles = _load_candles_for_symbol()
    runs: list[SweepRun] = []
    for start_index in (4000, 7500, 9750):
        meta = _load_baseline_meta(start_index)
        for budget in SWEEP_BUDGETS:
            run = _run_window_with_budget(
                all_candles=candles,
                baseline_meta=meta,
                start_index=start_index,
                loss_budget_usdt=budget,
            )
            runs.append(run)

    # Gesamt-CSV und pro-Start-CSV schreiben.
    all_csv = SWEEP_DIR / "loss_budget_sweep_all.csv"
    _write_all_runs_csv(all_csv, runs)
    _write_per_start_csvs(runs)

    # Markdown-Zusammenfassung.
    summary_md = SWEEP_DIR / "loss_budget_sweep_summary.md"
    _write_summary_markdown(summary_md, runs)

    # Mindestgrenzen (auf Basis der getesteten Stufen).
    thresholds_csv = SWEEP_DIR / "minimum_budget_thresholds.csv"
    _write_minimum_thresholds_csv(thresholds_csv, runs)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

