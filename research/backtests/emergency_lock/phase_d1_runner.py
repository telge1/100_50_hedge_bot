"""Phase D.1 runner: micro-unlock policies vs Phase-D controls on same events."""

from __future__ import annotations

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
from .phase_c_runner import phase_b_baseline_config
from .phase_d_runner import (
    MAIN_RELOCK_VARIANT,
    evaluate_event_signal,
    load_oracle_flags,
    load_phase_c_events,
)
from .phase_d1_policy import MicroUnlockConfig, MicroUnlockEngine, micro_unlock_configs
from .position_ledger import (
    PositionLedger,
    emergency_trigger_price,
    qty_from_notional,
)

DEFAULT_PHASE_D1_OUTPUT_DIR = "research/backtests/results/emergency_lock/phase_d1"
DEFAULT_PHASE_D_SUMMARY = (
    "research/backtests/results/emergency_lock/phase_d/signal_per_event_summary.csv"
)

CONTROL_VARIANTS = (
    "full_lock_control",
    "rebound_baseline",
    "swing_break_with_ema_existing",
)


def phase_d1_base_config(*, symbol: str = "APTUSDT") -> EmergencyLockRecoveryConfig:
    cfg = phase_b_baseline_config(symbol=symbol)
    cfg.output_dir = DEFAULT_PHASE_D1_OUTPUT_DIR
    return cfg


