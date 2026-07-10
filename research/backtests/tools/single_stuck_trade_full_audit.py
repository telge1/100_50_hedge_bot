from __future__ import annotations

"""
Single-trade full audit for Blocker Addon Short Recovery (Phase 3).

This module is backtest-only. It does not modify strategy, recovery rules,
fill models, or live-bot code paths. It consumes existing continuous backtest
artifacts plus the Phase-1/2 addon audit and optionally an instrumented
runtime re-run to produce a dense, economic PnL-focused audit for one trade.
"""

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Optional
import argparse
import csv
import json

from ..backtest_report import BacktestResult
from ..candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from ..historical_backtest import normalize_candles, run_historical_backtest
from ..backtest_audit_recorder import BacktestAuditRecorder, FillAuditRecord, AddonAuditRecord
from ..addon_short_recovery import AddonShortRecoveryConfig
from ..tools import addon_recovery_audit


@dataclass
class TradeIdentity:
    trade_block_id: str
    symbol: str
    direction: str
    start_index: int | None
    end_index: int | None
    start_time: str | None
    end_time: str | None
    fill_model: str | None
    max_fills_per_candle: int | None

    def to_signature_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EconomicComponents:
    main_realized_pnl: float
    addon_short_realized_pnl: float
    addon_long_reduce_realized_pnl: float
    economic_realized_pnl: float
    main_unrealized_long_pnl: float | None
    main_unrealized_short_pnl: float | None
    addon_short_unrealized_pnl: float | None
    economic_unrealized_pnl: float | None
    economic_total_pnl: float | None


@dataclass
class AuditCheck:
    check_id: str
    event_sequence: int | None
    description: str
    expected_value: Any
    actual_value: Any
    absolute_difference: float | None
    tolerance: float | None
    passed: bool
    expected_value_source: str
    actual_value_source: str
    independence_level: str
    check_is_circular: bool
    check_is_consistency_only: bool

    def to_row(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "event_sequence": self.event_sequence,
            "description": self.description,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "absolute_difference": self.absolute_difference,
            "tolerance": self.tolerance,
            "passed": self.passed,
            "expected_value_source": self.expected_value_source,
            "actual_value_source": self.actual_value_source,
            "independence_level": self.independence_level,
            "check_is_circular": self.check_is_circular,
            "check_is_consistency_only": self.check_is_consistency_only,
        }


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_trade_identity(run: dict[str, Any]) -> TradeIdentity:
    return TradeIdentity(
        trade_block_id=str(run.get("trade_block_id") or ""),
        symbol=str(run.get("symbol") or ""),
        direction=str(run.get("direction") or ""),
        start_index=int(run.get("start_index")) if run.get("start_index") is not None else None,
        end_index=int(run.get("end_index")) if run.get("end_index") is not None else None,
        start_time=str(run.get("start_time")) if run.get("start_time") is not None else None,
        end_time=str(run.get("end_time")) if run.get("end_time") is not None else None,
        fill_model=str(run.get("fill_model")) if run.get("fill_model") is not None else None,
        max_fills_per_candle=(
            int(run.get("max_fills_per_candle"))
            if run.get("max_fills_per_candle") is not None
            else None
        ),
    )


def _load_addon_audit_payload(results_dir: Path, trade_block_id: str) -> dict[str, Any]:
    """
    Ensure the Phase-2 addon audit exists (via run_single_trade_audit) and load its JSON.
    """
    paths = addon_recovery_audit.run_single_trade_audit(
        results_dir=str(results_dir),
        trade_block_id=trade_block_id,
    )
    audit_json = paths["audit_json"]
    with audit_json.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_addon_config_from_run(run: dict[str, Any]) -> AddonShortRecoveryConfig:
    """
    Reconstruct a backtest-only AddonShortRecoveryConfig from stored BacktestResult fields.
    """
    cfg = AddonShortRecoveryConfig()
    cfg.enabled = True
    # For Phase 3 we only read fields that are known to be present on BacktestResult
    # or set sensible defaults for others. This does not change strategy logic.
    if run.get("addon_short_step_fraction") is not None:
        cfg.addon_short_step_fraction = float(run["addon_short_step_fraction"])
    if run.get("allow_net_short") is not None:
        cfg.allow_net_short = bool(run["allow_net_short"])
    if run.get("addon_short_tp_pct") is not None:
        cfg.addon_short_tp_pct = float(run["addon_short_tp_pct"])
    if run.get("addon_short_reentry_buffer_pct") is not None:
        cfg.addon_short_reentry_buffer_pct = float(run["addon_short_reentry_buffer_pct"])
    if run.get("addon_short_min_favorable_move_pct") is not None:
        cfg.addon_short_min_favorable_move_pct = float(run["addon_short_min_favorable_move_pct"])
    if run.get("addon_short_rebound_close_pct") is not None:
        cfg.addon_short_rebound_close_pct = float(run["addon_short_rebound_close_pct"])
    if run.get("addon_short_hard_stop_pct") is not None:
        cfg.addon_short_hard_stop_pct = float(run["addon_short_hard_stop_pct"])
    if run.get("long_reduce_profit_usage_fraction") is not None:
        cfg.long_reduce_profit_usage_fraction = float(run["long_reduce_profit_usage_fraction"])
    if run.get("stop_when_long_qty_reaches_normal_short_qty") is not None:
        cfg.stop_when_long_qty_reaches_normal_short_qty = bool(
            run["stop_when_long_qty_reaches_normal_short_qty"]
        )
    if run.get("addon_short_recovery_activation_order") is not None:
        cfg.activation_order = str(run["addon_short_recovery_activation_order"])
    return cfg


