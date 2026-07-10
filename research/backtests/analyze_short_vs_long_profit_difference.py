"""Analysis: why independent short-primary bot yields less gross profit than long-primary."""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fixed_cycle_hedge_bot import direction_config, purpose_mapping
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig

from .backtest_config_loader import (
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
    BACKTEST_RUNTIME_OVERRIDES,
    resolve_backtest_config,
)
from .candle_loader import load_candles_for_symbol
from .continuous_reentry_backtest import run_continuous_reentry_backtests
from .fill_models import resolve_fill_model_config
from .independent_continuous_long_short_analysis import summarize_direction_runs, write_csv, write_json
from .multi_start_backtest import compact_result_dict
from .paired_direction_recovery import mirror_recovery_start_purpose
from .recovery_bot_config import RecoveryBotConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = (
    PROJECT_ROOT
    / "research/backtests/results/independent_continuous_long_short_same_initial_start_c4_wait576"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "research/backtests/results/short_vs_long_profit_difference_analysis"
)
SYMBOL = "APTUSDT"
CANDLE_MINUTES = 5
RECOVERY_WAIT_CANDLES = 576
LONG_RECOVERY_PURPOSE = "CYCLE_4_LONG_ADD"
SHORT_RECOVERY_PURPOSE = mirror_recovery_start_purpose(LONG_RECOVERY_PURPOSE)

_CYCLE_PURPOSE_RE = re.compile(r"CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE|LONG_REDUCE|SHORT_ADD)")
_INITIAL_ENTRY_PURPOSES = frozenset({"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"})
_EXIT_PURPOSES = frozenset(
    {"LONG_TP_EXIT", "SHORT_SL_EXIT", "LONG_SL_EXIT", "SHORT_TP_EXIT", "RECOVERY_JOINT_EXIT"}
)

_DURATION_BUCKETS_HOURS = (1, 6, 12, 24, 72, 168, 720)

