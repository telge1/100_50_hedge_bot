"""Phase B runner: Emergency Lock + unlock / re-lock / basket break-even."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
)

from .config import (
    EmergencyLockRecoveryConfig,
    validate_phase_b_config,
)
from .cost_model import (
    apply_long_open_slippage,
    apply_short_open_slippage,
    conservative_emergency_short_fill_price,
    funding_payment_usdt,
)
from .phase_a_runner import (
    EmergencyLockError,
    _ts_iso,
    resolve_start_index,
)
from .position_ledger import (
    PositionLedger,
    emergency_trigger_price,
    qty_from_notional,
)
from .state_machine import EmergencyLockStateMachine

TRACE_FIELDS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "state",
    "long_qty",
    "long_avg",
    "short_qty",
    "short_avg",
    "net_qty",
    "unrealized_long_pnl",
    "unrealized_short_pnl",
    "realized_long_pnl",
    "realized_short_pnl",
    "total_fees",
    "slippage_cost",
    "funding_cost",
    "basket_net_pnl",
    "trigger_price",
    "lock_timestamp",
    "lock_price",
    "short_avg_after_lock",
    "frozen_deficit_usdt",
    "post_lock_low",
    "full_lock_short_qty",
    "unlock_stage",
    "unlock_attempt",
    "unlock_reference",
    "last_unlock_fill",
    "last_unlock_qty",
    "relock_trigger",
    "open_short_profit_usdt",
    "distance_to_short_avg_pct",
    "net_long_qty",
    "net_long_fraction",
    "closing_fees",
    "relock_fees",
    "basket_pnl_at_lock",
    "added_loss_after_lock",
    "max_added_loss_after_lock",
    "failed_unlocks",
    "cooldown_bars_remaining",
    "projected_final_net_pnl_after_closing_costs",
)

ACTION_FIELDS = (
    "timestamp",
    "action",
    "reason",
    "stage",
    "attempt",
    "reference_price",
    "fill_price",
    "qty",
    "long_qty_after",
    "short_qty_after",
    "short_avg_after",
    "realized_short_pnl_delta",
    "fee_delta",
    "basket_pnl_before",
    "basket_pnl_after",
    "added_loss_after_lock",
)

DEFAULT_PHASE_B_OUTPUT_DIR = "research/backtests/results/emergency_lock/phase_b"


def load_phase_b_candles(cfg: EmergencyLockRecoveryConfig) -> list[dict[str, Any]]:
    return load_candles_for_symbol(
        symbol=cfg.symbol,
        timeframe=cfg.timeframe,
        data_dir=DEFAULT_DATA_DIR,
        limit=None,
    )


def _maybe_apply_funding(
    ledger: PositionLedger,
    *,
    cfg: EmergencyLockRecoveryConfig,
    mark_price: float,
    bars_since_start: int,
    candle_minutes: int = 5,
) -> float:
    if not cfg.funding_enabled or cfg.funding_rate_per_interval == 0.0:
        return 0.0
    interval_bars = max(1, int(cfg.funding_interval_hours * 60 // candle_minutes))
    if bars_since_start <= 0 or bars_since_start % interval_bars != 0:
        return 0.0
    payment = funding_payment_usdt(
        long_qty=ledger.long_qty,
        short_qty=ledger.short_qty,
        mark_price=mark_price,
        funding_rate=cfg.funding_rate_per_interval,
    )
    if payment != 0.0:
        ledger.apply_funding(payment)
    return payment


def _candle_triggers_lock(
    candle: dict[str, Any], trigger_price: float, cfg: EmergencyLockRecoveryConfig
) -> bool:
    if cfg.trigger_price_source != "low":
        raise EmergencyLockError(
            f"unsupported trigger_price_source: {cfg.trigger_price_source}"
        )
    return float(candle["low"]) <= float(trigger_price)


def run_phase_b(
    cfg: EmergencyLockRecoveryConfig,
    candles: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run Phase B emergency lock + unlock/re-lock/break-even simulation."""
    validate_phase_b_config(cfg)
    rows = list(candles) if candles is not None else load_phase_b_candles(cfg)
    if not rows:
        raise EmergencyLockError("no candles loaded")

    start_i = resolve_start_index(rows, cfg)
    window = rows[start_i:]
    if cfg.max_candles is not None and cfg.max_candles > 0:
        window = window[: int(cfg.max_candles)]
    if not window:
        raise EmergencyLockError("empty candle window after start resolution")

    start_candle = window[0]
    entry_ref = float(start_candle["close"])
    if entry_ref <= 0.0:
        raise EmergencyLockError("start candle close must be positive")

    long_fill = apply_long_open_slippage(
        reference_price=entry_ref, slippage_bps=cfg.slippage_bps
    )
    short_fill = apply_short_open_slippage(
        reference_price=entry_ref, slippage_bps=cfg.slippage_bps
    )
    long_qty = qty_from_notional(
        notional_usdt=cfg.initial_long_notional_usdt, price=entry_ref
    )
    short_qty = qty_from_notional(
        notional_usdt=cfg.initial_short_notional_usdt, price=entry_ref
    )

    ledger = PositionLedger()
    ledger.open_long(
        qty=long_qty,
        fill_price=long_fill,
        fee_rate=cfg.fee_rate,
        reference_price=entry_ref,
    )
    ledger.open_short(
        qty=short_qty,
        fill_price=short_fill,
        fee_rate=cfg.fee_rate,
        reference_price=entry_ref,
    )

    trigger = emergency_trigger_price(
        long_avg=ledger.long_avg, emergency_trigger_pct=cfg.emergency_trigger_pct
    )
    sm = EmergencyLockStateMachine(cfg=cfg, ledger=ledger)
    sm.emergency_trigger = trigger
    sm.state = "PRE_EMERGENCY"

    if _candle_triggers_lock(start_candle, trigger, cfg):
        if cfg.start_below_trigger_policy == "reject":
            raise EmergencyLockError(
                "start candle already at/below emergency trigger "
                f"(low={start_candle['low']}, trigger={trigger})"
            )

    trace: list[dict[str, Any]] = []
    short_avg_after_lock: float | None = None
    lock_triggered = False

    for offset, candle in enumerate(window):
        mark = float(candle["close"])
        ts = _ts_iso(candle["timestamp"])
        _maybe_apply_funding(
            ledger, cfg=cfg, mark_price=mark, bars_since_start=offset
        )
        is_last = offset == len(window) - 1

        if sm.state == "PRE_EMERGENCY" and _candle_triggers_lock(candle, trigger, cfg):
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
            if abs(ledger.long_qty - ledger.short_qty) > cfg.qty_tolerance:
                raise EmergencyLockError("full lock failed: long_qty != short_qty")
            if ledger.short_qty > ledger.long_qty + cfg.qty_tolerance:
                raise EmergencyLockError("short overhedge after emergency top-up")
            sm.enter_full_lock(
                timestamp=ts, candle=candle, fill_price=fill, mark=mark
            )
            short_avg_after_lock = float(ledger.short_avg)
            lock_triggered = True
            # Unlock / exit start on subsequent bars only (no same-bar unlock).
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

        metrics = sm.metrics_at_mark(mark) if lock_triggered else {
            "unlock_reference": None,
            "open_short_profit_usdt": 0.0,
            "distance_to_short_avg_pct": 0.0,
            "net_long_qty": max(ledger.long_qty - ledger.short_qty, 0.0),
            "net_long_fraction": 0.0,
            "added_loss_after_lock": None,
            "projected_final_net_pnl_after_closing_costs": None,
            "projected_closing_fees": None,
            "projected_exit_slippage": None,
            "basket_pnl_before_exit": None,
        }
        snap = ledger.snapshot(mark)
        row = {
            "timestamp": ts,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": mark,
            "state": sm.state,
            **{k: snap[k] for k in (
                "long_qty",
                "long_avg",
                "short_qty",
                "short_avg",
                "net_qty",
                "unrealized_long_pnl",
                "unrealized_short_pnl",
                "realized_long_pnl",
                "realized_short_pnl",
                "total_fees",
                "slippage_cost",
                "funding_cost",
                "basket_net_pnl",
            )},
            "trigger_price": trigger,
            "lock_timestamp": sm.lock_timestamp,
            "lock_price": sm.lock_price,
            "short_avg_after_lock": short_avg_after_lock,
            "frozen_deficit_usdt": sm.frozen_deficit_usdt,
            "post_lock_low": sm.post_lock_low,
            "full_lock_short_qty": sm.full_lock_short_qty or None,
            "unlock_stage": sm.next_unlock_stage,
            "unlock_attempt": sm.unlock_attempt_count,
            "unlock_reference": metrics["unlock_reference"],
            "last_unlock_fill": sm.last_unlock_fill,
            "last_unlock_qty": sm.last_unlock_qty,
            "relock_trigger": sm.relock_trigger,
            "open_short_profit_usdt": metrics["open_short_profit_usdt"],
            "distance_to_short_avg_pct": metrics["distance_to_short_avg_pct"],
            "net_long_qty": metrics["net_long_qty"],
            "net_long_fraction": metrics["net_long_fraction"],
            "closing_fees": float(ledger.closing_fees),
            "relock_fees": float(ledger.relock_opening_fees),
            "basket_pnl_at_lock": sm.basket_pnl_at_lock,
            "added_loss_after_lock": metrics["added_loss_after_lock"],
            "max_added_loss_after_lock": sm.max_added_loss_after_lock,
            "failed_unlocks": sm.failed_unlocks,
            "cooldown_bars_remaining": sm.cooldown_bars_remaining,
            "projected_final_net_pnl_after_closing_costs": metrics[
                "projected_final_net_pnl_after_closing_costs"
            ],
        }
        trace.append(row)

        if sm.state in {
            "CLOSED_BREAK_EVEN",
            "STOPPED_TIMEOUT",
            "STOPPED_MAX_ADDED_LOSS",
            "OPEN_AT_DATA_END",
        }:
            break

    final_mark = float(window[min(len(trace), len(window)) - 1]["close"]) if trace else entry_ref
    # Use last trace close
    if trace:
        final_mark = float(trace[-1]["close"])

    summary: dict[str, Any] = {
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "entry_timestamp": _ts_iso(start_candle["timestamp"]),
        "start_index": start_i,
        "entry_price": entry_ref,
        "lock_triggered": lock_triggered,
        "lock_timestamp": sm.lock_timestamp,
        "lock_price": sm.lock_price,
        "basket_pnl_at_lock": sm.basket_pnl_at_lock,
        "frozen_deficit_usdt": sm.frozen_deficit_usdt,
        "full_lock_short_qty": sm.full_lock_short_qty,
        "unlock_count": sm.unlock_count,
        "unlock_attempt_count": sm.unlock_attempt_count,
        "relock_count": sm.relock_count,
        "failed_unlocks": sm.failed_unlocks,
        "completed_unlock_stages": sm.next_unlock_stage,
        "maximum_net_long_fraction": float(cfg.maximum_net_long_fraction),
        "max_added_loss_after_lock": sm.max_added_loss_after_lock,
        "minimum_basket_pnl_after_lock": sm.minimum_basket_pnl_after_lock,
        "break_even_reached": sm.state == "CLOSED_BREAK_EVEN",
        "break_even_timestamp": sm.break_even_timestamp,
        "bars_from_lock_to_break_even": sm.bars_from_lock_to_break_even,
        "final_status": sm.state,
        "final_net_pnl": (
            sm.final_realized_net_pnl
            if sm.final_realized_net_pnl is not None
            else ledger.basket_net_pnl(final_mark)
        ),
        "basket_pnl_before_exit": sm.basket_pnl_before_exit,
        "projected_closing_fees": sm.projected_closing_fees,
        "projected_exit_slippage": sm.projected_exit_slippage,
        "total_fees": float(ledger.total_fees),
        "opening_fees": float(ledger.opening_fees),
        "lock_fees": float(ledger.lock_fees),
        "unlock_closing_fees": float(ledger.unlock_closing_fees),
        "relock_opening_fees": float(ledger.relock_opening_fees),
        "final_exit_fees": float(ledger.final_exit_fees),
        "slippage_cost_usdt": float(ledger.slippage_cost),
        "funding_cost": float(ledger.funding_cost),
        "bars_processed": len(trace),
        "config": asdict(cfg),
    }
    return {
        "summary": summary,
        "trace": trace,
        "actions": [a.as_dict() for a in sm.actions],
        "transitions": [t.as_dict() for t in sm.transitions],
        "ledger": ledger,
        "state_machine": sm,
    }


