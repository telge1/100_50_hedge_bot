from __future__ import annotations

"""
Long-only gap reduction scenario for backtest_long_continuous_trade_0012.

This runner:
- uses the same candle window and config as the confirmed addon-recovery audit
- runs a baseline backtest with addon-short recovery disabled
- uses that baseline state after Cycle 3 to simulate a purely hypothetical
  long-only gap reduction (1% trigger steps, 25% of remaining long size)
- compares three variants at the end of the series:
  1) Keine weitere Recovery (Baseline ohne neue Reduktionen)
  2) Bestehende Addon-Short-Recovery (Original-0012-Audit)
  3) Long-only-Gap-Reduction (dieses Skript)

It writes:
- trade_0012_long_gap_reduction_1pct_25pct_events.csv
- trade_0012_long_gap_reduction_1pct_25pct_summary.json

This code is backtest-only and does not modify the live bot or strategy logic.
"""

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .addon_short_recovery import AddonShortRecoveryConfig, default_addon_short_recovery_config
from .backtest_report import BacktestResult
from .historical_backtest import run_historical_backtest
from .long_gap_reduction import LongGapReductionConfig, simulate_long_gap_reduction
from .run_addon_recovery_trade_0012_audit import (  # type: ignore[import]
    SYMBOL,
    DIRECTION,
    START_INDEX,
    END_INDEX,
    FILL_MODEL,
    MAX_FILLS_PER_CANDLE,
    CONFIG_SOURCE,
    TP_PROFIT_TARGET_PCT,
    _load_and_slice_candles,
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
)


BASE_RESULTS_DIR = Path("research/backtests/results").resolve()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _load_addon_recovery_baseline() -> BacktestResult:
    """
    Load the existing addon-recovery baseline BacktestResult for trade 0012.

    We reuse the confirmed audit run rather than recomputing it.
    """
    base_dir = (
        BASE_RESULTS_DIR
        / "addon_recovery_trade_0012_full_audit"
        / "run_20260708T174903.535501_0000"
    )
    path = base_dir / "trade_0012_result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Minimal BacktestResult reconstruction: we only need summary fields.
    result = BacktestResult(
        symbol=payload["symbol"],
        direction=payload["direction"],
    )
    result.realized_pnl = float(payload.get("realized_pnl") or 0.0)
    result.unrealized_pnl = float(payload.get("unrealized_pnl") or 0.0)
    result.overall_pnl = float(payload.get("overall_pnl") or 0.0)
    result.final_long_qty = float(payload.get("final_long_qty") or 0.0)
    result.final_short_qty = float(payload.get("final_short_qty") or 0.0)
    result.addon_short_recovery_completed = bool(
        payload.get("addon_short_recovery_completed") or False
    )
    result.addon_short_recovery_completion_reason = payload.get(
        "addon_short_recovery_completion_reason"
    )
    result.addon_short_trade_count = int(payload.get("addon_short_trade_count") or 0)
    result.addon_short_tp_count = int(payload.get("addon_short_tp_count") or 0)
    result.addon_short_rebound_exit_count = int(
        payload.get("addon_short_rebound_exit_count") or 0
    )
    result.addon_short_hard_stop_count = int(
        payload.get("addon_short_hard_stop_count") or 0
    )
    result.addon_short_long_reduce_total_qty = float(
        payload.get("addon_short_long_reduce_total_qty") or 0.0
    )
    result.addon_short_long_reduce_total_pnl = float(
        payload.get("addon_short_long_reduce_total_pnl") or 0.0
    )
    result.addon_short_net_realized_pnl = float(
        payload.get("addon_short_net_realized_pnl") or 0.0
    )
    return result


def _run_no_recovery_baseline(
    trade_candles: List[Any],
) -> BacktestResult:
    """Run trade 0012 with addon-short recovery disabled."""
    cfg = default_addon_short_recovery_config()
    cfg.enabled = False
    result = run_historical_backtest(
        SYMBOL.upper(),
        DIRECTION,
        trade_candles,
        max_candles=len(trade_candles),
        fill_model=FILL_MODEL,
        max_fills_per_candle=MAX_FILLS_PER_CANDLE,
        initial_notional_usdt=100.0,
        config_source=CONFIG_SOURCE,
        long_config_path=DEFAULT_LONG_CONFIG_PATH,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        file_config_path=None,
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        addon_short_recovery_config=cfg,
        audit_recorder=None,
    )
    return result


