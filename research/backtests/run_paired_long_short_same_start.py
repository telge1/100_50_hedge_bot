"""Run paired long/short backtests from a fixed long start schedule (backtest-only)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .backtest_config_loader import DEFAULT_LONG_CONFIG_PATH, DEFAULT_SHORT_CONFIG_PATH
from .backtest_report import BacktestResult
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol_with_slice_info
from .historical_backtest import normalize_candles, run_historical_backtest
from .paired_direction_recovery import mirror_recovery_start_purpose
from .paired_long_short_analysis import (
    backtest_result_to_row,
    build_pair_comparison_rows,
    build_recovery_hedge_rows,
    combine_summaries,
    mtm_from_backtest_result,
    summarize_direction_runs,
    write_csv,
    write_json,
)
from .paired_start_schedule import (
    build_paired_start_schedule,
    load_long_continuous_results,
    result_to_schedule_entry,
    trade_mark_to_market,
    write_paired_start_schedule,
)
from .recovery_bot_config import RecoveryBotConfig, default_recovery_bot_config

DEFAULT_LONG_RESULTS = (
    "research/backtests/results/integrated_recovery_parameter_sweep_20260709T150115Z/"
    "variants/CYCLE_4_LONG_ADD_wait_576/APTUSDT_original_hedge_5m_continuous_results.json"
)
DEFAULT_OUTPUT_DIR = "research/backtests/results/paired_long_short_same_start_c4_wait576"
SYMBOL = "APTUSDT"
LONG_RECOVERY_PURPOSE = "CYCLE_4_LONG_ADD"
RECOVERY_WAIT_CANDLES = 576
RECOVERY_TRADE_NUMBERS = (22, 37, 46, 55, 56, 61, 82, 85, 87, 110, 134, 143, 148, 173, 176, 205, 214)
EXPECTED_LONG_REALIZED = 29.596409332329458
EXPECTED_LONG_MTM = 29.372758  # approximate from sweep
EXPECTED_RECOVERY_CLOSED = 17


def _short_recovery_config() -> RecoveryBotConfig:
    cfg = default_recovery_bot_config()
    cfg.enabled = True
    cfg.recovery_start_purpose = mirror_recovery_start_purpose(LONG_RECOVERY_PURPOSE)
    cfg.recovery_wait_candles = RECOVERY_WAIT_CANDLES
    return cfg


def _run_short_from_start(
    *,
    candles: list[Any],
    start_index: int,
    slice_info: Any,
    recovery_cfg: RecoveryBotConfig,
    max_candles: int | None = None,
    pair_number: int,
) -> BacktestResult:
    remaining = candles[start_index:]
    result = run_historical_backtest(
        SYMBOL,
        "short",
        remaining,
        max_candles=max_candles,
        fill_model="conservative",
        config_source="live",
        long_config_path=DEFAULT_LONG_CONFIG_PATH,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        recovery_bot_config=recovery_cfg,
        absolute_trade_start_index=start_index,
        input_slice_start_index=slice_info.input_slice_start_index,
    )
    result.trade_number = pair_number
    result.start_index = start_index
    result.end_index = start_index + int(result.candles_processed or 0)
    result.direction = "short"
    return result


def _checkpoint_mtm(
    *,
    candles: list[Any],
    start_index: int,
    local_exit_index: int,
    slice_info: Any,
    recovery_cfg: RecoveryBotConfig,
    pair_number: int,
) -> float:
    result = _run_short_from_start(
        candles=candles,
        start_index=start_index,
        slice_info=slice_info,
        recovery_cfg=recovery_cfg,
        max_candles=max(1, int(local_exit_index)),
        pair_number=pair_number,
    )
    return mtm_from_backtest_result(result)["mark_to_market_pnl"]


def _checkpoint_long_mtm(
    *,
    candles: list[Any],
    start_index: int,
    local_exit_index: int,
    slice_info: Any,
    recovery_cfg: RecoveryBotConfig,
    pair_number: int,
) -> float:
    remaining = candles[start_index:]
    result = run_historical_backtest(
        SYMBOL,
        "long",
        remaining,
        max_candles=max(1, int(local_exit_index)),
        fill_model="conservative",
        config_source="live",
        long_config_path=DEFAULT_LONG_CONFIG_PATH,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        recovery_bot_config=recovery_cfg,
        absolute_trade_start_index=start_index,
        input_slice_start_index=slice_info.input_slice_start_index,
    )
    result.trade_number = pair_number
    return mtm_from_backtest_result(result)["mark_to_market_pnl"]


def run_parallel_mode_shorts(
    schedule_pairs: list[dict[str, Any]],
    *,
    candles: list[Any],
    slice_info: Any,
    recovery_cfg: RecoveryBotConfig,
) -> tuple[list[dict[str, Any]], dict[int, float], dict[int, float]]:
    short_rows: list[dict[str, Any]] = []
    short_mtm_at_long_exit: dict[int, float] = {}
    long_mtm_at_short_exit: dict[int, float] = {}
    long_recovery_cfg = _short_recovery_config()
    long_recovery_cfg.recovery_start_purpose = LONG_RECOVERY_PURPOSE

    for index, entry in enumerate(schedule_pairs, start=1):
        pair_number = int(entry["pair_number"])
        start_index = int(entry["start_index"])
        result = _run_short_from_start(
            candles=candles,
            start_index=start_index,
            slice_info=slice_info,
            recovery_cfg=recovery_cfg,
            pair_number=pair_number,
        )
        row = backtest_result_to_row(result)
        row["pair_number"] = pair_number
        short_rows.append(row)

        long_end = int(entry["end_index"])
        short_end = int(row["end_index"] or 0)
        long_local_exit = long_end - start_index
        short_local_exit = short_end - start_index
        short_mtm = mtm_from_backtest_result(result)
        if short_end <= long_end:
            short_mtm_at_long_exit[pair_number] = short_mtm["mark_to_market_pnl"]
        else:
            short_mtm_at_long_exit[pair_number] = _checkpoint_mtm(
                candles=candles,
                start_index=start_index,
                local_exit_index=long_local_exit,
                slice_info=slice_info,
                recovery_cfg=recovery_cfg,
                pair_number=pair_number,
            )
        if long_end <= short_end:
            long_mtm_at_short_exit[pair_number] = trade_mark_to_market(entry)["mark_to_market_pnl"]
        else:
            long_mtm_at_short_exit[pair_number] = _checkpoint_long_mtm(
                candles=candles,
                start_index=start_index,
                local_exit_index=short_local_exit,
                slice_info=slice_info,
                recovery_cfg=long_recovery_cfg,
                pair_number=pair_number,
            )
        if index % 25 == 0:
            print(f"parallel short progress: {index}/{len(schedule_pairs)}", file=sys.stderr)

    return short_rows, short_mtm_at_long_exit, long_mtm_at_short_exit


def run_realistic_short_slot_mode(
    schedule_pairs: list[dict[str, Any]],
    *,
    candles: list[Any],
    slice_info: Any,
    recovery_cfg: RecoveryBotConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, float], dict[int, float]]:
    executed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    short_mtm_at_long_exit: dict[int, float] = {}
    long_mtm_at_short_exit: dict[int, float] = {}
    long_recovery_cfg = _short_recovery_config()
    long_recovery_cfg.recovery_start_purpose = LONG_RECOVERY_PURPOSE
    short_slot_free_at = 0

    long_by_pair = {int(entry["pair_number"]): entry for entry in schedule_pairs}
    for entry in schedule_pairs:
        pair_number = int(entry["pair_number"])
        start_index = int(entry["start_index"])
        if start_index < short_slot_free_at:
            skipped.append({**entry, "skip_reason": "short_slot_busy"})
            continue
        result = _run_short_from_start(
            candles=candles,
            start_index=start_index,
            slice_info=slice_info,
            recovery_cfg=recovery_cfg,
            pair_number=pair_number,
        )
        row = backtest_result_to_row(result)
        row["pair_number"] = pair_number
        executed.append(row)
        short_slot_free_at = int(row["end_index"] or start_index) + 1

        long_entry = long_by_pair[pair_number]
        long_end = int(long_entry["end_index"])
        short_end = int(row["end_index"] or 0)
        short_mtm = trade_mark_to_market(row)
        if short_end <= long_end:
            short_mtm_at_long_exit[pair_number] = short_mtm["mark_to_market_pnl"]
        else:
            short_mtm_at_long_exit[pair_number] = _checkpoint_mtm(
                candles=candles,
                start_index=start_index,
                local_exit_index=long_end - start_index,
                slice_info=slice_info,
                recovery_cfg=recovery_cfg,
                pair_number=pair_number,
            )
        if long_end <= short_end:
            long_mtm_at_short_exit[pair_number] = trade_mark_to_market(long_entry)["mark_to_market_pnl"]
        else:
            long_mtm_at_short_exit[pair_number] = _checkpoint_long_mtm(
                candles=candles,
                start_index=start_index,
                local_exit_index=short_end - start_index,
                slice_info=slice_info,
                recovery_cfg=long_recovery_cfg,
                pair_number=pair_number,
            )

    return executed, skipped, short_mtm_at_long_exit, long_mtm_at_short_exit


def validate_long_reference(long_runs: list[dict[str, Any]]) -> dict[str, Any]:
    realized = sum(trade_mark_to_market(row)["realized_pnl"] for row in long_runs)
    mtm = sum(trade_mark_to_market(row)["mark_to_market_pnl"] for row in long_runs)
    recovery_closed = sum(
        1
        for row in long_runs
        if bool(row.get("recovery_activated"))
        and str(row.get("exit_reason") or "") == "recovery_joint_exit"
    )
    overlaps = 0
    for left, right in zip(long_runs, long_runs[1:]):
        if int(left.get("end_index") or 0) >= int(right.get("start_index") or 0):
            overlaps += 1
    return {
        "trade_count": len(long_runs),
        "total_realized_pnl": realized,
        "total_mark_to_market_pnl": mtm,
        "recovery_closed_count": recovery_closed,
        "overlap_count": overlaps,
        "realized_matches_expected": abs(realized - EXPECTED_LONG_REALIZED) < 0.05,
        "recovery_closed_matches_expected": recovery_closed == EXPECTED_RECOVERY_CLOSED,
    }


def _safe(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def build_report_markdown(
    *,
    output_dir: Path,
    short_recovery_purpose: str,
    long_validation: dict[str, Any],
    parallel_summary: dict[str, Any],
    slot_summary: dict[str, Any],
    recovery_summary: dict[str, Any],
    pair_rows: list[dict[str, Any]],
) -> str:
    worst = min(pair_rows, key=lambda row: float(row.get("combined_mtm_pnl") or 0.0))
    longest = max(
        pair_rows,
        key=lambda row: int(row.get("long_duration_candles") or 0)
        + int(row.get("short_duration_candles") or 0),
    )
    lines = [
        "# Paired Long/Short Same-Start Report",
        "",
        f"- Output: `{output_dir}`",
        f"- Long recovery purpose: `{LONG_RECOVERY_PURPOSE}`",
        f"- Short recovery purpose: `{short_recovery_purpose}`",
        f"- Wait candles: `{RECOVERY_WAIT_CANDLES}`",
        "",
        "## Purpose mirroring rationale",
        "",
        "On the long-primary bot, `CYCLE_4_LONG_ADD` is cycle 4's **first leg**.",
        "On the short-primary bot, the direction-neutral equivalent first leg is `CYCLE_4_SHORT_REDUCE`",
        "(see `fixed_cycle_hedge_bot/direction_config.py`).",
        "",
        "## Long reference validation",
        "",
        json.dumps(long_validation, indent=2),
        "",
        "## Mode A – independent parallel evaluation",
        "",
        json.dumps(parallel_summary, indent=2),
        "",
        "## Mode B – realistic one-short-slot evaluation",
        "",
        json.dumps(slot_summary, indent=2),
        "",
        "## Recovery hedge (17 long recovery trades)",
        "",
        json.dumps(recovery_summary, indent=2),
        "",
        f"Worst combined pair: #{worst.get('pair_number')} MTM={worst.get('combined_mtm_pnl')}",
        f"Longest combined duration pair: #{longest.get('pair_number')}",
    ]
    return "\n".join(lines)


def run_paired_backtest(
    *,
    long_results_path: Path,
    output_dir: Path,
    limit: int = 52569,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    short_recovery_purpose = mirror_recovery_start_purpose(LONG_RECOVERY_PURPOSE)
    recovery_cfg = _short_recovery_config()

    schedule = build_paired_start_schedule(
        long_results_path,
        long_recovery_purpose=LONG_RECOVERY_PURPOSE,
        recovery_wait_candles=RECOVERY_WAIT_CANDLES,
    )
    schedule["short_recovery_purpose"] = short_recovery_purpose
    schedule["mode"] = "paired_same_start"
    write_paired_start_schedule(output_dir / "paired_start_schedule.json", schedule)

    long_payload = load_long_continuous_results(long_results_path)
    long_runs = [result_to_schedule_entry(run) for run in long_payload["runs"]]
    for row in long_runs:
        row["direction"] = "long"
        row["pair_number"] = row["trade_number"]
    write_json(output_dir / "long_reference_summary.json", {
        "source": str(long_results_path.resolve()),
        "validation": validate_long_reference(long_payload["runs"]),
        "summary": summarize_direction_runs(long_runs, direction="long"),
        "runs": long_runs,
    })

    candles, slice_info = load_candles_for_symbol_with_slice_info(
        SYMBOL,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=limit,
    )
    candle_list = normalize_candles(SYMBOL, candles)

    started = time.time()
    parallel_short_rows, short_at_long_exit, long_at_short_exit = run_parallel_mode_shorts(
        schedule["pairs"],
        candles=candle_list,
        slice_info=slice_info,
        recovery_cfg=recovery_cfg,
    )
    parallel_pair_rows = build_pair_comparison_rows(
        long_runs=long_runs,
        short_runs_by_pair={int(row["pair_number"]): row for row in parallel_short_rows},
        short_mtm_at_long_exit_by_pair=short_at_long_exit,
        long_mtm_at_short_exit_by_pair=long_at_short_exit,
    )
    write_csv(output_dir / "paired_long_short_trade_comparison.csv", parallel_pair_rows)

    slot_short_rows, skipped, slot_short_at_long, slot_long_at_short = run_realistic_short_slot_mode(
        schedule["pairs"],
        candles=candle_list,
        slice_info=slice_info,
        recovery_cfg=recovery_cfg,
    )
    slot_pair_rows = build_pair_comparison_rows(
        long_runs=long_runs,
        short_runs_by_pair={int(row["pair_number"]): row for row in slot_short_rows},
        short_mtm_at_long_exit_by_pair=slot_short_at_long,
        long_mtm_at_short_exit_by_pair=slot_long_at_short,
    )

    recovery_rows, recovery_summary = build_recovery_hedge_rows(
        parallel_pair_rows,
        recovery_trade_numbers=RECOVERY_TRADE_NUMBERS,
    )
    write_csv(output_dir / "recovery_trade_short_hedge_analysis.csv", recovery_rows)

    long_summary = summarize_direction_runs(long_runs, direction="long", planned_starts=len(long_runs))
    parallel_short_summary = summarize_direction_runs(
        parallel_short_rows,
        direction="short",
        planned_starts=len(schedule["pairs"]),
        skipped_starts=0,
    )
    slot_short_summary = summarize_direction_runs(
        slot_short_rows,
        direction="short",
        planned_starts=len(schedule["pairs"]),
        skipped_starts=len(skipped),
    )
    parallel_combined = combine_summaries(long_summary, parallel_short_summary)
    slot_combined = combine_summaries(long_summary, slot_short_summary)

    aggregate = {
        "long_recovery_purpose": LONG_RECOVERY_PURPOSE,
        "short_recovery_purpose": short_recovery_purpose,
        "recovery_wait_candles": RECOVERY_WAIT_CANDLES,
        "elapsed_seconds": time.time() - started,
        "mode_a_parallel": {
            "long": long_summary,
            "short": parallel_short_summary,
            "combined": parallel_combined,
        },
        "mode_b_realistic_short_slot": {
            "long": long_summary,
            "short": slot_short_summary,
            "combined": slot_combined,
            "skipped_short_starts": skipped,
        },
        "recovery_hedge_summary": recovery_summary,
    }
    write_json(output_dir / "paired_long_short_aggregate_summary.json", aggregate)
    write_json(
        output_dir / "short_same_start_results.json",
        {
            "mode_a_parallel_short_runs": parallel_short_rows,
            "mode_b_slot_short_runs": slot_short_rows,
            "mode_b_skipped_starts": skipped,
        },
    )
    write_json(
        output_dir / "realistic_slot_mode_summary.json",
        {
            "short": slot_short_summary,
            "combined": slot_combined,
            "skipped_short_starts": skipped,
            "pair_rows": slot_pair_rows,
        },
    )

    report = build_report_markdown(
        output_dir=output_dir,
        short_recovery_purpose=short_recovery_purpose,
        long_validation=validate_long_reference(long_payload["runs"]),
        parallel_summary={**aggregate["mode_a_parallel"], "recovery_hedge": recovery_summary},
        slot_summary=aggregate["mode_b_realistic_short_slot"],
        recovery_summary=recovery_summary,
        pair_rows=parallel_pair_rows,
    )
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")
    return aggregate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paired long/short same-start backtest runner")
    parser.add_argument("--long-results", type=Path, default=Path(DEFAULT_LONG_RESULTS))
    parser.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--limit", type=int, default=52569)
    parser.add_argument("--paired-start-schedule", type=Path, default=None)
    args = parser.parse_args(argv)

    long_results = args.long_results
    if args.paired_start_schedule is not None:
        schedule = json.loads(args.paired_start_schedule.read_text(encoding="utf-8"))
        long_results = Path(schedule["source_results_path"])

    payload = run_paired_backtest(
        long_results_path=long_results,
        output_dir=args.output_dir.resolve(),
        limit=args.limit,
    )
    print(json.dumps({
        "output_dir": str(args.output_dir.resolve()),
        "short_recovery_purpose": payload["short_recovery_purpose"],
        "mode_a_combined_mtm": payload["mode_a_parallel"]["combined"]["combined_mark_to_market_pnl"],
        "mode_b_combined_mtm": payload["mode_b_realistic_short_slot"]["combined"]["combined_mark_to_market_pnl"],
        "recovery_hedge": payload["recovery_hedge_summary"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