def run_micro_on_window(
    window: list[dict[str, Any]],
    cfg: EmergencyLockRecoveryConfig,
    policy: MicroUnlockConfig,
) -> dict[str, Any]:
    entry_ref = float(window[0]["close"])
    long_fill = apply_long_open_slippage(
        reference_price=entry_ref, slippage_bps=cfg.slippage_bps
    )
    short_fill = apply_short_open_slippage(
        reference_price=entry_ref, slippage_bps=cfg.slippage_bps
    )
    ledger = PositionLedger()
    ledger.open_long(
        qty=qty_from_notional(
            notional_usdt=cfg.initial_long_notional_usdt, price=entry_ref
        ),
        fill_price=long_fill,
        fee_rate=cfg.fee_rate,
        reference_price=entry_ref,
    )
    ledger.open_short(
        qty=qty_from_notional(
            notional_usdt=cfg.initial_short_notional_usdt, price=entry_ref
        ),
        fill_price=short_fill,
        fee_rate=cfg.fee_rate,
        reference_price=entry_ref,
    )
    trigger = emergency_trigger_price(
        long_avg=ledger.long_avg, emergency_trigger_pct=cfg.emergency_trigger_pct
    )
    engine = MicroUnlockEngine(
        policy=policy,
        ledger=ledger,
        fee_rate=float(cfg.fee_rate),
        slippage_bps=float(cfg.slippage_bps),
        qty_tolerance=float(cfg.qty_tolerance),
        max_post_lock_bars=int(cfg.max_post_lock_bars),
        basket_exit_target_usdt=float(cfg.basket_exit_target_usdt),
        basket_exit_buffer_usdt=float(cfg.basket_exit_buffer_usdt),
    )

    lock_triggered = False
    lock_timestamp = None
    short_avg_after_lock = None
    fees_at_lock = None

    for offset, candle in enumerate(window):
        mark = float(candle["close"])
        ts = _ts_iso(candle["timestamp"])
        is_last = offset == len(window) - 1

        if not lock_triggered:
            if float(candle["low"]) <= trigger:
                fill = conservative_emergency_short_fill_price(
                    trigger_price=trigger,
                    candle_low=float(candle["low"]),
                    slippage_bps=cfg.slippage_bps,
                )
                ledger.emergency_short_top_up(
                    fill_price=fill,
                    fee_rate=cfg.fee_rate,
                    reference_price=trigger,
                    qty_tolerance=cfg.qty_tolerance,
                )
                engine.enter_lock(timestamp=ts, bar_index=offset, mark=mark)
                lock_triggered = True
                lock_timestamp = ts
                short_avg_after_lock = float(ledger.short_avg)
                fees_at_lock = float(ledger.total_fees)
                engine.process_bar(
                    timestamp=ts,
                    candle=candle,
                    candles=window,
                    bar_index=offset,
                    mark=mark,
                    is_last_bar=is_last,
                )
            elif is_last:
                engine.basket_state = "OPEN_AT_DATA_END"
        else:
            engine.process_bar(
                timestamp=ts,
                candle=candle,
                candles=window,
                bar_index=offset,
                mark=mark,
                is_last_bar=is_last,
            )

        if engine.basket_state in {
            "CLOSED_BREAK_EVEN",
            "STOPPED_TIMEOUT",
            "STOPPED_MAX_ADDED_LOSS",
            "OPEN_AT_DATA_END",
        }:
            break

    engine.assert_exposure_caps()
    final_mark = float(window[min(len(window) - 1, offset)]["close"])
    final_status = engine.basket_state
    if not lock_triggered:
        final_status = "NO_EMERGENCY_TRIGGER"
    final_pnl = (
        engine.final_realized_net_pnl
        if engine.final_realized_net_pnl is not None
        else ledger.basket_net_pnl(final_mark)
    )
    s1 = engine.stage_1_pnl_last or {}
    return {
        "summary": {
            "variant": policy.variant_name,
            "lock_triggered": lock_triggered,
            "lock_timestamp": lock_timestamp,
            "basket_pnl_at_lock": engine.basket_pnl_at_lock,
            "short_avg_after_lock": short_avg_after_lock,
            "signal_bar": engine.signal_bar_stage_1,
            "swing_level": None if engine.stage_1 is None else engine.stage_1.swing_high,
            "ema9_at_stage_1": None if engine.stage_1 is None else engine.stage_1.ema9,
            "ema20_at_stage_1": None if engine.stage_1 is None else engine.stage_1.ema20,
            "unlock_count": engine.unlock_count,
            "stage_1_unlock_count": engine.stage_1_unlock_count,
            "stage_2_unlock_count": engine.stage_2_unlock_count,
            "relock_count": engine.relock_count,
            "max_open_unlock_pct": engine.max_open_unlock_pct,
            "cumulative_unlock_pct_final": engine.cumulative_unlock_pct,
            "stage_1_gross_pnl": s1.get("stage_1_gross_pnl"),
            "stage_1_fee_cost": s1.get("stage_1_fee_cost"),
            "stage_1_net_pnl": s1.get("stage_1_net_pnl"),
            "stage_1_break_even_confirmed": bool(engine.stage_1_be_ever),
            "bars_to_stage_2": engine.bars_to_stage_2,
            "stage_2_trigger_reason": engine.stage_2_trigger_reason,
            "relock_bar": engine.last_relock_bar,
            "relock_price": engine.last_relock_price,
            "relock_reason": engine.last_relock_reason,
            "bars_to_relock": engine.bars_to_relock_from_stage_1,
            "unlock_attempt_cycles": engine.unlock_attempt_cycles,
            "post_relock_attempts_used": engine.post_relock_attempts_used,
            "max_unlock_attempts_after_relock": policy.max_unlock_attempts_after_relock,
            "minimum_basket_pnl_after_lock": engine.minimum_basket_pnl_after_lock,
            "max_added_loss_after_lock": engine.max_added_loss_after_lock,
            "break_even_reached": engine.break_even_reached,
            "break_even_timestamp": engine.break_even_timestamp,
            "bars_lock_to_break_even": engine.bars_lock_to_break_even,
            "final_status": final_status,
            "final_net_pnl": final_pnl,
            "total_fees": float(ledger.total_fees),
            "fees_at_lock": fees_at_lock,
            "slippage_cost_usdt": float(ledger.slippage_cost),
            "policy_state_final": engine.policy_state,
            "policy_config": policy.as_public_dict(),
        },
        "actions": list(engine.actions),
        "transitions": list(engine.transitions),
        "diagnostics": list(engine.diagnostics),
        "ledger": ledger,
        "engine": engine,
    }


