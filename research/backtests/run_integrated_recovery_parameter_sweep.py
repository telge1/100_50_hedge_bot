"""Backtest-only parameter sweep for integrated Recovery Bot in continuous backtests.

Runs (or reuses) Recovery OFF baseline plus all purpose × wait-candle combinations
over the full APTUSDT history, then produces JSON/CSV/Markdown rankings and reports.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import statistics
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .backtest_config_loader import DEFAULT_LONG_CONFIG_PATH, DEFAULT_SHORT_CONFIG_PATH
from .backtest_report import BacktestResult
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol_with_slice_info
from .continuous_reentry_backtest import (
    CONTINUOUS_SUCCESSFUL_EXIT_REASONS,
    run_continuous_reentry_backtests,
)
from .debug_report import calculate_unrealized_pnl
from .recovery_bot_config import RecoveryBotConfig, default_recovery_bot_config
from .trade_block_export import write_trade_block_exports

# --- sweep constants (fixed full-history setup) ---
SYMBOL = "APTUSDT"
DIRECTION = "long"
LIMIT = 52569
CONTINUOUS_START_INDEX = 0
CONTINUOUS_WINDOW_CANDLES = 52569
CONTINUOUS_MAX_TRADES = 1000
FILL_MODEL = "conservative"
CONFIG_SOURCE = "live"
EXPECTED_CANDLES_LOADED = 52569
EXPECTED_LAST_CANDLE_INDEX = 52568
CANDLE_MINUTES = 5
DEFAULT_FEE_RATE = 0.00055

RECOVERY_PURPOSES = (
    "CYCLE_3_SHORT_REDUCE",
    "CYCLE_4_LONG_ADD",
    "CYCLE_4_SHORT_REDUCE",
)
WAIT_CANDLES = (0, 48, 96, 144, 288, 576)

# Known complete runs outside the sweep tree (never overwritten).
REUSE_EXISTING_DIRS: dict[str, str] = {
    "recovery_off": "research/backtests/results/long_full_history_recovery_off",
    "CYCLE_4_LONG_ADD_wait_144": (
        "research/backtests/results/long_full_history_recovery_on_c4la_144"
    ),
}

RECOVERY_DIAGNOSTIC_SEQUENCE = (
    "RECOVERY_REFERENCE_REACHED",
    "RECOVERY_WAIT_STARTED",
    "RECOVERY_ACTIVATED",
    "RECOVERY_GAP_REDUCE_STEP_1",
    "RECOVERY_GAP_REDUCE_STEP_2",
    "RECOVERY_GAP_REDUCE_STEP_3",
    "RECOVERY_GAP_REDUCE_STEP_4",
    "RECOVERY_GAP_CLOSED",
    "RECOVERY_JOINT_EXIT",
)

# Ranking B weights (must sum to 1.0)
RANK_B_WEIGHTS = {
    "total_mtm_pnl": 0.35,
    "max_trade_duration_candles": 0.20,
    "avg_trade_duration_candles": 0.15,
    "recovery_closed_count": 0.15,
    "open_count": 0.10,
    "recovery_over_30d_count": 0.05,
}


@dataclass(frozen=True)
class VariantSpec:
    variant_id: str
    recovery_enabled: bool
    recovery_start_purpose: str | None
    recovery_wait_candles: int | None

    @property
    def label(self) -> str:
        if not self.recovery_enabled:
            return "Recovery OFF (baseline)"
        return f"{self.recovery_start_purpose} + wait {self.recovery_wait_candles}"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_variant_specs() -> list[VariantSpec]:
    specs = [
        VariantSpec(
            variant_id="recovery_off",
            recovery_enabled=False,
            recovery_start_purpose=None,
            recovery_wait_candles=None,
        )
    ]
    for purpose in RECOVERY_PURPOSES:
        for wait in WAIT_CANDLES:
            specs.append(
                VariantSpec(
                    variant_id=f"{purpose}_wait_{wait}",
                    recovery_enabled=True,
                    recovery_start_purpose=purpose,
                    recovery_wait_candles=wait,
                )
            )
    return specs


def variant_cli_command(spec: VariantSpec, output_dir: Path) -> str:
    base = [
        "PYTHONPATH=. python3 -m research.backtests.run_original_hedge_backtest",
        f"--symbol {SYMBOL}",
        f"--direction {DIRECTION}",
        f"--limit {LIMIT}",
        "--continuous-reentry",
        f"--continuous-start-index {CONTINUOUS_START_INDEX}",
        f"--continuous-window-candles {CONTINUOUS_WINDOW_CANDLES}",
        f"--continuous-max-trades {CONTINUOUS_MAX_TRADES}",
        f"--fill-model {FILL_MODEL}",
        f"--config-source {CONFIG_SOURCE}",
        "--pnl-coverage-audit",
        "--trade-block-export",
        f"--output-dir {output_dir}",
    ]
    if spec.recovery_enabled:
        base.extend(
            [
                "--recovery-bot",
                f"--recovery-start-purpose {spec.recovery_start_purpose}",
                f"--recovery-wait-candles {spec.recovery_wait_candles}",
            ]
        )
    return " \\\n  ".join(base)


def _results_json_path(output_dir: Path) -> Path:
    return output_dir / f"{SYMBOL}_original_hedge_5m_continuous_results.json"


def _variant_complete(output_dir: Path) -> bool:
    json_path = _results_json_path(output_dir)
    if not json_path.is_file():
        return False
    try:
        payload = _read_json(json_path)
    except (OSError, json.JSONDecodeError):
        return False
    meta = payload.get("metadata") or {}
    runs = payload.get("runs") or []
    if int(meta.get("candles_loaded") or 0) != EXPECTED_CANDLES_LOADED:
        return False
    if not runs:
        return False
    last = runs[-1]
    if int(last.get("end_index") or -1) != EXPECTED_LAST_CANDLE_INDEX:
        return False
    return True


def _import_existing_variant(spec: VariantSpec, target_dir: Path) -> bool:
    source_rel = REUSE_EXISTING_DIRS.get(spec.variant_id)
    if not source_rel:
        return False
    source = Path(source_rel)
    if not source.is_dir() or not _variant_complete(source):
        return False
    if target_dir.exists():
        if _variant_complete(target_dir):
            return True
        raise FileExistsError(f"incomplete variant dir exists: {target_dir}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    # Copy only result artifacts, not re-run.
    shutil.copytree(source, target_dir)
    manifest = {
        "variant_id": spec.variant_id,
        "imported_from": str(source.resolve()),
        "imported_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(target_dir / "variant_import_manifest.json", manifest)
    return True


def _build_recovery_config(spec: VariantSpec) -> RecoveryBotConfig | None:
    if not spec.recovery_enabled:
        return None
    cfg = default_recovery_bot_config()
    cfg.enabled = True
    cfg.recovery_start_purpose = str(spec.recovery_start_purpose)
    cfg.recovery_wait_candles = int(spec.recovery_wait_candles or 0)
    return cfg


def run_variant(
    spec: VariantSpec,
    *,
    candles: list[Any],
    slice_info: Any,
    output_dir: Path,
    write_trade_blocks: bool,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if _variant_complete(output_dir):
        return {
            "variant_id": spec.variant_id,
            "status": "skipped_existing_complete",
            "output_dir": str(output_dir),
        }

    recovery_cfg = _build_recovery_config(spec)
    started = time.time()
    try:
        payload = run_continuous_reentry_backtests(
            symbol=SYMBOL,
            direction=DIRECTION,
            candles=candles,
            continuous_start_index=CONTINUOUS_START_INDEX,
            continuous_window_candles=CONTINUOUS_WINDOW_CANDLES,
            continuous_max_trades=CONTINUOUS_MAX_TRADES,
            config_source=CONFIG_SOURCE,
            fill_model=FILL_MODEL,
            long_config_path=DEFAULT_LONG_CONFIG_PATH,
            short_config_path=DEFAULT_SHORT_CONFIG_PATH,
            recovery_bot_config=recovery_cfg,
            input_slice_start_index=slice_info.input_slice_start_index,
            candle_source_total_count=slice_info.candle_source_total_count,
            input_slice_first_timestamp=slice_info.input_slice_first_timestamp,
            input_slice_last_timestamp=slice_info.input_slice_last_timestamp,
            output_dir=output_dir,
            write_json=True,
            write_csv=True,
            include_logs=False,
        )
        if write_trade_blocks:
            for result in payload.get("results") or []:
                if isinstance(result, BacktestResult):
                    write_trade_block_exports(result, output_dir)
        elapsed = time.time() - started
        if not _variant_complete(output_dir):
            return {
                "variant_id": spec.variant_id,
                "status": "failed_incomplete",
                "output_dir": str(output_dir),
                "elapsed_seconds": elapsed,
                "error": "post-run validation failed (incomplete history)",
            }
        return {
            "variant_id": spec.variant_id,
            "status": "ok",
            "output_dir": str(output_dir),
            "elapsed_seconds": elapsed,
            "trades_started": len(payload.get("results") or []),
        }
    except Exception as exc:  # noqa: BLE001 - sweep marks failure explicitly
        return {
            "variant_id": spec.variant_id,
            "status": "failed_exception",
            "output_dir": str(output_dir),
            "elapsed_seconds": time.time() - started,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _is_closed_run(run: dict[str, Any]) -> bool:
    exit_reason = str(run.get("exit_reason") or "")
    if exit_reason in CONTINUOUS_SUCCESSFUL_EXIT_REASONS:
        return True
    quality = str(run.get("exit_quality") or "")
    return quality in {
        "closed_ok",
        "closed_undercovered_final_exit",
        "closed_negative_pnl",
    }


def _estimate_closing_fees(run: dict[str, Any], *, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    long_qty = _safe_float(run.get("final_long_qty")) or 0.0
    short_qty = _safe_float(run.get("final_short_qty")) or 0.0
    if long_qty <= 0 and short_qty <= 0:
        return 0.0
    long_avg = _safe_float(run.get("final_long_avg_price")) or 0.0
    short_avg = _safe_float(run.get("final_short_avg_price")) or 0.0
    mark = long_avg or short_avg
    if mark <= 0:
        mark = _safe_float(run.get("entry_price")) or 0.0
    if mark <= 0:
        return 0.0
    return fee_rate * (long_qty * mark + short_qty * mark)


def _open_trade_mtm(run: dict[str, Any]) -> dict[str, float]:
    realized = _safe_float(run.get("realized_pnl")) or 0.0
    unreal_long = _safe_float(run.get("unrealized_long_pnl"))
    unreal_short = _safe_float(run.get("unrealized_short_pnl"))
    unreal_total = _safe_float(run.get("unrealized_pnl"))
    if unreal_total is None and (unreal_long is not None or unreal_short is not None):
        unreal_total = (unreal_long or 0.0) + (unreal_short or 0.0)
    if unreal_long is None or unreal_short is None:
        long_qty = _safe_float(run.get("final_long_qty")) or 0.0
        short_qty = _safe_float(run.get("final_short_qty")) or 0.0
        long_avg = _safe_float(run.get("final_long_avg_price")) or 0.0
        short_avg = _safe_float(run.get("final_short_avg_price")) or 0.0
        mark = long_avg or short_avg or (_safe_float(run.get("entry_price")) or 0.0)
        calc_long, calc_short, calc_total = calculate_unrealized_pnl(
            long_qty=long_qty,
            short_qty=short_qty,
            long_avg_price=long_avg,
            short_avg_price=short_avg,
            final_price=mark,
        )
        if unreal_long is None:
            unreal_long = calc_long or 0.0
        if unreal_short is None:
            unreal_short = calc_short or 0.0
        if unreal_total is None:
            unreal_total = calc_total or 0.0
    closing_fees = _estimate_closing_fees(run)
    mtm = realized + (unreal_total or 0.0) - closing_fees
    return {
        "realized_pnl": realized,
        "unrealized_long_pnl": float(unreal_long or 0.0),
        "unrealized_short_pnl": float(unreal_short or 0.0),
        "estimated_closing_fees": closing_fees,
        "mark_to_market_pnl": mtm,
    }


def _trade_overlap_count(runs: list[dict[str, Any]]) -> int:
    overlaps = 0
    for left, right in zip(runs, runs[1:]):
        left_end = int(left.get("end_index") or -1)
        right_start = int(right.get("start_index") or -1)
        if left_end >= right_start:
            overlaps += 1
    return overlaps


def _recovery_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run for run in runs if bool(run.get("recovery_activated"))]


def _recovery_closed_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        run
        for run in runs
        if str(run.get("exit_reason") or "") == "recovery_joint_exit"
    ]


def analyze_variant_payload(
    spec: VariantSpec,
    payload: dict[str, Any],
    *,
    run_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runs = list(payload.get("runs") or [])
    meta = payload.get("metadata") or {}
    durations = [int(run.get("candles_processed") or 0) for run in runs]
    closed_runs = [run for run in runs if _is_closed_run(run)]
    open_runs = [run for run in runs if str(run.get("final_status") or "") == "open"]
    normal_closed = [
        run for run in closed_runs if str(run.get("exit_reason") or "") == "flat_no_active_orders"
    ]
    recovery_activated = _recovery_runs(runs)
    recovery_closed = _recovery_closed_runs(runs)
    recovery_failed = [
        run for run in recovery_activated if run not in recovery_closed
    ]

    recovery_durations = [
        int(run.get("recovery_duration_candles") or 0)
        for run in recovery_closed
        if run.get("recovery_duration_candles") is not None
    ]
    recovery_pnls = [_safe_float(run.get("realized_pnl")) or 0.0 for run in recovery_closed]
    total_realized_pnl = sum(_safe_float(run.get("realized_pnl")) or 0.0 for run in runs)

    open_mtm_details: list[dict[str, Any]] = []
    open_mtm_total = 0.0
    for run in open_runs:
        mtm = _open_trade_mtm(run)
        open_mtm_total += mtm["mark_to_market_pnl"]
        open_mtm_details.append(
            {
                "trade_number": run.get("trade_number"),
                "trade_block_id": run.get("trade_block_id"),
                **mtm,
            }
        )

    closed_realized = sum(_safe_float(run.get("realized_pnl")) or 0.0 for run in closed_runs)
    total_mtm_pnl = closed_realized + open_mtm_total

    start_times = [_parse_ts(run.get("start_time")) for run in runs]
    end_times = [_parse_ts(run.get("end_time")) for run in runs]
    start_times = [ts for ts in start_times if ts is not None]
    end_times = [ts for ts in end_times if ts is not None]
    calendar_days = 0.0
    if start_times and end_times:
        calendar_days = max(
            (max(end_times) - min(start_times)).total_seconds() / 86400.0,
            1.0 / 86400.0,
        )

    last_end_index = int(runs[-1].get("end_index") or -1) if runs else -1
    history_complete = (
        int(meta.get("candles_loaded") or 0) == EXPECTED_CANDLES_LOADED
        and last_end_index == EXPECTED_LAST_CANDLE_INDEX
    )
    overlap_count = _trade_overlap_count(runs)

    candles_1d = 288
    candles_7d = 2016
    candles_30d = 8640
    recovery_over_1d = sum(1 for d in recovery_durations if d > candles_1d)
    recovery_over_7d = sum(1 for d in recovery_durations if d > candles_7d)
    recovery_over_30d = sum(1 for d in recovery_durations if d > candles_30d)

    open_candles_at_end = sum(int(run.get("candles_processed") or 0) for run in open_runs)

    metrics = {
        "variant_id": spec.variant_id,
        "label": spec.label,
        "recovery_enabled": spec.recovery_enabled,
        "recovery_start_purpose": spec.recovery_start_purpose,
        "recovery_wait_candles": spec.recovery_wait_candles,
        "run_status": (run_manifest or {}).get("status", "analyzed_from_disk"),
        "output_dir": (run_manifest or {}).get("output_dir"),
        "trades_started": len(runs),
        "normal_closed_count": len(normal_closed),
        "closed_count": len(closed_runs),
        "recovery_activated_count": len(recovery_activated),
        "recovery_closed_count": len(recovery_closed),
        "recovery_failed_count": len(recovery_failed),
        "open_at_series_end_count": len(open_runs),
        "total_realized_pnl": total_realized_pnl,
        "total_mark_to_market_pnl": total_mtm_pnl,
        "avg_pnl_per_started_trade": total_mtm_pnl / len(runs) if runs else 0.0,
        "avg_pnl_per_closed_trade": (
            sum(_safe_float(run.get("realized_pnl")) or 0.0 for run in closed_runs) / len(closed_runs)
            if closed_runs
            else 0.0
        ),
        "avg_trade_duration_candles": statistics.mean(durations) if durations else 0.0,
        "max_trade_duration_candles": max(durations) if durations else 0,
        "avg_recovery_duration_candles": (
            statistics.mean(recovery_durations) if recovery_durations else 0.0
        ),
        "max_recovery_duration_candles": max(recovery_durations) if recovery_durations else 0,
        "best_recovery_trade_pnl": max(recovery_pnls) if recovery_pnls else None,
        "worst_recovery_trade_pnl": min(recovery_pnls) if recovery_pnls else None,
        "total_recovery_trade_pnl": sum(recovery_pnls),
        "recovery_over_1d_count": recovery_over_1d,
        "recovery_over_7d_count": recovery_over_7d,
        "recovery_over_30d_count": recovery_over_30d,
        "last_end_index": last_end_index,
        "history_complete": history_complete,
        "overlap_count": overlap_count,
        "open_candles_at_series_end": open_candles_at_end,
        "calendar_days": calendar_days,
        "pnl_per_1000_candles": total_mtm_pnl / (EXPECTED_CANDLES_LOADED / 1000.0),
        "pnl_per_calendar_day": total_mtm_pnl / calendar_days if calendar_days else None,
        "avg_capital_binding_minutes": (
            statistics.mean(durations) * CANDLE_MINUTES if durations else 0.0
        ),
        "open_trades_mtm": open_mtm_details,
    }
    return metrics


def enrich_vs_baseline(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    out = dict(metrics)
    out["vs_baseline_pnl_diff"] = metrics["total_mark_to_market_pnl"] - baseline["total_mark_to_market_pnl"]
    out["vs_baseline_realized_pnl_diff"] = metrics["total_realized_pnl"] - baseline["total_realized_pnl"]
    out["vs_baseline_additional_closed_trades"] = metrics["closed_count"] - baseline["closed_count"]
    out["vs_baseline_avoided_open_candles"] = (
        baseline.get("open_candles_at_series_end", 0) - metrics.get("open_candles_at_series_end", 0)
    )
    neg_total = sum(
        min(0.0, _safe_float(run_pnl) or 0.0)
        for run_pnl in [metrics.get("total_recovery_trade_pnl")]
    )
    neg_recovery = min(0.0, float(metrics.get("total_recovery_trade_pnl") or 0.0))
    total_loss = min(0.0, metrics["total_mark_to_market_pnl"])
    out["recovery_share_of_total_loss"] = (
        abs(neg_recovery) / abs(total_loss) if total_loss < 0 else 0.0
    )
    return out


def _normalize_series(values: list[float], *, higher_is_better: bool) -> list[float]:
    if not values:
        return []
    vmin = min(values)
    vmax = max(values)
    if math.isclose(vmin, vmax):
        return [1.0 for _ in values]
    normed = []
    for value in values:
        ratio = (value - vmin) / (vmax - vmin)
        normed.append(ratio if higher_is_better else 1.0 - ratio)
    return normed


def compute_ranking_b_score(metrics: dict[str, Any], cohort: list[dict[str, Any]]) -> float:
    def series(key: str) -> list[float]:
        return [float(row.get(key) or 0.0) for row in cohort]

    n_pnl = _normalize_series(series("total_mark_to_market_pnl"), higher_is_better=True)
    n_max_dur = _normalize_series(series("max_trade_duration_candles"), higher_is_better=False)
    n_avg_dur = _normalize_series(series("avg_trade_duration_candles"), higher_is_better=False)
    n_rec_closed = _normalize_series(series("recovery_closed_count"), higher_is_better=True)
    n_open = _normalize_series(series("open_at_series_end_count"), higher_is_better=False)
    n_rec30 = _normalize_series(series("recovery_over_30d_count"), higher_is_better=False)

    index = next(i for i, row in enumerate(cohort) if row["variant_id"] == metrics["variant_id"])
    score = (
        RANK_B_WEIGHTS["total_mtm_pnl"] * n_pnl[index]
        + RANK_B_WEIGHTS["max_trade_duration_candles"] * n_max_dur[index]
        + RANK_B_WEIGHTS["avg_trade_duration_candles"] * n_avg_dur[index]
        + RANK_B_WEIGHTS["recovery_closed_count"] * n_rec_closed[index]
        + RANK_B_WEIGHTS["open_count"] * n_open[index]
        + RANK_B_WEIGHTS["recovery_over_30d_count"] * n_rec30[index]
    )
    return score


def rank_variants(all_metrics: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    complete = [row for row in all_metrics if row.get("history_complete") and row.get("overlap_count", 0) == 0]

    ranking_a = sorted(
        complete,
        key=lambda row: (
            -int(bool(row.get("history_complete"))),
            -float(row.get("total_mark_to_market_pnl") or 0.0),
            float(row.get("max_trade_duration_candles") or 0.0),
            int(row.get("open_at_series_end_count") or 0),
        ),
    )

    for row in complete:
        row["ranking_b_score"] = compute_ranking_b_score(row, complete)
    ranking_b = sorted(complete, key=lambda row: -float(row.get("ranking_b_score") or 0.0))
    return ranking_a, ranking_b


def _diagnostic_purposes_from_trade_block(path: Path) -> list[str]:
    purposes: list[str] = []
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if str(row.get("row_type") or "") != "diagnostic":
                    continue
                purpose = str(row.get("purpose") or "")
                if purpose:
                    purposes.append(purpose)
        return purposes

    payload = _read_json(path)
    rows = payload.get("trade_blocks") or payload.get("rows") or []
    for row in rows:
        if str(row.get("row_type") or "") != "diagnostic":
            continue
        purpose = str(row.get("purpose") or "")
        if purpose:
            purposes.append(purpose)
    return purposes


def plausibility_check_trade(
    output_dir: Path,
    *,
    trade_number: int,
    variant_label: str,
    results_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patterns = [
        f"*{trade_number:04d}*trade_blocks.json",
        f"*{trade_number:04d}*trade_blocks.jsonl",
        f"*{trade_number:04d}*.jsonl",
    ]
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(sorted(output_dir.glob(pattern)))
    matches = sorted(set(matches))
    if not matches:
        if results_payload is not None:
            run = next(
                (row for row in results_payload.get("runs") or [] if int(row.get("trade_number") or -1) == trade_number),
                None,
            )
            if run is not None:
                purposes = [
                    str(event.get("purpose") or "")
                    for event in run.get("recovery_diagnostic_events") or []
                    if str(event.get("purpose") or "")
                ]
                missing = [step for step in RECOVERY_DIAGNOSTIC_SEQUENCE if step not in purposes]
                return {
                    "variant_label": variant_label,
                    "trade_number": trade_number,
                    "source": "results_json_recovery_diagnostic_events",
                    "diagnostic_purposes_seen": purposes,
                    "expected_sequence": list(RECOVERY_DIAGNOSTIC_SEQUENCE),
                    "missing_steps": missing,
                    "sequence_ok": not missing,
                    "trade_exit_reason": run.get("exit_reason"),
                    "trade_realized_pnl": run.get("realized_pnl"),
                }
        return {
            "variant_label": variant_label,
            "trade_number": trade_number,
            "status": "missing_trade_block",
        }
    purposes = _diagnostic_purposes_from_trade_block(matches[0])
    missing = [step for step in RECOVERY_DIAGNOSTIC_SEQUENCE if step not in purposes]
    return {
        "variant_label": variant_label,
        "trade_number": trade_number,
        "trade_block_file": str(matches[0]),
        "diagnostic_purposes_seen": purposes,
        "expected_sequence": list(RECOVERY_DIAGNOSTIC_SEQUENCE),
        "missing_steps": missing,
        "sequence_ok": not missing,
    }


def pick_recovery_trade_for_plausibility(
    payload: dict[str, Any],
    *,
    prefer_wait: int | None = None,
) -> int | None:
    runs = payload.get("runs") or []
    candidates = [
        run
        for run in runs
        if str(run.get("exit_reason") or "") == "recovery_joint_exit"
        and bool(run.get("recovery_gap_fully_closed"))
    ]
    if prefer_wait is not None:
        filtered = [
            run
            for run in candidates
            if int(run.get("recovery_wait_candles") or -1) == prefer_wait
        ]
        if filtered:
            candidates = filtered
    if not candidates:
        return None
    # Prefer median-duration successful recovery for stable audit.
    candidates.sort(key=lambda run: int(run.get("recovery_duration_candles") or 0))
    return int(candidates[len(candidates) // 2].get("trade_number") or 0)


def write_ranking_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def build_markdown_report(
    *,
    sweep_dir: Path,
    all_metrics: list[dict[str, Any]],
    ranking_a: list[dict[str, Any]],
    ranking_b: list[dict[str, Any]],
    run_log: list[dict[str, Any]],
    plausibility: list[dict[str, Any]],
    cli_commands: dict[str, str],
) -> str:
    lines: list[str] = []
    lines.append("# Integrated Recovery Parameter Sweep Report")
    lines.append("")
    lines.append(f"- Sweep directory: `{sweep_dir}`")
    lines.append(f"- Generated at (UTC): `{datetime.now(timezone.utc).isoformat()}`")
    lines.append(f"- Symbol: `{SYMBOL}` | Direction: `{DIRECTION}` | Candles: `{EXPECTED_CANDLES_LOADED}`")
    lines.append("")
    lines.append("## Ranking A – highest mark-to-market total PnL")
    lines.append("")
    lines.append("Sort: history complete → MTM PnL ↓ → max trade duration ↑ → open trades ↑")
    lines.append("")
    for index, row in enumerate(ranking_a[:10], start=1):
        lines.append(
            f"{index}. **{row['label']}** — MTM `{row['total_mark_to_market_pnl']:.6f}`, "
            f"realized `{row['total_realized_pnl']:.6f}`, trades `{row['trades_started']}`, "
            f"recovery closed `{row['recovery_closed_count']}`, open `{row['open_at_series_end_count']}`"
        )
    lines.append("")
    lines.append("## Ranking B – balanced score")
    lines.append("")
    lines.append("Transparent score (weights sum to 1.0):")
    for key, weight in RANK_B_WEIGHTS.items():
        direction = "higher better" if key in {"total_mtm_pnl", "recovery_closed_count"} else "lower better"
        lines.append(f"- `{key}`: weight `{weight:.2f}` ({direction})")
    lines.append("")
    for index, row in enumerate(ranking_b[:10], start=1):
        lines.append(
            f"{index}. **{row['label']}** — score `{row.get('ranking_b_score', 0):.4f}`, "
            f"MTM `{row['total_mark_to_market_pnl']:.6f}`, max dur `{row['max_trade_duration_candles']}`"
        )
    lines.append("")
    lines.append("## Failed or incomplete runs")
    lines.append("")
    failed = [entry for entry in run_log if not str(entry.get("status", "")).startswith(("ok", "skipped", "imported"))]
    if not failed:
        lines.append("- None")
    else:
        for entry in failed:
            lines.append(f"- `{entry.get('variant_id')}`: {entry.get('status')} — {entry.get('error', '')}")
    lines.append("")
    lines.append("## Plausibility checks (diagnostic event sequence)")
    lines.append("")
    for check in plausibility:
        status = "OK" if check.get("sequence_ok") else "FAIL"
        lines.append(
            f"- [{status}] {check.get('variant_label')} trade #{check.get('trade_number')}: "
            f"missing={check.get('missing_steps', check.get('status'))}"
        )
    lines.append("")
    lines.append("## Open trades at series end (MTM view)")
    lines.append("")
    for row in all_metrics:
        if not row.get("open_trades_mtm"):
            continue
        lines.append(f"### {row['label']}")
        for open_row in row["open_trades_mtm"]:
            lines.append(
                f"- trade #{open_row.get('trade_number')}: realized `{open_row['realized_pnl']:.6f}`, "
                f"unreal L `{open_row['unrealized_long_pnl']:.6f}`, unreal S `{open_row['unrealized_short_pnl']:.6f}`, "
                f"closing fees `{open_row['estimated_closing_fees']:.6f}`, MTM `{open_row['mark_to_market_pnl']:.6f}`"
            )
        lines.append("")
    return "\n".join(lines)


def analyze_sweep(sweep_dir: Path) -> dict[str, Any]:
    specs = {spec.variant_id: spec for spec in build_variant_specs()}
    run_log_path = sweep_dir / "run_log.json"
    run_log = _read_json(run_log_path) if run_log_path.is_file() else []

    all_metrics: list[dict[str, Any]] = []
    for spec in build_variant_specs():
        variant_dir = sweep_dir / "variants" / spec.variant_id
        json_path = _results_json_path(variant_dir)
        manifest = next((entry for entry in run_log if entry.get("variant_id") == spec.variant_id), None)
        if not json_path.is_file():
            all_metrics.append(
                {
                    "variant_id": spec.variant_id,
                    "label": spec.label,
                    "run_status": "missing_results",
                    "history_complete": False,
                }
            )
            continue
        payload = _read_json(json_path)
        metrics = analyze_variant_payload(spec, payload, run_manifest=manifest)
        all_metrics.append(metrics)

    baseline = next((row for row in all_metrics if row.get("variant_id") == "recovery_off"), None)
    if baseline:
        all_metrics = [
            enrich_vs_baseline(row, baseline) if row.get("variant_id") != "recovery_off" else row
            for row in all_metrics
        ]

    ranking_a, ranking_b = rank_variants(all_metrics)

    # Plausibility: C4LA+144 plus two more variants with successful recovery.
    plausibility: list[dict[str, Any]] = []
    plausibility_targets = [
        ("CYCLE_4_LONG_ADD_wait_144", 144),
        ("CYCLE_4_LONG_ADD_wait_48", None),
        ("CYCLE_3_SHORT_REDUCE_wait_144", None),
    ]
    for variant_id, prefer_wait in plausibility_targets:
        variant_dir = sweep_dir / "variants" / variant_id
        json_path = _results_json_path(variant_dir)
        if not json_path.is_file():
            continue
        payload = _read_json(json_path)
        trade_number = pick_recovery_trade_for_plausibility(payload, prefer_wait=prefer_wait)
        if trade_number is None:
            continue
        plausibility.append(
            plausibility_check_trade(
                variant_dir,
                trade_number=trade_number,
                variant_label=specs[variant_id].label,
                results_payload=payload,
            )
        )

    cli_commands = {
        spec.variant_id: variant_cli_command(spec, sweep_dir / "variants" / spec.variant_id)
        for spec in build_variant_specs()
    }

    overview = {
        "sweep_dir": str(sweep_dir),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "constants": {
            "symbol": SYMBOL,
            "direction": DIRECTION,
            "limit": LIMIT,
            "expected_candles_loaded": EXPECTED_CANDLES_LOADED,
            "expected_last_candle_index": EXPECTED_LAST_CANDLE_INDEX,
        },
        "ranking_b_weights": RANK_B_WEIGHTS,
        "variants": all_metrics,
        "ranking_a": [row["variant_id"] for row in ranking_a],
        "ranking_b": [row["variant_id"] for row in ranking_b],
        "run_log": run_log,
        "plausibility_checks": plausibility,
        "cli_commands": cli_commands,
    }
    _write_json(sweep_dir / "sweep_overview.json", overview)
    write_ranking_csv(sweep_dir / "ranking_a_mtm.csv", ranking_a)
    write_ranking_csv(sweep_dir / "ranking_b_balanced.csv", ranking_b)
    failed_runs = [
        entry for entry in run_log if not str(entry.get("status", "")).startswith(("ok", "skipped", "imported"))
    ]
    _write_json(sweep_dir / "failed_runs.json", failed_runs)
    commands_path = sweep_dir / "cli_commands.sh"
    with commands_path.open("w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\nset -euo pipefail\n\n")
        for variant_id, command in cli_commands.items():
            handle.write(f"# {variant_id}\n{command}\n\n")
    report_md = build_markdown_report(
        sweep_dir=sweep_dir,
        all_metrics=all_metrics,
        ranking_a=ranking_a,
        ranking_b=ranking_b,
        run_log=run_log,
        plausibility=plausibility,
        cli_commands=cli_commands,
    )
    (sweep_dir / "REPORT.md").write_text(report_md, encoding="utf-8")
    return overview


def run_sweep(
    *,
    sweep_dir: Path,
    skip_run: bool = False,
    write_trade_blocks: bool = False,
) -> dict[str, Any]:
    sweep_dir.mkdir(parents=True, exist_ok=True)
    specs = build_variant_specs()
    run_log: list[dict[str, Any]] = []

    candles: list[Any] | None = None
    slice_info = None
    if not skip_run:
        candles, slice_info = load_candles_for_symbol_with_slice_info(
            SYMBOL,
            timeframe="5m",
            data_dir=DEFAULT_DATA_DIR,
            limit=LIMIT,
        )
        if len(candles) != EXPECTED_CANDLES_LOADED:
            raise RuntimeError(
                f"expected {EXPECTED_CANDLES_LOADED} candles, loaded {len(candles)}"
            )

    for spec in specs:
        variant_dir = sweep_dir / "variants" / spec.variant_id
        entry: dict[str, Any] = {
            "variant_id": spec.variant_id,
            "cli_command": variant_cli_command(spec, variant_dir),
        }
        if skip_run:
            entry["status"] = "analyze_only"
            run_log.append(entry)
            continue

        if not variant_dir.exists() and spec.variant_id in REUSE_EXISTING_DIRS:
            try:
                if _import_existing_variant(spec, variant_dir):
                    entry["status"] = "imported_existing"
                    entry["output_dir"] = str(variant_dir)
                    run_log.append(entry)
                    continue
            except FileExistsError as exc:
                entry["status"] = "failed_import"
                entry["error"] = str(exc)
                run_log.append(entry)
                continue

        if _variant_complete(variant_dir):
            entry["status"] = "skipped_existing_complete"
            entry["output_dir"] = str(variant_dir)
            run_log.append(entry)
            continue

        if variant_dir.exists():
            entry["status"] = "failed_incomplete_existing_dir"
            entry["error"] = "refusing to overwrite incomplete variant directory"
            entry["output_dir"] = str(variant_dir)
            run_log.append(entry)
            continue

        result = run_variant(
            spec,
            candles=candles or [],
            slice_info=slice_info,
            output_dir=variant_dir,
            write_trade_blocks=write_trade_blocks,
        )
        entry.update(result)
        run_log.append(entry)
        _write_json(sweep_dir / "run_log.json", run_log)

    _write_json(sweep_dir / "run_log.json", run_log)
    return analyze_sweep(sweep_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Integrated recovery bot parameter sweep")
    parser.add_argument(
        "--sweep-dir",
        type=Path,
        default=None,
        help="Output directory (default: research/backtests/results/integrated_recovery_parameter_sweep_<timestamp>)",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Only analyze an existing sweep directory (requires --sweep-dir)",
    )
    parser.add_argument(
        "--write-trade-blocks",
        action="store_true",
        help="Export trade blocks per variant (slower, needed for plausibility audits)",
    )
    args = parser.parse_args(argv)

    if args.sweep_dir is None:
        args.sweep_dir = Path(
            f"research/backtests/results/integrated_recovery_parameter_sweep_{_utc_stamp()}"
        )

    sweep_dir = args.sweep_dir.resolve()
    if args.analyze_only:
        if not sweep_dir.is_dir():
            print(f"error: sweep dir not found: {sweep_dir}", file=sys.stderr)
            return 1
        overview = analyze_sweep(sweep_dir)
    else:
        if sweep_dir.exists() and any(sweep_dir.iterdir()):
            print(f"error: sweep dir already exists and is non-empty: {sweep_dir}", file=sys.stderr)
            return 1
        overview = run_sweep(
            sweep_dir=sweep_dir,
            write_trade_blocks=args.write_trade_blocks,
        )

    print(json.dumps(
        {
            "sweep_dir": str(sweep_dir),
            "ranking_a_top3": overview.get("ranking_a", [])[:3],
            "ranking_b_top3": overview.get("ranking_b", [])[:3],
            "failed_runs": len([
                entry for entry in overview.get("run_log", [])
                if not str(entry.get("status", "")).startswith(("ok", "skipped", "imported"))
            ]),
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
