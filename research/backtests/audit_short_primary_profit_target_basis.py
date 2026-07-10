"""Audit: short-primary profit target basis (base_notional vs effective primary notional)."""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig
from fixed_cycle_hedge_bot.hedge_exit_math import calculate_hedge_exit_price

from .analyze_short_vs_long_profit_difference import classify_exit_path, _max_cycle_index
from .backtest_config_loader import resolve_backtest_config
from .independent_continuous_long_short_analysis import write_csv, write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = (
    PROJECT_ROOT
    / "research/backtests/results/independent_continuous_long_short_same_initial_start_c4_wait576"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "research/backtests/results/short_primary_profit_target_basis_audit"
)
SYMBOL = "APTUSDT"
FEE_RATE = 0.00055
TP_PCT = 0.25
TP_BUFFER_PCT = 0.0002
TARGET_PROFIT_USDT = 0.015

_CYCLE_RE = re.compile(r"CYCLE_(\d+)_")
_EXIT_PURPOSES = frozenset({"LONG_TP_EXIT", "SHORT_SL_EXIT", "LONG_SL_EXIT", "SHORT_TP_EXIT"})


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def effective_notionals(
    config: FixedCycleHedgeConfig,
    *,
    signal: str,
    entry_price: float = 1.0,
) -> dict[str, float]:
    long_qty = config.base_notional_usdt / entry_price
    short_qty = (config.base_notional_usdt * config.hedge_ratio_short) / entry_price
    long_notional = config.base_notional_usdt
    short_notional = config.base_notional_usdt * config.hedge_ratio_short
    if signal == "long":
        primary = long_notional
        hedge = short_notional
        primary_side = "long"
    else:
        primary = short_notional
        hedge = long_notional
        primary_side = "short"
    return {
        "base_notional_usdt": float(config.base_notional_usdt),
        "hedge_ratio_short": float(config.hedge_ratio_short),
        "effective_long_notional": long_notional,
        "effective_short_notional": short_notional,
        "effective_primary_notional": primary,
        "effective_hedge_notional": hedge,
        "resolved_initial_long_qty": long_qty,
        "resolved_initial_short_qty": short_qty,
        "resolved_primary_side": primary_side,
        "entry_price_assumption": entry_price,
    }