def _rerun_instrumented_backtest_for_trade(
    *,
    continuous_results_json: Path,
    addon_run: dict[str, Any],
    output_dir: Path,
) -> tuple[BacktestResult, BacktestAuditRecorder]:
    """
    Best-effort instrumented re-run for a single trade window using BacktestAuditRecorder.

    This uses the same candle series, direction, config_source and fill model as the
    continuous run, but is limited to the trade's [start_index, end_index] window.
    """
    payload = json.loads(continuous_results_json.read_text(encoding="utf-8"))
    metadata = payload.get("metadata") or {}
    symbol = str(metadata.get("symbol") or addon_run.get("symbol") or "")
    symbol_upper = symbol.upper()
    direction = str(addon_run.get("direction") or "long")
    fill_model = str(metadata.get("fill_model") or addon_run.get("fill_model") or "conservative")
    max_fills_per_candle = metadata.get("max_fills_per_candle")
    config_source = metadata.get("config_source") or "test"
    candles_loaded = int(metadata.get("candles_loaded") or 0)

    # Load the same candle window as the continuous run used.
    candle_rows = load_candles_for_symbol(
        symbol_upper,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=candles_loaded,
    )
    candle_list = normalize_candles(symbol_upper, candle_rows)

    start_index = int(addon_run.get("start_index") or 0)
    end_index = int(addon_run.get("end_index") or start_index)
    if start_index < 0 or end_index < start_index or end_index >= len(candle_list):
        raise ValueError(
            f"invalid trade window for {addon_run.get('trade_block_id')}: "
            f"start_index={start_index} end_index={end_index} candles={len(candle_list)}"
        )

    trade_candles = candle_list[start_index : end_index + 1]
    recorder = BacktestAuditRecorder(enabled=True)
    addon_cfg = _build_addon_config_from_run(addon_run)

    result = run_historical_backtest(
        symbol_upper,
        direction,
        trade_candles,
        max_candles=len(trade_candles),
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
        config_source=config_source,
        long_config_path=metadata.get("long_config_path") or "",
        short_config_path=metadata.get("short_config_path") or "",
        file_config_path=metadata.get("file_config_path"),
        tp_profit_target_pct=None,
        addon_short_recovery_config=addon_cfg,
        audit_recorder=recorder,
    )

    # Persist runtime records for reuse.
    runtime_path = output_dir / f"{addon_run.get('trade_block_id')}_runtime_records.jsonl"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    with runtime_path.open("w", encoding="utf-8") as handle:
        for rec in recorder.fills:
            obj = {"record_type": "fill", **asdict(rec)}
            handle.write(json.dumps(obj, ensure_ascii=False))
            handle.write("\n")
        for rec in recorder.addon_events:
            obj = {"record_type": "addon", **asdict(rec)}
            handle.write(json.dumps(obj, ensure_ascii=False))
            handle.write("\n")

    return result, recorder