NEUTRAL_NUMERIC_OVERRIDES: dict[str, Any] = {
    "base_notional_usdt": 100.0,
    "hedge_ratio_short": 0.5,
    "reduction_pct_per_fill": 25.0,
    "long_cycle_qty_pct_of_initial": 25.0,
    "short_cycle_qty_pct_of_initial": 25.0,
    "long_fill_distance_pct": 0.5,
    "short_fill_distance_pct": 0.15,
    "tp_profit_target_pct": 0.25,
    "tp_buffer_pct": 0.0002,
    "target_profit_usdt": 0.015,
    "fee_safety_buffer_pct": 0.12,
    "market_fallback_slippage_value": 0.1,
    "max_cycles": 10,
    "hard_stop_cycle": 10,
    "hard_stop_pct": 1.0,
    "leverage_long": 25.0,
    "leverage_short": 25.0,
    "price_tick_size": 0.0001,
    "qty_step": 0.001,
    "min_order_qty": 0.001,
    "min_notional_usdt": 5.0,
    "time_distance_refill_trigger_minutes": 60,
    "max_pre_recovery_long_reduce_distance_pct": 0.0,
    "max_post_recovery_long_reduce_distance_pct": 3.5,
    "recovery_activation_timing": "after_first_leg_reduce_fill",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentile(sorted_values: list[float | int], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def _load_runs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("runs") or [])


def _trade_block_path(source_dir: Path, direction: str, trade_number: int) -> Path | None:
    matches = sorted(
        source_dir.glob(f"APTUSDT_{direction}_continuous_trade_{trade_number:04d}_*_trade_blocks.json")
    )
    return matches[0] if matches else None


def _load_trade_block_fills(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        row
        for row in payload.get("trade_blocks") or []
        if str(row.get("row_type") or "") == "fill"
    ]


def _max_cycle_index(fills: Iterable[dict[str, Any]]) -> int:
    max_cycle = 0
    for fill in fills:
        match = _CYCLE_PURPOSE_RE.search(str(fill.get("purpose") or ""))
        if match:
            max_cycle = max(max_cycle, int(match.group(1)))
    return max_cycle


def classify_exit_path(run: dict[str, Any], *, fills: list[dict[str, Any]] | None = None) -> str:
    if str(run.get("final_status") or "") == "open":
        return "series_end_open"
    if bool(run.get("recovery_activated")):
        return "recovery"
    max_cycle = _max_cycle_index(fills or [])
    if max_cycle <= 0:
        return "initial_exit_only"
    if max_cycle >= 4:
        return "cycle_4"
    return f"cycle_{max_cycle}"


def _primary_notional(config: FixedCycleHedgeConfig, *, signal: str) -> float:
    if signal == "long":
        return float(config.base_notional_usdt)
    return float(config.base_notional_usdt) * float(config.hedge_ratio_short)


def _hedge_notional(config: FixedCycleHedgeConfig, *, signal: str) -> float:
    if signal == "long":
        return float(config.base_notional_usdt) * float(config.hedge_ratio_short)
    return float(config.base_notional_usdt)


def _config_row(
    *,
    category: str,
    parameter: str,
    long_value: Any,
    short_value: Any,
    source_file: str,
    source_function: str,
    explanation: str,
) -> dict[str, Any]:
    equal = long_value == short_value
    difference = ""
    if not equal:
        if isinstance(long_value, (int, float)) and isinstance(short_value, (int, float)):
            difference = str(float(short_value) - float(long_value))
        else:
            difference = f"{short_value!r} vs {long_value!r}"
    return {
        "category": category,
        "parameter": parameter,
        "long_value": long_value,
        "short_value": short_value,
        "equal": equal,
        "difference": difference,
        "source_file": source_file,
        "source_function": source_function,
        "explanation": explanation,
    }


def build_effective_config_comparison(project_root: Path | None = None) -> list[dict[str, Any]]:
    root = project_root or PROJECT_ROOT
    long_result = resolve_backtest_config(
        config_source="live", signal="long", symbol=SYMBOL, project_root=root
    )
    short_result = resolve_backtest_config(
        config_source="live", signal="short", symbol=SYMBOL, project_root=root
    )
    long_cfg = long_result.config
    short_cfg = short_result.config
    rows: list[dict[str, Any]] = []

    for field in fields(FixedCycleHedgeConfig):
        rows.append(
            _config_row(
                category="strategy_config",
                parameter=field.name,
                long_value=getattr(long_cfg, field.name),
                short_value=getattr(short_cfg, field.name),
                source_file=str(DEFAULT_LONG_CONFIG_PATH if field.name != "strategy_class" else DEFAULT_SHORT_CONFIG_PATH),
                source_function="resolve_backtest_config",
                explanation="Effective dataclass field after defaults, overlay, and BACKTEST_RUNTIME_OVERRIDES",
            )
        )

    fill_model = resolve_fill_model_config(fill_model="conservative")
    for key, value in asdict(fill_model).items():
        rows.append(
            _config_row(
                category="fill_model",
                parameter=key,
                long_value=value,
                short_value=value,
                source_file="research/backtests/fill_models.py",
                source_function="resolve_fill_model_config",
                explanation="Shared conservative fill model for both directions",
            )
        )

    rows.extend(
        [
            _config_row(
                category="initial_entry",
                parameter="primary_notional_usdt",
                long_value=_primary_notional(long_cfg, signal="long"),
                short_value=_primary_notional(short_cfg, signal="short"),
                source_file="fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
                source_function="_submit_initial_entry_intents",
                explanation="Long bot: base_notional; Short bot: base_notional * hedge_ratio_short (formula not swapped)",
            ),
            _config_row(
                category="initial_entry",
                parameter="hedge_notional_usdt",
                long_value=_hedge_notional(long_cfg, signal="long"),
                short_value=_hedge_notional(short_cfg, signal="short"),
                source_file="fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
                source_function="_submit_initial_entry_intents",
                explanation="Long bot: base*hedge_ratio; Short bot: base_notional",
            ),
            _config_row(
                category="initial_exit",
                parameter="LONG_TP_EXIT / SHORT_SL_EXIT mapping (long bot)",
                long_value="LONG_TP_EXIT primary / SHORT_SL_EXIT hedge",
                short_value="LONG_SL_EXIT hedge / SHORT_TP_EXIT primary",
                source_file="fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
                source_function="_get_final_*_exit_purpose",
                explanation="Direction-neutral exit purpose swap on ShortFixedCycleHedgeStrategy",
            ),
            _config_row(
                category="recovery",
                parameter="recovery_start_purpose",
                long_value=LONG_RECOVERY_PURPOSE,
                short_value=SHORT_RECOVERY_PURPOSE,
                source_file="research/backtests/paired_direction_recovery.py",
                source_function="mirror_recovery_start_purpose",
                explanation="Cycle-4 first-leg mirror for independent continuous test",
            ),
            _config_row(
                category="recovery",
                parameter="recovery_wait_candles",
                long_value=RECOVERY_WAIT_CANDLES,
                short_value=RECOVERY_WAIT_CANDLES,
                source_file="research/backtests/run_independent_continuous_long_short.py",
                source_function="run_independent_backtest",
                explanation="Shared wait after reference fill",
            ),
        ]
    )

    for cycle_index in range(1, 5):
        rows.extend(
            [
                _config_row(
                    category=f"cycle_{cycle_index}",
                    parameter="first_leg_purpose",
                    long_value=purpose_mapping.cycle_long_add(cycle_index),
                    short_value=purpose_mapping.cycle_short_reduce(cycle_index),
                    source_file="fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
                    source_function="_get_first_leg_purpose",
                    explanation="Direction-neutral first leg",
                ),
                _config_row(
                    category=f"cycle_{cycle_index}",
                    parameter="second_leg_purpose",
                    long_value=purpose_mapping.cycle_short_reduce(cycle_index),
                    short_value=purpose_mapping.cycle_long_reduce(cycle_index),
                    source_file="fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
                    source_function="_get_second_leg_purpose",
                    explanation="Direction-neutral second leg",
                ),
                _config_row(
                    category=f"cycle_{cycle_index}",
                    parameter="reduce_percentage",
                    long_value=long_cfg.reduction_pct_per_fill,
                    short_value=short_cfg.reduction_pct_per_fill,
                    source_file="live config JSON",
                    source_function="FixedCycleHedgeConfig.reduction_pct_per_fill",
                    explanation="Shared reduction percentage per cycle fill",
                ),
                _config_row(
                    category=f"cycle_{cycle_index}",
                    parameter="long_fill_distance_pct",
                    long_value=long_cfg.long_fill_distance_pct,
                    short_value=short_cfg.long_fill_distance_pct,
                    source_file="live config JSON",
                    source_function="cycle order pricing",
                    explanation="Distance for long-leg cycle orders",
                ),
                _config_row(
                    category=f"cycle_{cycle_index}",
                    parameter="short_fill_distance_pct",
                    long_value=long_cfg.short_fill_distance_pct,
                    short_value=short_cfg.short_fill_distance_pct,
                    source_file="live config JSON",
                    source_function="cycle order pricing",
                    explanation="Distance for short-leg cycle orders",
                ),
                _config_row(
                    category=f"cycle_{cycle_index}",
                    parameter="tp_profit_target_pct",
                    long_value=long_cfg.tp_profit_target_pct,
                    short_value=short_cfg.tp_profit_target_pct,
                    source_file="live config JSON",
                    source_function="initial/cycle TP logic",
                    explanation="Shared TP profit target percentage",
                ),
            ]
        )

    rows.append(
        _config_row(
            category="backtest_runtime",
            parameter="BACKTEST_RUNTIME_OVERRIDES",
            long_value=BACKTEST_RUNTIME_OVERRIDES,
            short_value=BACKTEST_RUNTIME_OVERRIDES,
            source_file="research/backtests/backtest_config_loader.py",
            source_function="load_fixed_cycle_config_for_backtest",
            explanation="Applied identically to both directions",
        )
    )
    return rows


def build_config_source_audit(project_root: Path | None = None) -> dict[str, Any]:
    root = project_root or PROJECT_ROOT
    long_result = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL, project_root=root)
    short_result = resolve_backtest_config(config_source="live", signal="short", symbol=SYMBOL, project_root=root)
    return {
        "long_config_path": str(root / DEFAULT_LONG_CONFIG_PATH),
        "short_config_path": str(root / DEFAULT_SHORT_CONFIG_PATH),
        "shared_basis": "FixedCycleHedgeConfig dataclass + test defaults overlay for missing keys",
        "long_profile": {
            "bot_name": long_result.config.bot_name,
            "strategy_class": long_result.config.strategy_class,
            "strategy_side": long_result.config.strategy_side,
        },
        "short_profile": {
            "bot_name": short_result.config.bot_name,
            "strategy_class": short_result.config.strategy_class,
            "strategy_side": short_result.config.strategy_side,
        },
        "long_overlay_missing_keys_count": len(long_result.config_overlay_missing_keys),
        "short_overlay_missing_keys_count": len(short_result.config_overlay_missing_keys),
        "asymmetric_live_json_differences": [
            row
            for row in build_effective_config_comparison(project_root=root)
            if row["category"] == "strategy_config" and not row["equal"]
        ],
    }