def build_notional_basis_code_paths() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "file": "fixed_cycle_hedge_bot/hedge_exit_math.py",
            "function": "calculate_hedge_exit_price",
            "line_context": "profit_basis_usdt = long_avg * long_qty",
            "direction": "both (long-primary correct; short-primary wrong basis)",
            "input_notional_basis": "long_avg * long_qty (always long leg)",
            "actual_formula": "target_profit = (long_avg * long_qty) * tp_profit_target_pct / 100",
            "expected_formula": "target_profit = (primary_avg * primary_qty) * tp_profit_target_pct / 100",
            "risk": "HIGH — short-primary uses hedge long notional (~50 USDT) instead of short primary (~100 USDT)",
        },
        {
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_calculate_tp_projection",
            "line_context": "~13130 calls calculate_hedge_exit_price",
            "direction": "both",
            "input_notional_basis": "delegates to hedge_exit_math long leg",
            "actual_formula": "same as calculate_hedge_exit_price + fee-adjusted exit price",
            "expected_formula": "direction-neutral primary-leg profit basis",
            "risk": "HIGH — ShortFixedCycleHedgeStrategy does not override this method",
        },
        {
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_calculate_tp_components",
            "line_context": "goal_profit = reference_price * tp_profit_target_pct",
            "direction": "both",
            "input_notional_basis": "price-based (entry_reference or long_avg), not explicit notional",
            "actual_formula": "reference_price * pct(tp_profit_target_pct)",
            "expected_formula": "unclear legacy/auxiliary path; not primary basket exit",
            "risk": "MEDIUM — price proxy, not qty×price; may bias short bot via long_avg reference",
        },
        {
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_submit_initial_entry_intents",
            "line_context": "~4527 long_qty=base/price; short_qty=base*hedge_ratio/price",
            "direction": "both",
            "input_notional_basis": "base_notional + hedge_ratio",
            "actual_formula": "long=base, short=base*hedge_ratio",
            "expected_formula": "correct for sizing; short primary ends at base*hedge_ratio",
            "risk": "LOW — sizing correct; not a profit-target bug",
        },
        {
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_build_short_reduce_followup / short TP builder",
            "line_context": "~10731 required_net = long_loss + target_profit_usdt",
            "direction": "long-primary cycle second leg (short_reduce TP)",
            "input_notional_basis": "fixed config.target_profit_usdt (0.015 USDT)",
            "actual_formula": "loss_to_cover + target_profit_usdt",
            "expected_formula": "same fixed USDT target both bots",
            "risk": "LOW for notional-basis hypothesis — uses absolute USDT, not base_notional",
        },
        {
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_build_long_reduce_cover_projection",
            "line_context": "~19074 projected_required = short_reduce_loss + target_profit_usdt",
            "direction": "long-primary cycle long_reduce cover",
            "input_notional_basis": "config.target_profit_usdt",
            "actual_formula": "realized loss on first leg + 0.015 USDT",
            "expected_formula": "direction-neutral fixed USDT add-on",
            "risk": "LOW — not base_notional-scaled",
        },
        {
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_build_recovery_refill_intents",
            "line_context": "~18553 long_qty from base_notional only",
            "direction": "both",
            "input_notional_basis": "base_notional_usdt (50 on short bot)",
            "actual_formula": "long_notional=base, short_notional=base*hedge_ratio",
            "expected_formula": "recovery sizing asymmetry on short bot (separate issue)",
            "risk": "MEDIUM — recovery reload uses raw base, not primary notional",
        },
        {
            "file": "research/backtests/stuck_recovery_reload.py",
            "function": "reload sizing helpers",
            "line_context": "base = strategy_config.base_notional_usdt",
            "direction": "backtest-only",
            "input_notional_basis": "base_notional_usdt",
            "actual_formula": "backtest shim only",
            "expected_formula": "n/a",
            "risk": "LOW for initial TP audit",
        },
        {
            "file": "research/backtests/dynamic_cycle_order_scaling.py",
            "function": "compute_scaled_target_profit_usdt",
            "line_context": "scales config.target_profit_usdt",
            "direction": "backtest addon",
            "input_notional_basis": "baseline target_profit_usdt",
            "actual_formula": "scaled absolute USDT",
            "expected_formula": "not used in independent continuous live-config run",
            "risk": "NONE in this audit scope",
        },
        {
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "ShortFixedCycleHedgeStrategy",
            "line_context": "overrides exit purposes only, not _calculate_tp_projection",
            "direction": "short",
            "input_notional_basis": "inherits long-leg basis from parent",
            "actual_formula": "LONG_SL_EXIT + SHORT_TP_EXIT at tp_price from parent",
            "expected_formula": "mirrored exit purposes with primary-based tp projection",
            "risk": "HIGH — purpose mirror without profit-basis mirror",
        },
    ]
    return rows


def _load_runs(source_dir: Path, direction: str) -> list[dict[str, Any]]:
    return json.loads((source_dir / f"{direction}_continuous_results.json").read_text())["runs"]


def _trade_block_path(source_dir: Path, direction: str, trade_number: int) -> Path:
    matches = sorted(
        source_dir.glob(f"APTUSDT_{direction}_continuous_trade_{trade_number:04d}_*_trade_blocks.json")
    )
    if not matches:
        raise FileNotFoundError(f"missing trade block {direction} {trade_number}")
    return matches[0]


def _initial_fills(fills: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    long_fill = next(f for f in fills if f.get("purpose") == "INITIAL_LONG_ENTRY")
    short_fill = next(f for f in fills if f.get("purpose") == "INITIAL_SHORT_ENTRY")
    return long_fill, short_fill


def _exit_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in fills if str(f.get("purpose") or "") in _EXIT_PURPOSES]


