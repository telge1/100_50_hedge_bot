"""Phase D runner: compare causal unlock signals on the Phase-C event manifest."""

from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol

from .config import EmergencyLockRecoveryConfig
from .cost_model import (
    apply_long_open_slippage,
    apply_short_open_slippage,
    conservative_emergency_short_fill_price,
)
from .event_finder import CrashEvent
from .phase_a_runner import _ts_iso
from .phase_c_runner import MODE_ORACLE, phase_b_baseline_config
from .phase_d_signals import (
    PHASE_D_TRADABLE_SIGNALS,
    PROTECTED_STRUCTURE_ADAPTER_AVAILABLE,
    PROTECTED_STRUCTURE_ADAPTER_SKIP_REASON,
    ReboundBaselineSignal,
    build_signal,
)
from .position_ledger import (
    PositionLedger,
    emergency_trigger_price,
    qty_from_notional,
)
from .state_machine import EmergencyLockStateMachine

DEFAULT_PHASE_C_MANIFEST = (
    "research/backtests/results/emergency_lock/phase_c/event_manifest.csv"
)
DEFAULT_PHASE_C_ORACLE = (
    "research/backtests/results/emergency_lock/phase_c/oracle_diagnostic_summary.csv"
)
DEFAULT_PHASE_D_OUTPUT_DIR = "research/backtests/results/emergency_lock/phase_d"

MAIN_RELOCK_VARIANT = "common_pct"
DIAG_RELOCK_VARIANT = "signal_invalidation"


def load_phase_c_events(manifest_path: str | Path = DEFAULT_PHASE_C_MANIFEST) -> list[CrashEvent]:
    path = Path(manifest_path)
    events: list[CrashEvent] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            events.append(
                CrashEvent(
                    event_id=str(row["event_id"]),
                    symbol=str(row["symbol"]),
                    timeframe=str(row["timeframe"]),
                    selection_type=str(row["selection_type"]),
                    peak_index=int(row["peak_index"]),
                    peak_timestamp=row.get("peak_timestamp"),
                    peak_price=float(row["peak_price"]),
                    low_index=int(row["low_index"]),
                    low_timestamp=row.get("low_timestamp"),
                    low_price=float(row["low_price"]),
                    max_drop_pct=float(row["max_drop_pct"]),
                    bars_peak_to_low=int(row["bars_peak_to_low"]),
                    qualified_10_pct=str(row["qualified_10_pct"]).lower() == "true",
                    qualified_12_5_pct=str(row["qualified_12_5_pct"]).lower() == "true",
                    qualified_15_pct=str(row["qualified_15_pct"]).lower() == "true",
                    simulation_start_index=int(row["simulation_start_index"]),
                    simulation_end_index=int(row["simulation_end_index"]),
                    window_truncated_at_data_end=str(
                        row["window_truncated_at_data_end"]
                    ).lower()
                    == "true",
                )
            )
    return events


def load_oracle_flags(path: str | Path = DEFAULT_PHASE_C_ORACLE) -> dict[str, bool]:
    out: dict[str, bool] = {}
    p = Path(path)
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            out[str(row["event_id"])] = str(row.get("oracle_break_even_possible")).lower() in {
                "true",
                "1",
            }
    return out


def phase_d_base_config(*, symbol: str = "APTUSDT") -> EmergencyLockRecoveryConfig:
    cfg = phase_b_baseline_config(symbol=symbol)
    cfg.output_dir = DEFAULT_PHASE_D_OUTPUT_DIR
    return cfg