def _control_signal_name(variant: str) -> str:
    if variant == "swing_break_with_ema_existing":
        return "swing_break_with_ema"
    return variant


def evaluate_control_event(
    candles: Sequence[dict[str, Any]],
    event: CrashEvent,
    cfg: EmergencyLockRecoveryConfig,
    *,
    variant: str,
    oracle_possible: bool | None,
    full_lock_final: float | None,
    full_lock_min: float | None,
    fees_at_full_lock: float | None,
) -> dict[str, Any]:
    signal_name = _control_signal_name(variant)
    evaluated = evaluate_event_signal(
        candles,
        event,
        cfg,
        signal_name=signal_name,
        relock_variant=MAIN_RELOCK_VARIANT,
        oracle_possible=oracle_possible,
        full_lock_final=full_lock_final,
        full_lock_min=full_lock_min,
    )
    row = dict(evaluated["row"])
    row["variant"] = variant
    row["stage_1_unlock_count"] = None
    row["stage_2_unlock_count"] = None
    row["max_open_unlock_pct"] = (
        0.0
        if variant == "full_lock_control"
        else None
    )
    row["stage_1_break_even_confirmed"] = None
    row["extra_fees_vs_full_lock"] = (
        None
        if fees_at_full_lock is None
        else float(row["total_fees"]) - float(fees_at_full_lock)
    )
    return {"row": row, "result": evaluated["result"]}


def evaluate_micro_event(
    candles: Sequence[dict[str, Any]],
    event: CrashEvent,
    cfg: EmergencyLockRecoveryConfig,
    policy: MicroUnlockConfig,
    *,
    oracle_possible: bool | None,
    full_lock_final: float | None,
    full_lock_min: float | None,
    fees_at_full_lock: float | None,
) -> dict[str, Any]:
    window = list(candles[event.simulation_start_index : event.simulation_end_index + 1])
    result = run_micro_on_window(window, cfg, policy)
    s = result["summary"]
    final = s["final_net_pnl"]
    min_b = s["minimum_basket_pnl_after_lock"]
    incr_final = (
        None
        if final is None or full_lock_final is None
        else float(final) - float(full_lock_final)
    )
    incr_worst = (
        None
        if min_b is None or full_lock_min is None
        else float(min_b) - float(full_lock_min)
    )
    be = bool(s["break_even_reached"])
    oracle_captured = (
        bool(oracle_possible) and be if oracle_possible is not None else None
    )
    drop = event.max_drop_pct
    if drop >= 0.15:
        bucket = ">=15%"
    elif drop >= 0.125:
        bucket = "12.5–15%"
    elif drop >= 0.10:
        bucket = "10–12.5%"
    else:
        bucket = "<10%"

    row = {
        "event_id": event.event_id,
        "variant": policy.variant_name,
        "drop_bucket": bucket,
        "max_drop_pct": event.max_drop_pct,
        "lock_timestamp": s["lock_timestamp"],
        "basket_pnl_at_lock": s["basket_pnl_at_lock"],
        "short_avg_after_lock": s["short_avg_after_lock"],
        "signal_bar": s["signal_bar"],
        "swing_level": s["swing_level"],
        "ema9_at_stage_1": s["ema9_at_stage_1"],
        "ema20_at_stage_1": s["ema20_at_stage_1"],
        "unlock_count": s["unlock_count"],
        "stage_1_unlock_count": s["stage_1_unlock_count"],
        "stage_2_unlock_count": s["stage_2_unlock_count"],
        "relock_count": s["relock_count"],
        "max_open_unlock_pct": s["max_open_unlock_pct"],
        "stage_1_gross_pnl": s["stage_1_gross_pnl"],
        "stage_1_fee_cost": s["stage_1_fee_cost"],
        "stage_1_net_pnl": s["stage_1_net_pnl"],
        "stage_1_break_even_confirmed": s["stage_1_break_even_confirmed"],
        "bars_to_stage_2": s["bars_to_stage_2"],
        "stage_2_trigger_reason": s["stage_2_trigger_reason"],
        "relock_bar": s["relock_bar"],
        "relock_price": s["relock_price"],
        "relock_reason": s["relock_reason"],
        "bars_to_relock": s["bars_to_relock"],
        "unlock_attempt_cycles": s["unlock_attempt_cycles"],
        "post_relock_attempts_used": s["post_relock_attempts_used"],
        "max_unlock_attempts_after_relock": s["max_unlock_attempts_after_relock"],
        "minimum_basket_pnl_after_lock": min_b,
        "max_added_loss_after_lock": s["max_added_loss_after_lock"],
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
        "extra_fees_vs_full_lock": (
            None
            if fees_at_full_lock is None
            else float(s["total_fees"]) - float(fees_at_full_lock)
        ),
        "slippage_cost_usdt": s["slippage_cost_usdt"],
        "policy_state_final": s["policy_state_final"],
        "frozen_deficit_usdt": (
            None if s["basket_pnl_at_lock"] is None else abs(float(s["basket_pnl_at_lock"]))
        ),
    }
    return {"row": row, "result": result}


