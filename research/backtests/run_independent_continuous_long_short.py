"""Independent continuous long/short backtests with shared initial start only (backtest-only)."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .backtest_config_loader import DEFAULT_LONG_CONFIG_PATH, DEFAULT_SHORT_CONFIG_PATH
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol_with_slice_info
from .continuous_reentry_backtest import run_continuous_reentry_backtests
from .historical_backtest import normalize_candles
from .independent_continuous_long_short_analysis import (
    build_combined_exposure_timeline,
    build_shared_initial_start_validation,
    combine_independent_summaries,
    merge_timeline,
    summarize_direction_runs,
    validate_independent_reentry,
    write_csv,
    write_json,
)
from .multi_start_backtest import compact_result_dict
from .paired_direction_recovery import mirror_recovery_start_purpose
from .recovery_bot_config import RecoveryBotConfig, default_recovery_bot_config
from .trade_block_export import write_trade_block_exports

SYMBOL = "APTUSDT"
LIMIT = 52569
EXPECTED_LAST_INDEX = 52568
LONG_RECOVERY_PURPOSE = "CYCLE_4_LONG_ADD"
SHORT_RECOVERY_PURPOSE = mirror_recovery_start_purpose(LONG_RECOVERY_PURPOSE)
RECOVERY_WAIT_CANDLES = 576

LONG_REFERENCE_RESULTS = (
    "research/backtests/results/integrated_recovery_parameter_sweep_20260709T150115Z/"
    "variants/CYCLE_4_LONG_ADD_wait_576/APTUSDT_original_hedge_5m_continuous_results.json"
)
LONG_REFERENCE_DIR = (
    "research/backtests/results/integrated_recovery_parameter_sweep_20260709T150115Z/"
    "variants/CYCLE_4_LONG_ADD_wait_576"
)
DEFAULT_OUTPUT_DIR = (
    "research/backtests/results/independent_continuous_long_short_same_initial_start_c4_wait576"
)
OLD_PAIRED_OUTPUT_DIR = "research/backtests/results/paired_long_short_same_start_c4_wait576"


def _recovery_config(purpose: str) -> RecoveryBotConfig:
    cfg = default_recovery_bot_config()
    cfg.enabled = True
    cfg.recovery_start_purpose = purpose
    cfg.recovery_wait_candles = RECOVERY_WAIT_CANDLES
    return cfg


def _copy_long_reference(output_dir: Path) -> Path:
    src_json = Path(LONG_REFERENCE_RESULTS)
    if not src_json.is_file():
        raise FileNotFoundError(f"long reference results missing: {src_json}")
    dst_json = output_dir / "long_continuous_results.json"
    shutil.copy2(src_json, dst_json)

    src_dir = Path(LONG_REFERENCE_DIR)
    if src_dir.is_dir():
        for path in src_dir.glob("APTUSDT_long_continuous_trade_*"):
            shutil.copy2(path, output_dir / path.name)
    return dst_json


def _load_runs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("runs") or [])


def _first_trade_fill_details(output_dir: Path, direction: str) -> dict[str, Any]:
    matches = sorted(output_dir.glob(f"APTUSDT_{direction}_continuous_trade_0001_*_trade_blocks.json"))
    if not matches:
        return {}
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    fills = [
        row
        for row in payload.get("trade_blocks") or []
        if str(row.get("row_type") or "") == "fill"
    ]
    initial_long = next((f for f in fills if "LONG" in str(f.get("purpose") or "").upper()), None)
    initial_short = next((f for f in fills if "SHORT" in str(f.get("purpose") or "").upper()), None)
    return {
        "initial_long_fill": initial_long,
        "initial_short_fill": initial_short,
    }


def run_independent_backtest(
    *,
    output_dir: Path,
    rerun_long: bool = False,
    rerun_short: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    candles, slice_info = load_candles_for_symbol_with_slice_info(
        SYMBOL,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=LIMIT,
    )
    candle_list = normalize_candles(SYMBOL, candles)
    if len(candle_list) != LIMIT:
        raise RuntimeError(f"expected {LIMIT} candles, loaded {len(candle_list)}")

    first_candle = {
        "timestamp": candle_list[0].timestamp.isoformat() if candle_list[0].timestamp else None,
        "open": float(candle_list[0].open),
        "high": float(candle_list[0].high),
        "low": float(candle_list[0].low),
        "close": float(candle_list[0].close),
    }
    candle_closes = [float(c.close) for c in candle_list]

    if rerun_long or not any(output_dir.glob("APTUSDT_long_continuous_trade_*_trade_blocks.json")):
        long_payload = run_continuous_reentry_backtests(
            symbol=SYMBOL,
            direction="long",
            candles=candles,
            continuous_start_index=0,
            continuous_window_candles=LIMIT,
            continuous_max_trades=1000,
            config_source="live",
            fill_model="conservative",
            recovery_bot_config=_recovery_config(LONG_RECOVERY_PURPOSE),
            input_slice_start_index=slice_info.input_slice_start_index,
            candle_source_total_count=slice_info.candle_source_total_count,
            input_slice_first_timestamp=slice_info.input_slice_first_timestamp,
            input_slice_last_timestamp=slice_info.input_slice_last_timestamp,
            output_dir=output_dir,
            write_json=False,
            write_csv=False,
        )
        long_runs = [compact_result_dict(r) for r in long_payload["results"]]
        write_json(output_dir / "long_continuous_results.json", {
            "metadata": {
                "direction": "long",
                "recovery_start_purpose": LONG_RECOVERY_PURPOSE,
                "recovery_wait_candles": RECOVERY_WAIT_CANDLES,
                "trades_started": len(long_runs),
            },
            "runs": long_runs,
        })
        for result in long_payload["results"]:
            write_trade_block_exports(result, output_dir)
    else:
        _copy_long_reference(output_dir)
        long_runs = _load_runs(output_dir / "long_continuous_results.json")

    short_runs_path = output_dir / "short_continuous_results.json"
    if short_runs_path.is_file() and not rerun_short:
        short_runs = _load_runs(short_runs_path)
    else:
        short_payload = run_continuous_reentry_backtests(
            symbol=SYMBOL,
            direction="short",
            candles=candles,
            continuous_start_index=0,
            continuous_window_candles=LIMIT,
            continuous_max_trades=1000,
            config_source="live",
            fill_model="conservative",
            recovery_bot_config=_recovery_config(SHORT_RECOVERY_PURPOSE),
            input_slice_start_index=slice_info.input_slice_start_index,
            candle_source_total_count=slice_info.candle_source_total_count,
            input_slice_first_timestamp=slice_info.input_slice_first_timestamp,
            input_slice_last_timestamp=slice_info.input_slice_last_timestamp,
            output_dir=output_dir,
            write_json=False,
            write_csv=False,
        )
        short_runs = [compact_result_dict(r) for r in short_payload["results"]]
        write_json(output_dir / "short_continuous_results.json", {
            "metadata": {
                "direction": "short",
                "recovery_start_purpose": SHORT_RECOVERY_PURPOSE,
                "recovery_wait_candles": RECOVERY_WAIT_CANDLES,
            },
            "runs": short_runs,
        })
        for result in short_payload["results"]:
            write_trade_block_exports(result, output_dir)

    long_first = long_runs[0]
    short_first = short_runs[0]
    initial_validation = build_shared_initial_start_validation(
        first_candle=first_candle,
        long_first_trade=long_first,
        short_first_trade=short_first,
    )
    fill_details = {
        "long": _first_trade_fill_details(output_dir, "long"),
        "short": _first_trade_fill_details(output_dir, "short"),
    }
    initial_validation["fill_details"] = fill_details
    long_fill = (fill_details.get("long") or {}).get("initial_long_fill") or {}
    short_fill = (fill_details.get("short") or {}).get("initial_short_fill") or {}
    initial_validation["long_initial_entry_fill_price"] = long_fill.get("fill_price")
    initial_validation["short_initial_entry_fill_price"] = short_fill.get("fill_price")
    initial_validation["long_initial_long_qty_after_entry"] = long_fill.get("long_qty_after")
    initial_validation["long_initial_short_qty_after_entry"] = long_fill.get("short_qty_after")
    initial_validation["short_initial_long_qty_after_entry"] = short_fill.get("long_qty_after")
    initial_validation["short_initial_short_qty_after_entry"] = short_fill.get("short_qty_after")
    initial_validation["recovery_config"] = {
        "long_recovery_purpose": LONG_RECOVERY_PURPOSE,
        "short_recovery_purpose": SHORT_RECOVERY_PURPOSE,
        "recovery_wait_candles": RECOVERY_WAIT_CANDLES,
        "mirroring_rationale": (
            "CYCLE_4_LONG_ADD is cycle-4 first leg on long-primary bot; "
            "CYCLE_4_SHORT_REDUCE is cycle-4 first leg on short-primary bot "
            "(see fixed_cycle_hedge_bot/direction_config.py)."
        ),
    }
    write_json(output_dir / "shared_initial_start_validation.json", initial_validation)

    long_summary = summarize_direction_runs(long_runs, direction="long")
    short_summary = summarize_direction_runs(short_runs, direction="short")
    combined_summary = combine_independent_summaries(long_summary, short_summary)
    combined_summary["reentry_validation"] = validate_independent_reentry(long_runs, short_runs)
    combined_summary["series_end_index"] = {
        "long_last_end_index": int(long_runs[-1].get("end_index") or -1),
        "short_last_end_index": int(short_runs[-1].get("end_index") or -1),
        "expected_last_index": EXPECTED_LAST_INDEX,
    }
    combined_summary["comparison_to_old_paired_test"] = {
        "old_test_misinterpretation": (
            "226 Long-Startpunkte wurden als Short-Startplan verwendet. "
            "Das war nicht die gewünschte Simulation."
        ),
        "new_test_behavior": (
            "Nur erster Start ist identisch. Danach laufen Long und Short unabhängig continuous."
        ),
        "old_paired_output_dir": OLD_PAIRED_OUTPUT_DIR,
        "old_paired_results_must_not_be_used": True,
    }

    write_json(output_dir / "long_summary.json", long_summary)
    write_json(output_dir / "short_summary.json", short_summary)
    write_json(output_dir / "combined_summary.json", combined_summary)

    timeline = merge_timeline(long_runs, short_runs)
    write_csv(output_dir / "independent_long_short_trade_timeline.csv", timeline)

    exposure_timeline, exposure_summary = build_combined_exposure_timeline(
        long_runs=long_runs,
        short_runs=short_runs,
        long_output_dir=output_dir,
        short_output_dir=output_dir,
        candle_count=LIMIT,
        candle_closes=candle_closes,
    )
    write_csv(output_dir / "combined_exposure_timeline.csv", exposure_timeline)
    combined_summary["exposure_summary"] = exposure_summary

    report = _build_report(
        output_dir=output_dir,
        initial_validation=initial_validation,
        long_summary=long_summary,
        short_summary=short_summary,
        combined_summary=combined_summary,
        exposure_summary=exposure_summary,
    )
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")
    write_json(output_dir / "combined_summary.json", combined_summary)

    return {
        "output_dir": str(output_dir),
        "elapsed_seconds": time.time() - started,
        "long_summary": long_summary,
        "short_summary": short_summary,
        "combined_summary": combined_summary,
        "initial_validation": initial_validation,
    }


def _build_report(
    *,
    output_dir: Path,
    initial_validation: dict[str, Any],
    long_summary: dict[str, Any],
    short_summary: dict[str, Any],
    combined_summary: dict[str, Any],
    exposure_summary: dict[str, Any],
) -> str:
    lines = [
        "# Independent Continuous Long/Short — Shared Initial Start Only",
        "",
        f"Output: `{output_dir}`",
        "",
        "## Recovery configuration",
        "",
        f"- Long: `{LONG_RECOVERY_PURPOSE}` wait={RECOVERY_WAIT_CANDLES}",
        f"- Short: `{SHORT_RECOVERY_PURPOSE}` wait={RECOVERY_WAIT_CANDLES}",
        "",
        "## Shared initial start",
        "",
        json.dumps(initial_validation, indent=2),
        "",
        "## Long summary",
        "",
        json.dumps(long_summary, indent=2),
        "",
        "## Short summary",
        "",
        json.dumps(short_summary, indent=2),
        "",
        "## Combined summary",
        "",
        json.dumps(combined_summary, indent=2),
        "",
        "## Exposure summary",
        "",
        json.dumps(exposure_summary, indent=2),
        "",
        "## Difference from old paired test",
        "",
        json.dumps(combined_summary.get("comparison_to_old_paired_test", {}), indent=2),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Independent continuous long/short with shared initial start only"
    )
    parser.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--rerun-long", action="store_true", help="Re-run long instead of copying reference")
    parser.add_argument("--rerun-short", action="store_true", help="Re-run short even if results exist")
    args = parser.parse_args(argv)

    payload = run_independent_backtest(
        output_dir=args.output_dir.resolve(),
        rerun_long=bool(args.rerun_long),
        rerun_short=bool(args.rerun_short),
    )
    print(json.dumps({
        "output_dir": payload["output_dir"],
        "long_trades": payload["long_summary"]["trades_started"],
        "short_trades": payload["short_summary"]["trades_started"],
        "combined_mtm_pnl": payload["combined_summary"]["combined_mtm_pnl"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