def _compute_economic_components_from_phase2(
    *,
    run: dict[str, Any],
    phase2: dict[str, Any],
) -> EconomicComponents:
    """Compute economic realized/unrealized PnL components from Phase-2 aggregates."""
    main_breakdown = phase2["main_realized_pnl_breakdown"]
    addon_checks = phase2["addon_aggregate_checks"]
    long_reduce_checks = phase2["long_reduce_aggregate_checks"]

    main_realized = float(main_breakdown["main_realized_pnl_without_addon_long_reduces"] or 0.0)
    addon_short_realized = float(addon_checks["reconstructed_addon_net_realized_pnl"] or 0.0)
    addon_long_reduce_realized = float(long_reduce_checks["reconstructed_long_reduce_total_pnl"] or 0.0)
    economic_realized = main_realized + addon_short_realized + addon_long_reduce_realized

    # Unrealized components use BacktestResult semantics at series end.
    long_unreal = _safe_float(run.get("unrealized_long_pnl"))
    short_unreal = _safe_float(run.get("unrealized_short_pnl"))
    # Addon shorts are fully realized on close; unrealized component is only for open addon.
    addon_unreal: float | None = None
    if run.get("addon_short_trade_count"):
        # Offline run stores only realized addon PnL; unrealized component is 0 by design.
        addon_unreal = 0.0

    parts: list[float] = []
    if long_unreal is not None:
        parts.append(long_unreal)
    if short_unreal is not None:
        parts.append(short_unreal)
    if addon_unreal is not None:
        parts.append(addon_unreal)
    economic_unreal = sum(parts) if parts else None

    economic_total: float | None
    if economic_unreal is not None:
        economic_total = economic_realized + economic_unreal
    else:
        economic_total = None

    return EconomicComponents(
        main_realized_pnl=main_realized,
        addon_short_realized_pnl=addon_short_realized,
        addon_long_reduce_realized_pnl=addon_long_reduce_realized,
        economic_realized_pnl=economic_realized,
        main_unrealized_long_pnl=long_unreal,
        main_unrealized_short_pnl=short_unreal,
        addon_short_unrealized_pnl=addon_unreal,
        economic_unrealized_pnl=economic_unreal,
        economic_total_pnl=economic_total,
    )