def write_phase_b_outputs(
    result: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace_path = out / "per_bar_trace.csv"
    actions_path = out / "actions.csv"
    summary_path = out / "summary.json"

    with trace_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACE_FIELDS)
        writer.writeheader()
        for row in result["trace"]:
            writer.writerow({k: row.get(k) for k in TRACE_FIELDS})

    with actions_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDS)
        writer.writeheader()
        for row in result["actions"]:
            writer.writerow({k: row.get(k) for k in ACTION_FIELDS})

    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(result["summary"], handle, indent=2, sort_keys=True)
        handle.write("\n")

    return {
        "per_bar_trace_csv": trace_path,
        "actions_csv": actions_path,
        "summary_json": summary_path,
    }


def run_phase_b_to_disk(
    cfg: EmergencyLockRecoveryConfig,
    candles: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Never overwrite Phase A outputs by accident.
    output_dir = cfg.output_dir
    if Path(output_dir).resolve() == Path(
        "research/backtests/results/emergency_lock/phase_a"
    ).resolve():
        output_dir = DEFAULT_PHASE_B_OUTPUT_DIR
    result = run_phase_b(cfg, candles=candles)
    paths = write_phase_b_outputs(result, output_dir)
    result["output_paths"] = {k: str(v) for k, v in paths.items()}
    return result