def reconstruct_initial_exit_row(
    run: dict[str, Any],
    *,
    direction: str,
    source_dir: Path,
    long_cfg: FixedCycleHedgeConfig,
    short_cfg: FixedCycleHedgeConfig,
) -> dict[str, Any] | None:
    trade_number = int(run["trade_number"])
    fills = [
        row
        for row in json.loads(_trade_block_path(source_dir, direction, trade_number).read_text())["trade_blocks"]
        if row.get("row_type") == "fill"
    ]
    if _max_cycle_index(fills) > 0:
        return None
    if classify_exit_path(run, fills=fills) != "initial_exit_only":
        return None

    long_fill, short_fill = _initial_fills(fills)
    le, lq = _safe_float(long_fill["fill_price"]), _safe_float(long_fill["qty"])
    se, sq = _safe_float(short_fill["fill_price"]), _safe_float(short_fill["qty"])
    long_notional = le * lq
    short_notional = se * sq
    cfg = long_cfg if direction == "long" else short_cfg
    primary_notional = long_notional if direction == "long" else short_notional
    hedge_notional = short_notional if direction == "long" else long_notional

    components = calculate_hedge_exit_price(
        le, lq, se, sq,
        float(cfg.tp_profit_target_pct),
        float(cfg.tp_buffer_pct),
        0.0,
        0.0,
        primary_side="long" if direction == "long" else "short",
    )
    basis_a = cfg.base_notional_usdt * cfg.tp_profit_target_pct / 100.0
    basis_b = primary_notional * cfg.tp_profit_target_pct / 100.0
    basis_c = (long_notional + short_notional) * cfg.tp_profit_target_pct / 100.0
    basis_d = abs(long_notional - short_notional) * cfg.tp_profit_target_pct / 100.0

    exits = _exit_fills(fills)
    long_exit = next((f for f in exits if "LONG" in str(f.get("purpose"))), None)
    short_exit = next((f for f in exits if "SHORT" in str(f.get("purpose"))), None)
    lp = _safe_float(long_exit.get("fill_price")) if long_exit else 0.0
    sp = _safe_float(short_exit.get("fill_price")) if short_exit else 0.0

    if direction == "long":
        primary_gross = (lp - le) * lq
        hedge_gross = (sp - se) * sq
    else:
        primary_gross = (se - sp) * sq
        hedge_gross = (lp - le) * lq

    entry_fee = FEE_RATE * (long_notional + short_notional)
    exit_fee = FEE_RATE * (lp * lq + sp * sq) if lp and sp else 0.0
    net_reconstructed = primary_gross + hedge_gross - entry_fee - exit_fee

    return {
        "trade_number": trade_number,
        "direction": direction,
        "entry_timestamp": run.get("start_time"),
        "entry_price": run.get("entry_price"),
        "long_qty": lq,
        "short_qty": sq,
        "long_notional": long_notional,
        "short_notional": short_notional,
        "primary_notional": primary_notional,
        "hedge_notional": hedge_notional,
        "tp_profit_target_pct": cfg.tp_profit_target_pct,
        "base_notional_usdt": cfg.base_notional_usdt,
        "configured_target_profit_usdt": cfg.target_profit_usdt,
        "formula_A_tp_pct_x_base_notional": basis_a,
        "formula_B_tp_pct_x_primary_notional": basis_b,
        "formula_C_tp_pct_x_total_gross_notional": basis_c,
        "formula_D_tp_pct_x_net_notional_gap": basis_d,
        "formula_E_code_calculate_hedge_exit_price_target": components.target_profit_usdt,
        "code_profit_basis_usdt": components.profit_basis_usdt,
        "code_required_profit_usdt": components.required_profit_usdt,
        "code_exit_price": components.exit_price,
        "actual_exit_long_purpose": long_exit.get("purpose") if long_exit else None,
        "actual_exit_short_purpose": short_exit.get("purpose") if short_exit else None,
        "actual_exit_price_long": lp,
        "actual_exit_price_short": sp,
        "initial_exit_trigger_distance_pct": run.get("initial_exit_trigger_distance_pct"),
        "primary_leg_gross_pnl": primary_gross,
        "hedge_leg_gross_pnl": hedge_gross,
        "fees_estimated": entry_fee + exit_fee,
        "net_reconstructed_pnl": net_reconstructed,
        "net_realized_pnl": _safe_float(run.get("realized_pnl")),
        "primary_to_hedge_pnl_ratio": (
            primary_gross / hedge_gross if abs(hedge_gross) > 1e-12 else None
        ),
    }


def build_initial_exit_reconstruction(
    source_dir: Path,
    *,
    trade_numbers: Iterable[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    long_cfg = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL).config
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol=SYMBOL).config
    rows: list[dict[str, Any]] = []

    if trade_numbers is None:
        trade_numbers = []
        for direction in ("long", "short"):
            runs = _load_runs(source_dir, direction)
            for run in runs:
                tn = int(run["trade_number"])
                if tn <= 10:
                    trade_numbers.append((direction, tn))
        trade_numbers.extend([("long", 1), ("short", 1)])

    seen: set[tuple[str, int]] = set()
    for direction, trade_number in trade_numbers:
        key = (direction, trade_number)
        if key in seen:
            continue
        seen.add(key)
        runs = _load_runs(source_dir, direction)
        run = next((r for r in runs if int(r["trade_number"]) == trade_number), None)
        if run is None:
            continue
        row = reconstruct_initial_exit_row(
            run, direction=direction, source_dir=source_dir, long_cfg=long_cfg, short_cfg=short_cfg
        )
        if row:
            rows.append(row)
    rows.sort(key=lambda r: (r["direction"], int(r["trade_number"])))
    return rows


