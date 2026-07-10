"""CLI: archive prior results, run small reproducible backtest, trust audit."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backtest_audit_recorder import BacktestAuditRecorder
from .backtester_trust_audit import (
    audit_results_directory,
    archive_existing_results,
    build_report_markdown,
    default_audit_gaps,
    export_fill_audit_records,
    git_head_commit,
    run_determinism_check,
)
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol_with_slice_info
from .historical_backtest import normalize_candles, run_historical_backtest
from .multi_start_backtest import compact_result_dict, generate_start_indices
from .trade_block_export import ensure_backtest_trade_block_ids, write_trade_block_exports

SYMBOL = "APTUSDT"
DEFAULT_LIMIT = 52569
DEFAULT_START_STEP = 500
DEFAULT_WINDOW = 6000
DEFAULT_MAX_STARTS = 3
DEFAULT_OUTPUT = Path("research/backtests/results/backtester_trust_audit")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_trust_backtests(
    *,
    output_dir: Path,
    directions: list[str],
    start_indices: list[int],
    candles: list[Any],
    input_slice_start_index: int,
    window_candles: int,
) -> dict[str, Any]:
    runs_dir = output_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    all_runs: dict[str, list[dict[str, Any]]] = {}
    fill_audit_dir = output_dir / "fill_audit"
    fill_audit_dir.mkdir(parents=True, exist_ok=True)

    for direction in directions:
        direction_runs: list[dict[str, Any]] = []
        for trade_number, start_index in enumerate(start_indices, start=1):
            slice_candles = candles[start_index : start_index + window_candles]
            recorder = BacktestAuditRecorder(enabled=True)
            result = run_historical_backtest(
                SYMBOL,
                direction,
                slice_candles,
                fill_model="conservative",
                config_source="live",
                audit_recorder=recorder,
                absolute_trade_start_index=start_index,
                input_slice_start_index=input_slice_start_index,
            )
            result.trade_number = trade_number
            ensure_backtest_trade_block_ids(result)
            write_trade_block_exports(result, runs_dir)
            export_fill_audit_records(
                fill_audit_dir / f"{result.trade_block_id or f'trade_{trade_number:04d}'}_fill_audit.json",
                recorder,
            )
            compact = compact_result_dict(result)
            compact["trade_number"] = trade_number
            compact["start_index"] = start_index
            compact["input_slice_start_index"] = input_slice_start_index
            direction_runs.append(compact)
        all_runs[direction] = direction_runs
        write_json(runs_dir / f"{direction}_continuous_results.json", {
            "metadata": {
                "symbol": SYMBOL,
                "direction": direction,
                "fill_model": "conservative",
                "config_source": "live",
                "recovery_bot": False,
                "start_indices": start_indices,
                "window_candles": window_candles,
            },
            "runs": direction_runs,
        })
    return {"runs_dir": str(runs_dir), "runs": all_runs}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Original hedge backtester trust audit")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-archive", action="store_true")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-starts", type=int, default=DEFAULT_MAX_STARTS)
    parser.add_argument("--start-step-candles", type=int, default=DEFAULT_START_STEP)
    parser.add_argument("--window-candles", type=int, default=DEFAULT_WINDOW)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    results_root = Path("research/backtests/results")
    archive_path: str | None = None
    if not args.skip_archive:
        archive_name = f"archive_before_backtester_trust_audit_{_timestamp_slug()}"
        archive_existing_results(
            results_root,
            archive_name=archive_name,
            preserve={output_dir.name},
        )
        archive_path = str(results_root / archive_name)

    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    candles, slice_info = load_candles_for_symbol_with_slice_info(
        args.symbol,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=args.limit,
    )
    candle_list = normalize_candles(args.symbol, candles)
    start_indices = generate_start_indices(
        len(candle_list),
        start_step_candles=args.start_step_candles,
        window_candles=args.window_candles,
        max_starts=args.max_starts,
    )
    if not start_indices:
        raise RuntimeError("no start indices generated")

    payload = run_trust_backtests(
        output_dir=output_dir,
        directions=["long", "short"],
        start_indices=start_indices,
        candles=candle_list,
        input_slice_start_index=slice_info.input_slice_start_index,
        window_candles=args.window_candles,
    )

    determinism_checks: dict[str, Any] = {}
    for direction in ("long", "short"):
        passed, fp_a, fp_b = run_determinism_check(
            symbol=args.symbol,
            direction=direction,
            candles=candle_list,
            start_index=start_indices[0],
            window_candles=min(args.window_candles, 3000),
        )
        determinism_checks[direction] = {
            "passed": passed,
            "fingerprint_a": fp_a,
            "fingerprint_b": fp_b,
        }

    summaries: list[Any] = []
    runs_dir = Path(payload["runs_dir"])
    fill_audit_dir = output_dir / "fill_audit"
    for direction, runs in payload["runs"].items():
        summaries.extend(
            audit_results_directory(
                output_dir=runs_dir,
                runs=runs,
                candles=candle_list,
                fill_audit_dir=fill_audit_dir,
                start_indices=start_indices,
                input_slice_start_index=slice_info.input_slice_start_index,
            )
        )

    reproduction_command = (
        "PYTHONPATH=. python3 -m research.backtests.run_backtester_trust_audit "
        f"--output-dir {output_dir} --skip-archive"
    )

    report_payload = {
        "verdict": "VERTRAUENSWÜRDIG"
        if all(s.trusted for s in summaries) and all(v["passed"] for v in determinism_checks.values())
        else "NICHT VERTRAUENSWÜRDIG",
        "reproduction_command": reproduction_command,
        "git_commit": git_head_commit(),
        "archive_path": archive_path,
        "elapsed_seconds": time.time() - started,
        "input": {
            "symbol": args.symbol,
            "candle_count": len(candle_list),
            "start_indices": start_indices,
            "window_candles": args.window_candles,
            "fill_model": "conservative",
            "config_source": "live",
            "data_dir": str(DEFAULT_DATA_DIR),
            "input_slice_start_index": slice_info.input_slice_start_index,
        },
        "determinism": determinism_checks,
        "audit_gaps": default_audit_gaps(),
        "trade_summaries": [
            {
                "trade_block_id": s.trade_block_id,
                "direction": s.direction,
                "start_index": s.start_index,
                "checks_passed": s.checks_passed,
                "checks_failed": s.checks_failed,
                "realized_pnl": s.realized_pnl,
                "findings": [f.to_dict() for f in s.findings],
                "forward_rows": s.forward_rows,
            }
            for s in summaries
        ],
    }
    write_json(output_dir / "report.json", report_payload)
    (output_dir / "REPORT.md").write_text(
        build_report_markdown(
            reproduction_command=reproduction_command,
            git_commit=report_payload["git_commit"],
            config_summary={
                "fill_model": "conservative",
                "config_source": "live",
                "recovery_bot": False,
                "dynamic_scaling": False,
                "addon_recovery": False,
            },
            input_summary=report_payload["input"],
            summaries=summaries,
            determinism=determinism_checks,
            archive_path=archive_path,
            audit_gaps=default_audit_gaps(),
        ),
        encoding="utf-8",
    )

    print(json.dumps({
        "verdict": report_payload["verdict"],
        "output_dir": str(output_dir),
        "trades_audited": len(summaries),
        "failed_trades": sum(1 for s in summaries if not s.trusted),
        "archive_path": archive_path,
    }, indent=2))
    return 0 if report_payload["verdict"] == "VERTRAUENSWÜRDIG" else 1


if __name__ == "__main__":
    raise SystemExit(main())