def run_signal_on_window(
    window: list[dict[str, Any]],
    cfg: EmergencyLockRecoveryConfig,
    *,
    signal_name: str,
    relock_variant: str = MAIN_RELOCK_VARIANT,
) -> dict[str, Any]:
    """Run one event window with a Phase-D unlock signal (or full-lock)."""
    enable_unlock = signal_name != "full_lock_control"
    run_cfg = replace(cfg, enable_unlock=enable_unlock, start_index=0, max_candles=len(window))

    entry_ref = float(window[0]["close"])
    long_fill = apply_long_open_slippage(
        reference_price=entry_ref, slippage_bps=run_cfg.slippage_bps
    )
    short_fill = apply_short_open_slippage(
        reference_price=entry_ref, slippage_bps=run_cfg.slippage_bps
    )
    ledger = PositionLedger()
    ledger.open_long(
        qty=qty_from_notional(
            notional_usdt=run_cfg.initial_long_notional_usdt, price=entry_ref
        ),
        fill_price=long_fill,
        fee_rate=run_cfg.fee_rate,
        reference_price=entry_ref,
    )
    ledger.open_short(
        qty=qty_from_notional(
            notional_usdt=run_cfg.initial_short_notional_usdt, price=entry_ref
        ),
        fill_price=short_fill,
        fee_rate=run_cfg.fee_rate,
        reference_price=entry_ref,
    )
    trigger = emergency_trigger_price(
        long_avg=ledger.long_avg, emergency_trigger_pct=run_cfg.emergency_trigger_pct
    )

    signal = None
    if enable_unlock:
        if signal_name == "rebound_baseline":
            signal = ReboundBaselineSignal()
        else:
            signal = build_signal(signal_name)
        signal.reset()

    sm = EmergencyLockStateMachine(cfg=run_cfg, ledger=ledger)
    sm.emergency_trigger = trigger
    sm.state = "PRE_EMERGENCY"
    sm.unlock_signal = signal
    sm.relock_mode_variant = relock_variant if enable_unlock else MAIN_RELOCK_VARIANT
    sm.simulation_candles = window

    short_avg_after_lock = None
    lock_triggered = False
    lock_offset = None

    for offset, candle in enumerate(window):
        mark = float(candle["close"])
        ts = _ts_iso(candle["timestamp"])
        is_last = offset == len(window) - 1
        sm.simulation_index = offset

        if sm.state == "PRE_EMERGENCY" and float(candle["low"]) <= trigger:
            fill = conservative_emergency_short_fill_price(
                trigger_price=trigger,
                candle_low=float(candle["low"]),
                slippage_bps=run_cfg.slippage_bps,
            )
            ledger.emergency_short_top_up(
                fill_price=fill,
                fee_rate=run_cfg.fee_rate,
                reference_price=trigger,
                qty_tolerance=run_cfg.qty_tolerance,
            )
            sm.enter_full_lock(timestamp=ts, candle=candle, fill_price=fill, mark=mark)
            sm.post_lock_start_index = offset
            short_avg_after_lock = float(ledger.short_avg)
            lock_triggered = True
            lock_offset = offset
        elif sm.state not in {
            "PRE_EMERGENCY",
            "CLOSED_BREAK_EVEN",
            "STOPPED_TIMEOUT",
            "STOPPED_MAX_ADDED_LOSS",
            "OPEN_AT_DATA_END",
        }:
            sm.process_post_lock_bar(
                timestamp=ts,
                candle=candle,
                mark=mark,
                is_last_bar=is_last,
                simulation_index=offset,
            )
        elif sm.state == "PRE_EMERGENCY" and is_last:
            sm._transition(
                timestamp=ts,
                state_to="OPEN_AT_DATA_END",
                action="stop",
                reason="data_end_before_lock",
                basket_pnl_before=ledger.basket_net_pnl(mark),
                basket_pnl_after=ledger.basket_net_pnl(mark),
            )

        if sm.state in {
            "CLOSED_BREAK_EVEN",
            "STOPPED_TIMEOUT",
            "STOPPED_MAX_ADDED_LOSS",
            "OPEN_AT_DATA_END",
        }:
            break

    final_mark = float(window[min(len(window) - 1, sm.simulation_index or 0)]["close"])
    final_status = sm.state
    if not lock_triggered:
        final_status = "NO_EMERGENCY_TRIGGER"

    return {
        "summary": {
            "signal_name": signal_name,
            "relock_variant": relock_variant,
            "lock_triggered": lock_triggered,
            "lock_timestamp": sm.lock_timestamp,
            "lock_price": sm.lock_price,
            "entry_price": entry_ref,
            "entry_timestamp": _ts_iso(window[0]["timestamp"]),
            "basket_pnl_at_lock": sm.basket_pnl_at_lock,
            "short_avg_after_lock": short_avg_after_lock,
            "signal_count": sm.signal_trigger_count,
            "unlock_count": sm.unlock_count,
            "unlock_attempt_count": sm.unlock_attempt_count,
            "relock_count": sm.relock_count,
            "failed_unlocks": sm.failed_unlocks,
            "completed_unlock_stages": sm.next_unlock_stage,
            "minimum_basket_pnl_after_lock": sm.minimum_basket_pnl_after_lock,
            "max_added_loss_after_lock": sm.max_added_loss_after_lock,
            "maximum_net_long_fraction": float(run_cfg.maximum_net_long_fraction),
            "break_even_reached": sm.state == "CLOSED_BREAK_EVEN",
            "break_even_timestamp": sm.break_even_timestamp,
            "bars_lock_to_break_even": sm.bars_from_lock_to_break_even,
            "final_status": final_status,
            "final_net_pnl": (
                sm.final_realized_net_pnl
                if sm.final_realized_net_pnl is not None
                else ledger.basket_net_pnl(final_mark)
            ),
            "total_fees": float(ledger.total_fees),
            "slippage_cost_usdt": float(ledger.slippage_cost),
            "lock_offset": lock_offset,
        },
        "actions": [a.as_dict() for a in sm.actions],
        "diagnostics": list(sm.signal_diagnostics),
        "ledger": ledger,
        "state_machine": sm,
    }


