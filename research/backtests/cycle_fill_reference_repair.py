"""Repair cycle fill reference prices corrupted by commit+advance double counting in backtests.

Live strategy first commits a terminal second-leg fill price, then ``_advance_cycle_from_fill``
accumulates the same fill again in ``short_fills`` / ``long_fills``. A later ``force_commit``
reads the halved VWAP and overwrites ``short_reduce_fill_price`` / ``long_reduce_fill_price``,
which makes the next cycle first-leg trigger roughly 50% too low/high.

This module patches the strategy instance used by the backtest harness only.
"""

from __future__ import annotations

import re
from typing import Any

from fixed_cycle_hedge_bot.models import FillEvent, RuntimeState

_CYCLE_INDEX_RE = re.compile(r"CYCLE_(\d+)_")


def _cycle_index_from_purpose(purpose: str | None) -> int:
    match = _CYCLE_INDEX_RE.search(str(purpose or "").upper())
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def _repair_vwap_fill_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Fix doubled ``total_qty`` when commit and advance both counted the same fill."""
    if not entry:
        return entry
    repaired = dict(entry)
    weighted_price_sum = float(repaired.get("weighted_price_sum") or 0.0)
    fill_qty = float(repaired.get("qty") or repaired.get("exec_qty") or 0.0)
    total_qty = float(repaired.get("total_qty") or 0.0)
    price = float(repaired.get("price") or 0.0)

    if fill_qty <= 0:
        return repaired

    if weighted_price_sum > 0 and total_qty > fill_qty * 1.001:
        repaired["total_qty"] = fill_qty
        repaired["avg_price"] = weighted_price_sum / fill_qty
        repaired["price"] = float(repaired["avg_price"])
        return repaired

    if weighted_price_sum > 0:
        expected_avg = weighted_price_sum / fill_qty
        if price > 0 and price < expected_avg * 0.75:
            repaired["avg_price"] = expected_avg
            repaired["price"] = expected_avg
    return repaired


def _sync_cycle_sequence_fill_price(
    strategy: Any,
    runtime_state: RuntimeState,
    *,
    cycle_index: int,
    fill_price_field: str,
    fill_confirmed_field: str,
    fill_price: float,
) -> None:
    if cycle_index <= 0 or fill_price <= 0:
        return
    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    existing = float(entry.get(fill_price_field) or 0.0)
    if existing <= 0:
        should_update = True
    else:
        relative_delta = abs(existing - fill_price) / fill_price
        should_update = relative_delta > 0.005 or existing < fill_price * 0.75
    if should_update:
        entry[fill_price_field] = float(fill_price)
        entry[fill_confirmed_field] = True
        strategy._persist_cycle_sequence_state(runtime_state)


def repair_cycle_fill_maps_after_advance(
    strategy: Any,
    runtime_state: RuntimeState,
    fill_event: FillEvent,
) -> None:
    """Reconcile cycle fill maps after ``_advance_cycle_from_fill`` in backtests."""
    purpose = str(fill_event.purpose or "").upper()
    cycle_index = _cycle_index_from_purpose(purpose)
    if cycle_index <= 0:
        return

    cycle_state = strategy._ensure_cycle_state(runtime_state)

    if "_SHORT_" in purpose and ("SHORT_REDUCE" in purpose or "SHORT_TP" in purpose):
        fills = cycle_state.setdefault("short_fills", {})
        key = str(cycle_index)
        repaired = _repair_vwap_fill_entry(dict(fills.get(key) or {}))
        if repaired:
            fills[key] = repaired
            avg_price = float(repaired.get("avg_price") or repaired.get("price") or 0.0)
            _sync_cycle_sequence_fill_price(
                strategy,
                runtime_state,
                cycle_index=cycle_index,
                fill_price_field=strategy._second_leg_fill_price_field(),
                fill_confirmed_field=strategy._second_leg_fill_confirmed_field(),
                fill_price=avg_price,
            )
        return

    if "_LONG_" in purpose and ("LONG_REDUCE" in purpose or "LONG_ADD" in purpose):
        fills = cycle_state.setdefault("long_fills", {})
        key = str(cycle_index)
        repaired = _repair_vwap_fill_entry(dict(fills.get(key) or {}))
        if repaired:
            fills[key] = repaired
            avg_price = float(repaired.get("avg_price") or repaired.get("price") or 0.0)
            _sync_cycle_sequence_fill_price(
                strategy,
                runtime_state,
                cycle_index=cycle_index,
                fill_price_field=strategy._second_leg_fill_price_field(),
                fill_confirmed_field=strategy._second_leg_fill_confirmed_field(),
                fill_price=avg_price,
            )


def install_cycle_fill_reference_repair(strategy: Any) -> None:
    """Patch strategy fill commit/advance hooks for backtest-only reference repair."""
    if getattr(strategy, "_backtest_cycle_fill_reference_repair_installed", False):
        return

    original_commit = strategy._commit_short_reduce_terminal_fill
    original_advance = strategy._advance_cycle_from_fill

    def wrapped_commit(
        runtime_state: RuntimeState,
        cycle_index: int,
        *,
        fill_event: FillEvent | None = None,
        avg_price: float | None = None,
        filled_qty: float | None = None,
        source: str = "terminal_fill",
    ) -> None:
        fill_event_price = float(getattr(fill_event, "exec_price", 0.0) or 0.0)
        if fill_event is not None and fill_event_price > 0:
            return original_commit(
                runtime_state,
                cycle_index,
                fill_event=fill_event,
                avg_price=fill_event_price,
                filled_qty=float(getattr(fill_event, "exec_qty", 0.0) or 0.0) or filled_qty,
                source=source,
            )

        if source == "force_commit" and fill_event is None:
            entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
            fill_price_field = strategy._second_leg_fill_price_field()
            fill_confirmed_field = strategy._second_leg_fill_confirmed_field()
            existing_price = float(entry.get(fill_price_field) or 0.0)
            cycle_state = strategy._ensure_cycle_state(runtime_state)
            side_key = "short_fills" if fill_price_field == "short_reduce_fill_price" else "long_fills"
            fill_entry = dict((cycle_state.get(side_key) or {}).get(str(cycle_index)) or {})
            repaired = _repair_vwap_fill_entry(fill_entry)
            repaired_avg = float(repaired.get("avg_price") or repaired.get("price") or 0.0)
            repaired_qty = float(repaired.get("qty") or repaired.get("exec_qty") or 0.0)
            if (
                existing_price > 0
                and bool(entry.get(fill_confirmed_field))
                and repaired_avg > 0
                and abs(existing_price - repaired_avg) / repaired_avg <= 0.005
            ):
                return
            if repaired_avg > 0:
                if side_key in cycle_state and str(cycle_index) in cycle_state[side_key]:
                    cycle_state[side_key][str(cycle_index)] = repaired
                return original_commit(
                    runtime_state,
                    cycle_index,
                    avg_price=repaired_avg,
                    filled_qty=repaired_qty if repaired_qty > 0 else None,
                    source=source,
                )
        return original_commit(
            runtime_state,
            cycle_index,
            fill_event=fill_event,
            avg_price=avg_price,
            filled_qty=filled_qty,
            source=source,
        )

    def wrapped_advance(
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        context: Any | None = None,
    ) -> None:
        original_advance(fill_event, runtime_state, context)
        repair_cycle_fill_maps_after_advance(strategy, runtime_state, fill_event)

    strategy._commit_short_reduce_terminal_fill = wrapped_commit
    strategy._advance_cycle_from_fill = wrapped_advance
    strategy._backtest_cycle_fill_reference_repair_installed = True