def _build_basic_checks_from_phase2(
    *,
    run: dict[str, Any],
    phase2: dict[str, Any],
) -> list[AuditCheck]:
    """
    Convert key Phase-2 aggregate comparisons into structured AuditChecks.

    Diese Checks sind primär Konsistenzprüfungen gegen bereits gespeicherte
    BacktestResult-Aggregate; sie gelten daher formal als
    independence_level='consistency_only'.
    """
    checks: list[AuditCheck] = []
    abs_tol = 1e-9

    def _mk(label: str, expected: float, actual: float, source: str) -> AuditCheck:
        diff = expected - actual
        passed = abs(diff) <= abs_tol
        return AuditCheck(
            check_id=label.upper(),
            event_sequence=None,
            description=f"{label} comparison ({source})",
            expected_value=expected,
            actual_value=actual,
            absolute_difference=diff,
            tolerance=abs_tol,
            passed=passed,
            expected_value_source="reconstructed_from_events",
            actual_value_source="stored_backtest_result",
            independence_level="consistency_only",
            check_is_circular=False,
            check_is_consistency_only=True,
        )

    addon_agg = phase2["addon_aggregate_checks"]
    main_breakdown = phase2["main_realized_pnl_breakdown"]
    long_reduce_agg = phase2["long_reduce_aggregate_checks"]

    # Addon net realized PnL vs stored.
    expected_net = float(addon_agg["reconstructed_addon_net_realized_pnl"] or 0.0)
    actual_net = float(run.get("addon_short_net_realized_pnl") or 0.0)
    checks.append(_mk("PNL_ADDON_AGG_NET_REALIZED", expected_net, actual_net, "addon_phase2"))

    # Long-reduce aggregate qty/pnl vs stored.
    expected_qty = float(long_reduce_agg["reconstructed_long_reduce_total_qty"] or 0.0)
    actual_qty = float(run.get("addon_short_long_reduce_total_qty") or 0.0)
    checks.append(_mk("PNL_LONG_REDUCE_AGG_QTY", expected_qty, actual_qty, "addon_phase2"))

    expected_pnl = float(long_reduce_agg["reconstructed_long_reduce_total_pnl"] or 0.0)
    actual_pnl = float(run.get("addon_short_long_reduce_total_pnl") or 0.0)
    checks.append(_mk("PNL_LONG_REDUCE_AGG_PNL", expected_pnl, actual_pnl, "addon_phase2"))

    # Main realized PnL (without addon long-reduces) vs stored realized_pnl.
    expected_main = float(main_breakdown["main_realized_pnl_without_addon_long_reduces"] or 0.0)
    actual_main = float(run.get("realized_pnl") or 0.0)
    checks.append(_mk("PNL_MAIN_REALIZED_NO_ADDON", expected_main, actual_main, "main_phase2"))

    return checks


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_single_stuck_trade_full_audit(
    *,
    results_dir: Path,
    trade_block_id: str,
    symbol: str = "APTUSDT",
    direction: str = "long",
    output_dir: Path | None = None,
    rerun_instrumented: bool = False,
    strict: bool = True,
) -> dict[str, Path]:
    """
    High-level orchestration for a single-trade full audit.

    - Reuses Phase-1/2 addon audit (addon_recovery_audit.run_single_trade_audit)
    - Optionally re-runs the trade window with BacktestAuditRecorder enabled
      to persist runtime records (FillAuditRecord, AddonAuditRecord)
    - Computes economic PnL components and structured audit checks
    - Writes human- and machine-readable artifacts for downstream analysis
    """
    base_results = results_dir
    if output_dir is None:
        output_dir = base_results / "single_stuck_trade_full_audit"
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) Load / create Phase-2 addon audit payload.
    addon_payload = _load_addon_audit_payload(base_results, trade_block_id)
    run = addon_payload["run"]
    phase2 = addon_payload["phase2"]
    events = addon_payload["events"]

    identity = _build_trade_identity(run)

    # 2) Optional instrumented re-run for runtime audit records.
    runtime_result: BacktestResult | None = None
    recorder: BacktestAuditRecorder | None = None
    continuous_results_json = base_results / f"{symbol.upper()}_original_hedge_5m_continuous_results.json"
    if rerun_instrumented and continuous_results_json.exists():
        runtime_result, recorder = _rerun_instrumented_backtest_for_trade(
            continuous_results_json=continuous_results_json,
            addon_run=run,
            output_dir=output_dir,
        )
        # If strict, verify that core aggregates match original run.
        if strict and runtime_result is not None:
            if abs(float(runtime_result.realized_pnl) - float(run.get("realized_pnl") or 0.0)) > 1e-6:
                raise ValueError(
                    "instrumented re-run realized_pnl does not match stored run "
                    f"for {trade_block_id}"
                )

    # 3) Economic PnL components from Phase-2 aggregates.
    econ = _compute_economic_components_from_phase2(run=run, phase2=phase2)

    # 4) Build structured checks (currently based on Phase-2 aggregates).
    checks = _build_basic_checks_from_phase2(run=run, phase2=phase2)

    # 5) Write core output files for this trade.
    suffix = trade_block_id.replace("backtest_", "")
    prefix = f"trade_{suffix}"

    # a) Economic PnL timeline: for now just one row at series-end using Phase-2 aggregates.
    econ_rows = [
        {
            "trade_block_id": identity.trade_block_id,
            "symbol": identity.symbol,
            "direction": identity.direction,
            "start_index": identity.start_index,
            "end_index": identity.end_index,
            "main_realized_pnl": econ.main_realized_pnl,
            "addon_short_realized_pnl": econ.addon_short_realized_pnl,
            "addon_long_reduce_realized_pnl": econ.addon_long_reduce_realized_pnl,
            "economic_realized_pnl": econ.economic_realized_pnl,
            "main_unrealized_long_pnl": econ.main_unrealized_long_pnl,
            "main_unrealized_short_pnl": econ.main_unrealized_short_pnl,
            "addon_short_unrealized_pnl": econ.addon_short_unrealized_pnl,
            "economic_unrealized_pnl": econ.economic_unrealized_pnl,
            "economic_total_pnl": econ.economic_total_pnl,
        }
    ]
    econ_csv = output_dir / f"{prefix}_economic_pnl_timeline.csv"
    _write_csv(econ_csv, econ_rows)

    # b) Raw addon events timeline from Phase-2 audit for convenience.
    events_csv = output_dir / f"{prefix}_addon_event_timeline.csv"
    _write_csv(events_csv, (ev for ev in addon_payload["events"]))

    # c) Audit checks and failures.
    checks_csv = output_dir / f"{prefix}_audit_checks.csv"
    _write_csv(checks_csv, (c.to_row() for c in checks))
    failures_csv = output_dir / f"{prefix}_audit_failures.csv"
    _write_csv(
        failures_csv,
        (c.to_row() for c in checks if not c.passed),
    )

    # d) Independence checks JSON (currently identical to checks but structured).
    independence_json = output_dir / f"{prefix}_independence_checks.json"
    independence_payload = {
        "trade_block_id": identity.trade_block_id,
        "trade_identity_signature": identity.to_signature_dict(),
        "independence_checks": [c.to_row() for c in checks],
    }
    independence_json.write_text(
        json.dumps(independence_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # e) Summary JSON + Markdown.
    summary_json = output_dir / f"{prefix}_summary.json"
    summary_md = output_dir / f"{prefix}_summary.md"
    audit_failures_count = sum(1 for c in checks if not c.passed)
    independent_checks = sum(1 for c in checks if c.independence_level == "independent")
    circular_checks = sum(1 for c in checks if c.check_is_circular)

    summary_payload = {
        "trade_block_id": identity.trade_block_id,
        "trade_identity_signature": identity.to_signature_dict(),
        "economic_pnl": asdict(econ),
        "audit_checks_total": len(checks),
        "audit_failures": audit_failures_count,
        "independent_checks": independent_checks,
        "circular_checks": circular_checks,
    }
    summary_json.write_text(
        json.dumps(summary_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with summary_md.open("w", encoding="utf-8") as handle:
        handle.write(f"# Full Audit Summary for {identity.trade_block_id}\n\n")
        handle.write("## Trade Identity\n\n")
        handle.write(f"- symbol: `{identity.symbol}`\n")
        handle.write(f"- direction: `{identity.direction}`\n")
        handle.write(f"- trade_block_id: `{identity.trade_block_id}`\n")
        handle.write(f"- start_index: {identity.start_index}\n")
        handle.write(f"- end_index: {identity.end_index}\n")
        handle.write(f"- start_time: {identity.start_time}\n")
        handle.write(f"- end_time: {identity.end_time}\n")
        handle.write("\n## Economic PnL\n\n")
        handle.write(f"- main_realized_pnl: {econ.main_realized_pnl}\n")
        handle.write(f"- addon_short_realized_pnl: {econ.addon_short_realized_pnl}\n")
        handle.write(f"- addon_long_reduce_realized_pnl: {econ.addon_long_reduce_realized_pnl}\n")
        handle.write(f"- economic_realized_pnl: {econ.economic_realized_pnl}\n")
        handle.write(f"- economic_unrealized_pnl: {econ.economic_unrealized_pnl}\n")
        handle.write(f"- economic_total_pnl: {econ.economic_total_pnl}\n")
        handle.write("\n## Audit Checks\n\n")
        handle.write(f"- checks_total: {len(checks)}\n")
        handle.write(f"- failures: {audit_failures_count}\n")
        handle.write(f"- independent_checks: {independent_checks}\n")
        handle.write(f"- circular_checks: {circular_checks}\n")

        if audit_failures_count:
            first_failure = next(c for c in checks if not c.passed)
            handle.write("\n### First Failure\n\n")
            handle.write(f"- check_id: `{first_failure.check_id}`\n")
            handle.write(f"- description: {first_failure.description}\n")
            handle.write(
                f"- expected={first_failure.expected_value} actual={first_failure.actual_value} "
                f"diff={first_failure.absolute_difference} tol={first_failure.tolerance}\n"
            )

    return {
        "economic_pnl_csv": econ_csv,
        "addon_event_timeline_csv": events_csv,
        "audit_checks_csv": checks_csv,
        "audit_failures_csv": failures_csv,
        "independence_checks_json": independence_json,
        "summary_json": summary_json,
        "summary_md": summary_md,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Single-trade full audit for Blocker Addon Short Recovery.",
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        help="Directory containing *_continuous_results.json and trade_blocks for the run.",
    )
    parser.add_argument(
        "--trade-block-id",
        required=True,
        help="Trade block id to audit, e.g. backtest_long_continuous_trade_0012.",
    )
    parser.add_argument(
        "--symbol",
        default="APTUSDT",
        help="Symbol used for the continuous run (default: APTUSDT).",
    )
    parser.add_argument(
        "--direction",
        default="long",
        choices=["long", "short"],
        help="Direction of the trade (default: long).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for full audit artifacts (default: <results-dir>/single_stuck_trade_full_audit).",
    )
    parser.add_argument(
        "--rerun-instrumented",
        action="store_true",
        help=(
            "Re-run the trade window with BacktestAuditRecorder enabled to persist "
            "runtime Fill/Addon audit records for this trade."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if instrumented re-run does not numerically match stored run aggregates.",
    )
    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir)
    output_dir = Path(args.output_dir) if args.output_dir is not None else None
    try:
        run_single_stuck_trade_full_audit(
            results_dir=results_dir,
            trade_block_id=args.trade_block_id,
            symbol=args.symbol,
            direction=args.direction,
            output_dir=output_dir,
            rerun_instrumented=bool(args.rerun_instrumented),
            strict=bool(args.strict),
        )
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())