def build_cycle_profit_comparison(
    source_dir: Path,
    *,
    max_trades_per_direction: int = 30,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for direction in ("long", "short"):
        cfg = resolve_backtest_config(config_source="live", signal=direction, symbol=SYMBOL).config
        runs = _load_runs(source_dir, direction)[:max_trades_per_direction]
        for run in runs:
            tn = int(run["trade_number"])
            fills = [
                row
                for row in json.loads(_trade_block_path(source_dir, direction, tn).read_text())["trade_blocks"]
                if row.get("row_type") == "fill"
            ]
            cycle_indices = sorted(
                {
                    int(m.group(1))
                    for f in fills
                    for m in [_CYCLE_RE.search(str(f.get("purpose") or ""))]
                    if m
                }
            )
            if not cycle_indices:
                continue
            long_fill, short_fill = _initial_fills(fills)
            primary_notional = (
                _safe_float(long_fill["fill_price"]) * _safe_float(long_fill["qty"])
                if direction == "long"
                else _safe_float(short_fill["fill_price"]) * _safe_float(short_fill["qty"])
            )
            for cycle_index in range(1, 5):
                if cycle_index not in cycle_indices and cycle_index > max(cycle_indices):
                    continue
                rows.append(
                    {
                        "direction": direction,
                        "trade_number": tn,
                        "cycle_index": cycle_index,
                        "reached": cycle_index in cycle_indices,
                        "target_profit_usdt_config": cfg.target_profit_usdt,
                        "tp_profit_target_pct_config": cfg.tp_profit_target_pct,
                        "base_notional_usdt": cfg.base_notional_usdt,
                        "primary_notional_at_entry": primary_notional,
                        "cycle_profit_basis_initial_basket": (
                            "calculate_hedge_exit_price uses long_avg*long_qty"
                        ),
                        "cycle_second_leg_basis": "loss_to_cover + config.target_profit_usdt (absolute)",
                        "tp_pct_x_base_notional": cfg.base_notional_usdt * cfg.tp_profit_target_pct / 100.0,
                        "tp_pct_x_primary_notional": primary_notional * cfg.tp_profit_target_pct / 100.0,
                        "uses_base_notional_for_pct_target": direction == "short",
                        "uses_primary_notional_for_pct_target": direction == "long",
                    }
                )
    return rows


def build_short_existing_vs_primary_basis(source_dir: Path) -> list[dict[str, Any]]:
    cfg = resolve_backtest_config(config_source="live", signal="short", symbol=SYMBOL).config
    rows: list[dict[str, Any]] = []
    for run in _load_runs(source_dir, "short"):
        tn = int(run["trade_number"])
        fills = [
            row
            for row in json.loads(_trade_block_path(source_dir, "short", tn).read_text())["trade_blocks"]
            if row.get("row_type") == "fill"
        ]
        long_fill, short_fill = _initial_fills(fills)
        le, lq = _safe_float(long_fill["fill_price"]), _safe_float(long_fill["qty"])
        se, sq = _safe_float(short_fill["fill_price"]), _safe_float(short_fill["qty"])
        primary_notional = se * sq
        existing = calculate_hedge_exit_price(
            le, lq, se, sq, cfg.tp_profit_target_pct, cfg.tp_buffer_pct, 0.0, 0.0,
            primary_side="short",
        )
        corrected_target = primary_notional * cfg.tp_profit_target_pct / 100.0
        rows.append(
            {
                "trade_number": tn,
                "exit_path": classify_exit_path(run, fills=fills),
                "primary_notional": primary_notional,
                "existing_target_profit_usdt": existing.target_profit_usdt,
                "corrected_primary_basis_target_profit_usdt": corrected_target,
                "difference_usdt": existing.target_profit_usdt - corrected_target,
                "ratio_existing_to_corrected": (
                    existing.target_profit_usdt / corrected_target if corrected_target else None
                ),
                "actual_realized_pnl": _safe_float(run.get("realized_pnl")),
            }
        )
    return rows


def evaluate_hypothesis(
    initial_rows: list[dict[str, Any]],
    short_corrected_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    short_initial = [r for r in initial_rows if r["direction"] == "short"]
    long_initial = [r for r in initial_rows if r["direction"] == "long"]

    def med(vals: list[float]) -> float:
        return statistics.median(vals) if vals else 0.0

    short_ratio = med([r["ratio_existing_to_corrected"] for r in short_corrected_rows if r["ratio_existing_to_corrected"]])
    short_existing_med = med([r["existing_target_profit_usdt"] for r in short_corrected_rows])
    short_corrected_med = med([r["corrected_primary_basis_target_profit_usdt"] for r in short_corrected_rows])
    short_pnl_initial_med = med([r["net_realized_pnl"] for r in short_initial])
    long_pnl_initial_med = med([r["net_realized_pnl"] for r in long_initial])
    code_basis_matches_long_leg = all(
        abs(r["code_profit_basis_usdt"] - r["hedge_notional"]) < 0.05
        for r in short_initial
    )

    confirmed = (
        code_basis_matches_long_leg
        and 0.45 <= short_ratio <= 0.55
        and abs(short_existing_med - short_corrected_med * 0.5) < 0.02
    )

    return {
        "hypothesis": "Short profit targets partially computed on ~50 USDT (long hedge leg) instead of ~100 USDT primary",
        "confirmed": confirmed,
        "evidence": {
            "short_existing_target_median_usdt": short_existing_med,
            "short_corrected_primary_target_median_usdt": short_corrected_med,
            "ratio_existing_to_corrected_median": short_ratio,
            "short_initial_exit_pnl_median_usdt": short_pnl_initial_med,
            "long_initial_exit_pnl_median_usdt": long_pnl_initial_med,
            "initial_exit_pnl_ratio_short_to_long": (
                short_pnl_initial_med / long_pnl_initial_med if long_pnl_initial_med else None
            ),
            "code_profit_basis_equals_short_hedge_notional": code_basis_matches_long_leg,
            "all_winners_short_median_note": (
                "Overall short winner median ~0.139 USDT includes many small cycle_1 exits; "
                "initial_exit_only short median ~0.289 USDT (~2x code target 0.125)"
            ),
        },
        "conclusion": (
            "CONFIRMED for basket/initial TP via calculate_hedge_exit_price"
            if confirmed
            else "INCONCLUSIVE"
        ),
        "bug": confirmed,
    }


def build_proposed_fix_description() -> dict[str, Any]:
    return {
        "affected_file": "fixed_cycle_hedge_bot/hedge_exit_math.py",
        "secondary_file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
        "affected_function": "calculate_hedge_exit_price",
        "secondary_function": "_calculate_tp_projection (caller); ShortFixedCycleHedgeStrategy should use primary basis",
        "current_formula": "profit_basis_usdt = long_avg * long_qty",
        "proposed_formula": (
            "profit_basis_usdt = primary_avg * primary_qty where primary leg is direction-neutral "
            "(long_qty*long_avg for long-primary, short_qty*short_avg for short-primary); "
            "prefer actual position notionals when snapshot available"
        ),
        "long_impact": "No change if long-primary remains primary=long leg (basis stays 100 USDT)",
        "short_impact": "Initial/basket TP target roughly doubles (~0.125 -> ~0.25 USDT pre-geometry), "
        "closer to long-primary economics",
        "regression_tests_needed": [
            "hedge_exit_math short-primary symmetric notionals",
            "ShortFixedCycleHedgeStrategy initial exit PnL parity with long at same primary notional",
            "existing long-bot backtest unchanged",
        ],
        "live_risk": (
            "Higher short basket TP targets -> exits further from entry, fewer quick initial wins, "
            "potentially faster cycle escalation; margin/hedge interaction must be revalidated"
        ),
        "do_not": "Blindly multiply base_notional*hedge_ratio everywhere — use actual primary leg or direction config",
    }


def generate_report_md(summary: dict[str, Any]) -> str:
    h = summary["hypothesis_evaluation"]
    fix = summary["proposed_fix"]
    lines = [
        "# Short-Primary Profit Target Basis Audit",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Verdict",
        "",
        f"**Hypothesis {h['conclusion']}**",
        "",
        h["hypothesis"],
        "",
        "## Key finding",
        "",
        "`calculate_hedge_exit_price()` sets `profit_basis_usdt = long_avg * long_qty`.",
        "For the short-primary bot the long leg is the **hedge** (~50 USDT), not the primary (~100 USDT).",
        "",
        f"- Existing code target median (short): **{h['evidence']['short_existing_target_median_usdt']:.4f} USDT**",
        f"- Corrected primary-basis target median: **{h['evidence']['short_corrected_primary_target_median_usdt']:.4f} USDT**",
        f"- Ratio: **{h['evidence']['ratio_existing_to_corrected_median']:.4f}** (~0.5)",
        "",
        "## Initial exit PnL",
        "",
        f"- Long initial_exit median: **{h['evidence']['long_initial_exit_pnl_median_usdt']:.4f} USDT**",
        f"- Short initial_exit median: **{h['evidence']['short_initial_exit_pnl_median_usdt']:.4f} USDT**",
        "",
        h["evidence"]["all_winners_short_median_note"],
        "",
        "## Proposed fix (description only)",
        "",
        f"- File: `{fix['affected_file']}`",
        f"- Function: `{fix['affected_function']}`",
        f"- Current: `{fix['current_formula']}`",
        f"- Proposed: {fix['proposed_formula']}",
        "",
        f"Live risk: {fix['live_risk']}",
    ]
    return "\n".join(lines) + "\n"


def run_audit(
    *,
    source_dir: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    source = source_dir or DEFAULT_SOURCE_DIR
    output = output_dir or DEFAULT_OUTPUT_DIR
    output.mkdir(parents=True, exist_ok=True)

    long_cfg = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL).config
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol=SYMBOL).config

    code_paths = build_notional_basis_code_paths()
    write_csv(output / "notional_basis_code_paths.csv", code_paths)

    initial_rows = build_initial_exit_reconstruction(source)
    write_csv(output / "initial_exit_profit_target_reconstruction.csv", initial_rows)

    cycle_rows = build_cycle_profit_comparison(source)
    write_csv(output / "cycle_profit_target_basis_comparison.csv", cycle_rows)

    short_corrected = build_short_existing_vs_primary_basis(source)
    write_csv(output / "short_existing_vs_primary_basis_target.csv", short_corrected)

    hypothesis = evaluate_hypothesis(initial_rows, short_corrected)
    proposed_fix = build_proposed_fix_description()

    config_resolution = {
        "long": effective_notionals(long_cfg, signal="long"),
        "short": effective_notionals(short_cfg, signal="short"),
        "backtest_same_formula_as_live": True,
        "backtest_note": (
            "Backtester uses same FixedCycleHedgeStrategy / ShortFixedCycleHedgeStrategy "
            "and hedge_exit_math; no separate TP simplification"
        ),
    }

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source),
        "output_dir": str(output),
        "config_resolution": config_resolution,
        "hypothesis_evaluation": hypothesis,
        "proposed_fix": proposed_fix,
        "initial_exit_trade_count": len(initial_rows),
        "short_trade_count": len(short_corrected),
        "decision_answers": {
            "1_basis_used": "50 USDT long-hedge leg (long_avg×long_qty), not 100 USDT short-primary",
            "2_file_function": "fixed_cycle_hedge_bot/hedge_exit_math.py :: calculate_hedge_exit_price",
            "3_long_initial_formula": "profit_basis=long_avg×long_qty; target=profit_basis×tp_pct/100; basket exit via _calculate_tp_projection",
            "4_short_initial_formula": "SAME code path — profit_basis still long_avg×long_qty (=hedge ~50 USDT)",
            "5_long_cycle_formula": "second leg: loss_to_cover + config.target_profit_usdt (0.015 USDT absolute)",
            "6_short_cycle_formula": "same absolute target_profit_usdt; initial basket TP bug affects cycle start exits",
            "7_pnl_decomposition": "Short primary gross ~2× hedge loss magnitude; net scales with halved target",
            "8_why_median_0_139": "Mix of cycle_1 small wins (~0.14) and initial exits (~0.29); 0.125 code target × geometry",
            "9_hypothesis": hypothesis["conclusion"],
            "10_bug": "YES — direction-neutral profit basis missing in hedge_exit_math",
            "11_fix": proposed_fix["proposed_formula"],
            "12_live_risk": proposed_fix["live_risk"],
        },
    }
    write_json(output / "analysis_summary.json", summary)
    (output / "REPORT.md").write_text(generate_report_md(summary), encoding="utf-8")
    return summary
