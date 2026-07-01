"""CLI runner for original hedge bot historical backtests (Phase 5/7)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .backtest_config_loader import (
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
    ConfigSource,
)
from .backtest_report import (
    BacktestResult,
    comparison_output_paths,
    default_output_paths,
    write_results_json,
    write_summary_csv,
)
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from .debug_report import print_debug_report
from .dynamic_cycle_order_scaling import (
    DynamicCycleOrderScalingConfig,
    config_from_json_string as dynamic_scaling_config_from_json_string,
    default_dynamic_cycle_order_scaling_config,
)
from .cycle_short_tp_relief import (
    CycleShortTpReliefConfig,
    config_from_json_string as cycle_short_tp_relief_config_from_json_string,
    default_cycle_short_tp_relief_config,
)
from .stuck_recovery_reload import (
    StuckRecoveryReloadConfig,
    config_from_json_string as stuck_reload_config_from_json_string,
    default_stuck_recovery_reload_config,
)
from .fill_models import COMPARE_FILL_MODELS, resolve_fill_model_config
from .historical_backtest import run_historical_backtest
from .continuous_reentry_backtest import (
    print_continuous_reentry_summary,
    run_continuous_reentry_backtests,
)
from .multi_start_backtest import print_multi_start_summary, run_multi_start_backtests
from .unfinished_deep_dive import (
    parse_deep_dive_start_indices,
    print_unfinished_deep_dive_summary,
    run_unfinished_deep_dive_after_multi_start,
)
from .pnl_coverage_audit import export_pnl_coverage_audits, print_pnl_coverage_audit_summary
from .trade_block_export import (
    export_trade_blocks_for_results,
    iter_payload_results,
    parse_trade_block_start_indices,
    print_trade_block_export_summary,
)


def resolve_dynamic_cycle_scaling_config(args: argparse.Namespace) -> DynamicCycleOrderScalingConfig | None:
    json_payload = getattr(args, "dynamic_cycle_order_scaling_config_json", None)
    if json_payload:
        return dynamic_scaling_config_from_json_string(str(json_payload))
    if bool(getattr(args, "dynamic_cycle_order_scaling", False)):
        return default_dynamic_cycle_order_scaling_config()
    return None


def resolve_stuck_recovery_reload_config(args: argparse.Namespace) -> StuckRecoveryReloadConfig | None:
    json_payload = getattr(args, "stuck_recovery_reload_config_json", None)
    if json_payload:
        return stuck_reload_config_from_json_string(str(json_payload))
    if bool(getattr(args, "stuck_recovery_reload", False)):
        return default_stuck_recovery_reload_config()
    return None


def resolve_cycle_short_tp_relief_config(args: argparse.Namespace) -> CycleShortTpReliefConfig | None:
    json_payload = getattr(args, "cycle_short_tp_relief_config_json", None)
    if json_payload:
        return cycle_short_tp_relief_config_from_json_string(str(json_payload))
    if bool(getattr(args, "cycle_short_tp_relief", False)):
        return default_cycle_short_tp_relief_config()
    return None


def resolve_directions(direction: str) -> list[str]:
    normalized = str(direction or "both").strip().lower()
    if normalized == "both":
        return ["long", "short"]
    if normalized in {"long", "short"}:
        return [normalized]
    raise ValueError(f"unsupported direction: {direction}")


def _load_candles(
    *,
    symbol: str,
    limit: int,
    data_dir: str | Path,
) -> list[dict[str, Any]]:
    candles = load_candles_for_symbol(
        symbol,
        timeframe="5m",
        data_dir=data_dir,
        limit=limit,
    )
    if not candles:
        raise FileNotFoundError(f"no candles loaded for {symbol.upper()} under {data_dir}")
    return candles


def run_original_hedge_backtests(
    *,
    symbol: str = "APTUSDT",
    direction: str = "both",
    limit: int = 1000,
    max_candles: int | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    output_dir: str | Path = "research/backtests/results",
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    config_source: ConfigSource = "test",
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    write_json: bool = True,
    write_csv: bool = True,
    candles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    symbol_upper = symbol.upper()
    directions = resolve_directions(direction)
    effective_max_candles = max_candles if max_candles is not None else limit
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )

    candle_rows = candles
    if candle_rows is None:
        candle_rows = _load_candles(symbol=symbol_upper, limit=limit, data_dir=data_dir)

    results: dict[str, BacktestResult] = {}
    for run_direction in directions:
        results[run_direction] = run_historical_backtest(
            symbol_upper,
            run_direction,
            candle_rows,
            max_candles=effective_max_candles,
            fill_model=fill_config.fill_model,
            max_fills_per_candle=fill_config.max_fills_per_candle,
            config_source=config_source,
            long_config_path=long_config_path,
            short_config_path=short_config_path,
            file_config_path=file_config_path,
        )

    json_path, csv_path = default_output_paths(output_dir, symbol_upper)
    written: dict[str, str | None] = {"json": None, "csv": None}

    if write_json:
        written["json"] = str(
            write_results_json(
                json_path,
                symbol=symbol_upper,
                limit=limit,
                max_candles=effective_max_candles,
                results=results,
                meta={
                    "data_dir": str(data_dir),
                    "fill_model": fill_config.fill_model,
                    "max_fills_per_candle": fill_config.max_fills_per_candle,
                    "directions": directions,
                    "config_source": config_source,
                    "long_config_path": str(long_config_path),
                    "short_config_path": str(short_config_path),
                    "file_config_path": str(file_config_path) if file_config_path else None,
                },
            )
        )
    if write_csv:
        written["csv"] = str(write_summary_csv(csv_path, results.values()))

    return {
        "symbol": symbol_upper,
        "directions": directions,
        "limit": limit,
        "max_candles": effective_max_candles,
        "candles_loaded": len(candle_rows),
        "fill_model": fill_config.fill_model,
        "max_fills_per_candle": fill_config.max_fills_per_candle,
        "config_source": config_source,
        "results": results,
        "output_files": written,
    }


def run_fill_model_comparison(
    *,
    symbol: str = "APTUSDT",
    direction: str = "both",
    limit: int = 1000,
    max_candles: int | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    output_dir: str | Path = "research/backtests/results",
    write_json: bool = True,
    write_csv: bool = True,
    config_source: ConfigSource = "test",
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
) -> dict[str, Any]:
    symbol_upper = symbol.upper()
    directions = resolve_directions(direction)
    effective_max_candles = max_candles if max_candles is not None else limit
    candle_rows = _load_candles(symbol=symbol_upper, limit=limit, data_dir=data_dir)

    results: dict[str, BacktestResult] = {}
    for model_name, explicit_max in COMPARE_FILL_MODELS:
        fill_config = resolve_fill_model_config(
            fill_model=model_name,
            max_fills_per_candle=explicit_max,
        )
        for run_direction in directions:
            key = f"{run_direction}:{fill_config.fill_model}"
            results[key] = run_historical_backtest(
                symbol_upper,
                run_direction,
                candle_rows,
                max_candles=effective_max_candles,
                fill_model=fill_config.fill_model,
                max_fills_per_candle=fill_config.max_fills_per_candle,
                config_source=config_source,
                long_config_path=long_config_path,
                short_config_path=short_config_path,
                file_config_path=file_config_path,
            )

    json_path, csv_path = comparison_output_paths(output_dir, symbol_upper)
    written: dict[str, str | None] = {"json": None, "csv": None}
    if write_json:
        written["json"] = str(
            write_results_json(
                json_path,
                symbol=symbol_upper,
                limit=limit,
                max_candles=effective_max_candles,
                results=results,
                meta={
                    "data_dir": str(data_dir),
                    "compare_fill_models": True,
                    "models": [model for model, _ in COMPARE_FILL_MODELS],
                    "directions": directions,
                },
            )
        )
    if write_csv:
        written["csv"] = str(write_summary_csv(csv_path, results.values()))

    return {
        "symbol": symbol_upper,
        "directions": directions,
        "limit": limit,
        "max_candles": effective_max_candles,
        "candles_loaded": len(candle_rows),
        "compare_fill_models": True,
        "results": results,
        "output_files": written,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run original hedge bot historical backtest on feather candles.",
    )
    parser.add_argument("--symbol", default="APTUSDT", help="Symbol, e.g. APTUSDT")
    parser.add_argument(
        "--direction",
        default="both",
        choices=["long", "short", "both"],
        help="Strategy direction to run",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Load last N candles from feather file",
    )
    parser.add_argument(
        "--max-candles",
        type=int,
        default=None,
        help="Max candles to process after entry (default: same as --limit)",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Directory containing Bybit futures feather files",
    )
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results",
        help="Directory for JSON/CSV output",
    )
    parser.add_argument(
        "--fill-model",
        default="conservative",
        choices=["conservative", "conservative_multi", "paired_exit"],
        help="5m fill model variant",
    )
    parser.add_argument(
        "--max-fills-per-candle",
        type=int,
        default=None,
        help="Override model default max fills per candle",
    )
    parser.add_argument(
        "--compare-fill-models",
        action="store_true",
        help="Run conservative, conservative_multi, and paired_exit comparison",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON results (default: write JSON and CSV)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Write CSV summary (default: write JSON and CSV)",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Skip JSON output",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="Skip CSV output",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print extended debug summary per direction",
    )
    parser.add_argument(
        "--print-fill-log",
        action="store_true",
        help="With --debug, print full fill_log",
    )
    parser.add_argument(
        "--print-order-log",
        action="store_true",
        help="With --debug, print full order_log",
    )
    parser.add_argument(
        "--print-intent-log",
        action="store_true",
        help="With --debug, print full intent_log",
    )
    parser.add_argument(
        "--print-exit-diagnostics",
        action="store_true",
        help="With --debug, print full final exit diagnostics",
    )
    parser.add_argument(
        "--print-config-diagnostics",
        action="store_true",
        help="With --debug, print full config and exit-level diagnostics",
    )
    parser.add_argument(
        "--config-source",
        default="test",
        choices=["test", "live", "file"],
        help="Config source: test defaults, live bot JSON, or explicit file",
    )
    parser.add_argument(
        "--long-config-path",
        default=str(DEFAULT_LONG_CONFIG_PATH),
        help="Long bot live config path for --config-source live",
    )
    parser.add_argument(
        "--short-config-path",
        default=str(DEFAULT_SHORT_CONFIG_PATH),
        help="Short bot live config path for --config-source live",
    )
    parser.add_argument(
        "--config-path",
        default=None,
        help="Config file path for --config-source file",
    )
    parser.add_argument(
        "--multi-start",
        action="store_true",
        help="Run multiple backtests at staggered start points",
    )
    parser.add_argument(
        "--start-step-candles",
        type=int,
        default=100,
        help="Candle step between multi-start windows (default: 100)",
    )
    parser.add_argument(
        "--window-candles",
        type=int,
        default=1000,
        help="Max candles per multi-start window (default: 1000)",
    )
    parser.add_argument(
        "--max-starts",
        type=int,
        default=20,
        help="Maximum number of multi-start runs per direction (default: 20)",
    )
    parser.add_argument(
        "--multi-fill-models",
        action="store_true",
        help="With --multi-start, run conservative, conservative_multi, and paired_exit",
    )
    parser.add_argument(
        "--include-logs",
        action="store_true",
        help="With --multi-start JSON output, include full fill/order/intent logs",
    )
    parser.add_argument(
        "--deep-dive-unfinished",
        action="store_true",
        help="With --multi-start, re-run unfinished windows with an extended horizon",
    )
    parser.add_argument(
        "--extended-window-candles",
        type=int,
        default=3000,
        help="Extended candle window for --deep-dive-unfinished (default: 3000)",
    )
    parser.add_argument(
        "--deep-dive-start-indices",
        default=None,
        help="Optional comma-separated start indices to deep-dive, e.g. 800,1300,1600",
    )
    parser.add_argument(
        "--trade-block-export",
        action="store_true",
        help="Export fills/orders/intents grouped by trade_block_id",
    )
    parser.add_argument(
        "--trade-block-start-indices",
        default=None,
        help="With --multi-start, export only these start indices (comma-separated)",
    )
    parser.add_argument(
        "--pnl-coverage-audit",
        action="store_true",
        help="Export cycle PnL coverage audit CSV/JSON",
    )
    parser.add_argument(
        "--continuous-reentry",
        action="store_true",
        help="Chain backtests: start a new trade after each closed trade block",
    )
    parser.add_argument(
        "--continuous-start-index",
        type=int,
        default=0,
        help="First candle index for continuous re-entry (default: 0)",
    )
    parser.add_argument(
        "--continuous-window-candles",
        type=int,
        default=None,
        help="Candle horizon for continuous re-entry (default: --limit candles loaded)",
    )
    parser.add_argument(
        "--continuous-max-trades",
        type=int,
        default=None,
        help="Optional cap on consecutive trade blocks in continuous re-entry",
    )
    parser.add_argument(
        "--dynamic-cycle-order-scaling",
        action="store_true",
        help="Backtest-only: apply dynamic scaling to CYCLE_X_LONG_ADD and CYCLE_X_SHORT_REDUCE",
    )
    parser.add_argument(
        "--dynamic-cycle-order-scaling-config-json",
        default=None,
        help="JSON config for dynamic cycle order scaling (overrides default bands)",
    )
    parser.add_argument(
        "--stuck-recovery-reload",
        action="store_true",
        help="Backtest-only: reload trades stuck on CYCLE_N_SHORT_REDUCE",
    )
    parser.add_argument(
        "--stuck-recovery-reload-config-json",
        default=None,
        help="JSON config for stuck recovery reload (overrides defaults)",
    )
    parser.add_argument(
        "--cycle-short-tp-relief",
        action="store_true",
        help="Backtest-only: cap cycle short-reduce distance and carry uncovered loss to exits",
    )
    parser.add_argument(
        "--cycle-short-tp-relief-config-json",
        default=None,
        help="JSON config for cycle short-TP relief (overrides defaults)",
    )
    return parser


def _print_run_summary(payload: dict[str, Any]) -> None:
    header = (
        f"symbol={payload['symbol']} candles_loaded={payload['candles_loaded']}"
    )
    if payload.get("compare_fill_models"):
        print(f"{header} compare_fill_models=True")
    else:
        print(
            f"{header} fill_model={payload.get('fill_model')} "
            f"max_fills_per_candle={payload.get('max_fills_per_candle')} "
            f"config_source={payload.get('config_source')}"
        )

    for key, result in payload["results"].items():
        label = key if payload.get("compare_fill_models") else key
        print(
            f"  {label}: status={result.final_status} "
            f"fills={result.fills_count} pnl={result.realized_pnl:.4f} "
            f"candles={result.candles_processed} exit={result.exit_reason} "
            f"fill_model={result.fill_model} max_fills={result.max_fills_per_candle} "
            f"config_source={result.config_source} price_tick_size={result.price_tick_size}"
        )
    if payload["output_files"]["json"]:
        print(f"json={payload['output_files']['json']}")
    if payload["output_files"]["csv"]:
        print(f"csv={payload['output_files']['csv']}")


def _warn_if_test_config_for_research(symbol: str, config_source: str) -> None:
    """Warn when research backtests use fallback test config instead of live bot JSON."""
    normalized_source = str(config_source or "test").strip().lower()
    smoke_symbols = {"BTCUSDT"}
    if normalized_source != "test":
        return
    if str(symbol or "").upper() in smoke_symbols:
        return
    print(
        "WARNING: config_source=test uses fallback price_tick_size (typically 0.1). "
        "Research backtests for production symbols should use --config-source live.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _warn_if_test_config_for_research(args.symbol, args.config_source)

    write_json = not args.no_json
    write_csv = not args.no_csv
    if args.json:
        write_json = True
    if args.csv:
        write_csv = True
    if args.no_json and args.no_csv and not args.json and not args.csv:
        write_json = True
        write_csv = True

    try:
        if args.continuous_reentry:
            if args.multi_start:
                raise ValueError("--continuous-reentry cannot be combined with --multi-start")
            if args.compare_fill_models:
                raise ValueError("--continuous-reentry cannot be combined with --compare-fill-models")
            candle_rows = _load_candles(
                symbol=args.symbol,
                limit=args.limit,
                data_dir=args.data_dir,
            )
            payload = run_continuous_reentry_backtests(
                symbol=args.symbol,
                direction=args.direction,
                candles=candle_rows,
                continuous_start_index=args.continuous_start_index,
                continuous_window_candles=args.continuous_window_candles,
                continuous_max_trades=args.continuous_max_trades,
                config_source=args.config_source,
                fill_model=args.fill_model,
                max_fills_per_candle=args.max_fills_per_candle,
                long_config_path=args.long_config_path,
                short_config_path=args.short_config_path,
                file_config_path=args.config_path,
                output_dir=args.output_dir,
                write_json=write_json,
                write_csv=write_csv,
                include_logs=args.include_logs or args.debug,
            )
        elif args.multi_start:
            if args.compare_fill_models:
                raise ValueError("--multi-start cannot be combined with --compare-fill-models")
            if args.deep_dive_unfinished and args.extended_window_candles <= args.window_candles:
                raise ValueError(
                    "--extended-window-candles must be greater than --window-candles for deep dive"
                )
            candle_rows = _load_candles(
                symbol=args.symbol,
                limit=args.limit,
                data_dir=args.data_dir,
            )
            payload = run_multi_start_backtests(
                symbol=args.symbol,
                direction=args.direction,
                candles=candle_rows,
                config_source=args.config_source,
                fill_model=args.fill_model,
                max_fills_per_candle=args.max_fills_per_candle,
                multi_fill_models=args.multi_fill_models,
                start_step_candles=args.start_step_candles,
                window_candles=args.window_candles,
                max_starts=args.max_starts,
                long_config_path=args.long_config_path,
                short_config_path=args.short_config_path,
                file_config_path=args.config_path,
                output_dir=args.output_dir,
                write_json=write_json,
                write_csv=write_csv,
                include_logs=args.include_logs or args.debug,
                dynamic_cycle_scaling_config=resolve_dynamic_cycle_scaling_config(args),
                stuck_recovery_reload_config=resolve_stuck_recovery_reload_config(args),
                cycle_short_tp_relief_config=resolve_cycle_short_tp_relief_config(args),
            )
            if args.deep_dive_unfinished:
                deep_dive_payload = run_unfinished_deep_dive_after_multi_start(
                    multi_start_payload=payload,
                    candles=candle_rows,
                    config_source=args.config_source,
                    fill_model=args.fill_model,
                    max_fills_per_candle=args.max_fills_per_candle,
                    original_window_candles=args.window_candles,
                    extended_window_candles=args.extended_window_candles,
                    deep_dive_start_indices=parse_deep_dive_start_indices(
                        args.deep_dive_start_indices
                    ),
                    long_config_path=args.long_config_path,
                    short_config_path=args.short_config_path,
                    file_config_path=args.config_path,
                    output_dir=args.output_dir,
                    write_json=write_json,
                    write_csv=write_csv,
                    include_logs=args.include_logs or args.debug,
                )
                payload["deep_dive"] = deep_dive_payload
        elif args.compare_fill_models:
            payload = run_fill_model_comparison(
                symbol=args.symbol,
                direction=args.direction,
                limit=args.limit,
                max_candles=args.max_candles,
                data_dir=args.data_dir,
                output_dir=args.output_dir,
                write_json=write_json,
                write_csv=write_csv,
                config_source=args.config_source,
                long_config_path=args.long_config_path,
                short_config_path=args.short_config_path,
                file_config_path=args.config_path,
            )
        else:
            payload = run_original_hedge_backtests(
                symbol=args.symbol,
                direction=args.direction,
                limit=args.limit,
                max_candles=args.max_candles,
                data_dir=args.data_dir,
                output_dir=args.output_dir,
                fill_model=args.fill_model,
                max_fills_per_candle=args.max_fills_per_candle,
                write_json=write_json,
                write_csv=write_csv,
                config_source=args.config_source,
                long_config_path=args.long_config_path,
                short_config_path=args.short_config_path,
                file_config_path=args.config_path,
            )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    trade_block_written: list[dict[str, str]] = []
    if args.trade_block_export:
        if payload.get("multi_start"):
            start_indices = parse_trade_block_start_indices(args.trade_block_start_indices)
            if not start_indices:
                print(
                    "error: --trade-block-start-indices is required with "
                    "--multi-start --trade-block-export",
                    file=sys.stderr,
                )
                return 1
            trade_block_written = export_trade_blocks_for_results(
                iter_payload_results(payload),
                args.output_dir,
                start_indices=start_indices,
            )
        else:
            trade_block_written = export_trade_blocks_for_results(
                iter_payload_results(payload),
                args.output_dir,
            )

    pnl_audit_written: list[dict[str, str]] = []
    pnl_audit_rows: list[list[dict[str, Any]]] = []
    if args.pnl_coverage_audit:
        audit_results = iter_payload_results(payload)
        audit_start_indices = None
        if payload.get("multi_start") and args.trade_block_start_indices:
            audit_start_indices = parse_trade_block_start_indices(args.trade_block_start_indices)
        pnl_audit_written, pnl_audit_rows = export_pnl_coverage_audits(
            audit_results,
            args.output_dir,
            start_indices=audit_start_indices,
        )

    if payload.get("continuous_reentry"):
        print_continuous_reentry_summary(payload)
    elif payload.get("multi_start"):
        print_multi_start_summary(payload)
        if payload.get("deep_dive"):
            print_unfinished_deep_dive_summary(payload["deep_dive"])
    else:
        _print_run_summary(payload)

    if trade_block_written:
        print_trade_block_export_summary(trade_block_written)

    if pnl_audit_written:
        print_pnl_coverage_audit_summary(pnl_audit_written, pnl_audit_rows)

    if args.debug and not payload.get("multi_start") and not payload.get("continuous_reentry"):
        for _, result in payload["results"].items():
            print_debug_report(
                result,
                print_fill_log=args.print_fill_log,
                print_order_log=args.print_order_log,
                print_intent_log=args.print_intent_log,
                print_exit_diagnostics=args.print_exit_diagnostics,
                print_config_diagnostics=args.print_config_diagnostics,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