def _drop_bucket(max_drop_pct: float) -> str:
    d = float(max_drop_pct)
    if d >= 0.15:
        return ">=15%"
    if d >= 0.125:
        return "12.5–15%"
    if d >= 0.10:
        return "10–12.5%"
    return "<10%"


def evaluate_event_signal(
    candles: Sequence[dict[str, Any]],
    event: CrashEvent,
    cfg: EmergencyLockRecoveryConfig,
    *,
    signal_name: str,
    relock_variant: str,
    oracle_possible: bool | None,
    full_lock_final: float | None,
    full_lock_min: float | None,
) -> dict[str, Any]:
    window = list(candles[event.simulation_start_index : event.simulation_end_index + 1])
    result = run_signal_on_window(
        window, cfg, signal_name=signal_name, relock_variant=relock_variant
    )
    s = result["summary"]
    final = s["final_net_pnl"]
    min_b = s["minimum_basket_pnl_after_lock"]
    incr_final = None
    incr_worst = None
    if final is not None and full_lock_final is not None:
        incr_final = float(final) - float(full_lock_final)
    if min_b is not None and full_lock_min is not None:
        incr_worst = float(min_b) - float(full_lock_min)

    be = bool(s["break_even_reached"])
    oracle_captured = bool(oracle_possible) and be if oracle_possible is not None else None

    row = {
        "event_id": event.event_id,
        "signal_name": signal_name,
        "relock_variant": relock_variant,
        "drop_bucket": _drop_bucket(event.max_drop_pct),
        "max_drop_pct": event.max_drop_pct,
        "peak_timestamp": event.peak_timestamp,
        "lock_timestamp": s["lock_timestamp"],
        "basket_pnl_at_lock": s["basket_pnl_at_lock"],
        "short_avg_after_lock": s["short_avg_after_lock"],
        "signal_count": s["signal_count"],
        "unlock_count": s["unlock_count"],
        "unlock_attempt_count": s["unlock_attempt_count"],
        "relock_count": s["relock_count"],
        "failed_unlocks": s["failed_unlocks"],
        "completed_unlock_stages": s["completed_unlock_stages"],
        "minimum_basket_pnl_after_lock": min_b,
        "max_added_loss_after_lock": s["max_added_loss_after_lock"],
        "maximum_net_long_fraction": s["maximum_net_long_fraction"],
        "break_even_reached": be,
        "break_even_timestamp": s["break_even_timestamp"],
        "bars_lock_to_break_even": s["bars_lock_to_break_even"],
        "final_status": s["final_status"],
        "final_net_pnl": final,
        "incremental_final_pnl_vs_full_lock": incr_final,
        "incremental_worst_pnl_vs_full_lock": incr_worst,
        "better_than_full_lock": incr_final is not None and incr_final > 1e-9,
        "worse_than_full_lock": incr_final is not None and incr_final < -1e-9,
        "oracle_break_even_possible": oracle_possible,
        "oracle_captured": oracle_captured,
        "total_fees": s["total_fees"],
        "slippage_cost_usdt": s["slippage_cost_usdt"],
        "window_truncated_at_data_end": event.window_truncated_at_data_end,
        "frozen_deficit_usdt": (
            None
            if s["basket_pnl_at_lock"] is None
            else float(s["basket_pnl_at_lock"])  # approx; exact frozen excl fees in SM
        ),
    }
    return {"row": row, "result": result}