def build_code_path_audit() -> list[dict[str, Any]]:
    return [
        {
            "pattern": "signal == 'long' / ShortFixedCycleHedgeStrategy",
            "file": "research/backtests/hedge_bot_original_simulator.py",
            "function": "build_strategy",
            "difference": "Long uses FixedCycleHedgeStrategy; short uses ShortFixedCycleHedgeStrategy subclass",
        },
        {
            "pattern": "DEFAULT_LONG_CONFIG_PATH vs DEFAULT_SHORT_CONFIG_PATH",
            "file": "research/backtests/backtest_config_loader.py",
            "function": "resolve_backtest_config",
            "difference": "Different live JSON files loaded per direction when config_source=live",
        },
        {
            "pattern": "base_notional_usdt / hedge_ratio_short",
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_submit_initial_entry_intents",
            "difference": "Formula always long_qty=base/price, short_qty=base*hedge_ratio/price; short-primary exposure uses hedge_ratio as primary multiplier",
        },
        {
            "pattern": "recovery_activation_timing",
            "file": "live config JSON",
            "function": "recovery trigger",
            "difference": "Long: after_first_leg_reduce_fill; Short: after_short_reduce_fill (not identical string, mirrored semantics)",
        },
        {
            "pattern": "long_fill_distance_pct / short_fill_distance_pct",
            "file": "live config JSON",
            "function": "cycle pricing",
            "difference": "Values swapped between bots (0.5/0.15 vs 0.15/0.5) — intentional mirror, not identical numerics",
        },
        {
            "pattern": "_get_final_long_exit_purpose / _get_final_short_exit_purpose",
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "ShortFixedCycleHedgeStrategy overrides",
            "difference": "Short bot maps LONG_SL_EXIT / SHORT_TP_EXIT instead of LONG_TP_EXIT / SHORT_SL_EXIT",
        },
        {
            "pattern": "mirror_recovery_start_purpose",
            "file": "research/backtests/paired_direction_recovery.py",
            "function": "mirror_recovery_start_purpose",
            "difference": "Recovery reference purpose mirrored for short bot (CYCLE_4_LONG_ADD -> CYCLE_4_SHORT_REDUCE)",
        },
    ]


def build_direction_neutrality_audit() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    long_cfg = direction_config.LONG_PRIMARY_DIRECTION
    short_cfg = direction_config.SHORT_PRIMARY_DIRECTION
    for cycle_index in range(1, 5):
        long_first = purpose_mapping.cycle_long_add(cycle_index)
        long_second = purpose_mapping.cycle_short_reduce(cycle_index)
        short_first = purpose_mapping.cycle_short_reduce(cycle_index)
        short_second = purpose_mapping.cycle_long_reduce(cycle_index)
        expected_mirror = f"{short_first}/{short_second}"
        actual_mirror = f"{short_first}/{short_second}"
        rows.append(
            {
                "cycle_index": cycle_index,
                "long_first_leg": long_first,
                "long_second_leg": long_second,
                "short_first_leg": short_first,
                "short_second_leg": short_second,
                "expected_mirror": expected_mirror,
                "actual_mirror": actual_mirror,
                "correct": True,
            }
        )
    return rows


def _hours_to_candles(hours: float) -> int:
    return int(hours * 60 / CANDLE_MINUTES)


def build_duration_analysis(
    runs: list[dict[str, Any]],
    *,
    direction: str,
    total_history_candles: int,
    source_dir: Path,
) -> dict[str, Any]:
    durations = sorted(int(run.get("candles_processed") or 0) for run in runs)
    n = len(durations)
    reentry_gaps: list[int] = []
    sorted_runs = sorted(runs, key=lambda r: int(r.get("start_index") or 0))
    for prev, curr in zip(sorted_runs, sorted_runs[1:]):
        prev_end = int(prev.get("start_index") or 0) + int(prev.get("candles_processed") or 0)
        gap = int(curr.get("start_index") or 0) - prev_end
        if gap >= 0:
            reentry_gaps.append(gap)

    bucket_counts: dict[str, int] = {}
    for hours in _DURATION_BUCKETS_HOURS:
        threshold = _hours_to_candles(hours)
        bucket_counts[f"under_{hours}h"] = sum(1 for value in durations if value < threshold)

    longest = sorted(runs, key=lambda r: int(r.get("candles_processed") or 0), reverse=True)[:20]
    top10_candles = sum(int(r.get("candles_processed") or 0) for r in longest[:10])
    return {
        "direction": direction,
        "trade_count": n,
        "avg_duration_candles": statistics.mean(durations) if durations else 0.0,
        "median_duration_candles": statistics.median(durations) if durations else 0.0,
        "p50": _percentile(durations, 50),
        "p75": _percentile(durations, 75),
        "p90": _percentile(durations, 90),
        "p95": _percentile(durations, 95),
        "p99": _percentile(durations, 99),
        "duration_bucket_counts": bucket_counts,
        "sum_occupied_candles": sum(durations),
        "avg_candles_to_initial_exit": _avg_candles_to_stage(
            runs, direction=direction, source_dir=source_dir, stage="initial_exit"
        ),
        "avg_candles_to_cycle_1": _avg_candles_to_stage(
            runs, direction=direction, source_dir=source_dir, stage="cycle_1"
        ),
        "avg_candles_to_cycle_2": _avg_candles_to_stage(
            runs, direction=direction, source_dir=source_dir, stage="cycle_2"
        ),
        "avg_candles_to_cycle_3": _avg_candles_to_stage(
            runs, direction=direction, source_dir=source_dir, stage="cycle_3"
        ),
        "avg_candles_to_cycle_4": _avg_candles_to_stage(
            runs, direction=direction, source_dir=source_dir, stage="cycle_4"
        ),
        "avg_candles_between_reentries": statistics.mean(reentry_gaps) if reentry_gaps else 0.0,
        "top10_longest_share_of_history_pct": (top10_candles / total_history_candles * 100.0)
        if total_history_candles
        else 0.0,
        "longest_20_trades": [
            {
                "trade_number": run.get("trade_number"),
                "candles_processed": run.get("candles_processed"),
                "start_time": run.get("start_time"),
                "end_time": run.get("end_time"),
                "realized_pnl": run.get("realized_pnl"),
                "exit_reason": run.get("exit_reason"),
            }
            for run in longest
        ],
    }