def run_phase_d1(
    *,
    candles: Sequence[dict[str, Any]] | None = None,
    events: Sequence[CrashEvent] | None = None,
    cfg: EmergencyLockRecoveryConfig | None = None,
) -> dict[str, Any]:
    base = cfg or phase_d1_base_config()
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
    micros = micro_unlock_configs()

    per_event: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    results: dict[tuple[str, str], dict[str, Any]] = {}

    # Full lock first
    full_lock_cache: dict[str, dict[str, Any]] = {}
    for event in evs:
        fl = evaluate_control_event(
            rows,
            event,
            base,
            variant="full_lock_control",
            oracle_possible=oracle.get(event.event_id),
            full_lock_final=None,
            full_lock_min=None,
            fees_at_full_lock=None,
        )
        fl["row"]["incremental_final_pnl_vs_full_lock"] = 0.0
        fl["row"]["incremental_worst_pnl_vs_full_lock"] = 0.0
        fl["row"]["better_than_full_lock"] = False
        fl["row"]["worse_than_full_lock"] = False
        fl["row"]["extra_fees_vs_full_lock"] = 0.0
        full_lock_cache[event.event_id] = fl
        per_event.append(fl["row"])
        results[(event.event_id, "full_lock_control")] = fl["result"]

    for event in evs:
        fl_row = full_lock_cache[event.event_id]["row"]
        fl_final = fl_row.get("final_net_pnl")
        fl_min = fl_row.get("minimum_basket_pnl_after_lock")
        fl_fees = fl_row.get("total_fees")

        for variant in ("rebound_baseline", "swing_break_with_ema_existing"):
            out = evaluate_control_event(
                rows,
                event,
                base,
                variant=variant,
                oracle_possible=oracle.get(event.event_id),
                full_lock_final=fl_final,
                full_lock_min=fl_min,
                fees_at_full_lock=fl_fees,
            )
            per_event.append(out["row"])
            results[(event.event_id, variant)] = out["result"]

        for name, policy in micros.items():
            out = evaluate_micro_event(
                rows,
                event,
                base,
                policy,
                oracle_possible=oracle.get(event.event_id),
                full_lock_final=fl_final,
                full_lock_min=fl_min,
                fees_at_full_lock=fl_fees,
            )
            per_event.append(out["row"])
            results[(event.event_id, name)] = out["result"]
            for tr in out["result"]["transitions"]:
                transitions.append(
                    {
                        "event_id": event.event_id,
                        "variant": name,
                        **tr,
                    }
                )

    return {
        "candles": rows,
        "events": evs,
        "per_event_rows": per_event,
        "transitions": transitions,
        "results": results,
        "config": base,
        "micro_configs": {k: v.as_public_dict() for k, v in micros.items()},
    }