def run_phase_d(
    *,
    candles: Sequence[dict[str, Any]] | None = None,
    events: Sequence[CrashEvent] | None = None,
    cfg: EmergencyLockRecoveryConfig | None = None,
    include_signal_invalidation: bool = True,
) -> dict[str, Any]:
    base = cfg or phase_d_base_config()
    rows = (
        list(candles)
        if candles is not None
        else load_candles_for_symbol(
            symbol=base.symbol,
            timeframe=base.timeframe,
            data_dir=DEFAULT_DATA_DIR,
            limit=None,
        )
    )
    evs = list(events) if events is not None else load_phase_c_events()
    oracle = load_oracle_flags()

    per_event: list[dict[str, Any]] = []
    actions_out: list[dict[str, Any]] = []
    diagnostics_out: list[dict[str, Any]] = []
    result_store: dict[tuple[str, str, str], dict[str, Any]] = {}

    # First pass: full lock control per event for incremental metrics.
    full_lock_cache: dict[str, dict[str, Any]] = {}
    for event in evs:
        fl = evaluate_event_signal(
            rows,
            event,
            base,
            signal_name="full_lock_control",
            relock_variant=MAIN_RELOCK_VARIANT,
            oracle_possible=oracle.get(event.event_id),
            full_lock_final=None,
            full_lock_min=None,
        )
        full_lock_cache[event.event_id] = fl
        per_event.append(fl["row"])
        for a in fl["result"]["actions"]:
            actions_out.append(
                {"event_id": event.event_id, "signal_name": "full_lock_control", "relock_variant": MAIN_RELOCK_VARIANT, **a}
            )
        result_store[(event.event_id, "full_lock_control", MAIN_RELOCK_VARIANT)] = fl["result"]

    signal_names = [s for s in PHASE_D_TRADABLE_SIGNALS if s != "full_lock_control"]
    relock_variants = [MAIN_RELOCK_VARIANT]
    if include_signal_invalidation:
        # Diagnostic only for structure/EMA signals (not rebound / full lock).
        pass

    for event in evs:
        fl_row = full_lock_cache[event.event_id]["row"]
        fl_final = fl_row.get("final_net_pnl")
        fl_min = fl_row.get("minimum_basket_pnl_after_lock")
        for signal_name in signal_names:
            variants = [MAIN_RELOCK_VARIANT]
            if include_signal_invalidation and signal_name in {
                "swing_high_break",
                "swing_break_retest",
                "ema_reclaim",
                "swing_break_with_ema",
            }:
                variants.append(DIAG_RELOCK_VARIANT)
            for relock_variant in variants:
                evaluated = evaluate_event_signal(
                    rows,
                    event,
                    base,
                    signal_name=signal_name,
                    relock_variant=relock_variant,
                    oracle_possible=oracle.get(event.event_id),
                    full_lock_final=fl_final,
                    full_lock_min=fl_min,
                )
                # Fix full-lock incremental on full_lock row itself to 0
                per_event.append(evaluated["row"])
                for a in evaluated["result"]["actions"]:
                    actions_out.append(
                        {
                            "event_id": event.event_id,
                            "signal_name": signal_name,
                            "relock_variant": relock_variant,
                            **a,
                        }
                    )
                for d in evaluated["result"]["diagnostics"]:
                    diagnostics_out.append(
                        {
                            "event_id": event.event_id,
                            "signal_name": signal_name,
                            "relock_variant": relock_variant,
                            **d,
                        }
                    )
                result_store[(event.event_id, signal_name, relock_variant)] = evaluated[
                    "result"
                ]

    # Zero incremental for full lock vs itself
    for row in per_event:
        if row["signal_name"] == "full_lock_control":
            row["incremental_final_pnl_vs_full_lock"] = 0.0
            row["incremental_worst_pnl_vs_full_lock"] = 0.0
            row["better_than_full_lock"] = False
            row["worse_than_full_lock"] = False

    return {
        "candles": rows,
        "events": evs,
        "per_event_rows": per_event,
        "actions": actions_out,
        "diagnostics": diagnostics_out,
        "results": result_store,
        "config": base,
        "protected_structure_adapter_available": PROTECTED_STRUCTURE_ADAPTER_AVAILABLE,
        "protected_structure_adapter_skip_reason": PROTECTED_STRUCTURE_ADAPTER_SKIP_REASON,
    }