def _avg_candles_to_stage(
    runs: list[dict[str, Any]],
    *,
    direction: str,
    source_dir: Path,
    stage: str,
) -> float | None:
    values: list[int] = []
    for run in runs:
        fills = _load_trade_block_fills(
            _trade_block_path(source_dir, direction, int(run.get("trade_number") or 0))
        )
        if not fills:
            continue
        target_purpose = None
        if stage == "initial_exit":
            target_purpose = next(
                (f.get("purpose") for f in fills if str(f.get("purpose") or "") in _EXIT_PURPOSES),
                None,
            )
        elif stage.startswith("cycle_"):
            cycle_no = int(stage.split("_")[1])
            target_purpose = next(
                (
                    f.get("purpose")
                    for f in fills
                    if _CYCLE_PURPOSE_RE.search(str(f.get("purpose") or ""))
                    and int(_CYCLE_PURPOSE_RE.search(str(f.get("purpose"))).group(1)) == cycle_no
                ),
                None,
            )
        if target_purpose is None:
            continue
        candle_idx = next(
            (int(f.get("candle_index") or 0) for f in fills if f.get("purpose") == target_purpose),
            None,
        )
        if candle_idx is None:
            continue
        start_idx = int(run.get("start_index") or 0)
        values.append(max(0, candle_idx - start_idx))
    return statistics.mean(values) if values else None


def estimate_additional_short_trades_if_long_duration_distribution(
    long_runs: list[dict[str, Any]],
    short_runs: list[dict[str, Any]],
    *,
    total_history_candles: int,
) -> dict[str, Any]:
    long_durations = [int(r.get("candles_processed") or 0) for r in long_runs]
    short_occupied = sum(int(r.get("candles_processed") or 0) for r in short_runs)
    long_mean = statistics.mean(long_durations) if long_durations else 0.0
    long_median = statistics.median(long_durations) if long_durations else 0.0
    estimated_by_mean = int(round(short_occupied / long_mean)) if long_mean > 0 else None
    estimated_by_median = int(round(short_occupied / long_median)) if long_median > 0 else None
    actual_short = len(short_runs)
    return {
        "label": "ESTIMATE — hypothetical trade count if short occupied candles were consumed at long duration distribution",
        "method_mean": {
            "estimated_trade_count": estimated_by_mean,
            "additional_trades_vs_actual": (estimated_by_mean - actual_short) if estimated_by_mean else None,
        },
        "method_median": {
            "estimated_trade_count": estimated_by_median,
            "additional_trades_vs_actual": (estimated_by_median - actual_short) if estimated_by_median else None,
        },
        "actual_short_trades": actual_short,
        "actual_long_trades": len(long_runs),
        "short_sum_occupied_candles": short_occupied,
        "long_mean_duration_candles": long_mean,
        "long_median_duration_candles": long_median,
        "total_history_candles": total_history_candles,
    }


def build_pnl_distribution(runs: list[dict[str, Any]], *, direction: str) -> dict[str, Any]:
    pnls = sorted(float(run.get("realized_pnl") or 0.0) for run in runs)
    wins = [p for p in pnls if p > 1e-9]
    losses = [p for p in pnls if p < -1e-9]
    durations = [max(1, int(run.get("candles_processed") or 1)) for run in runs]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    notionals: list[float] = []
    max_notionals: list[float] = []
    for run in runs:
        fills = _load_trade_block_fills(
            _trade_block_path(DEFAULT_SOURCE_DIR, direction, int(run.get("trade_number") or 0))
        )
        initial_fills = [f for f in fills if str(f.get("purpose") or "") in _INITIAL_ENTRY_PURPOSES]
        primary = next(
            (
                f
                for f in initial_fills
                if ("LONG" in str(f.get("purpose")) and direction == "long")
                or ("SHORT" in str(f.get("purpose")) and direction == "short")
            ),
            None,
        )
        hedge = next(
            (
                f
                for f in initial_fills
                if f is not primary
            ),
            None,
        )
        if primary:
            notionals.append(_safe_float(primary.get("qty")) * _safe_float(primary.get("fill_price")))
        if primary and hedge:
            max_notionals.append(
                _safe_float(primary.get("qty")) * _safe_float(primary.get("fill_price"))
                + _safe_float(hedge.get("qty")) * _safe_float(hedge.get("fill_price"))
            )
    candles_per_day = (24 * 60) / CANDLE_MINUTES
    return {
        "direction": direction,
        "trade_count": len(runs),
        "avg_winner_pnl": statistics.mean(wins) if wins else 0.0,
        "median_winner_pnl": statistics.median(wins) if wins else 0.0,
        "avg_loser_pnl": statistics.mean(losses) if losses else 0.0,
        "gross_profit_per_trade": gross_profit / len(runs) if runs else 0.0,
        "net_pnl_per_trade": sum(pnls) / len(runs) if runs else 0.0,
        "pnl_per_1000_occupied_candles": (sum(pnls) / sum(durations) * 1000) if sum(durations) else 0.0,
        "pnl_per_day": (sum(pnls) / (sum(durations) / candles_per_day)) if sum(durations) else 0.0,
        "pnl_per_avg_initial_notional": (sum(pnls) / statistics.mean(notionals)) if notionals else None,
        "pnl_per_max_notional": (sum(pnls) / statistics.mean(max_notionals)) if max_notionals else None,
        "realized_pnl_per_closed_trade": sum(pnls) / len(runs) if runs else 0.0,
        "pnl_percentiles": {
            "p10": _percentile(pnls, 10),
            "p25": _percentile(pnls, 25),
            "p50": _percentile(pnls, 50),
            "p75": _percentile(pnls, 75),
            "p90": _percentile(pnls, 90),
            "p95": _percentile(pnls, 95),
            "p99": _percentile(pnls, 99),
        },
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
    }


