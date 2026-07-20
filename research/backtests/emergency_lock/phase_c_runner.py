"""Phase C runner: crash events × baseline / full-lock / oracle modes.

Lookahead separation
--------------------
* Event finder may use future lows (hindsight labelling only).
* Baseline and full-lock modes call :func:`run_phase_b` with only
  ``start_index`` + ``max_candles`` — never peak/low metadata.
* Oracle is explicitly ``NON_CAUSAL_ORACLE_DIAGNOSTIC`` and never feeds
  baseline or full-lock decisions.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Sequence

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol

from .config import EmergencyLockRecoveryConfig, apply_cli_overrides, validate_phase_c_config
from .cost_model import (
    apply_long_open_slippage,
    apply_short_open_slippage,
    conservative_emergency_short_fill_price,
    conservative_short_close_fill_price,
)
from .event_finder import CrashEvent, EventFinderResult, drop_bucket, find_crash_events
from .phase_a_runner import _ts_iso
from .phase_b_runner import run_phase_b
from .position_ledger import (
    PositionLedger,
    emergency_trigger_price,
    qty_from_notional,
)

MODE_BASELINE = "phase_b_baseline"
MODE_FULL_LOCK = "full_lock_control"
MODE_ORACLE = "NON_CAUSAL_ORACLE_DIAGNOSTIC"

DEFAULT_PHASE_C_OUTPUT_DIR = "research/backtests/results/emergency_lock/phase_c"


def phase_b_baseline_config(
    *,
    symbol: str = "APTUSDT",
    timeframe: str = "5m",
) -> EmergencyLockRecoveryConfig:
    """Mechanical Phase-B baseline — identical for every Phase-C event."""
    return EmergencyLockRecoveryConfig(
        symbol=symbol,
        timeframe=timeframe,
        initial_long_notional_usdt=100.0,
        initial_short_notional_usdt=50.0,
        emergency_trigger_pct=0.10,
        fee_rate=0.00055,
        slippage_bps=2.0,
        unlock_rebound_pcts=(0.03, 0.05, 0.075, 0.10),
        unlock_steps=(0.10, 0.10, 0.15, 0.15),
        relock_distance_pct=0.02,
        max_failed_unlocks=2,
        cooldown_bars_after_relock=12,
        maximum_net_long_fraction=0.50,
        basket_exit_target_usdt=0.0,
        basket_exit_buffer_usdt=0.05,
        minimum_short_profit_buffer_usdt=0.0,
        minimum_distance_to_short_avg_pct=0.0,
        enable_unlock=True,
        output_dir=DEFAULT_PHASE_C_OUTPUT_DIR,
    )


def full_lock_control_config(base: EmergencyLockRecoveryConfig) -> EmergencyLockRecoveryConfig:
    return replace(base, enable_unlock=False)


def _event_window_cfg(
    base: EmergencyLockRecoveryConfig,
    event: CrashEvent,
) -> EmergencyLockRecoveryConfig:
    max_candles = int(event.simulation_end_index - event.simulation_start_index + 1)
    return replace(
        base,
        start_index=int(event.simulation_start_index),
        start_timestamp=None,
        max_candles=max_candles,
        # Strategy timeout still applies inside the window.
        max_post_lock_bars=base.max_post_lock_bars,
    )


def _row_from_phase_b(
    *,
    event: CrashEvent,
    mode: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    s = result["summary"]
    lock_triggered = bool(s.get("lock_triggered"))
    final_status = s.get("final_status")
    if not lock_triggered:
        final_status = "NO_EMERGENCY_TRIGGER"
    short_avg_after_lock = None
    for tr in result.get("trace") or []:
        if tr.get("short_avg_after_lock") is not None:
            short_avg_after_lock = tr.get("short_avg_after_lock")
            break
    return {
        "event_id": event.event_id,
        "mode": mode,
        "selection_type": event.selection_type,
        "drop_bucket": drop_bucket(event.max_drop_pct),
        "peak_timestamp": event.peak_timestamp,
        "peak_index": event.peak_index,
        "peak_price": event.peak_price,
        "low_timestamp": event.low_timestamp,
        "low_index": event.low_index,
        "low_price": event.low_price,
        "max_drop_pct": event.max_drop_pct,
        "qualified_10_pct": event.qualified_10_pct,
        "qualified_12_5_pct": event.qualified_12_5_pct,
        "qualified_15_pct": event.qualified_15_pct,
        "entry_timestamp": s.get("entry_timestamp"),
        "entry_price": s.get("entry_price"),
        "simulation_start_index": event.simulation_start_index,
        "simulation_end_index": event.simulation_end_index,
        "lock_triggered": lock_triggered,
        "lock_timestamp": s.get("lock_timestamp"),
        "lock_price": s.get("lock_price"),
        "basket_pnl_at_lock": s.get("basket_pnl_at_lock"),
        "short_avg_after_lock": short_avg_after_lock,
        "unlock_count": s.get("unlock_count", 0),
        "relock_count": s.get("relock_count", 0),
        "failed_unlocks": s.get("failed_unlocks", 0),
        "completed_unlock_stages": s.get("completed_unlock_stages", 0),
        "minimum_basket_pnl_after_lock": s.get("minimum_basket_pnl_after_lock"),
        "max_added_loss_after_lock": s.get("max_added_loss_after_lock"),
        "loss_added_by_unlocks": s.get("max_added_loss_after_lock"),
        "maximum_net_long_fraction": s.get("maximum_net_long_fraction"),
        "break_even_reached": bool(s.get("break_even_reached")),
        "break_even_timestamp": s.get("break_even_timestamp"),
        "bars_lock_to_break_even": s.get("bars_from_lock_to_break_even"),
        "final_status": final_status,
        "final_net_pnl": s.get("final_net_pnl"),
        "total_fees": s.get("total_fees"),
        "slippage_cost_usdt": s.get("slippage_cost_usdt"),
        "window_truncated_at_data_end": event.window_truncated_at_data_end,
        "oracle_break_even_possible": None,
        "oracle_best_final_net_pnl": None,
        "oracle_earliest_break_even_timestamp": None,
        "oracle_required_net_long_fraction": None,
        "oracle_best_unlock_timestamp": None,
        "oracle_bound_type": None,
    }


def _simulate_to_lock(
    candles: Sequence[dict[str, Any]],
    cfg: EmergencyLockRecoveryConfig,
) -> tuple[PositionLedger, int, float, float] | None:
    """Open hedge and advance until emergency lock; return (ledger, lock_offset, mark, trigger)."""
    if not candles:
        return None
    entry_ref = float(candles[0]["close"])
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
    for offset, candle in enumerate(candles):
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
            return ledger, offset, float(candle["close"]), trigger
    return None


def run_oracle_diagnostic(
    candles: Sequence[dict[str, Any]],
    cfg: EmergencyLockRecoveryConfig,
    event: CrashEvent,
) -> dict[str, Any]:
    """Optimistic non-causal upper bound (documented).

    Bound type ``single_fraction_best_bar``:
    * After the causal emergency lock, try each cumulative unlock fraction
      ``{0.10, 0.20, 0.35, 0.50}`` (from baseline steps) at every post-lock bar.
    * Unlock once (no re-lock) at that bar using conservative fill.
    * Then scan forward for the earliest / best basket exit using the same
      projected closing-cost rule as Phase B.
    * Also evaluate holding to window end without exit.
    * Take the best final net PnL across all trials.

    This is an **optimistic research ceiling**, not a tradable strategy.
    """
    window = list(candles)
    locked = _simulate_to_lock(window, cfg)
    base_row = {
        "event_id": event.event_id,
        "mode": MODE_ORACLE,
        "selection_type": event.selection_type,
        "drop_bucket": drop_bucket(event.max_drop_pct),
        "peak_timestamp": event.peak_timestamp,
        "peak_index": event.peak_index,
        "peak_price": event.peak_price,
        "low_timestamp": event.low_timestamp,
        "low_index": event.low_index,
        "low_price": event.low_price,
        "max_drop_pct": event.max_drop_pct,
        "qualified_10_pct": event.qualified_10_pct,
        "qualified_12_5_pct": event.qualified_12_5_pct,
        "qualified_15_pct": event.qualified_15_pct,
        "entry_timestamp": _ts_iso(window[0]["timestamp"]) if window else None,
        "entry_price": float(window[0]["close"]) if window else None,
        "simulation_start_index": event.simulation_start_index,
        "simulation_end_index": event.simulation_end_index,
        "lock_triggered": False,
        "lock_timestamp": None,
        "lock_price": None,
        "basket_pnl_at_lock": None,
        "short_avg_after_lock": None,
        "unlock_count": 0,
        "relock_count": 0,
        "failed_unlocks": 0,
        "completed_unlock_stages": 0,
        "minimum_basket_pnl_after_lock": None,
        "max_added_loss_after_lock": None,
        "loss_added_by_unlocks": None,
        "maximum_net_long_fraction": float(cfg.maximum_net_long_fraction),
        "break_even_reached": False,
        "break_even_timestamp": None,
        "bars_lock_to_break_even": None,
        "final_status": "NO_EMERGENCY_TRIGGER",
        "final_net_pnl": None,
        "total_fees": None,
        "slippage_cost_usdt": None,
        "window_truncated_at_data_end": event.window_truncated_at_data_end,
        "oracle_break_even_possible": False,
        "oracle_best_final_net_pnl": None,
        "oracle_earliest_break_even_timestamp": None,
        "oracle_required_net_long_fraction": None,
        "oracle_best_unlock_timestamp": None,
        "oracle_bound_type": "single_fraction_best_bar_OPTIMISTIC_UPPER_BOUND",
    }
    if locked is None:
        return base_row

    ledger0, lock_offset, lock_mark, _trigger = locked
    basket_at_lock = ledger0.basket_net_pnl(lock_mark)
    full_q = float(ledger0.short_qty)
    base_row.update(
        {
            "lock_triggered": True,
            "lock_timestamp": _ts_iso(window[lock_offset]["timestamp"]),
            "basket_pnl_at_lock": basket_at_lock,
            "short_avg_after_lock": float(ledger0.short_avg),
            "final_status": "ORACLE_EVALUATED",
        }
    )

    # Cumulative unlock fractions from baseline steps, capped by max net long.
    fracs: list[float] = []
    acc = 0.0
    for step in cfg.unlock_steps:
        acc += float(step)
        if acc - float(cfg.maximum_net_long_fraction) <= 1e-12:
            fracs.append(acc)
    if not fracs:
        fracs = [float(cfg.maximum_net_long_fraction)]

    target = float(cfg.basket_exit_target_usdt)
    buffer = float(cfg.basket_exit_buffer_usdt)
    best_pnl = float("-inf")
    best_be_ts: str | None = None
    best_unlock_ts: str | None = None
    best_frac: float | None = None
    be_possible = False
    min_basket = basket_at_lock
    max_added = 0.0

    post = window[lock_offset:]
    # Subsample unlock bars for speed on long windows (still optimistic bound).
    unlock_indices = list(range(1, len(post)))  # skip lock bar itself
    if len(unlock_indices) > 400:
        step = max(1, len(unlock_indices) // 400)
        unlock_indices = unlock_indices[::step]
        if unlock_indices[-1] != len(post) - 1:
            unlock_indices.append(len(post) - 1)

    for frac in fracs:
        qty = full_q * float(frac)
        for ui in unlock_indices:
            # Clone ledger state at lock via re-sim to lock then skip to ui
            # Efficient path: deep copy lock ledger for each trial.
            trial = copy.deepcopy(ledger0)
            candle_u = post[ui]
            # Unlock reference: use close as optimistic research trigger proxy.
            unlock_ref = float(candle_u["close"])
            fill = conservative_short_close_fill_price(
                trigger_price=unlock_ref,
                candle_high=float(candle_u["high"]),
                slippage_bps=cfg.slippage_bps,
            )
            close_qty = min(qty, float(trial.short_qty))
            if close_qty <= cfg.qty_tolerance:
                continue
            trial.close_short(
                qty=close_qty,
                fill_price=fill,
                fee_rate=cfg.fee_rate,
                reference_price=unlock_ref,
                fee_bucket="unlock_closing",
            )
            unlock_ts = _ts_iso(candle_u["timestamp"])
            local_be_ts = None
            local_best = float("-inf")
            for uj in range(ui, len(post)):
                mark = float(post[uj]["close"])
                basket = trial.basket_net_pnl(mark)
                min_basket = min(min_basket, basket)
                max_added = max(max_added, max(basket_at_lock - basket, 0.0))
                proj = trial.project_full_close_net_pnl(
                    reference_price=mark,
                    fee_rate=cfg.fee_rate,
                    slippage_bps=cfg.slippage_bps,
                )
                projected = float(proj["projected_final_net_pnl_after_closing_costs"])
                if basket + 1e-12 >= target + buffer and projected + 1e-12 >= target:
                    be_possible = True
                    if local_be_ts is None:
                        local_be_ts = _ts_iso(post[uj]["timestamp"])
                    local_best = max(local_best, projected)
                local_best = max(local_best, basket)
            # Hold-to-end mark
            end_mark = float(post[-1]["close"])
            end_pnl = trial.basket_net_pnl(end_mark)
            local_best = max(local_best, end_pnl)
            if local_best > best_pnl:
                best_pnl = local_best
                best_unlock_ts = unlock_ts
                best_frac = float(frac)
                if local_be_ts is not None:
                    best_be_ts = local_be_ts

    # Also evaluate never-unlock hold (full lock path) for completeness.
    hold = copy.deepcopy(ledger0)
    for candle in post[1:]:
        mark = float(candle["close"])
        basket = hold.basket_net_pnl(mark)
        min_basket = min(min_basket, basket)
        max_added = max(max_added, max(basket_at_lock - basket, 0.0))
        proj = hold.project_full_close_net_pnl(
            reference_price=mark,
            fee_rate=cfg.fee_rate,
            slippage_bps=cfg.slippage_bps,
        )
        projected = float(proj["projected_final_net_pnl_after_closing_costs"])
        if basket + 1e-12 >= target + buffer and projected + 1e-12 >= target:
            be_possible = True
            ts = _ts_iso(candle["timestamp"])
            if best_be_ts is None:
                best_be_ts = ts
            if projected > best_pnl:
                best_pnl = projected
                best_frac = 0.0
                best_unlock_ts = None

    end_hold = hold.basket_net_pnl(float(post[-1]["close"]))
    if end_hold > best_pnl:
        best_pnl = end_hold
        best_frac = 0.0
        best_unlock_ts = None

    base_row.update(
        {
            "oracle_break_even_possible": bool(be_possible),
            "oracle_best_final_net_pnl": float(best_pnl) if best_pnl > float("-inf") else end_hold,
            "oracle_earliest_break_even_timestamp": best_be_ts,
            "oracle_required_net_long_fraction": best_frac,
            "oracle_best_unlock_timestamp": best_unlock_ts,
            "break_even_reached": bool(be_possible),
            "break_even_timestamp": best_be_ts,
            "final_net_pnl": float(best_pnl) if best_pnl > float("-inf") else end_hold,
            "minimum_basket_pnl_after_lock": min_basket,
            "max_added_loss_after_lock": max_added,
            "total_fees": float(ledger0.total_fees),
            "slippage_cost_usdt": float(ledger0.slippage_cost),
        }
    )
    return base_row


def evaluate_event_modes(
    candles: Sequence[dict[str, Any]],
    event: CrashEvent,
    base_cfg: EmergencyLockRecoveryConfig,
) -> dict[str, Any]:
    """Run baseline, full-lock control, and oracle for one event."""
    window = candles[event.simulation_start_index : event.simulation_end_index + 1]

    baseline_cfg = _event_window_cfg(base_cfg, event)
    # Pass only the window with start_index=0 so absolute event indices are not
    # required inside the strategy — avoids leaking low_index.
    baseline_cfg = replace(baseline_cfg, start_index=0, max_candles=len(window))
    baseline_result = run_phase_b(baseline_cfg, candles=window)
    baseline_row = _row_from_phase_b(
        event=event, mode=MODE_BASELINE, result=baseline_result
    )

    control_cfg = full_lock_control_config(baseline_cfg)
    control_result = run_phase_b(control_cfg, candles=window)
    control_row = _row_from_phase_b(
        event=event, mode=MODE_FULL_LOCK, result=control_result
    )

    oracle_row = run_oracle_diagnostic(window, baseline_cfg, event)

    # Incremental diagnostics (baseline vs full-lock).
    b_final = baseline_row.get("final_net_pnl")
    c_final = control_row.get("final_net_pnl")
    b_min = baseline_row.get("minimum_basket_pnl_after_lock")
    c_min = control_row.get("minimum_basket_pnl_after_lock")
    incremental_final = None
    incremental_worst = None
    if b_final is not None and c_final is not None:
        incremental_final = float(b_final) - float(c_final)
    if b_min is not None and c_min is not None:
        incremental_worst = float(b_min) - float(c_min)

    for row in (baseline_row, control_row, oracle_row):
        row["incremental_final_pnl_vs_full_lock"] = (
            incremental_final if row["mode"] == MODE_BASELINE else None
        )
        row["incremental_worst_loss_vs_full_lock"] = (
            incremental_worst if row["mode"] == MODE_BASELINE else None
        )
        row["baseline_better_than_full_lock"] = (
            (incremental_final is not None and incremental_final > 1e-9)
            if row["mode"] == MODE_BASELINE
            else None
        )
        row["baseline_worse_than_full_lock"] = (
            (incremental_final is not None and incremental_final < -1e-9)
            if row["mode"] == MODE_BASELINE
            else None
        )

    return {
        "baseline": baseline_row,
        "full_lock": control_row,
        "oracle": oracle_row,
        "baseline_result": baseline_result,
        "full_lock_result": control_result,
    }


def run_phase_c(
    cfg: EmergencyLockRecoveryConfig | None = None,
    candles: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Find events and evaluate all three modes on each event."""
    base = cfg or phase_b_baseline_config()
    validate_phase_c_config(base)
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
    finder: EventFinderResult = find_crash_events(rows, base)
    per_event_rows: list[dict[str, Any]] = []
    baseline_results: dict[str, dict[str, Any]] = {}
    full_lock_results: dict[str, dict[str, Any]] = {}

    for event in finder.events:
        evaluated = evaluate_event_modes(rows, event, base)
        per_event_rows.extend(
            [evaluated["baseline"], evaluated["full_lock"], evaluated["oracle"]]
        )
        baseline_results[event.event_id] = evaluated["baseline_result"]
        full_lock_results[event.event_id] = evaluated["full_lock_result"]

    return {
        "candles": rows,
        "finder": finder,
        "per_event_rows": per_event_rows,
        "baseline_results": baseline_results,
        "full_lock_results": full_lock_results,
        "config": base,
    }