def _find_cycle3_state(result: BacktestResult) -> Tuple[int, float, float, float, float]:
    """
    Find the state directly after CYCLE_3_SHORT_REDUCE in the no-recovery run.

    Returns:
        (local_candle_index, long_qty_after, short_qty_after, long_avg_after, short_avg_after)
    """
    cycle3_fill = None
    for row in result.fill_log or []:
        if row.get("purpose") == "CYCLE_3_SHORT_REDUCE":
            cycle3_fill = row
    if cycle3_fill is None:
        raise RuntimeError("CYCLE_3_SHORT_REDUCE not found in fill_log")

    local_candle_index = int(cycle3_fill["candle_index"])
    long_qty_after = float(cycle3_fill.get("long_qty_after") or 0.0)
    short_qty_after = float(cycle3_fill.get("short_qty_after") or 0.0)
    long_avg_after = float(cycle3_fill.get("long_avg_after") or 0.0)
    short_avg_after = float(cycle3_fill.get("short_avg_after") or 0.0)
    return local_candle_index, long_qty_after, short_qty_after, long_avg_after, short_avg_after


def run_long_gap_reduction_trade_0012_audit() -> Dict[str, Any]:
    """Main orchestrator for the long-only gap reduction scenario."""
    trade_candles, candle_meta = _load_and_slice_candles()

    # 1) Baseline ohne neue Recovery (Addon disabled).
    no_recovery_result = _run_no_recovery_baseline(trade_candles)

    # 2) Bestehende Addon-Short-Recovery-Baseline laden.
    addon_baseline = _load_addon_recovery_baseline()

    # 3) Startzustand nach Cycle 3 aus no-recovery-Run.
    c3_idx, long_qty_c3, short_qty_c3, long_avg_c3, short_avg_c3 = _find_cycle3_state(
        no_recovery_result
    )
    # Referenzpreis: Fillpreis der CYCLE_3_SHORT_REDUCE.
    c3_fill_price = None
    for row in no_recovery_result.fill_log or []:
        if row.get("purpose") == "CYCLE_3_SHORT_REDUCE":
            c3_fill_price = float(row.get("fill_price") or 0.0)
    if c3_fill_price is None:
        raise RuntimeError("CYCLE_3_SHORT_REDUCE fill_price not found")

    # 4) Long-only-Gap-Reduction-Szenario simulieren.
    # Use the same fee_rate as the underlying backtest if available; otherwise
    # fall back to the strategy default (order_fee_rate_pct=0.055% => 0.00055).
    fee_rate: float | None = None
    for row in no_recovery_result.fill_log or []:
        if row.get("purpose") == "CYCLE_3_SHORT_REDUCE":
            meta = row.get("metadata") or {}
            raw_rate = meta.get("runtime_fee_rate") or meta.get("fee_rate")
            if raw_rate is not None:
                try:
                    fee_rate = float(raw_rate)
                except (TypeError, ValueError):
                    fee_rate = None
            break

    if fee_rate is None:
        fee_rate = 0.00055

    cfg = LongGapReductionConfig(step_trigger_pct=1.0, num_steps=4, fee_rate=fee_rate)
    events, lg_summary = simulate_long_gap_reduction(
        candles=trade_candles,
        start_local_candle_index=c3_idx,
        absolute_start_index=START_INDEX,
        initial_long_qty=long_qty_c3,
        initial_short_qty=short_qty_c3,
        long_avg=long_avg_c3,
        short_avg=short_avg_c3,
        reference_price=c3_fill_price,
        base_main_realized_pnl=float(no_recovery_result.realized_pnl or 0.0),
        cfg=cfg,
    )

    # 5) Outputs schreiben.
    run_dir = BASE_RESULTS_DIR
    events_path = run_dir / "trade_0012_long_gap_reduction_1pct_25pct_events.csv"
    summary_path = run_dir / "trade_0012_long_gap_reduction_1pct_25pct_summary.json"

    if events:
        with events_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(events[0].keys()))
            writer.writeheader()
            writer.writerows(events)

    # 6) Summaries und Variantenvergleich.
    # Baseline ohne neue Recovery (Positionen nach C3 unverändert halten).
    final_long_no = float(no_recovery_result.final_long_qty or 0.0)
    final_short_no = float(no_recovery_result.final_short_qty or 0.0)
    remaining_gap_no = max(final_long_no - final_short_no, 0.0)

    no_recovery_summary = {
        "variant": "no_recovery_after_cycle3",
        "realized_pnl": float(no_recovery_result.realized_pnl or 0.0),
        "unrealized_pnl": float(no_recovery_result.unrealized_pnl or 0.0),
        "total_pnl": float(no_recovery_result.overall_pnl or 0.0),
        "final_long_qty": final_long_no,
        "final_short_qty": final_short_no,
        "remaining_gap": remaining_gap_no,
    }

    # Bestehende Addon-Short-Recovery.
    final_long_addon = float(addon_baseline.final_long_qty or 0.0)
    final_short_addon = float(addon_baseline.final_short_qty or 0.0)
    remaining_gap_addon = max(final_long_addon - final_short_addon, 0.0)
    addon_summary = {
        "variant": "addon_short_recovery",
        "realized_pnl": float(addon_baseline.realized_pnl or 0.0),
        "unrealized_pnl": float(addon_baseline.unrealized_pnl or 0.0),
        "total_pnl": float(addon_baseline.overall_pnl or 0.0),
        "final_long_qty": final_long_addon,
        "final_short_qty": final_short_addon,
        "remaining_gap": remaining_gap_addon,
        "addon_short_trade_count": int(addon_baseline.addon_short_trade_count or 0),
        "addon_short_tp_count": int(addon_baseline.addon_short_tp_count or 0),
        "addon_short_rebound_exit_count": int(
            addon_baseline.addon_short_rebound_exit_count or 0
        ),
        "addon_short_hard_stop_count": int(
            addon_baseline.addon_short_hard_stop_count or 0
        ),
        "addon_short_long_reduce_total_qty": float(
            addon_baseline.addon_short_long_reduce_total_qty or 0.0
        ),
        "addon_short_long_reduce_total_pnl": float(
            addon_baseline.addon_short_long_reduce_total_pnl or 0.0
        ),
        "addon_short_net_realized_pnl": float(
            addon_baseline.addon_short_net_realized_pnl or 0.0
        ),
    }

    # Long-only-Gap-Reduction (Szenario).
    # Endzustand aus letztem Event.
    final_ev = events[-1] if events else {}
    long_only_summary = {
        "variant": "long_gap_reduction_1pct_25pct",
        "start_state": {
            "cycle3_candle_index": c3_idx,
            "cycle3_fill_price": c3_fill_price,
            "long_qty_after_cycle3": long_qty_c3,
            "short_qty_after_cycle3": short_qty_c3,
            "long_avg": long_avg_c3,
            "short_avg": short_avg_c3,
            "base_main_realized_pnl": float(no_recovery_result.realized_pnl or 0.0),
        },
        "events_count": lg_summary.get("events"),
        "initial_gap_qty": lg_summary.get("initial_gap_qty"),
        "planned_gap_reduce_qty_per_step": lg_summary.get(
            "planned_gap_reduce_qty_per_step"
        ),
        "total_reduced_long_qty": lg_summary.get("total_reduced_qty"),
        "gap_reduction_gross_pnl": lg_summary.get("total_gap_reduction_gross_pnl"),
        "gap_reduction_fees": lg_summary.get("total_gap_reduction_fees"),
        "gap_reduction_net_pnl": lg_summary.get("total_gap_reduction_net_pnl"),
        "final_long_qty": lg_summary.get("final_long_qty"),
        "final_short_qty": lg_summary.get("final_short_qty"),
        "remaining_gap": lg_summary.get("remaining_gap_qty"),
        "gap_fully_closed": lg_summary.get("gap_fully_closed"),
        "final_unrealized_long_pnl": final_ev.get("unrealized_long_pnl"),
        "final_unrealized_short_pnl": final_ev.get("unrealized_short_pnl"),
        "final_combined_unrealized_pnl": final_ev.get("combined_unrealized_pnl"),
        "final_total_trade_pnl": final_ev.get("total_trade_pnl"),
    }

    comparison_table = [
        {
            "variant": "no_recovery",
            "realized": no_recovery_summary["realized_pnl"],
            "unrealized": no_recovery_summary["unrealized_pnl"],
            "total_pnl": no_recovery_summary["total_pnl"],
            "long_qty": no_recovery_summary["final_long_qty"],
            "short_qty": no_recovery_summary["final_short_qty"],
            "remaining_gap": no_recovery_summary["remaining_gap"],
        },
        {
            "variant": "addon_short_recovery",
            "realized": addon_summary["realized_pnl"],
            "unrealized": addon_summary["unrealized_pnl"],
            "total_pnl": addon_summary["total_pnl"],
            "long_qty": addon_summary["final_long_qty"],
            "short_qty": addon_summary["final_short_qty"],
            "remaining_gap": addon_summary["remaining_gap"],
        },
        {
            "variant": "long_gap_reduction_1pct_25pct",
            "realized": long_only_summary["final_total_trade_pnl"]
            - (final_ev.get("combined_unrealized_pnl") or 0.0)
            if events
            else None,
            "unrealized": long_only_summary["final_combined_unrealized_pnl"],
            "total_pnl": long_only_summary["final_total_trade_pnl"],
            "long_qty": long_only_summary["final_long_qty"],
            "short_qty": long_only_summary["final_short_qty"],
            "remaining_gap": long_only_summary["remaining_gap"],
        },
    ]

    summary_payload = {
        "no_recovery": no_recovery_summary,
        "addon_short_recovery": addon_summary,
        "long_gap_reduction": long_only_summary,
        "comparison_table": comparison_table,
    }
    _write_json(summary_path, summary_payload)

    return {
        "events_path": events_path,
        "summary_path": summary_path,
    }


def main(argv: List[str] | None = None) -> int:
    try:
        outputs = run_long_gap_reduction_trade_0012_audit()
        print("Long-gap reduction outputs:")
        for key, path in outputs.items():
            print(f"  {key}: {path}")
        return 0
    except Exception as exc:  # pragma: no cover
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