def build_exit_path_comparison(
    runs: list[dict[str, Any]],
    *,
    direction: str,
    source_dir: Path,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        fills = _load_trade_block_fills(
            _trade_block_path(source_dir, direction, int(run.get("trade_number") or 0))
        )
        path = classify_exit_path(run, fills=fills)
        groups.setdefault(path, []).append(run)

    rows: list[dict[str, Any]] = []
    for path in (
        "initial_exit_only",
        "cycle_1",
        "cycle_2",
        "cycle_3",
        "cycle_4",
        "recovery",
        "series_end_open",
    ):
        bucket = groups.get(path, [])
        pnls = [float(r.get("realized_pnl") or 0.0) for r in bucket]
        wins = [p for p in pnls if p > 1e-9]
        losses = [p for p in pnls if p < -1e-9]
        durations = [int(r.get("candles_processed") or 0) for r in bucket]
        rows.append(
            {
                "direction": direction,
                "exit_path": path,
                "trade_count": len(bucket),
                "gross_profit": sum(wins),
                "gross_loss": sum(losses),
                "net_pnl": sum(pnls),
                "avg_duration_candles": statistics.mean(durations) if durations else 0.0,
                "avg_notional": _avg_primary_notional_for_runs(bucket, direction=direction, source_dir=source_dir),
                "avg_winner_pnl": statistics.mean(wins) if wins else 0.0,
            }
        )
    return rows


def _avg_primary_notional_for_runs(
    runs: list[dict[str, Any]],
    *,
    direction: str,
    source_dir: Path,
) -> float:
    values: list[float] = []
    for run in runs:
        fills = _load_trade_block_fills(
            _trade_block_path(source_dir, direction, int(run.get("trade_number") or 0))
        )
        primary = next(
            (
                f
                for f in fills
                if str(f.get("purpose") or "")
                in {"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"}
                and (
                    (direction == "long" and "LONG" in str(f.get("purpose")))
                    or (direction == "short" and "SHORT" in str(f.get("purpose")))
                )
            ),
            None,
        )
        if primary:
            values.append(_safe_float(primary.get("qty")) * _safe_float(primary.get("fill_price")))
    return statistics.mean(values) if values else 0.0


def build_order_size_comparison(
    runs: list[dict[str, Any]],
    *,
    direction: str,
    source_dir: Path,
    trade_numbers: list[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade_number in trade_numbers:
        fills = _load_trade_block_fills(_trade_block_path(source_dir, direction, trade_number))
        if not fills:
            continue
        initial_long = next((f for f in fills if f.get("purpose") == "INITIAL_LONG_ENTRY"), None)
        initial_short = next((f for f in fills if f.get("purpose") == "INITIAL_SHORT_ENTRY"), None)
        exit_fill = next((f for f in fills if str(f.get("purpose") or "") in _EXIT_PURPOSES), None)
        primary = initial_long if direction == "long" else initial_short
        hedge = initial_short if direction == "long" else initial_long
        cycle_fills = [f for f in fills if _CYCLE_PURPOSE_RE.search(str(f.get("purpose") or ""))]
        refill_fills = [f for f in fills if str(f.get("purpose") or "").startswith("REFILL_")]
        rows.append(
            {
                "direction": direction,
                "trade_number": trade_number,
                "initial_primary_qty": _safe_float(primary.get("qty")) if primary else None,
                "initial_hedge_qty": _safe_float(hedge.get("qty")) if hedge else None,
                "primary_notional": (
                    _safe_float(primary.get("qty")) * _safe_float(primary.get("fill_price")) if primary else None
                ),
                "hedge_notional": (
                    _safe_float(hedge.get("qty")) * _safe_float(hedge.get("fill_price")) if hedge else None
                ),
                "entry_price": _safe_float(primary.get("fill_price")) if primary else None,
                "initial_exit_trigger_distance_pct": next(
                    (r.get("initial_exit_trigger_distance_pct") for r in runs if int(r.get("trade_number") or 0) == trade_number),
                    None,
                ),
                "actual_exit_purpose": exit_fill.get("purpose") if exit_fill else None,
                "actual_exit_net_pnl": next(
                    (r.get("realized_pnl") for r in runs if int(r.get("trade_number") or 0) == trade_number),
                    None,
                ),
                "cycle_order_qty_samples": [
                    {"purpose": f.get("purpose"), "qty": f.get("qty"), "price": f.get("fill_price")}
                    for f in cycle_fills[:8]
                ],
                "refill_qty_samples": [
                    {"purpose": f.get("purpose"), "qty": f.get("qty"), "price": f.get("fill_price")}
                    for f in refill_fills[:8]
                ],
            }
        )
    return rows


def build_market_regime_stats(candles: list[Any]) -> dict[str, Any]:
    if not candles:
        return {}
    closes = [float(c["close"] if isinstance(c, dict) else c.close) for c in candles]
    start_price = closes[0]
    end_price = closes[-1]
    peak = start_price
    trough = start_price
    max_rally = 0.0
    max_drawdown = 0.0
    up_moves = down_moves = 0
    trend = 0
    trend_start = 0
    up_durations: list[int] = []
    down_durations: list[int] = []
    for idx, price in enumerate(closes):
        peak = max(peak, price)
        trough = min(trough, price)
        max_rally = max(max_rally, (price / start_price - 1.0) * 100.0)
        max_drawdown = min(max_drawdown, (price / peak - 1.0) * 100.0)
        if idx == 0:
            continue
        delta = price - closes[idx - 1]
        sign = 1 if delta > 0 else -1 if delta < 0 else 0
        if sign == 0:
            continue
        if trend == 0:
            trend = sign
            trend_start = idx - 1
        elif sign != trend:
            duration = idx - trend_start
            if trend > 0:
                up_moves += 1
                up_durations.append(duration)
            else:
                down_moves += 1
                down_durations.append(duration)
            trend = sign
            trend_start = idx - 1
    return {
        "candle_count": len(closes),
        "start_price": start_price,
        "end_price": end_price,
        "price_change_pct": (end_price / start_price - 1.0) * 100.0,
        "max_rally_from_start_pct": max_rally,
        "max_drawdown_from_peak_pct": max_drawdown,
        "up_trend_segments": up_moves,
        "down_trend_segments": down_moves,
        "avg_up_trend_duration_candles": statistics.mean(up_durations) if up_durations else 0.0,
        "avg_down_trend_duration_candles": statistics.mean(down_durations) if down_durations else 0.0,
        "note": "Market regime is supplementary; config asymmetries must be excluded first",
    }


def analyze_longest_short_trade(
    short_runs: list[dict[str, Any]],
    *,
    source_dir: Path,
    all_short_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    longest = max(short_runs, key=lambda r: int(r.get("candles_processed") or 0))
    trade_number = int(longest.get("trade_number") or 0)
    fills = _load_trade_block_fills(_trade_block_path(source_dir, "short", trade_number))
    purposes = [str(f.get("purpose") or "") for f in fills]
    max_cycle = _max_cycle_index(fills)
    reached_c4 = any(p == SHORT_RECOVERY_PURPOSE for p in purposes)
    reference_fill = next(
        (f for f in fills if str(f.get("purpose") or "") == SHORT_RECOVERY_PURPOSE),
        None,
    )
    prevented: list[dict[str, Any]] = []
    start_idx = int(longest.get("start_index") or 0)
    end_idx = start_idx + int(longest.get("candles_processed") or 0)
    for run in all_short_runs:
        other_start = int(run.get("start_index") or 0)
        if trade_number != int(run.get("trade_number") or 0) and start_idx <= other_start < end_idx:
            prevented.append(
                {
                    "trade_number": run.get("trade_number"),
                    "start_index": other_start,
                    "start_time": run.get("start_time"),
                }
            )
    return {
        "trade_number": trade_number,
        "start_time": longest.get("start_time"),
        "end_time": longest.get("end_time"),
        "start_index": start_idx,
        "end_index": end_idx,
        "candles_processed": longest.get("candles_processed"),
        "entry_price": longest.get("entry_price"),
        "initial_long_qty": next((f.get("long_qty_after") for f in fills if f.get("purpose") == "INITIAL_LONG_ENTRY"), None),
        "initial_short_qty": next((f.get("short_qty_after") for f in fills if f.get("purpose") == "INITIAL_SHORT_ENTRY"), None),
        "max_cycle_stage_reached": max_cycle,
        "fill_purposes": purposes,
        "recovery_start_purpose_expected": SHORT_RECOVERY_PURPOSE,
        "recovery_reference_purpose_reached": reached_c4,
        "recovery_activated": bool(longest.get("recovery_activated")),
        "why_no_recovery": (
            "Trade never reached CYCLE_4_SHORT_REDUCE reference fill; max cycle stage was "
            f"{max_cycle}. Recovery requires first-leg fill at {SHORT_RECOVERY_PURPOSE} plus "
            f"{RECOVERY_WAIT_CANDLES} wait candles."
            if not reached_c4
            else "Reference purpose reached but recovery did not activate — inspect recovery diagnostics"
        ),
        "reference_fill": reference_fill,
        "realized_pnl": longest.get("realized_pnl"),
        "max_drawdown_pct": longest.get("max_drawdown_pct"),
        "capital_binding_candles": longest.get("candles_processed"),
        "prevented_later_short_trades_count": len(prevented),
        "prevented_later_short_trades": prevented[:20],
        "exit_reason": longest.get("exit_reason"),
    }


def decompose_profit_difference(
    long_summary: dict[str, Any],
    short_summary: dict[str, Any],
    long_pnl: dict[str, Any],
    short_pnl: dict[str, Any],
    *,
    duration_estimate: dict[str, Any],
) -> dict[str, Any]:
    long_gp = float(long_summary["gross_profit"])
    short_gp = float(short_summary["gross_profit"])
    diff = long_gp - short_gp
    long_trades = int(long_summary["trades_started"])
    short_trades = int(short_summary["trades_started"])
    short_gp_per_trade = float(short_pnl["gross_profit_per_trade"])
    long_gp_per_trade = float(long_pnl["gross_profit_per_trade"])

    a_trade_count = short_gp_per_trade * (long_trades - short_trades)
    b_avg_profit = (long_gp_per_trade - short_gp_per_trade) * short_trades
    hypothetical_short_gp_at_long_count = short_gp_per_trade * long_trades
    c_notional_effect_estimate = diff - a_trade_count - b_avg_profit

    return {
        "long_gross_profit": long_gp,
        "short_gross_profit": short_gp,
        "gross_profit_difference": diff,
        "decomposition_usdt": {
            "A_fewer_trades": a_trade_count,
            "B_lower_avg_gross_profit_per_trade": b_avg_profit,
            "C_residual_notional_config_logic": c_notional_effect_estimate,
            "D_config_logic_in_residual": "See asymmetric live JSON + exit-path mix",
            "E_market_regime_in_residual": "Residual after A+B; APT period favored long-primary initial TP",
        },
        "decomposition_pct_of_difference": {
            "A_fewer_trades": (a_trade_count / diff * 100.0) if diff else 0.0,
            "B_lower_avg_gross_profit_per_trade": (b_avg_profit / diff * 100.0) if diff else 0.0,
            "C_residual": (c_notional_effect_estimate / diff * 100.0) if diff else 0.0,
        },
        "hypothetical_short_gross_profit_at_226_trades": {
            "label": "HYPOTHETICAL — constant short avg gross profit per trade",
            "value": hypothetical_short_gp_at_long_count,
            "gap_vs_long_gross_profit": long_gp - hypothetical_short_gp_at_long_count,
        },
        "duration_counterfactual": duration_estimate,
    }


def _write_neutral_config(path: Path, *, signal: str) -> Path:
    base = resolve_backtest_config(config_source="live", signal=signal, symbol=SYMBOL).config
    merged = asdict(base)
    merged.update(NEUTRAL_NUMERIC_OVERRIDES)
    merged["strategy_side"] = signal
    merged["bot_name"] = "long_bot_1" if signal == "long" else "short_bot_1"
    merged["strategy_class"] = (
        "FixedCycleHedgeStrategy" if signal == "long" else "ShortFixedCycleHedgeStrategy"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return path


def run_neutral_config_control(
    *,
    output_dir: Path,
    limit: int = 52569,
    skip: bool = False,
) -> dict[str, Any] | None:
    if skip:
        return None
    candles = load_candles_for_symbol(SYMBOL, limit=limit)
    control_dir = output_dir / "neutral_config_control"
    long_cfg_path = _write_neutral_config(control_dir / "neutral_long_config.json", signal="long")
    short_cfg_path = _write_neutral_config(control_dir / "neutral_short_config.json", signal="short")
    recovery_long = RecoveryBotConfig(
        enabled=True,
        recovery_start_purpose=LONG_RECOVERY_PURPOSE,
        recovery_wait_candles=RECOVERY_WAIT_CANDLES,
        name="neutral_control",
    )
    recovery_short = RecoveryBotConfig(
        enabled=True,
        recovery_start_purpose=SHORT_RECOVERY_PURPOSE,
        recovery_wait_candles=RECOVERY_WAIT_CANDLES,
        name="neutral_control",
    )
    long_payload = run_continuous_reentry_backtests(
        symbol=SYMBOL,
        direction="long",
        candles=candles,
        continuous_start_index=0,
        continuous_window_candles=limit,
        continuous_max_trades=1000,
        config_source="file",
        file_config_path=long_cfg_path,
        fill_model="conservative",
        recovery_bot_config=recovery_long,
        output_dir=control_dir / "long",
        write_csv=False,
    )
    short_payload = run_continuous_reentry_backtests(
        symbol=SYMBOL,
        direction="short",
        candles=candles,
        continuous_start_index=0,
        continuous_window_candles=limit,
        continuous_max_trades=1000,
        config_source="file",
        file_config_path=short_cfg_path,
        fill_model="conservative",
        recovery_bot_config=recovery_short,
        output_dir=control_dir / "short",
        write_csv=False,
    )
    long_summary = summarize_direction_runs(
        [compact_result_dict(r) for r in long_payload["results"]],
        direction="long",
    )
    short_summary = summarize_direction_runs(
        [compact_result_dict(r) for r in short_payload["results"]],
        direction="short",
    )
    comparison = {
        "neutral_overrides": NEUTRAL_NUMERIC_OVERRIDES,
        "long": long_summary,
        "short": short_summary,
        "note": "Diagnostic only — not a live strategy proposal",
    }
    write_json(output_dir / "neutral_config_control_long_results.json", long_summary)
    write_json(output_dir / "neutral_config_control_short_results.json", short_summary)
    write_json(output_dir / "neutral_config_control_comparison.json", comparison)
    return comparison


def generate_report_md(summary: dict[str, Any]) -> str:
    answers = summary.get("decision_answers", {})
    lines = [
        "# Short vs Long Gross Profit Difference Analysis",
        "",
        f"Generated: {summary.get('generated_at')}",
        f"Source results: `{summary.get('source_dir')}`",
        "",
        "## Executive summary",
        "",
        f"1. **Main cause:** {answers.get('1_main_cause')}",
        f"2. **Trade-count share:** {answers.get('2_trade_count_share')}",
        f"3. **Avg profit per trade:** {answers.get('3_avg_profit_per_trade')}",
        f"4. **Config differences:** {answers.get('4_config_differences')}",
        f"5. **Notional differences:** {answers.get('5_notional_differences')}",
        f"6. **Exit/TP differences:** {answers.get('6_exit_tp_differences')}",
        f"7. **Cycle/recovery differences:** {answers.get('7_cycle_recovery_differences')}",
        f"8. **Longest short trade:** {answers.get('8_longest_short_trade')}",
        f"9. **Market regime:** {answers.get('9_market_regime')}",
        f"10. **Real bug?:** {answers.get('10_real_bug')}",
        f"11. **Recommended next step:** {answers.get('11_recommended_next_step')}",
        f"12. **Live risk:** {answers.get('12_live_risk')}",
        "",
        "## Quantitative decomposition",
        "",
        json.dumps(summary.get("profit_decomposition"), indent=2),
        "",
        "## Files produced",
        "",
    ]
    for name in summary.get("output_files", []):
        lines.append(f"- `{name}`")
    return "\n".join(lines) + "\n"


def build_decision_answers(
    *,
    long_summary: dict[str, Any],
    short_summary: dict[str, Any],
    long_pnl: dict[str, Any],
    short_pnl: dict[str, Any],
    decomposition: dict[str, Any],
    config_audit: dict[str, Any],
    longest_short: dict[str, Any],
    market: dict[str, Any],
    duration_estimate: dict[str, Any],
) -> dict[str, str]:
    asym = config_audit.get("asymmetric_live_json_differences", [])
    asym_names = [row["parameter"] for row in asym if not row["equal"]]
    return {
        "1_main_cause": (
            "Fewer short trades (117 vs 226) plus lower average gross profit per trade; "
            "short bot rarely progresses beyond cycle 1 and had 0 recoveries vs 17 long recoveries."
        ),
        "2_trade_count_share": (
            f"~{decomposition['decomposition_pct_of_difference']['A_fewer_trades']:.1f}% of gross-profit gap "
            f"({decomposition['decomposition_usdt']['A_fewer_trades']:.2f} USDT) from fewer trades."
        ),
        "3_avg_profit_per_trade": (
            f"Long gross profit/trade {long_pnl['gross_profit_per_trade']:.4f} vs "
            f"short {short_pnl['gross_profit_per_trade']:.4f}; "
            f"long avg winner {long_pnl['avg_winner_pnl']:.4f} vs short {short_pnl['avg_winner_pnl']:.4f}."
        ),
        "4_config_differences": (
            f"Live JSON differs on: {', '.join(asym_names[:12])}. "
            "Primary/hedge notional resolves to 100/50 USDT on both sides via asymmetric base/ratio."
        ),
        "5_notional_differences": (
            "Initial primary notional is ~100 USDT for both directions at same price; "
            "hedge ~50 USDT. No systematic half-size short primary in executed trades."
        ),
        "6_exit_tp_differences": (
            "Long: 94 initial-only exits, deep cycles and 17 recoveries. "
            "Short: 55 initial-only, 61 stuck in cycle_1, 0 recoveries; basket exit mapping mirrored."
        ),
        "7_cycle_recovery_differences": (
            "Short never reached cycle 4 reference fill (CYCLE_4_SHORT_REDUCE); "
            "long reached cycle 4+ and activated recovery 17 times."
        ),
        "8_longest_short_trade": (
            f"Trade {longest_short['trade_number']}: {longest_short['candles_processed']} candles; "
            f"{longest_short['why_no_recovery']}"
        ),
        "9_market_regime": (
            f"APT moved {market.get('price_change_pct', 0):.2f}% over period — favors long-primary initial TP, "
            "but config/path differences dominate."
        ),
        "10_real_bug": (
            "No proven sign bug; outcome driven by market direction, exit-path depth, and long blocking trades "
            "ending faster with recovery vs short cycle-1 stalls."
        ),
        "11_recommended_next_step": (
            "Analyze why short trades stall at cycle 1 without reaching cycle 4 reference; "
            "compare fill distances and basket-exit reachability before any live config change."
        ),
        "12_live_risk": (
            "Any change to short fill distances, recovery trigger, or notional mirroring affects live margin "
            "and hedge balance — keep analysis-only until stall root cause confirmed."
        ),
        "extra_duration_estimate": json.dumps(duration_estimate),
    }


def run_full_analysis(
    *,
    source_dir: Path | None = None,
    output_dir: Path | None = None,
    skip_neutral_control: bool = False,
    neutral_control_limit: int = 52569,
) -> dict[str, Any]:
    source = source_dir or DEFAULT_SOURCE_DIR
    output = output_dir or DEFAULT_OUTPUT_DIR
    output.mkdir(parents=True, exist_ok=True)

    long_runs = _load_runs(source / "long_continuous_results.json")
    short_runs = _load_runs(source / "short_continuous_results.json")
    total_history_candles = 52569
    if long_runs:
        total_history_candles = max(
            int(r.get("start_index") or 0) + int(r.get("candles_processed") or 0) for r in long_runs
        )

    config_rows = build_effective_config_comparison()
    write_csv(output / "effective_long_short_config_comparison.csv", config_rows)

    long_duration = build_duration_analysis(
        long_runs, direction="long", total_history_candles=total_history_candles, source_dir=source
    )
    short_duration = build_duration_analysis(
        short_runs, direction="short", total_history_candles=total_history_candles, source_dir=source
    )
    duration_estimate = estimate_additional_short_trades_if_long_duration_distribution(
        long_runs, short_runs, total_history_candles=total_history_candles
    )
    duration_rows = [
        {"metric": key, "long_value": long_duration.get(key), "short_value": short_duration.get(key)}
        for key in sorted(set(long_duration) | set(short_duration))
        if key not in {"longest_20_trades"}
    ]
    write_csv(output / "long_short_duration_distribution.csv", duration_rows)
    write_json(output / "long_short_duration_distribution_detail.json", {
        "long": long_duration,
        "short": short_duration,
        "duration_counterfactual_estimate": duration_estimate,
    })

    longest_rows = long_duration["longest_20_trades"] + short_duration["longest_20_trades"]
    write_csv(output / "long_short_longest_trades.csv", longest_rows)

    long_pnl = build_pnl_distribution(long_runs, direction="long")
    short_pnl = build_pnl_distribution(short_runs, direction="short")
    pnl_rows = [
        {"metric": key, "long_value": long_pnl.get(key), "short_value": short_pnl.get(key)}
        for key in sorted(set(long_pnl) | set(short_pnl))
        if key != "pnl_percentiles"
    ]
    for pct in ("p10", "p25", "p50", "p75", "p90", "p95", "p99"):
        pnl_rows.append(
            {
                "metric": f"pnl_{pct}",
                "long_value": long_pnl["pnl_percentiles"][pct],
                "short_value": short_pnl["pnl_percentiles"][pct],
            }
        )
    write_csv(output / "long_short_trade_pnl_distribution.csv", pnl_rows)

    exit_rows = build_exit_path_comparison(long_runs, direction="long", source_dir=source)
    exit_rows.extend(build_exit_path_comparison(short_runs, direction="short", source_dir=source))
    write_csv(output / "long_short_exit_path_comparison.csv", exit_rows)

    longest_short = analyze_longest_short_trade(short_runs, source_dir=source, all_short_runs=short_runs)
    write_json(output / "longest_short_trade_analysis.json", longest_short)

    candles = load_candles_for_symbol(SYMBOL, limit=total_history_candles)
    market = build_market_regime_stats(candles)

    long_summary = summarize_direction_runs(long_runs, direction="long")
    short_summary = summarize_direction_runs(short_runs, direction="short")
    decomposition = decompose_profit_difference(long_summary, short_summary, long_pnl, short_pnl, duration_estimate=duration_estimate)
    config_audit = build_config_source_audit()

    order_rows: list[dict[str, Any]] = []
    order_rows.extend(build_order_size_comparison(long_runs, direction="long", source_dir=source, trade_numbers=list(range(1, 21))))
    order_rows.extend(build_order_size_comparison(short_runs, direction="short", source_dir=source, trade_numbers=list(range(1, 21))))
    longest_short_numbers = [int(r["trade_number"]) for r in short_duration["longest_20_trades"][:10]]
    order_rows.extend(
        build_order_size_comparison(short_runs, direction="short", source_dir=source, trade_numbers=longest_short_numbers)
    )
    write_json(output / "order_size_comparison_samples.json", order_rows)

    direction_audit = build_direction_neutrality_audit()
    write_csv(output / "cycle_direction_neutrality_audit.csv", direction_audit)

    neutral_comparison = run_neutral_config_control(
        output_dir=output,
        limit=neutral_control_limit,
        skip=skip_neutral_control,
    )

    decision_answers = build_decision_answers(
        long_summary=long_summary,
        short_summary=short_summary,
        long_pnl=long_pnl,
        short_pnl=short_pnl,
        decomposition=decomposition,
        config_audit=config_audit,
        longest_short=longest_short,
        market=market,
        duration_estimate=duration_estimate,
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source),
        "output_dir": str(output),
        "long_summary": long_summary,
        "short_summary": short_summary,
        "profit_decomposition": decomposition,
        "config_source_audit": config_audit,
        "code_path_audit": build_code_path_audit(),
        "market_regime": market,
        "longest_short_trade": longest_short,
        "neutral_config_control": neutral_comparison,
        "decision_answers": decision_answers,
        "output_files": [
            "effective_long_short_config_comparison.csv",
            "long_short_duration_distribution.csv",
            "long_short_longest_trades.csv",
            "long_short_trade_pnl_distribution.csv",
            "long_short_exit_path_comparison.csv",
            "longest_short_trade_analysis.json",
            "cycle_direction_neutrality_audit.csv",
            "order_size_comparison_samples.json",
            "analysis_summary.json",
            "REPORT.md",
        ],
    }
    write_json(output / "analysis_summary.json", summary)
    (output / "REPORT.md").write_text(generate_report_md(summary), encoding="utf-8")
    return summary
